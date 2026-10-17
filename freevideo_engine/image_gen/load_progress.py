"""Byte accounting for completed safetensor reads, without opening tensor data."""
from math import prod


_DTYPE_BYTES = {'BOOL': 1, 'U8': 1, 'I8': 1, 'I16': 2, 'I32': 4, 'I64': 8,
                'F16': 2, 'BF16': 2, 'F32': 4, 'F64': 8,
                'F8_E4M3': 1, 'F8_E5M2': 1}


class TensorProgress:
    def __init__(self, checkpoint, callback, keys=None):
        self.callback = callback
        self.sizes = {}
        for key in checkpoint.keys() if keys is None else keys:
            value = checkpoint.get_slice(key)
            self.sizes[key] = prod(value.get_shape()) * _DTYPE_BYTES[value.get_dtype()]
        self.total = sum(self.sizes.values())
        self.done = self.last = 0

    def tensors(self, *keys):
        self.advance(sum(self.sizes[key] for key in keys))

    def advance(self, size):
        self.done += size
        if self.done > self.last and ((self.done - self.last) * 50 >= self.total or self.done == self.total):
            self.last = self.done
            self.callback(self.done, self.total)
