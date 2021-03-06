#! /usr/bin/env python

import argparse
import os
import numpy as np
import json
from voc import parse_voc_annotation
from yolo import create_yolov3_model
from generator import BatchGenerator
from utils.utils import normalize, evaluate
from keras.callbacks import EarlyStopping, ModelCheckpoint
from keras.optimizers import Adam
from keras.models import load_model

os.environ["CUDA_DEVICE_ORDER"]="PCI_BUS_ID"
os.environ["CUDA_VISIBLE_DEVICES"]="0" # define the GPU to work on here

argparser = argparse.ArgumentParser(
    description='Train and evaluate YOLO_v3 model on any dataset')

argparser.add_argument(
    '-c',
    '--conf',
    help='path to configuration file')

def create_training_instances(
    train_annot_folder,
    train_image_folder,
    valid_annot_folder,
    valid_image_folder,
    labels,
    include_empty
):
    # parse annotations of the training set
    train_ints, train_labels = parse_voc_annotation(train_annot_folder, train_image_folder, labels, include_empty)

    # parse annotations of the validation set, if any, otherwise split the training set
    if os.path.exists(valid_annot_folder):
        print("valid_annot_folder not exists. Spliting the trainining set.")
        valid_ints, valid_labels = parse_voc_annotation(valid_annot_folder, valid_image_folder, labels, include_empty)
    else:
        train_valid_split = int(0.8*len(train_ints))
        np.random.shuffle(train_ints)

        valid_ints = train_ints[train_valid_split:]
        train_ints = train_ints[:train_valid_split]

    # compare the seen labels with the given labels in config.json
    if len(labels) > 0:
        overlap_labels = set(labels).intersection(set(train_labels.keys()))

        print('Seen labels: \t\t'  + str(train_labels))
        print('Given labels: \t\t' + str(labels))
        print('Overlap labels: \t' + str(list(overlap_labels)))

        # return None, None, None if some given label is not in the dataset
        if len(overlap_labels) < len(labels):
            print('Some labels have no annotations! Please revise the list of labels in the config.json.')
            return None, None, None
    else:
        print('No labels are provided. Train on all seen labels.')
        labels = sorted(train_labels.keys())

    return train_ints, valid_ints, labels

def create_callbacks(saved_weights_name):
    early_stop = EarlyStopping(
        monitor='val_loss', 
        min_delta=0.001, 
        patience=3, 
        mode='min', 
        verbose=1
    )

    checkpoint = ModelCheckpoint(
        saved_weights_name, 
        monitor='val_loss', 
        verbose=1, 
        save_best_only=True, 
        mode='min', 
        period=1
    )

    return [early_stop, checkpoint]

def _main_(args):
    config_path = args.conf

    with open(config_path) as config_buffer:    
        config = json.loads(config_buffer.read())

    ###############################
    #   Parse the annotations 
    ###############################
    train_ints, valid_ints, labels = create_training_instances(
        config['train']['train_annot_folder'],
        config['train']['train_image_folder'],
        config['valid']['valid_annot_folder'],
        config['valid']['valid_image_folder'],
        config['model']['labels'],
        config['train']['include_empty']
    )

    ###############################
    #   Create the generators 
    ###############################    
    train_generator = BatchGenerator(
        instances           = train_ints, 
        anchors             = config['model']['anchors'],   
        labels              = labels,        
        downsample          = 32, # ratio between network input's size and network output's size, 32 for YOLOv3
        max_box_per_image   = config['model']['max_box_per_image'],
        batch_size          = config['train']['batch_size'],
        min_net_size        = config['model']['min_input_size'],
        max_net_size        = config['model']['max_input_size'],   
        shuffle             = True, 
        jitter              = 0.3, 
        norm                = normalize
    )
    
    valid_generator = BatchGenerator(
        instances           = valid_ints, 
        anchors             = config['model']['anchors'],   
        labels              = labels,        
        downsample          = 32, # ratio between network input's size and network output's size, 32 for YOLOv3
        max_box_per_image   = config['model']['max_box_per_image'],
        batch_size          = config['train']['batch_size'],
        min_net_size        = config['model']['min_input_size'],
        max_net_size        = config['model']['max_input_size'],   
        shuffle             = True, 
        jitter              = 0.0, 
        norm                = normalize
    )

    ###############################
    #   Create the model 
    ###############################
    if os.path.exists(config['train']['saved_weights_name']): 
        warmup_batches = 0 # no need warmup if the pretrained weight exists
    else:
        warmup_batches  = config['train']['warmup_epochs'] * (config['train']['train_times']*len(train_generator) + \
                                                              config['valid']['valid_times']*len(valid_generator))     

    train_model, infer_model = create_yolov3_model(
        nb_class            = len(labels), 
        anchors             = config['model']['anchors'], 
        max_box_per_image   = config['model']['max_box_per_image'], 
        max_grid            = [config['model']['max_input_size'], config['model']['max_input_size']], 
        batch_size          = config['train']['batch_size'], 
        warmup_batches      = warmup_batches,
        ignore_thresh       = config['train']['ignore_thresh']
    )

    # load the pretrained weight if exists, otherwise load the backend weight only
    if os.path.exists(config['train']['saved_weights_name']): 
        print("Loading pretrained weights.")
        train_model.load_weights(config['train']['saved_weights_name'], by_name=True)
    else:
        train_model.load_weights("backend.h5", by_name=True)

    ###############################
    #   Kick off the training
    ###############################
    optimizer = Adam(lr=config['train']['learning_rate'], beta_1=0.9, beta_2=0.999, epsilon=1e-08, decay=0.0)
    train_model.compile(loss=lambda y_true, y_pred: y_pred, optimizer=optimizer)

    callbacks = create_callbacks(config['train']['saved_weights_name'])

    train_model.fit_generator(
        generator        = train_generator, 
        steps_per_epoch  = len(train_generator) * config['train']['train_times'], 
        epochs           = config['train']['nb_epochs'] + config['train']['warmup_epochs'], 
        verbose          = 2 if config['train']['debug'] else 1,
        validation_data  = valid_generator,
        validation_steps = len(valid_generator) * config['valid']['valid_times'],
        callbacks        = callbacks, 
        workers          = 3,
        max_queue_size   = 8
    )

    infer_model.load_weights(config['train']['saved_weights_name'], by_name=True)
    infer_model.save(config['train']['saved_weights_name'])

    ###############################
    #   Run the evaluation
    ###############################   
    # compute mAP for all the classes
    average_precisions = evaluate(infer_model, valid_generator)

    # print the score
    for label, average_precision in average_precisions.items():
        print(labels[label] + ': {:.4f}'.format(average_precision))
    print('mAP: {:.4f}'.format(sum(average_precisions.values()) / len(average_precisions)))           

if __name__ == '__main__':
    args = argparser.parse_args()
    _main_(args)
