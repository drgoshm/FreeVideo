"""Shape-selected query slices for native FP32 H3 vision and language attention.

Small calls retain the original callable, arguments and stack. Larger calls use
the same Comfy basic kernel, complete keys and additive mask rows. No allocator
queries, admission, OOM retries or backend changes take place here.
"""
import json

FULL_ATTENTION_MAX_BYTES = 640 * 2**20
# Additional explicit tensors, not an allocator reservation. The encoder streams
# one weight at a time; its 2 GiB system reserve is OUTSIDE the 3 GiB MPS cap.
# 256 MiB is below the tightest observed remaining allowance (~0.29 GiB).
SLICE_WORKING_BYTES = 256 * 2**20


def slice_plan(tokens, heads, dim, kv_heads=None):
    """Pinned batch-one FP32 contract; include full output and both tile outputs.

    GQA visits one KV head's query heads at a time, with zero-stride KV views.
    This avoids basic's full repeat_interleave copies (191 MiB at N=3056).
    The estimate bounds explicit tensors, not opaque Metal kernel workspace.
    If even one row exceeds the allowance, retain one row and expose the
    over-budget estimate; do not perform admission or enlarge the allocator cap.
    """
    kv_heads = heads if kv_heads is None else kv_heads
    if min(tokens, heads, dim, kv_heads) < 1 or heads % kv_heads:
        raise ValueError('Expected positive self-attention dimensions and divisible GQA heads')
    group_heads = heads if heads == kv_heads else heads // kv_heads
    groups = heads // group_heads
    output_bytes = tokens * heads * dim * 4
    per_row = 2 * group_heads * (tokens + dim) * 4
    # Even if a whole GQA group fits, large calls still split the query axis.
    rows = min((tokens + 1) // 2, max(1, (SLICE_WORKING_BYTES - output_bytes) // per_row))
    return dict(query_rows=rows, head_groups=groups,
                slice_working_bytes=output_bytes + rows * per_row,
                minimum_slice_bytes=output_bytes + per_row)


def plan(tokens, heads, dim, kv_heads=None):
    kv_heads = heads if kv_heads is None else kv_heads
    selected = slice_plan(tokens, heads, dim, kv_heads)
    full_bytes = heads * tokens * tokens * 4
    full = full_bytes <= FULL_ATTENTION_MAX_BYTES
    if full:
        selected.update(query_rows=tokens, head_groups=1, slice_working_bytes=None)
    return dict(tokens=tokens, heads=heads, kv_heads=kv_heads, head_dim=dim,
                batch=1, dtype='torch.float32', full_matrix_bytes=full_bytes,
                mode='full' if full else 'sliced', **selected)


def sliced_basic(q, k, v, heads, rows, basic, *, mask=None, skip_reshape=True, enable_gqa=False):
    """Native batch-one FP32 self attention, with optional additive causal/pad mask."""
    import torch
    if (q.ndim != 4 or k.ndim != 4 or k.shape != v.shape or q.shape[0] != 1
            or k.shape[0] != 1 or q.shape[1] != heads or q.shape[2:] != k.shape[2:]
            or heads % k.shape[1] or (k.shape[1] != heads and not enable_gqa)
            or not skip_reshape or rows < 1
            or q.dtype != torch.float32 or k.dtype != q.dtype or v.dtype != q.dtype
            or k.device != q.device or v.device != q.device):
        raise ValueError('Expected native batch-one FP32 H3 self-attention tensors')
    if torch.is_grad_enabled():
        raise RuntimeError('Bounded encoder attention requires inference without gradients')
    tokens, dim = q.shape[2:]
    if mask is not None and (mask.dtype != q.dtype or mask.device != q.device
            or mask.ndim not in (2, 4) or mask.shape[-1] != tokens
            or mask.shape[-2] not in (1, tokens)
            or (mask.ndim == 4 and (mask.shape[0] != 1 or mask.shape[1] not in (1, heads)))):
        raise ValueError('Expected a native additive [N,N] or [1,1|H,1|N,N] mask')
    output = q.new_empty((1, tokens, heads * dim))
    kv_heads = k.shape[1]
    group_heads = heads if kv_heads == heads else heads // kv_heads
    for start in range(0, tokens, rows):
        stop = min(tokens, start + rows)
        row_mask = mask
        if mask is not None and mask.shape[-2] != 1:
            row_mask = mask[..., start:stop, :]
        for head in range(0, heads, group_heads):
            if kv_heads == heads:
                keys, values = k, v
            else:
                index = head // group_heads
                # Batch one is important: flattening [1,G,N,D] in basic keeps
                # these broadcasts as views, rather than allocating repeated KV.
                keys = k[:, index:index + 1].expand(1, group_heads, tokens, dim)
                values = v[:, index:index + 1].expand_as(keys)
            part_mask = row_mask
            if mask is not None and mask.ndim == 4 and mask.shape[1] == heads:
                part_mask = row_mask[:, head:head + group_heads]
            options = dict(skip_reshape=True)
            if mask is not None:
                options['mask'] = part_mask
            part = basic(q[:, head:head + group_heads, start:stop], keys, values,
                         group_heads, **options)
            output[:, start:stop, head * dim:(head + group_heads) * dim].copy_(part)
            if q.device.type == 'mps':
                # Complete pending Metal work before another tile can allocate.
                torch.mps.synchronize()
            del part
    return output


def install(clip):
    """Return shared dispatch statistics and removable instance hooks.

    Counts are attempts/planned basic invocations, not successful completions.
    Installed for text/audio-only prompts too; no vendor definitions are changed.
    """
    from comfy.ldm.modules.attention import attention_basic
    transformer = clip.cond_stage_model.qwen3vl_32b.transformer
    vision = [block.attn for block in transformer.visual.blocks]
    language = [layer.self_attn for layer in transformer.model.layers]
    if (not vision or not language
            or any((m.num_heads, m.head_dim) != (16, 72) for m in vision)
            or any((m.num_heads, m.num_kv_heads, m.head_dim) != (64, 8, 128) for m in language)):
        raise ValueError('Expected the pinned H3 vision and language attention geometry')
    stats = dict(full_calls=0, sliced_calls=0, slices=0, plans=[],
                 full_attention_max_bytes=FULL_ATTENTION_MAX_BYTES,
                 slice_working_bytes=SLICE_WORKING_BYTES,
                 kernel='Comfy attention_basic: einsum, scale, additive mask, softmax, einsum; FP32')
    seen = set()

    def attention(q, k, v, heads, *, mask=None, skip_reshape=False, enable_gqa=False):
        if q.device.type != 'mps' or not skip_reshape:
            raise ValueError('Bounded native encoder attention requires MPS and pre-shaped heads')
        selected = plan(q.shape[2], heads, q.shape[3], k.shape[1])
        stats['sliced_calls'] += 1
        stats['slices'] += ((q.shape[2] + selected['query_rows'] - 1) // selected['query_rows']
                           * selected['head_groups'])
        return sliced_basic(q, k, v, heads, selected['query_rows'], attention_basic,
                            mask=mask, skip_reshape=skip_reshape, enable_gqa=enable_gqa)

    def selector(kind):
        def select(module, args, kwargs):
            if kwargs.get('optimized_attention') is not attention_basic:
                return None
            x = args[0] if args else kwargs['x' if kind == 'vision' else 'hidden_states']
            tokens = x.shape[0] if kind == 'vision' else x.shape[1]
            selected = plan(tokens, module.num_heads, module.head_dim,
                            module.num_heads if kind == 'vision' else module.num_kv_heads)
            key = (kind, tokens)
            if key not in seen:
                seen.add(key)
                stats['plans'].append(dict(kind=kind, **selected))
                print(json.dumps(dict(event='encoder_' + kind + '_attention', **selected,
                                      kernel=stats['kernel'])), flush=True)
            if selected['mode'] == 'full':
                stats['full_calls'] += 1
                stats['slices'] += 1
                # Preserve the exact original callable, kwargs and call stack.
                return None
            if kind == 'language' and (x.shape[0] != 1 or kwargs.get('past_key_value') is not None):
                raise ValueError('Expected H3 batch-one conditioning without a KV cache')
            return args, dict(kwargs, optimized_attention=attention)
        return select

    handles = []
    try:
        for modules, kind in ((vision, 'vision'), (language, 'language')):
            select = selector(kind)
            for module in modules:
                handles.append(module.register_forward_pre_hook(select, with_kwargs=True))
    except BaseException:
        for handle in handles:
            handle.remove()
        raise
    return stats, handles
