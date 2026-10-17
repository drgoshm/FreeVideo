# Copyright 2025 HuggingFace Inc.
# Copyright 2026 The HuggingFace Team. All rights reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy at http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Small local equivalents of the inference helpers used by the upstream models."""
from collections import OrderedDict
from copy import deepcopy
from functools import wraps
import inspect
import math

import torch
from torch import nn


class ModelConfig(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def model_config(init):
    """Record explicit/default constructor values, with no serialization framework."""
    signature = inspect.signature(init)

    @wraps(init)
    def wrapped(self, *args, **kwargs):
        if len(args) == 1 and isinstance(args[0], dict):
            kwargs = {**args[0], **kwargs}
            args = ()
        bound = signature.bind(self, *args, **kwargs)
        bound.apply_defaults()
        self.config = ModelConfig({key: deepcopy(value) for key, value in bound.arguments.items() if key != "self"})
        init(self, *args, **kwargs)

    return wrapped


class ModelOutput(OrderedDict):
    """Attribute, mapping and tuple-index access used by Diffusers callers."""
    def __init__(self, **values):
        super().__init__(values)
        self.__dict__.update(values)

    def __getitem__(self, key):
        return super().__getitem__(key) if isinstance(key, str) else self.to_tuple()[key]

    def to_tuple(self):
        return tuple(self.values())


AutoencoderKLOutput = ModelOutput


class RMSNorm(nn.Module):
    """Upstream FP32 variance/rsqrt arithmetic and checkpoint-compatible weight."""
    def __init__(self, dim, eps):
        super().__init__()
        self.eps = eps
        self.weight = nn.Parameter(torch.ones(dim))

    def forward(self, hidden_states):
        variance = hidden_states.float().pow(2).mean(-1, keepdim=True)
        hidden_states = hidden_states * torch.rsqrt(variance + self.eps)
        if self.weight.dtype in (torch.float16, torch.bfloat16):
            hidden_states = hidden_states.to(self.weight.dtype)
        return hidden_states * self.weight


class TimestepEmbedding(nn.Module):
    def __init__(self, in_channels, time_embed_dim, sample_proj_bias=False):
        super().__init__()
        self.linear_1 = nn.Linear(in_channels, time_embed_dim, bias=sample_proj_bias)
        self.act = nn.SiLU()
        self.linear_2 = nn.Linear(time_embed_dim, time_embed_dim, bias=sample_proj_bias)

    def forward(self, sample):
        return self.linear_2(self.act(self.linear_1(sample)))


class DiagonalGaussianDistribution:
    """Inference posterior; arithmetic follows Diffusers' diagonal Gaussian."""
    def __init__(self, parameters, deterministic=False):
        self.parameters = parameters
        self.mean, self.logvar = parameters.chunk(2, dim=1)
        self.logvar = self.logvar.clamp(-30.0, 20.0)
        self.deterministic = deterministic
        self.std = torch.exp(0.5 * self.logvar)
        self.var = torch.exp(self.logvar)
        if deterministic:
            self.var = self.std = torch.zeros_like(self.mean)

    def sample(self, generator=None):
        # Match randn_tensor's CPU-generator support for accelerator posteriors.
        noise_device = generator.device if generator is not None else self.mean.device
        noise = torch.randn(self.mean.shape, generator=generator, device=noise_device, dtype=self.mean.dtype)
        return self.mean + self.std * noise.to(self.mean.device)

    def mode(self):
        return self.mean

    def kl(self, other=None):
        if self.deterministic:
            return torch.tensor([0.0])
        if other is None:
            value = self.mean.pow(2) + self.var - 1.0 - self.logvar
        else:
            value = (self.mean - other.mean).pow(2) / other.var + self.var / other.var - 1.0 - self.logvar + other.logvar
        return 0.5 * value.sum(dim=(1, 2, 3))

    def nll(self, sample, dims=(1, 2, 3)):
        if self.deterministic:
            return torch.tensor([0.0])
        return 0.5 * (math.log(2.0 * math.pi) + self.logvar + (sample - self.mean).pow(2) / self.var).sum(dim=dims)
