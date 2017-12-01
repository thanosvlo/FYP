
# -*- coding: utf-8 -*-
from __future__ import unicode_literals
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import

import argparse
import os
import pandas as pd
import tensorflow as tf
import numpy as np
from dltk.core.metrics import dice

from dltk.io.abstract_reader import Reader
from dltk.networks.segmentation.unet import residual_unet_3d

from reader_custom import read_fn

EVAL_EVERY_N_STEPS = 1000
EVAL_STEPS = 1

NUM_CLASSES = 2
NUM_CHANNELS = 3

NUM_FEATURES_IN_SUMMARIES = min(4, NUM_CHANNELS)

BATCH_SIZE = 8
SHUFFLE_CACHE_SIZE = 64
MAX_STEPS = 50000

#

def model_fn(features,labels,mode, params):

    net_output_ops=residual_unet_3d(inputs=features['x'],
        num_classes=NUM_CLASSES,
        num_res_units=2,
        filters=(16, 32, 64, 128),
        strides=((1, 1, 1), (1, 2, 2), (1, 2, 2), (1, 2, 2)),
        mode=mode,
        kernel_regularizer=tf.contrib.layers.l2_regularizer(1e-6))

    if mode==tf.estimator.ModeKeys.PREDICT:
        return tf.estimator.EstimatorSpec(
                mode=mode,
                predictions=net_output_ops,
                export_outputs={'out':tf.estimator.export.PredictOutput(net_output_ops)})

    ce=tf.nn.sparse_softmax_cross_entropy_with_logits(labels=labels['y'],
                                                logits=net_output_ops['logits'])
    loss=tf.reduce_mean(ce)
    global_step=tf.train.get_global_step()
    
    optimiser = tf.train.MomentumOptimizer(
        learning_rate=params["learning_rate"],
        momentum=0.9)

    
    update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
    with tf.control_dependencies(update_ops):
        train_op=optimiser.minimize(loss,global_step=global_step)


    # 4.1 (optional) create custom image summaries for tensorboard
    my_image_summaries = {}
    my_image_summaries['feat'] = features['x'][0, 0, :, :, 0]
    my_image_summaries['labels'] = tf.cast(labels['y'], tf.float32)[0, 0, :, :]
    my_image_summaries['predictions'] = tf.cast(net_output_ops['y_'], tf.float32)[0, 0, :, :]


    
    expected_output_size = [1, 256, 256, 1]  # [B, W, H, C]
    [tf.summary.image(name, tf.reshape(image, expected_output_size))
     for name, image in my_image_summaries.items()]
    # 4.2 (optional) create custom metric summaries for tensorboard
    dice_tensor = tf.py_func(dice, [net_output_ops['y_'],
                                    labels['y'],
                                    tf.constant(NUM_CLASSES)], tf.float32)
    [tf.summary.scalar('dsc_l{}'.format(i), dice_tensor[i])
     for i in range(NUM_CLASSES)]
    
    return tf.estimator.EstimatorSpec(mode=mode,
                                        predictions=net_output_ops,
                                        loss=loss,
                                        train_op=train_op,
                                        eval_metric_ops=None)



if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Example: MRBrainS13 example segmentation training script')
    parser.add_argument('--run_validation', default=True)
    parser.add_argument('--restart', default=False, action='store_true')
    parser.add_argument('--verbose', default=True, action='store_true')
    parser.add_argument('--cuda_devices', '-c', default='0')

    parser.add_argument('--model_path', '-p', default='./outputs')
    parser.add_argument('--train_csv', default='/home/av2514/DLTK-master/examples/applications/MRBrainS13_tissue_segmentation/Training/train_full.csv')
    args = parser.parse_args()

    # Set verbosity
    if args.verbose:
        os.environ['TF_CPP_MIN_LOG_LEVEL'] = '1'
        tf.logging.set_verbosity(tf.logging.INFO)
    else:
        os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
        tf.logging.set_verbosity(tf.logging.ERROR)

    if args.restart:
        print('Restarting training from scratch.')
        os.system('rm -rf {}'.format(args.model_path))
    if not os.path.isdir(args.model_path):
        os.system('mkdir -p {}'.format(args.model_path))
    else:
        print('Resuming training on model_path {}'.format(args.model_path))



    os.environ['CUDA_VISIBLE_DEVICES'] = '0'

    ############################################################
    np.random.seed(42)
    tf.set_random_seed(42)
    print('Setting up...')

    all_filenames=pd.read_csv(
    args.train_csv,   
     dtype=object,
    keep_default_na=False,
    na_values=[]).as_matrix()

    train_filenames = all_filenames[1:18000]
    val_filenames = all_filenames[18001:18327]
    reader_params = {'n_examples': 18,
                     'example_size': [1, 256, 256],
                     'extract_examples': False}
    reader_example_shapes = {'features': {'x': reader_params['example_size'] + [NUM_CHANNELS, ]},
                             'labels': {'y': reader_params['example_size']}}

    
    reader = Reader(read_fn,
                    {'features': {'x': tf.float32},
                     'labels': {'y': tf.int32}})


    train_input_fn, train_qinit_hook = reader.get_inputs(
        file_references=train_filenames,
        mode=tf.estimator.ModeKeys.TRAIN,
        example_shapes=reader_example_shapes,
        batch_size=BATCH_SIZE,
        shuffle_cache_size=SHUFFLE_CACHE_SIZE,
        params=reader_params)

    val_input_fn, val_qinit_hook = reader.get_inputs(
        file_references=val_filenames,
        mode=tf.estimator.ModeKeys.EVAL,
        example_shapes=reader_example_shapes,
        batch_size=BATCH_SIZE,
        shuffle_cache_size=SHUFFLE_CACHE_SIZE,
        params=reader_params)

    nn = tf.estimator.Estimator(model_fn=model_fn, 
                            model_dir=args.model_path, 
                            params={"learning_rate": 1e-6},
                            config=tf.estimator.RunConfig())

    # Hooks for validation summaries
    val_summary_hook = tf.contrib.training.SummaryAtEndHook(
        os.path.join(args.model_path, 'eval'))
    step_cnt_hook = tf.train.StepCounterHook(
        every_n_steps=EVAL_EVERY_N_STEPS,
        output_dir=args.model_path)

    print('Starting training...')
    try:
        for _ in range(MAX_STEPS // EVAL_EVERY_N_STEPS):
            nn.train(
                input_fn=train_input_fn,
                hooks=[train_qinit_hook, step_cnt_hook],
                steps=EVAL_EVERY_N_STEPS)

            if args.run_validation:
                results_val = nn.evaluate(
                    input_fn=val_input_fn,
                    hooks=[val_qinit_hook, val_summary_hook],
                    steps=EVAL_STEPS)
                print('Step = {}; val loss = {:.5f};'.format(
                    results_val['global_step'], results_val['loss']))
    except KeyboardInterrupt:
        pass

    print('Stopping now.')
    export_dir = nn.export_savedmodel(
        export_dir_base=args.model_path,
        serving_input_receiver_fn=reader.serving_input_receiver_fn(reader_example_shapes))
    print('Model saved to {}.'.format(export_dir))