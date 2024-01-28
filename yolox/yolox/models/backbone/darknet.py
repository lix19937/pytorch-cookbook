#!/usr/bin/env python
# -*- encoding: utf-8 -*-
# Copyright (c) Megvii Inc. All rights reserved.

from torch import nn

from yolox.models.components.network_blocks import BaseConv, CSPLayer, DWConv, Focus, ASPP, ResLayer, SPPBottleneck
#-----------------------------------------------------------------
from yolox.nv_qdq import QDQ
#-----------------------------------------------------------------

class Darknet(nn.Module):
    # number of blocks from dark2 to dark5.
    depth2blocks = {21: [1, 2, 2, 1], 53: [2, 8, 8, 4]}

    def __init__(self,
                 depth,
                 in_channels=3,
                 stem_out_channels=32,
                 out_features=("dark3", "dark4", "dark5"), 
                 quantize:bool = False
                 ):
        """
        Args:
            depth (int): depth of darknet used in model, usually use [21, 53] for this param.
            in_channels (int): number of input channels, for example, use 3 for RGB image.
            stem_out_channels (int): number of output channels of darknet stem.
                It decides channels of darknet layer2 to layer5.
            out_features (Tuple[str]): desired output layer name.
        """
        super().__init__()
        assert out_features, "please provide output features of Darknet"
        self.out_features = out_features
        self._quantize = quantize
        ##lixxxx
        self.stem = nn.Sequential(BaseConv(in_channels, stem_out_channels, ksize=3, stride=1, act="lrelu", quantize=quantize),
                                  *self.make_group_layer(stem_out_channels, num_blocks=1, stride=2) )
        in_channels = stem_out_channels * 2  # 64

        num_blocks = Darknet.depth2blocks[depth]
        # create darknet with `stem_out_channels` and `num_blocks` layers.
        # to make model structure more clear, we don't use `for` statement in python.
        self.dark2 = nn.Sequential(*self.make_group_layer(in_channels, num_blocks[0], stride=2))
        in_channels *= 2  # 128
        self.dark3 = nn.Sequential(*self.make_group_layer(in_channels, num_blocks[1], stride=2))
        in_channels *= 2  # 256
        self.dark4 = nn.Sequential(*self.make_group_layer(in_channels, num_blocks[2], stride=2))
        in_channels *= 2  # 512

        self.dark5 = nn.Sequential(*self.make_group_layer(in_channels, num_blocks[3], stride=2),
                                   *self.make_spp_block([in_channels, in_channels * 2], in_channels * 2),
                                   )

    def make_group_layer(self, in_channels: int, num_blocks: int, stride: int = 1):
        "starts with conv layer then has `num_blocks` `ResLayer`"
        return [BaseConv(in_channels, in_channels * 2, ksize=3, stride=stride, act="lrelu", quantize=self._quantize),
                *[(ResLayer(in_channels * 2, quantize=self._quantize)) for _ in range(num_blocks)],
                ]

    def make_spp_block(self, filters_list, in_filters):
        m = nn.Sequential(
            *[
                BaseConv(in_filters, filters_list[0], 1, stride=1, act="lrelu", quantize=self._quantize),
                BaseConv(filters_list[0], filters_list[1], 3, stride=1, act="lrelu", quantize=self._quantize),
                SPPBottleneck(in_channels=filters_list[1], out_channels=filters_list[0], activation="lrelu", quantize=self._quantize ),
                BaseConv(filters_list[0], filters_list[1], 3, stride=1, act="lrelu", quantize=self._quantize),
                BaseConv(filters_list[1], filters_list[0], 1, stride=1, act="lrelu", quantize=self._quantize),
            ]
        )
        return m

    def forward(self, x):
        outputs = {}
        x = self.stem(x)
        outputs["stem"] = x
        x = self.dark2(x)
        outputs["dark2"] = x
        x = self.dark3(x)
        outputs["dark3"] = x
        x = self.dark4(x)
        outputs["dark4"] = x
        x = self.dark5(x)
        outputs["dark5"] = x
        return {k: v for k, v in outputs.items() if k in self.out_features}


class CSPDarknet(nn.Module):
    def __init__(self,
                 dep_mul,
                 wid_mul,
                 out_features=("dark3", "dark4", "dark5"),
                 depthwise=False,
                 act="silu",
				         quantize:bool = False
                 ):
        super().__init__()
        assert out_features, "please provide output features of Darknet"
        self.out_features = out_features
        Conv = DWConv if depthwise else BaseConv

        base_channels = int(wid_mul * 64)  # 64
        base_depth = max(round(dep_mul * 3), 1)  # 3

        # stem
        # self.stem = Focus(3, base_channels, ksize=3, act=act)
        self.stem = ASPP(3, base_channels, act=act, quantize=quantize)

        # dark2
        self.dark2 = nn.Sequential(Conv(base_channels, base_channels * 2, 3, 2, act=act, quantize=quantize),
                                   CSPLayer(base_channels * 2, base_channels * 2, n=base_depth, depthwise=depthwise, act=act, quantize=quantize ), )

        # dark3
        self.dark3 = nn.Sequential(Conv(base_channels * 2, base_channels * 4, 3, 2, act=act, quantize=quantize),
                                   CSPLayer(base_channels * 4,
                                            base_channels * 4,
                                            n=base_depth * 3,
                                            depthwise=depthwise,
                                            act=act, 
											                      quantize=quantize
                                            ),
                                   )

        # dark4
        self.dark4 = nn.Sequential(Conv(base_channels * 4, base_channels * 8, 3, 2, act=act, quantize=quantize),
                                   CSPLayer(base_channels * 8,
                                            base_channels * 8,
                                            n=base_depth * 3,
                                            depthwise=depthwise,
                                            act=act,
											                      quantize=quantize
                                            ),
                                   )

        # dark5
        self.dark5 = nn.Sequential(Conv(base_channels * 8, base_channels * 16, 3, 2, act=act, quantize=quantize),
                                   SPPBottleneck(base_channels * 16, base_channels * 16, activation=act, quantize=quantize),
                                   CSPLayer(base_channels * 16,
                                            base_channels * 16,
                                            n=base_depth,
                                            shortcut=False,
                                            depthwise=depthwise,
                                            act=act,
											                      quantize=quantize
                                            ),
                                   )

        self._quantize = quantize             
        if self._quantize: 
          self.qdq = QDQ.quant_nn.TensorQuantizer(QDQ.quant_nn.QuantConv2d.default_quant_desc_input)

    def forward(self, x):
        outputs = {}
        x = self.stem(x)
        outputs["stem"] = x
        x = self.dark2(x)
        outputs["dark2"] = x
        x = self.dark3(x)
        outputs["dark3"] = x
        if self._quantize: 
          x = self.qdq(self.dark4(x))# dark3's output
        else:
          x = self.dark4(x)  

        outputs["dark4"] = x
        x = self.dark5(x)
        outputs["dark5"] = x
        return {k: v for k, v in outputs.items() if k in self.out_features}
