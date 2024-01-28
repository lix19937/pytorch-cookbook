#!/usr/bin/env python3
# -*- coding:utf-8 -*-
# Copyright (c) Megvii, Inc. and its affiliates.

import argparse
import os
from loguru import logger

import torch
from torch import nn
import sys

sys.path.append('.')
from yolox.exp import get_exp
from yolox.models.components.network_blocks import SiLU
from yolox.utils import replace_module
#-----------------------------------------------------------------
from yolox.nv_qdq import QDQ
#-----------------------------------------------------------------

def make_parser():
    parser = argparse.ArgumentParser("YOLOX onnx deploy")
    parser.add_argument("--output-name", type=str, default="yolox.onnx", help="output name of models")
    parser.add_argument("--input", default="images", type=str, help="input node name of onnx model")
    parser.add_argument("--output", default="output", type=str, help="output node name of onnx model")
    parser.add_argument("-o", "--opset", default=11, type=int, help="onnx opset version")
    parser.add_argument("--batch-size", type=int, default=1, help="batch size")
    parser.add_argument("--dynamic", action="store_true", help="whether the input shape should be dynamic or not")
    parser.add_argument("--no-onnxsim", action="store_true", help="use onnxsim or not")
    parser.add_argument("-f", "--exp_file", default=None, type=str, help="experiment description file", )
    parser.add_argument("-expn", "--experiment-name", type=str, default=None)
    parser.add_argument("-n", "--name", type=str, default=None, help="model name")
    parser.add_argument("-c", "--ckpt", default=None, type=str, help="ckpt path")
    parser.add_argument("-p", "--platform", default=None, type=str, help="onnx for platform")
    parser.add_argument("opts", help="Modify config options using the command-line", default=None, nargs=argparse.REMAINDER, )
    parser.add_argument("--decode_in_inference", action="store_true", help="decode in inference or not")
    return parser

@logger.catch
def main():
    args = make_parser().parse_args()
    logger.info("args value: {}".format(args))
    exp = get_exp(args.exp_file, args.name)
    exp.merge(args.opts)

    if not args.experiment_name:
        args.experiment_name = exp.exp_name

    model = exp.get_model(head_type='obb')
    if args.ckpt is None:
        file_name = os.path.join(exp.output_dir, args.experiment_name)
        ckpt_file = os.path.join(file_name, "best_ckpt.pth")
    else:
        ckpt_file = args.ckpt

    # load the model state dict
    ckpt = torch.load(ckpt_file, map_location="cpu")
    logger.info("load done: {}".format(type(ckpt)))

    model.eval()
    QDQ.quant_nn.TensorQuantizer.use_fb_fake_quant = True

    if "model" in ckpt:
        ckpt = ckpt["model"]
    model.load_state_dict(ckpt)
    model = replace_module(model, nn.SiLU, SiLU)
    model.head.decode_in_inference = args.decode_in_inference

    logger.info("loading checkpoint done.")
    dummy_input = torch.randn(args.batch_size, 3, exp.test_size[0], exp.test_size[1])

    logger.info("export ...")

    torch.onnx._export(model,
                       dummy_input,
                       args.output_name,
                       input_names=[args.input],
                       output_names=[args.output],
                       # dynamic_axes={args.input: {0: 'batch'},
                       #               args.output: {0: 'batch'}} if args.dynamic else None,
                       do_constant_folding=True,
                       opset_version=args.opset,
                       )
    logger.info("generated onnx model named {}".format(args.output_name))

    if not args.no_onnxsim:
        import onnx
        from onnxsim import simplify

        input_shapes = {args.input: list(dummy_input.shape)} if args.dynamic else None

        # use onnxsimplify to reduce reduent model.
        onnx_model = onnx.load(args.output_name)
        model_simp, check = simplify(onnx_model,
                                     # dynamic_input_shape=args.dynamic,
                                     input_shapes=input_shapes)

        # if args.platform == 'dla':
        #     graph = model_simp.graph
        #     nodes = graph.node
        #     for i in range(len(nodes)):
        #         print(i, nodes[i])
        #     graph.node.remove(nodes[291])
        #     graph.node.remove(nodes[290])
        #     graph.node.remove(nodes[289])
        #     graph.node.remove(nodes[288])
        #     graph.node.remove(nodes[287])
        #     #
        #     info0 = onnx.helper.make_tensor_value_info('222', onnx.TensorProto.FLOAT, [1, 7, 80, 80])
        #     info1 = onnx.helper.make_tensor_value_info('261', onnx.TensorProto.FLOAT, [1, 7, 40, 40])
        #     info2 = onnx.helper.make_tensor_value_info('286', onnx.TensorProto.FLOAT, [1, 7, 20, 20])
        #     #
        #     # # 将构建的输出插入到模型中
        #     model_simp.graph.output.insert(0, info0)
        #     model_simp.graph.output.insert(1, info1)
        #     model_simp.graph.output.insert(2, info2)
        #
        #     # model_simp.graph.output.insert(0, info0)
        #     # model_simp.graph.output.insert(1, info1)
        #     # model_simp.graph.output.insert(2, info2)
        #
        #     # out = model_simp.graph.output
        #     # del out[-1]
        #     logger.info('model_simp.graph.output: {}'.format(model_simp.graph.output))
        #
        #     model_simp, check = simplify(model_simp,
        #                                  dynamic_input_shape=args.dynamic,
        #                                  input_shapes=input_shapes)
        #     assert check, "Simplified ONNX model could not be validated"
        #     onnx.save(model_simp, args.output_name)

        assert check, "Simplified ONNX model could not be validated"
        onnx.save(model_simp, args.output_name)
        logger.info("generated simplified onnx model named {}".format(args.output_name))

if __name__ == "__main__":
    main()
