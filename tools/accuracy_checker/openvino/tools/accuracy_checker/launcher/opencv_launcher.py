"""
Copyright (c) 2018-2022 Intel Corporation

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

import re
from collections import OrderedDict
import numpy as np
import cv2
from pathlib import Path

from ..config import PathField, StringField, ConfigError, ListInputsField
from ..logging import print_info
from .launcher import Launcher, LauncherConfigValidator
from ..utils import get_or_parse_value, get_path

DEVICE_REGEX = r'(?P<device>cpu$|gpu|gpu_fp16)?'
BACKEND_REGEX = r'(?P<backend>ocv|ie)?'


class OpenCVLauncherConfigValidator(LauncherConfigValidator):
    def validate(self, entry, field_uri=None, fetch_only=False):
        self.fields['inputs'].optional = self.delayed_model_loading
        error_stack = super().validate(entry, field_uri)
        if not self.delayed_model_loading:
            inputs = entry.get('inputs')
            for input_layer in inputs:
                if 'shape' not in input_layer:
                    if not fetch_only:
                        raise ConfigError('input value should have shape field')
                    error_stack.extend(self.build_error(entry, field_uri, 'input value should have shape field'))
        return error_stack


class OpenCVLauncher(Launcher):
    """
    Class for infer model using OpenCV library.
    """
    __provider__ = 'opencv'

    OPENCV_BACKENDS = {
        'ocv': cv2.dnn.DNN_BACKEND_OPENCV,
        'ie': cv2.dnn.DNN_BACKEND_INFERENCE_ENGINE
    }

    TARGET_DEVICES = {
        'cpu': cv2.dnn.DNN_TARGET_CPU,
        'gpu': cv2.dnn.DNN_TARGET_OPENCL,
        'gpu_fp16': cv2.dnn.DNN_TARGET_OPENCL_FP16
    }

    @classmethod
    def parameters(cls):
        parameters = super().parameters()
        parameters.update({
            'model': PathField(description="Path to model file.", file_or_directory=True),
            'weights': PathField(description="Path to weights file.", optional=True, check_exists=False, file_or_directory=True),
            'device': StringField(
                regex=DEVICE_REGEX, choices=OpenCVLauncher.TARGET_DEVICES.keys(),
                description="Device name: {}".format(', '.join(OpenCVLauncher.TARGET_DEVICES.keys()))
            ),
            'backend': StringField(
                regex=BACKEND_REGEX, choices=OpenCVLauncher.OPENCV_BACKENDS.keys(),
                optional=True, default='IE',
                description="Backend name: {}".format(', '.join(OpenCVLauncher.OPENCV_BACKENDS.keys()))),
            'inputs': ListInputsField(optional=False, description="Inputs.")
        })

        return parameters

    def __init__(self, config_entry: dict, *args, **kwargs):
        super().__init__(config_entry, *args, **kwargs)
        self._delayed_model_loading = kwargs.get('delayed_model_loading', False)
        self.validate_config(config_entry, delayed_model_loading=self._delayed_model_loading)
        match = re.match(BACKEND_REGEX, self.get_value_from_config('backend').lower())
        selected_backend = match.group('backend')
        print_info('backend: {}'.format(selected_backend))
        self.backend = OpenCVLauncher.OPENCV_BACKENDS.get(selected_backend)
        match = re.match(DEVICE_REGEX, self.get_value_from_config('device').lower())
        selected_device = match.group('device')

        if 'tags' in self.config:
            tags = self.config['tags']
            if ('FP16' in tags) and (selected_device == 'gpu'):
                selected_device = 'gpu_fp16'

        self.target = OpenCVLauncher.TARGET_DEVICES.get(selected_device)

        if self.target is None:
            raise ConfigError('{} is not supported device'.format(selected_device))

        if not self._delayed_model_loading:
            self.model, self.weights = self.automatic_model_search()
            self.network = self.create_network(self.model, self.weights)
            self._inputs_shapes = self.get_inputs_from_config(self.config)
            self.network.setInputsNames(list(self._inputs_shapes.keys()))
            self.output_names = self.network.getUnconnectedOutLayersNames()

    @classmethod
    def validate_config(cls, config, delayed_model_loading=False, fetch_only=False, uri_prefix=''):
        return OpenCVLauncherConfigValidator(
            uri_prefix or 'launcher.{}'.format(cls.__provider__),
            fields=cls.parameters(), delayed_model_loading=delayed_model_loading
        ).validate(config, fetch_only=fetch_only)

    @property
    def inputs(self):
        """
        Returns:
            inputs in NCHW format.
        """
        return self._inputs_shapes

    @property
    def batch(self):
        return 1

    @property
    def output_blob(self):
        return next(iter(self.output_names))

    def predict(self, inputs, metadata=None, **kwargs):
        """
        Args:
            inputs: dictionary where keys are input layers names and values are data for them.
            metadata: metadata of input representations
        Returns:
            raw data from network.
        """
        results = []
        for input_blobs in inputs:
            for blob_name in self._inputs_shapes:
                self.network.setInput(input_blobs[blob_name].astype(np.float32), blob_name)
            list_prediction = self.network.forward(self.output_names)
            dict_result = dict(zip(self.output_names, list_prediction))
            results.append(dict_result)

        if metadata is not None:
            for meta_ in metadata:
                meta_['input_shape'] = self.inputs_info_for_meta()

        return results

    def predict_async(self, *args, **kwargs):
        raise ValueError('OpenCV Launcher does not support async mode yet')

    def automatic_model_search(self):
        def get_xml(model_dir):
            models_list = list(model_dir.glob('{}.xml'.format(self._model_name)))
            if not models_list:
                models_list = list(model_dir.glob('*.xml'))
            return models_list

        def get_blob(model_dir):
            blobs_list = list(Path(model_dir).glob('{}.blob'.format(self._model_name)))
            if not blobs_list:
                blobs_list = list(Path(model_dir).glob('*.blob'))
            return blobs_list

        def get_onnx(model_dir):
            onnx_list = list(Path(model_dir).glob('{}.onnx'.format(self._model_name)))
            if not onnx_list:
                onnx_list = list(Path(model_dir).glob('*.onnx'))
            return onnx_list

        def get_model():
            model = Path(self.get_value_from_config('model'))
            model_is_blob = self.get_value_from_config('_model_is_blob')
            if not model.is_dir():
                accepted_suffixes = ['.blob', '.onnx', '.xml']
                if model.suffix not in accepted_suffixes:
                    raise ConfigError('Models with following suffixes are allowed: {}'.format(accepted_suffixes))
                print_info('Found model {}'.format(model))
                return model, model.suffix == '.blob'
            if model_is_blob:
                model_list = get_blob(model)
            else:
                model_list = get_xml(model)
                if not model_list and model_is_blob is None:
                    model_list = get_blob(model)
                if not model_list:
                    model_list = get_onnx(model)
            if not model_list:
                raise ConfigError('suitable model is not found')
            if len(model_list) != 1:
                raise ConfigError('More than one model matched, please specify explicitly')
            model = model_list[0]
            print_info('Found model {}'.format(model))
            return model, model.suffix == '.blob'

        model, is_blob = get_model()
        if is_blob:
            return model, None
        weights = self.get_value_from_config('weights')
        if (weights is None or Path(weights).is_dir()) and model.suffix != '.onnx':
            weights_dir = weights or model.parent
            weights = Path(weights_dir) / model.name.replace('xml', 'bin')
        if weights is not None:
            accepted_weights_suffixes = ['.bin']
            if weights.suffix not in accepted_weights_suffixes:
                raise ConfigError('Weights with following suffixes are allowed: {}'.format(accepted_weights_suffixes))
            print_info('Found weights {}'.format(get_path(weights)))

        return model, weights

    def create_network(self, model, weights):
        network = cv2.dnn.readNet(str(model), str(weights))
        network.setPreferableBackend(self.backend)
        network.setPreferableTarget(self.target)

        return network

    @staticmethod
    def get_inputs_from_config(config):
        inputs = config.get('inputs')
        if not inputs:
            raise ConfigError('inputs should be provided in config')

        def parse_shape_value(shape):
            return (1, *map(int, get_or_parse_value(shape, ())))

        return OrderedDict([(elem.get('name'), parse_shape_value(elem.get('shape'))) for elem in inputs])

    def release(self):
        """
        Releases launcher.
        """
        del self.network
