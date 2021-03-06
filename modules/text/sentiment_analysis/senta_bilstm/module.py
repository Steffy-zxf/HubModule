# -*- coding:utf-8 -*-
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import argparse
import ast
import json
import math
import numpy as np
import os
import six

import paddle.fluid as fluid
from paddle.fluid.core import PaddleTensor, AnalysisConfig, create_paddle_predictor
import paddlehub as hub
from paddlehub.common.utils import sys_stdin_encoding
from paddlehub.io.parser import txt_parser
from paddlehub.module.module import serving
from paddlehub.module.module import moduleinfo
from paddlehub.module.module import runnable

from senta_bilstm.net import bilstm_net
from senta_bilstm.processor import load_vocab, preprocess, postprocess


class DataFormatError(Exception):
    def __init__(self, *args):
        self.args = args


@moduleinfo(
    name="senta_bilstm",
    version="1.1.0",
    summary="Baidu's open-source Sentiment Classification System.",
    author="baidu-nlp",
    author_email="",
    type="nlp/sentiment_analysis")
class SentaBiLSTM(hub.Module):
    def _initialize(self):
        """
        initialize with the necessary elements
        """
        self.pretrained_model_path = os.path.join(self.directory, "infer_model")
        self.vocab_path = os.path.join(self.directory, "assets/vocab.txt")
        self.word_dict = load_vocab(self.vocab_path)
        self._word_seg_module = None

        self._set_config()

    @property
    def word_seg_module(self):
        """
        lac module
        """
        if not self._word_seg_module:
            self._word_seg_module = hub.Module(name="lac")
        return self._word_seg_module

    def _set_config(self):
        """
        predictor config setting
        """
        cpu_config = AnalysisConfig(self.pretrained_model_path)
        cpu_config.disable_glog_info()
        cpu_config.disable_gpu()
        self.cpu_predictor = create_paddle_predictor(cpu_config)

        try:
            _places = os.environ["CUDA_VISIBLE_DEVICES"]
            int(_places[0])
            use_gpu = True
        except:
            use_gpu = False
        if use_gpu:
            gpu_config = AnalysisConfig(
                os.path.join(self.directory, "infer_model"))
            gpu_config.disable_glog_info()
            gpu_config.enable_use_gpu(memory_pool_init_size_mb=500, device_id=0)
            self.gpu_predictor = create_paddle_predictor(gpu_config)

    def context(self, trainable=False):
        """
        Get the input ,output and program of the pretrained senta_bilstm

        Args:
             trainable(bool): whether fine-tune the pretrained parameters of senta_bilstm or not

        Returns:
             inputs(dict): the input variables of senta_bilstm (words)
             outputs(dict): the output variables of senta_bilstm (the sentiment prediction results)
             main_program(Program): the main_program of lac with pretrained prameters
        """
        main_program = fluid.Program()
        startup_program = fluid.Program()
        with fluid.program_guard(main_program, startup_program):
            with fluid.unique_name.guard("@HUB_senta_bilstm@"):
                data = fluid.layers.data(
                    name="words", shape=[1], dtype="int64", lod_level=1)

                pred, fc = bilstm_net(data, 1256606)

                for param in main_program.global_block().iter_parameters():
                    param.trainable = trainable

                place = fluid.CPUPlace()
                exe = fluid.Executor(place)

                # load the senta_bilstm pretrained model
                def if_exist(var):
                    return os.path.exists(
                        os.path.join(self.pretrained_model_path, var.name))

                fluid.io.load_vars(
                    exe, self.pretrained_model_path, predicate=if_exist)

                inputs = {"words": data}
                outputs = {"class_probs": pred, "sentence_feature": fc}

                return inputs, outputs, main_program

    def to_unicode(self, texts):
        """
        Convert each element's type(str) of texts(list) to unicode in python2.7
        Args:
             texts(list): each element's type is str in python2.7
        Returns:
             texts(list): each element's type is unicode in python2.7
        """
        if six.PY2:
            unicode_texts = []
            for text in texts:
                if not isinstance(text, unicode):
                    unicode_texts.append(
                        text.decode(sys_stdin_encoding()).decode("utf8"))
                else:
                    unicode_texts.append(text)
            texts = unicode_texts
        return texts

    def texts2tensor(self, texts):
        """
        Tranform the texts(dict) to PaddleTensor
        Args:
             texts(dict): texts
        Returns:
             tensor(PaddleTensor): tensor with texts data
        """
        lod = [0]
        data = []
        for i, text in enumerate(texts):
            data += text['processed']
            lod.append(len(text['processed']) + lod[i])
        tensor = PaddleTensor(np.array(data).astype('int64'))
        tensor.name = "words"
        tensor.lod = [lod]
        tensor.shape = [lod[-1], 1]
        return tensor

    @serving
    def sentiment_classify(self, texts=[], data={}, use_gpu=False,
                           batch_size=1):
        """
        Get the sentiment prediction results results with the texts as input

        Args:
             texts(list): the input texts to be predicted, if texts not data
             data(dict): key must be 'text', value is the texts to be predicted, if data not texts
             use_gpu(bool): whether use gpu to predict or not
             batch_size(int): the program deals once with one batch

        Returns:
             results(list): the word segmentation results
        """
        try:
            _places = os.environ["CUDA_VISIBLE_DEVICES"]
            int(_places[0])
        except:
            use_gpu = False

        if texts != [] and isinstance(texts, list) and data == {}:
            predicted_data = texts
        elif texts == [] and isinstance(data, dict) and isinstance(
                data.get('text', None), list) and data['text']:
            predicted_data = data["text"]
        else:
            raise ValueError(
                "The input data is inconsistent with expectations.")

        predicted_data = self.to_unicode(predicted_data)
        start_idx = 0
        iteration = int(math.ceil(len(predicted_data) / batch_size))
        results = []
        for i in range(iteration):
            if i < (iteration - 1):
                batch_data = predicted_data[start_idx:(start_idx + batch_size)]
            else:
                batch_data = predicted_data[start_idx:]

            start_idx = start_idx + batch_size
            processed_results = preprocess(self.word_seg_module, batch_data,
                                           self.word_dict, use_gpu, batch_size)
            tensor_words = self.texts2tensor(processed_results)

            if use_gpu:
                batch_out = self.gpu_predictor.run([tensor_words])
            else:
                batch_out = self.cpu_predictor.run([tensor_words])
            batch_result = postprocess(batch_out[0], processed_results)
            results += batch_result
        return results

    @runnable
    def run_cmd(self, argvs):
        """
        Run as a command
        """
        self.parser = argparse.ArgumentParser(
            description="Run the senta_bilstm module.",
            prog='hub run senta_bilstm',
            usage='%(prog)s',
            add_help=True)

        self.arg_input_group = self.parser.add_argument_group(
            title="Input options", description="Input data. Required")
        self.arg_config_group = self.parser.add_argument_group(
            title="Config options",
            description=
            "Run configuration for controlling module behavior, not required.")

        self.add_module_config_arg()
        self.add_module_input_arg()

        args = self.parser.parse_args(argvs)

        try:
            input_data = self.check_input_data(args)
        except DataFormatError and RuntimeError:
            self.parser.print_help()
            return None

        if args.user_dict:
            self.set_user_dict(args.user_dict)

        results = self.sentiment_classify(
            texts=input_data, use_gpu=args.use_gpu, batch_size=args.batch_size)

        return results

    def add_module_config_arg(self):
        """
        Add the command config options
        """
        self.arg_config_group.add_argument(
            '--use_gpu',
            type=ast.literal_eval,
            default=False,
            help="whether use GPU for prediction")

        self.arg_config_group.add_argument(
            '--batch_size',
            type=int,
            default=1,
            help="batch size for prediction")
        self.arg_config_group.add_argument(
            '--user_dict',
            type=str,
            default=None,
            help=
            "customized dictionary for intervening the word segmentation result"
        )

    def add_module_input_arg(self):
        """
        Add the command input options
        """
        self.arg_input_group.add_argument(
            '--input_file',
            type=str,
            default=None,
            help="file contain input data")
        self.arg_input_group.add_argument(
            '--input_text', type=str, default=None, help="text to predict")

    def check_input_data(self, args):
        input_data = []
        if args.input_file:
            if not os.path.exists(args.input_file):
                print("File %s is not exist." % args.input_file)
                raise RuntimeError
            else:
                input_data = txt_parser.parse(args.input_file, use_strip=True)
        elif args.input_text:
            if args.input_text.strip() != '':
                if six.PY2:
                    input_data = [
                        args.input_text.decode(
                            sys_stdin_encoding()).decode("utf8")
                    ]
                else:
                    input_data = [args.input_text]
            else:
                print(
                    "ERROR: The input data is inconsistent with expectations.")

        if input_data == []:
            print("ERROR: The input data is inconsistent with expectations.")
            raise DataFormatError

        return input_data

    def get_vocab_path(self):
        """
        Get the path to the vocabulary whih was used to pretrain

        Returns:
             self.vocab_path(str): the path to vocabulary
        """
        return self.vocab_path

    def get_labels(self):
        """
        Get the labels which was used when pretraining
        Returns:
             self.labels(dict)
        """
        self.labels = {"positive": 1, "negative": 0}
        return self.labels


if __name__ == "__main__":
    senta = SentaBiLSTM()
    # Data to be predicted
    test_text = ["这家餐厅很好吃", "这部电影真的很差劲"]

    # execute predict and print the result
    input_dict = {"text": test_text}
    results = senta.sentiment_classify(data=input_dict, batch_size=3)
    for index, result in enumerate(results):
        if six.PY2:
            print(json.dumps(
                results[index], encoding="utf8", ensure_ascii=False))
        else:
            print(results[index])
    results = senta.sentiment_classify(texts=test_text)
    for index, result in enumerate(results):
        if six.PY2:
            print(json.dumps(
                results[index], encoding="utf8", ensure_ascii=False))
        else:
            print(results[index])
