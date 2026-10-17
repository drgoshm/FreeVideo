"""Shape-selected projection and token slices for the native Mac encoder.

No allocator queries or precision changes. The 3 GiB pool's explicit tensor
ledger reserves <1 GiB for surrounding H3 state, 256 MiB for MLP activations,
128 MiB for a projection (including its assembled output), and 256 MiB for
attention. These stages do not all overlap. The remaining space is headroom
for Metal; it is not a promise about undocumented kernel/driver allocations.
"""
import json
import math
import time
import types

FULL_LINEAR_MAX_BYTES = 128 * 2**20
LINEAR_WORKING_BYTES = 128 * 2**20
MLP_WORKING_BYTES = 256 * 2**20


def linear_plan(tokens, inputs, outputs, itemsize=4, *, packed_columns=None, packed_rows=None):
    """Bound full output + decoded slab + packed/scales + slab result + bias.

    Packed geometry can include padding on the input axis. A result whose full
    output alone exceeds the budget still uses one row and reports that minimum
    explicitly; the parent MLP's token plan avoids this for the H3 workload.
    """
    if min(tokens, inputs, outputs, itemsize) < 1:
        raise ValueError('Expected positive linear dimensions')
    columns = inputs if packed_columns is None else packed_columns
    if columns < inputs:
        raise ValueError('Packed columns cannot be smaller than the input')
    output = tokens * outputs * itemsize
    weight = (outputs if packed_rows is None else packed_rows) * columns * itemsize
    # NVFP4 upload: 1/2 byte per value + FP32 scale per 16 values.
    upload = 0 if packed_columns is None else columns // 2 + columns // 16 * 4
    per_row = columns * itemsize + upload + (tokens + 1) * itemsize
    scale = 4 if packed_columns is not None else 0
    rows = min(outputs, max(1, (LINEAR_WORKING_BYTES - output - scale) // per_row))
    full = weight + output <= FULL_LINEAR_MAX_BYTES
    return dict(tokens=tokens, inputs=inputs, outputs=outputs, itemsize=itemsize,
                decoded_weight_bytes=weight, output_bytes=output,
                full_weight_output_bytes=weight + output,
                mode='full' if full else 'sliced', output_rows=outputs if full else rows,
                slice_working_bytes=None if full else output + rows * per_row + scale,
                minimum_slice_bytes=output + per_row + scale)


def mlp_plan(tokens, hidden, intermediate, itemsize=4):
    """Three simultaneous intermediate arrays plus two hidden-sized row arrays.

    The full assembled MLP output stays live across chunks. Linear workspace is
    counted separately, conservatively counting some output storage twice.
    """
    if min(tokens, hidden, intermediate, itemsize) < 1:
        raise ValueError('Expected positive MLP dimensions')
    output = tokens * hidden * itemsize
    per_row = (3 * intermediate + 2 * hidden) * itemsize
    full = tokens * per_row <= MLP_WORKING_BYTES
    rows = min(tokens, max(1, (MLP_WORKING_BYTES - output) // per_row))
    return dict(tokens=tokens, hidden=hidden, intermediate=intermediate, itemsize=itemsize,
                mode='full' if full else 'sliced', token_rows=tokens if full else rows,
                full_activation_bytes=tokens * per_row,
                slice_activation_bytes=None if full else output + rows * per_row,
                minimum_slice_bytes=output + per_row)


def sliced_linear(input, outputs, rows, load_weight, bias=None, *, synchronize=lambda: None,
                  release=lambda: None):
    """Each output dot product uses ALL input channels; never sum partial dots."""
    import torch
    if torch.is_grad_enabled():
        raise RuntimeError('Bounded encoder linear requires inference without gradients')
    if rows < 1:
        raise ValueError('Expected positive output row slice')
    output = input.new_empty((*input.shape[:-1], outputs))
    for start in range(0, outputs, rows):
        stop = min(outputs, start + rows)
        weight = part = local_bias = None
        try:
            weight = load_weight(start, stop)
            if bias is not None:
                local_bias = bias[start:stop].to(device=input.device, dtype=input.dtype)
            part = torch.nn.functional.linear(input, weight, local_bias)
            output[..., start:stop].copy_(part)
            synchronize()
        finally:
            del weight, part, local_bias
            release()
    return output


def sliced_mlp(input, rows, original, *, synchronize=lambda: None, release=lambda: None):
    """Keep the native MLP/AWQ expression and dtype for each token row slice."""
    import torch
    if torch.is_grad_enabled():
        raise RuntimeError('Bounded encoder MLP requires inference without gradients')
    if input.ndim != 3 or input.shape[0] != 1 or rows < 1:
        raise ValueError('Expected batch-one H3 MLP input and positive token rows')
    output = torch.empty_like(input)
    for start in range(0, input.shape[1], rows):
        part = None
        try:
            part = original(input[:, start:start + rows])
            output[:, start:start + rows].copy_(part)
            synchronize()
        finally:
            del part
            release()
    return output


class _Restore:
    """Also restore an active override if execution aborts before its post hook."""
    def __init__(self, module, attribute):
        self.module, self.attribute = module, attribute
        self.original = module.__dict__.get(attribute)

    def remove(self):
        if self.original is None:
            self.module.__dict__.pop(self.attribute, None)
        else:
            setattr(self.module, self.attribute, self.original)


def install(linears, layers, *, decoder, quantized_type, backend, guard, release, reader_stats):
    """Select before the native forward looks up its projection/MLP callable.

    Below the thresholds do not assign anything: the fourth-version weight
    reader and vendor MLP function objects remain intact. MLP selection occurs
    at the parent layer because Module reads its own forward before pre-hooks.
    """
    stats = dict(linear_sliced_calls=0, linear_slices=0, mlp_sliced_calls=0, mlp_slices=0,
                 plans=[], full_linear_max_bytes=FULL_LINEAR_MAX_BYTES,
                 linear_working_bytes=LINEAR_WORKING_BYTES, mlp_working_bytes=MLP_WORKING_BYTES)
    handles, seen = [], set()

    def record(kind, selected):
        key = (kind, tuple(selected.values()))
        if key not in seen:
            seen.add(key)
            stats['plans'].append(dict(kind=kind, **selected))
            print(json.dumps(dict(event='encoder_' + kind + '_slices', **selected)), flush=True)

    def install_linear(module):
        restore = _Restore(module, 'forward_comfy_cast_weights')
        active = False

        def before(module, args, kwargs):
            nonlocal active
            x = args[0] if args else kwargs['input']
            quantized = isinstance(module.weight, quantized_type)
            columns = module.weight._qdata.shape[1] * 2 if quantized else None
            selected = linear_plan(math.prod(x.shape[:-1]), module.in_features,
                                   module.out_features, x.element_size(), packed_columns=columns,
                                   packed_rows=module.weight._qdata.shape[0] if quantized else None)
            record('linear', selected)
            if selected['mode'] == 'full':
                return None

            def linear(module, input, *args, **kwargs):
                import torch
                if input.device.type != 'mps' or input.dtype != torch.float32:
                    raise ValueError('Native H3 encoder must execute FP32 operations on MPS')
                guard()
                source = module.weight
                if quantized and module.quant_format != 'nvfp4':
                    raise NotImplementedError('Unsupported MPS encoder projection storage: ' + module.quant_format)

                def load(start, stop):
                    tick = time.monotonic()
                    weight = (decoder(source, input.dtype, input.device, row_range=(start, stop))
                              if quantized else source[start:stop].to(device=input.device, dtype=input.dtype))
                    reader_stats['weight_decode_seconds'] += time.monotonic() - tick
                    reader_stats['largest_weight_bytes'] = max(reader_stats['largest_weight_bytes'],
                        weight.untyped_storage().nbytes())
                    stats['linear_slices'] += 1
                    return weight

                def cleanup():
                    backend.empty_cache()
                    release()

                stats['linear_sliced_calls'] += 1
                result = sliced_linear(input, module.out_features, selected['output_rows'], load,
                                       module.bias, synchronize=backend.synchronize, release=cleanup)
                reader_stats['linear_calls'] += 1
                return result

            module.forward_comfy_cast_weights = types.MethodType(linear, module)
            active = True

        def after(module, args, kwargs, output):
            nonlocal active
            if active:
                restore.remove()
                active = False

        handles.append(restore)
        handles.append(module.register_forward_pre_hook(before, with_kwargs=True))
        handles.append(module.register_forward_hook(after, with_kwargs=True, always_call=True))

    def install_mlp(layer):
        mlp = layer.mlp
        if getattr(mlp, 'merged_mlp', False):
            raise ValueError('Expected native H3 separate gate/up projections')
        original = mlp.forward
        restore = _Restore(mlp, 'forward')
        active = False

        def before(module, args, kwargs):
            nonlocal active
            x = args[0] if args else kwargs['x']
            selected = mlp_plan(math.prod(x.shape[:-1]), mlp.down_proj.out_features,
                                mlp.down_proj.in_features, x.element_size())
            record('mlp', selected)
            if selected['mode'] == 'full':
                return None

            def forward(module, input):
                stats['mlp_sliced_calls'] += 1
                stats['mlp_slices'] += (input.shape[1] + selected['token_rows'] - 1) // selected['token_rows']
                return sliced_mlp(input, selected['token_rows'], original,
                                  synchronize=backend.synchronize, release=backend.empty_cache)

            mlp.forward = types.MethodType(forward, mlp)
            active = True

        def after(module, args, kwargs, output):
            nonlocal active
            if active:
                restore.remove()
                active = False

        handles.append(restore)
        handles.append(layer.register_forward_pre_hook(before, with_kwargs=True))
        handles.append(layer.register_forward_hook(after, with_kwargs=True, always_call=True))

    try:
        for module in linears:
            install_linear(module)
        for layer in layers:
            install_mlp(layer)
    except BaseException:
        for handle in handles:
            handle.remove()
        raise
    return stats, handles
