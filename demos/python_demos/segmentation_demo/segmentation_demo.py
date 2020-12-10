#!/usr/bin/env python3
"""
 Copyright (C) 2018-2020 Intel Corporation

 Licensed under the Apache License, Version 2.0 (the "License");
 you may not use this file except in compliance with the License.
 You may obtain a copy of the License at

      http://www.apache.org/licenses/LICENSE-2.0

 Unless required by applicable law or agreed to in writing, software
 distributed under the License is distributed on an "AS IS" BASIS,
 WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 See the License for the specific language governing permissions and
 limitations under the License.
"""

import logging
import random
import sys
from argparse import ArgumentParser, SUPPRESS
from pathlib import Path
from time import perf_counter
import os

import cv2
import numpy as np
from openvino.inference_engine import IECore

sys.path.append(str(Path(__file__).resolve().parents[1] / 'common'))

from models import SegmentationModel
import monitors
from pipelines import AsyncPipeline
from images_capture import open_images_capture
from performance_metrics import PerformanceMetrics

logging.basicConfig(format='[ %(levelname)s ] %(message)s', level=logging.INFO, stream=sys.stdout)
log = logging.getLogger()


default_color_map = [
    (150, 150, 150),
    (58, 55, 169),
    (211, 51, 17),
    (157, 80, 44),
    (23, 95, 189),
    (210, 133, 34),
    (76, 226, 202),
    (101, 138, 127),
    (223, 91, 182),
    (80, 128, 113),
    (235, 155, 55),
    (44, 151, 243),
    (159, 80, 170),
    (239, 208, 44),
    (128, 50, 51),
    (82, 141, 193),
    (9, 107, 10),
    (223, 90, 142),
    (50, 248, 83),
    (178, 101, 130),
    (71, 30, 204)
]


def create_color_map(input):
    global default_color_map
    global color_map

    if input and os.path.isfile(input):
        default_color_map = []
        with open(input, 'r') as file:
            for line in file.readlines():
                default_color_map.append(eval(line.strip()))

    rng = random.Random(0xACE)
    classes = np.array(default_color_map)
    color_map = np.zeros((256, 1, 3), dtype=np.uint8)
    color_map[:len(classes), 0, :] = classes.astype('uint8')
    color_map[len(classes):, 0, :] = rng.uniform(0, 255)


def apply_color_map(input):
    input_3d = cv2.merge([input, input, input])
    return cv2.LUT(input_3d, color_map)


def render_segmentation_data(frame, objects):
    # Visualizing result data over source image
    return np.floor_divide(frame, 2) + np.floor_divide(apply_color_map(objects), 2)


def build_argparser():
    parser = ArgumentParser(add_help=False)
    args = parser.add_argument_group('Options')
    args.add_argument('-h', '--help', action='help', default=SUPPRESS, help='Show this help message and exit.')
    args.add_argument('-m', '--model', help='Required. Path to an .xml file with a trained model.',
                      required=True, type=Path)
    args.add_argument('-i', '--input', required=True,
                      help='Required. An input to process. The input must be a single image, '
                           'a folder of images or anything that cv2.VideoCapture can process.')
    args.add_argument('-d', '--device', default='CPU', type=str,
                      help='Optional. Specify the target device to infer on; CPU, GPU, FPGA, HDDL or MYRIAD is '
                           'acceptable. The sample will look for a suitable plugin for device specified. '
                           'Default value is CPU.')

    common_model_args = parser.add_argument_group('Common model options')
    common_model_args.add_argument('-c', '--colors', type=Path,
                                   help='Optional. Path to a text file containing colors for classes.')

    infer_args = parser.add_argument_group('Inference options')
    infer_args.add_argument('-nireq', '--num_infer_requests', help='Optional. Number of infer requests',
                            default=1, type=int)
    infer_args.add_argument('-nstreams', '--num_streams',
                            help='Optional. Number of streams to use for inference on the CPU or/and GPU in throughput '
                                 'mode (for HETERO and MULTI device cases use format '
                                 '<device1>:<nstreams1>,<device2>:<nstreams2> or just <nstreams>).',
                            default='', type=str)
    infer_args.add_argument('-nthreads', '--num_threads', default=None, type=int,
                            help='Optional. Number of threads to use for inference on CPU (including HETERO cases).')

    io_args = parser.add_argument_group('Input/output options')
    io_args.add_argument('--loop', default=False, action='store_true',
                         help='Optional. Enable reading the input in a loop.')
    io_args.add_argument('--no_show', help="Optional. Don't show output.", action='store_true')
    io_args.add_argument('-u', '--utilization_monitors', default='', type=str,
                         help='Optional. List of monitors to show initially.')
    return parser


def get_plugin_configs(device, num_streams, num_threads):
    config_user_specified = {}

    devices_nstreams = {}
    if num_streams:
        devices_nstreams = {device: num_streams for device in ['CPU', 'GPU'] if device in device} \
            if num_streams.isdigit() \
            else dict(device.split(':', 1) for device in num_streams.split(','))

    if 'CPU' in device:
        if num_threads is not None:
            config_user_specified['CPU_THREADS_NUM'] = str(num_threads)
        if 'CPU' in devices_nstreams:
            config_user_specified['CPU_THROUGHPUT_STREAMS'] = devices_nstreams['CPU'] \
                if int(devices_nstreams['CPU']) > 0 \
                else 'CPU_THROUGHPUT_AUTO'

    if 'GPU' in device:
        if 'GPU' in devices_nstreams:
            config_user_specified['GPU_THROUGHPUT_STREAMS'] = devices_nstreams['GPU'] \
                if int(devices_nstreams['GPU']) > 0 \
                else 'GPU_THROUGHPUT_AUTO'

    return config_user_specified


def main():
    metrics = PerformanceMetrics()
    args = build_argparser().parse_args()

    log.info('Initializing Inference Engine...')
    ie = IECore()

    plugin_config = get_plugin_configs(args.device, args.num_streams, args.num_threads)

    log.info('Loading network...')

    model = SegmentationModel(ie, args.model)

    pipeline = AsyncPipeline(ie, model, plugin_config, device=args.device, max_num_requests=args.num_infer_requests)

    cap = open_images_capture(args.input, args.loop)
    create_color_map(args.colors)

    next_frame_id = 0
    next_frame_id_to_show = 0

    log.info('Starting inference...')
    print("To close the application, press 'CTRL+C' here or switch to the output window and press ESC key")

    while True:
        if pipeline.is_ready():
            # Get new image/frame
            start_time = perf_counter()
            frame = cap.read()
            if frame is None:
                break
            if next_frame_id == 0:
                frame_size = frame.shape
                presenter = monitors.Presenter(args.utilization_monitors, 55,
                                               (round(frame_size[1] / 4), round(frame_size[0] / 8)))
            # Submit for inference
            pipeline.submit_data(frame, next_frame_id, {'frame': frame, 'start_time': start_time})
            next_frame_id += 1
        else:
            # Wait for empty request
            pipeline.await_any()

        if pipeline.callback_exceptions:
            raise pipeline.callback_exceptions[0]
        # Process all completed requests
        results = pipeline.get_result(next_frame_id_to_show)
        if results:
            objects, frame_meta = results
            frame = frame_meta['frame']
            start_time = frame_meta['start_time']

            frame = render_segmentation_data(frame, objects)
            presenter.drawGraphs(frame)
            metrics.update(start_time, frame)
            if not args.no_show:
                cv2.imshow('Segmentation Results', frame)
                key = cv2.waitKey(1)
                if key == 27 or key == 'q' or key == 'Q':
                    break
                presenter.handleKey(key)
            next_frame_id_to_show += 1

    pipeline.await_all()
    # Process completed requests
    while pipeline.has_completed_request():
        results = pipeline.get_result(next_frame_id_to_show)
        if results:
            objects, frame_meta = results
            frame = frame_meta['frame']
            start_time = frame_meta['start_time']

            frame = render_segmentation_data(frame, objects)
            presenter.drawGraphs(frame)
            metrics.update(start_time, frame)
            if not args.no_show:
                cv2.imshow('Segmentation Results', frame)
                key = cv2.waitKey(1)
            next_frame_id_to_show += 1
        else:
            break

    metrics.print_total()
    print(presenter.reportMeans())


if __name__ == '__main__':
    sys.exit(main() or 0)
