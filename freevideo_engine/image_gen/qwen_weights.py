"""Strict, tensor-at-a-time loaders for the original Comfy Qwen-Image files.

The DiT skeleton is created on meta. Quantized rows are never dequantized or
requantized; the fused gate_up is sliced gate-first, as verified by the B200
prototype. Nonquantized parameters are BF16; row scales remain FP32.
"""
import inspect
import json

import torch
from torch import nn

from ..streamed_weights import _open_safetensors
from .qwen_dit import QwenImageDiT
from .qwen_vae import QwenImageVAE, comfy_vae_key_to_diffusers, vae_key_map
from .load_progress import TensorProgress


CONVROT_METADATA = {"format": "int8_tensorwise", "convrot": True, "convrot_groupsize": 256}
COMFY_ATTENTION_HINT = {"attention": "comfy_kitchen_int8"}


def _config(metadata, model_class, override):
    """Optional JSON `config` metadata; absent metadata uses official 2.1 defaults."""
    try:
        value = json.loads(metadata["config"]) if "config" in metadata else {}
    except (ValueError, TypeError) as exc:
        raise ValueError("Checkpoint config metadata must be a JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError("Checkpoint config metadata must be a JSON object")
    value = {key: item for key, item in value.items() if not key.startswith("_")}
    if override is not None:
        value.update(override)
    allowed = set(inspect.signature(model_class.__init__).parameters) - {"self"}
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"Unknown {model_class.__name__} config fields: {sorted(unknown)}")
    return value


def _keys(checkpoint, prefix=""):
    result = {}
    for disk_key in checkpoint.keys():
        key = disk_key[len(prefix):] if prefix and disk_key.startswith(prefix) else disk_key
        if key in result:
            raise ValueError(f"Duplicate normalized checkpoint key: {key}")
        result[key] = disk_key
    return result


def _shape_dtype(checkpoint, key):
    tensor_slice = checkpoint.get_slice(key)
    return tuple(tensor_slice.get_shape()), tensor_slice.get_dtype()


def _check_tensor(checkpoint, key, shape, dtypes):
    actual_shape, dtype = _shape_dtype(checkpoint, key)
    if actual_shape != tuple(shape) or dtype not in dtypes:
        raise ValueError(f"Unsupported tensor {key}: shape={actual_shape}, dtype={dtype}; expected {tuple(shape)}, {dtypes}")


def _assign(model, name, tensor, device, dtype):
    """Install only this tensor; no full state_dict or initialized weight copy."""
    parent, _, child = name.rpartition(".")
    module = model.get_submodule(parent) if parent else model
    value = tensor.to(device=device, dtype=dtype).contiguous()
    if child in module._parameters:
        module._parameters[child] = nn.Parameter(value, requires_grad=False)
    elif child in module._buffers:
        module._buffers[child] = value
    else:
        raise ValueError(f"Checkpoint target is not a parameter or buffer: {name}")


def _quant_targets(model, name):
    if name.endswith("img_mlp.gate_up"):
        prefix = name[:-len("gate_up")]
        targets = [prefix + "gate_layer", prefix + "proj"]
    else:
        targets = [name]
    modules = []
    for target in targets:
        try:
            module = model.get_submodule(target)
        except AttributeError as exc:
            raise ValueError(f"Unknown quantized DiT layer: {name}") from exc
        if not isinstance(module, nn.Linear) or module.in_features % 256:
            raise ValueError(f"ConvRot target must be a Linear with group-256 input: {name}")
        modules.append(module)
    if len(modules) == 2 and modules[0].weight.shape != modules[1].weight.shape:
        raise ValueError(f"Fused gate_up has incompatible gate/proj shapes: {name}")
    return targets, modules


def load_dit(path, device="cpu", *, config=None, progress=None):
    """Load a Comfy Turbo int8_convrot DiT; CPU supports storage, CUDA execution.

    The optional config override is for small models/tests. Production files
    without JSON config metadata use the official 32-block Qwen-Image 2.1 config.
    An unknown marker, tensor layout, missing key, or extra key is an error.
    ``progress(done_bytes, total_bytes)`` reports completed source tensor batches.
    """
    with _open_safetensors(path, framework="pt", device="cpu") as checkpoint:
        reads = TensorProgress(checkpoint, progress) if progress is not None else None
        with torch.device("meta"):
            model = QwenImageDiT(**_config(checkpoint.metadata() or {}, QwenImageDiT, config))
        keys = _keys(checkpoint, "model.diffusion_model.")
        expected = model.state_dict()
        quantized = {}
        consumed, replaced = set(), set()
        names = sorted(key[:-len(".comfy_quant")] for key in keys if key.endswith(".comfy_quant"))
        if not names:
            raise ValueError("Expected Comfy int8_convrot DiT with .comfy_quant markers")
        # Validate the complete key/shape/dtype contract before reading large matrices.
        for name in names:
            marker_key = keys[name + ".comfy_quant"]
            marker_shape, marker_dtype = _shape_dtype(checkpoint, marker_key)
            if marker_dtype != "U8" or len(marker_shape) != 1 or not 0 < marker_shape[0] <= 4096:
                raise ValueError(f"Invalid Comfy quantization marker tensor: {marker_key}")
            try:
                marker = json.loads(bytes(checkpoint.get_tensor(marker_key).tolist()).decode("utf-8"))
            except (ValueError, UnicodeError) as exc:
                raise ValueError(f"Invalid Comfy quantization JSON: {marker_key}") from exc
            if marker != CONVROT_METADATA or type(marker.get("convrot")) is not bool or type(marker.get("convrot_groupsize")) is not int:
                raise ValueError(f"Unsupported Comfy quantization format for {name}: {marker!r}")
            if reads is not None:
                reads.tensors(marker_key)
            targets, modules = _quant_targets(model, name)
            rows = sum(module.out_features for module in modules)
            width = modules[0].in_features
            for suffix, shape, dtypes in (("weight", (rows, width), {"I8"}), ("weight_scale", (rows, 1), {"F32"})):
                key = name + "." + suffix
                if key not in keys:
                    raise ValueError(f"Missing quantized DiT tensor: {key}")
                _check_tensor(checkpoint, keys[key], shape, dtypes)
                consumed.add(key)
            consumed.add(name + ".comfy_quant")
            for target in targets:
                if target + ".weight" in replaced:
                    raise ValueError(f"Multiple checkpoint tensors target the same Linear: {target}")
                replaced.add(target + ".weight")
            quantized[name] = targets
        # Comfy writes one attention hint per block ({"attention": "comfy_kitchen_int8"}); this
        # engine runs the reference BF16 attention, so the hint is validated and not used.
        for key in sorted(k for k in keys if k.endswith(".attn.comfy_attention.config")):
            try:
                hint = json.loads(bytes(checkpoint.get_tensor(keys[key]).tolist()).decode("utf-8"))
            except (ValueError, UnicodeError) as exc:
                raise ValueError(f"Invalid Comfy attention hint: {key}") from exc
            if hint != COMFY_ATTENTION_HINT:
                raise ValueError(f"Unsupported Comfy attention format for {key}: {hint!r}")
            if reads is not None:
                reads.tensors(keys[key])
            consumed.add(key)
        ordinary = set(expected) - replaced
        missing, extra = ordinary - set(keys), set(keys) - consumed - ordinary
        if missing or extra:
            raise ValueError(f"DiT checkpoint keys differ: missing={sorted(missing)}, unexpected={sorted(extra)}")
        for key in ordinary:
            _check_tensor(checkpoint, keys[key], expected[key].shape, {"BF16", "F32"})
        # Reuse the engine's wrapper construction and exact runtime member names.
        from ..int8_ops import install_cached_linears
        specs = {}
        for targets in quantized.values():
            for target in targets:
                shape = list(expected[target + ".weight"].shape)
                specs[target] = dict(storage="int8", convrot_group=256, weight_shape=shape,
                                     scale_shape=[shape[0], 1], input_dtype="bfloat16")
        install_cached_linears(model, specs, dict(kind="convrot", group=256))
        del expected
        # Load scales and weights together, releasing each view before the next layer.
        for name, targets in quantized.items():
            scales = checkpoint.get_tensor(keys[name + ".weight_scale"])
            if not torch.isfinite(scales).all() or not (scales > 0).all():
                raise ValueError(f"DiT weight scales must be finite and positive: {name}")
            weight = checkpoint.get_tensor(keys[name + ".weight"])
            offset = 0
            for target in targets:
                rows = model.get_submodule(target).out_features
                _assign(model, target + ".weight_int8", weight[offset:offset + rows], device, torch.int8)
                _assign(model, target + ".weight_scale", scales[offset:offset + rows], device, torch.float32)
                offset += rows
            del weight, scales
            if reads is not None:
                reads.tensors(keys[name + ".weight"], keys[name + ".weight_scale"])
        for key in sorted(ordinary):
            # A quantized Linear's original bias (if a future supported config has
            # one) lives under original.bias in the engine wrapper.
            parent, _, leaf = key.rpartition(".")
            target = key if parent not in specs or leaf != "bias" else parent + ".original.bias"
            _assign(model, target, checkpoint.get_tensor(keys[key]), device, torch.bfloat16)
            if reads is not None:
                reads.tensors(keys[key])
        model.to(device=device)
        return model.eval().requires_grad_(False)


def load_vae(path, device="cpu", *, config=None, progress=None):
    """Load the Comfy BF16 VAE, accepting only explicit one-to-one key mappings.

    Single-frame Comfy Conv3d weights may retain a size-one temporal axis. It is
    squeezed without numerical conversion; any other shape is rejected.
    ``progress(done_bytes, total_bytes)`` counts source bytes before squeezing.
    """
    with _open_safetensors(path, framework="pt", device="cpu") as checkpoint:
        reads = TensorProgress(checkpoint, progress) if progress is not None else None
        with torch.device("meta"):
            model = QwenImageVAE(**_config(checkpoint.metadata() or {}, QwenImageVAE, config))
        table = vae_key_map(model)
        expected = model.state_dict()
        disk_targets = {}
        for key in checkpoint.keys():
            target = comfy_vae_key_to_diffusers(key)
            if key not in table or target not in expected:
                raise ValueError(f"Unknown Comfy VAE key: {key}")
            if target in disk_targets:
                raise ValueError(f"Duplicate Comfy VAE target: {target}")
            shape, dtype = _shape_dtype(checkpoint, key)
            wanted = tuple(expected[target].shape)
            module = model.get_submodule(target.rpartition(".")[0])
            if target.endswith(".weight") and isinstance(module, nn.Conv2d) and len(shape) == 5 and len(wanted) == 4 and shape[2] == 1:
                shape = shape[:2] + shape[3:]
            if shape != wanted or dtype != "BF16":
                raise ValueError(f"Unsupported Comfy VAE tensor {key}: shape={shape}, dtype={dtype}; expected {wanted}, BF16")
            disk_targets[target] = key
        missing = set(expected) - set(disk_targets)
        if missing:
            raise ValueError(f"Missing Comfy VAE keys: {sorted(missing)}")
        del expected
        for target, key in disk_targets.items():
            tensor = checkpoint.get_tensor(key)
            if tensor.ndim == 5 and isinstance(model.get_submodule(target.rpartition(".")[0]), nn.Conv2d):
                tensor = tensor.squeeze(2)
            _assign(model, target, tensor, device, torch.bfloat16)
            del tensor
            if reads is not None:
                reads.tensors(key)
        return model.eval().requires_grad_(False)
