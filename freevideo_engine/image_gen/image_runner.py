"""One process's loaded image model and the per-request placement.

``QwenImageGenerator`` (qwen_pipeline) knows the model; this module decides
where the parts live. The DiT blocks and the text-encoder layers are frozen
CPU weights streamed through ``StreamedStack`` (the engine's LayerOffloader);
what stays on the card between uses comes from ``plan_image``. The vocabulary
table is looked up on the CPU, the VAE and the vision tower visit the card
only for the stage that needs them.
"""
import time
import math
from pathlib import Path

import torch

from . import image_policy
from .image_stream import StreamedStack

GiB = 2**30
_DIT_FILE = 'diffusion_models/qwen_image_2.1_turbo_int8_convrot.safetensors'
_TE_FILE = 'text_encoders/qwen3vl_8b_int8_convrot.safetensors'
_VAE_FILE = 'vae/qwen_image_2.1_vae_bf16.safetensors'
# Rounded mean stage times from the local cold/hot constructor comparison.
# See benchmarks/image_load_progress_045afix/summary.txt (small-model fallback).
_LOAD_SHARES = dict(dit=.42, vae=.03, text=.18, pin=.37)


class _LoadProgress:
    def __init__(self, callback):
        self.callback = callback
        self.fraction = self.emitted = 0.
        self.callback('load', 0.)
        self.last_time = time.monotonic()

    def report(self, stage, done, total):
        offset = 0.
        for name, share in _LOAD_SHARES.items():
            if name == stage:
                fraction = offset + share * (done / total if total else 0.)
                break
            offset += share
        self.fraction = max(self.fraction, min(fraction, math.nextafter(1., 0.)))
        now = time.monotonic()
        if self.fraction > self.emitted and now - self.last_time >= .25:
            self.callback('load', self.fraction)
            self.emitted, self.last_time = self.fraction, now

    def complete(self, stage):
        self.report(stage, 1, 1)

    def finish(self):
        self.callback('load', 1.)


def _non_block_children(module, skip):
    for name, child in module.named_children():
        if name != skip:
            yield child


class _Placement:
    """Stage hooks for QwenImageGenerator: nothing but moves between host and card."""

    def __init__(self, runner):
        self.runner = runner

    def enter(self, stage):
        runner = self.runner
        if stage == 'text':
            visual = getattr(runner.text_model.model, 'visual', None)
            if visual is not None:
                visual.to(runner.device)
        elif stage in ('encode', 'decode'):
            runner.vae.to(runner.device)

    def leave(self, stage):
        runner = self.runner
        if stage == 'text':
            visual = getattr(runner.text_model.model, 'visual', None)
            if visual is not None:
                visual.to('cpu')
            self._trim()
        elif stage in ('encode', 'decode'):
            runner.vae.to('cpu')
            self._trim()
        elif stage == 'denoise':
            self._trim()

    @staticmethod
    def _trim():
        # Stage peaks are planned on allocated bytes; give the cached blocks back so the
        # next stage's workspace is not fragmented by the previous one's.
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


class ImageRunner:
    """Load the pack once, then ``generate`` repeatedly.

    ``gpu_budget_bytes`` caps what a request may plan against (None reads the
    card: free memory plus what this process already holds). Residency and VAE
    tiling are chosen per request by ``image_policy.plan_image``.
    """

    def __init__(self, pack_dir, *, device='cuda', gpu_budget_bytes=None, log=None, progress=None):
        from .qwen_pipeline import QwenImageGenerator
        from .qwen_te_weights import load_text_encoder
        from .qwen_weights import load_dit, load_vae

        self.pack_dir = Path(pack_dir)
        self.device = torch.device(device)
        self.gpu_budget_bytes = gpu_budget_bytes
        self.log = log or (lambda *args, **kwargs: None)
        self.load_seconds = {}

        def path(directory, name):
            nested = self.pack_dir / directory / name
            return nested if nested.exists() else self.pack_dir / name

        loading = _LoadProgress(progress or (lambda stage, fraction: None))
        started = time.monotonic()
        self.transformer = load_dit(self.pack_dir / _DIT_FILE, device='cpu',
                                    progress=lambda done, total: loading.report('dit', done, total))
        self.load_seconds['dit'] = time.monotonic() - started
        loading.complete('dit')
        started = time.monotonic()
        self.vae = load_vae(self.pack_dir / _VAE_FILE, device='cpu',
                            progress=lambda done, total: loading.report('vae', done, total))
        self.load_seconds['vae'] = time.monotonic() - started
        loading.complete('vae')
        started = time.monotonic()
        config_dir = self.pack_dir / 'text_encoder'
        processor_dir = self.pack_dir / 'processor'
        text = load_text_encoder(self.pack_dir / _TE_FILE, config_path=config_dir / 'config.json', device='cpu',
                                 progress=lambda done, total: loading.report('text', done, total))
        from transformers import Qwen3VLProcessor
        processor = Qwen3VLProcessor.from_pretrained(str(processor_dir), local_files_only=True)
        self.load_seconds['text'] = time.monotonic() - started
        self.text_model = text
        loading.complete('text')

        # Parts that always sit on the card: everything of the DiT except its blocks, and the
        # encoder's final norm and rotary table. The 1.2 GB vocabulary stays on the host and is
        # looked up there (a token id list is tiny), which keeps it out of the encode peak.
        for child in _non_block_children(self.transformer, 'transformer_blocks'):
            child.to(self.device)
        for parameter in self.transformer.parameters(recurse=False):
            parameter.data = parameter.data.to(self.device)
        language = text.model.language_model
        for name, child in language.named_children():
            if name not in ('layers', 'embed_tokens'):
                child.to(self.device)
        self._embed_hooks = self._lookup_on_host(language.embed_tokens)

        started = time.monotonic()
        from ..offload import cpu_weights
        stacks = [self.transformer.transformer_blocks, language.layers]
        stack_bytes = [sum(value.numel() * value.element_size() for layer in layers
                           for _, value in cpu_weights(layer)) for layers in stacks]
        pin_total = sum(stack_bytes)

        def pin_progress(index):
            offset = sum(stack_bytes[:index])
            return lambda done, total: loading.report(
                'pin', offset + stack_bytes[index] * (done / total if total else 0.), pin_total)

        with torch.no_grad():   # LayerOffloader refuses to be built with autograd on
            self.dit_stack = StreamedStack(stacks[0], name='dit', device=self.device, progress=pin_progress(0))
            loading.report('pin', stack_bytes[0], pin_total)
            self.te_stack = StreamedStack(stacks[1], name='text_encoder', device=self.device, progress=pin_progress(1))
        self.load_seconds['pin'] = time.monotonic() - started
        loading.complete('pin')

        self.shape = image_policy.ImageShape.measure(
            list(self.transformer.transformer_blocks), list(language.layers),
            total_weight_bytes=self.dit_stack.host_bytes + self.te_stack.host_bytes)
        self.generator = QwenImageGenerator(
            None, placement=_Placement(self), device=self.device, transformer=self.transformer, vae=self.vae,
            text_encoder=text, processor=processor, sample_sigmas=None, vae_tile=True)
        from .qwen_pipeline import load_sample_sigmas
        self.generator.sample_sigmas = load_sample_sigmas(self.pack_dir / _DIT_FILE)
        self.last_plan = None
        loading.finish()

    def _lookup_on_host(self, embedding):
        device = self.device

        def to_host(module, args):
            return (args[0].to('cpu'),) + tuple(args[1:])

        def to_card(module, args, output):
            return output.to(device)

        return [embedding.register_forward_pre_hook(to_host), embedding.register_forward_hook(to_card)]

    def budget(self):
        if self.gpu_budget_bytes is not None:
            return int(self.gpu_budget_bytes)
        free, _ = torch.cuda.mem_get_info(self.device)
        return int(free + torch.cuda.memory_reserved(self.device))

    def plan(self, width, height, reference_images=None):
        width, height = width // 32 * 32, height // 32 * 32
        reference_pixels = 0
        resolution = self.generator.reference_resolution
        for image in reference_images or []:
            scale = (resolution * resolution / (image.width * image.height)) ** 0.5
            reference_pixels += round(image.width * scale) * round(image.height * scale)
        try:
            # The weights are loaded by now, so what is left to find is the request's
            # working memory. Credit what the weights already hold, locked or not, and
            # read free RAM itself: the locked-page budget (half of RAM on Windows)
            # and its headroom only limit pinning, which happened at load. Gating on
            # them refused every request on a 28 GB Windows machine, the second one
            # after a first had just been made at the same size.
            from ..system import system_memory
            memory = system_memory()
            free = memory.get('physical_available_bytes') or memory['available_bytes']
            host_available = free + self.dit_stack.host_bytes + self.te_stack.host_bytes
        except Exception:
            host_available = None
        return image_policy.plan_image(
            gpu_budget_bytes=self.budget(), host_available_bytes=host_available, width=width, height=height,
            reference_pixels=reference_pixels, shape=self.shape)

    def generate(self, prompt, *, width, height, seed=None, reference_images=None, progress=None, cancel=None):
        plan = self.plan(width, height, reference_images)
        self.last_plan = plan
        if not plan.feasible:
            raise MemoryError('; '.join(plan.reasons) or 'the image model does not fit this machine')
        self.dit_stack.set_resident(plan.dit_resident_layers)
        self.te_stack.set_resident(plan.te_resident_layers)
        self.generator.vae_tile = plan.vae_tiling
        self.generator.vae_tile_size = plan.vae_tile_size
        return self.generator.generate(prompt, width=width, height=height, seed=seed,
                                       reference_images=reference_images, progress=progress, cancel=cancel)

    def at_boundary(self):
        """False when a failure left a weights streamer inside a layer pass; the next request cannot use it."""
        return self.dit_stack.offloader.at_boundary and self.te_stack.offloader.at_boundary

    def stats(self):
        return dict(dit=self.dit_stack.stats(), text_encoder=self.te_stack.stats(),
                    plan=self.last_plan.to_dict() if self.last_plan else None, load_seconds=self.load_seconds)

    def close(self):
        for hook in self._embed_hooks:
            hook.remove()
        self.generator.close()
        self.dit_stack.close()
        self.te_stack.close()
        self.transformer = self.vae = self.text_model = None
