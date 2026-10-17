"""Step-skipping caches for MiniMax H3 sampling, ported from the ComfyUI community.

Both approximate an earlier step of the same pass (one forward per step, no CFG).
Only explicit step_cache configuration or FREEVIDEO_STEP_CACHE=<kind>[:<threshold>]
enables them. Numerical operations follow the FreeVideo H200 experiment snapshot
fb7786481cd10f0908c646667ff0e41e23b81dcfbaae656cb9b0622e73d579fd.

fbcache: MiniMax H3 FirstBlockCache by duckyshell (MIT), Fast preset, upstream
f7a27128e73e1859f2295e64698164203799029d. Compare the video rows' first-block
residual with the last full step (relative mean absolute difference, row/channel
stride 8, threshold 0.10, window 10-95%, at most two consecutive hits). Audio
is sampled for diagnostics only. A hit adds the last full step's cached tail.

easycache-av: ComfyUI EasyCache, reimplemented from the paper/node behaviour
(ComfyUI #15326; original snapshot SHA-256
746fd299918efff7f2abe919fb8a0c20abd384be1e28a0f122c0d321a69ce174).
The last full step's rate k = |d out| / |d in| predicts k |d in| / |out|.
The H3 AV adaptation accumulates the larger audio/video prediction; below 0.2
in the 15-95% window, reuse each modality's output-minus-input difference.
As in the H200 snapshot, this method has no consecutive-hit cap.

FirstBlockCache attribution and MIT permission notice:
MIT License

Copyright (c) 2026 duckyshell

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
import os

DEFAULTS = {'easycache-av': 0.2, 'fbcache': 0.10}
WINDOWS = {'easycache-av': (0.15, 0.95), 'fbcache': (0.10, 0.95)}
BLOCK_LEVEL = ('fbcache',)
STRIDE = 8


def requested(config=None):
    """Resolve (kind, threshold, source); a nonempty experiment overrides config."""
    value = os.environ.get('FREEVIDEO_STEP_CACHE', '').strip()
    if value:
        kind, _, threshold = value.partition(':')
        if kind not in DEFAULTS:
            raise ValueError('Unknown FREEVIDEO_STEP_CACHE kind %r; use one of %s' % (kind, ', '.join(DEFAULTS)))
        return kind, float(threshold) if threshold else DEFAULTS[kind], 'environment'
    if config is None:
        return None
    if config not in DEFAULTS:
        raise ValueError('Unknown step_cache %r; use one of %s' % (config, ', '.join(DEFAULTS)))
    return config, DEFAULTS[config], 'config'


def _relative(current, previous):
    return float((current - previous).abs().mean() / previous.abs().mean().clamp_min(1e-6))


class StepCache:
    def __init__(self, kind, threshold, steps, *, source=None, offloader=None, disabled_reason=None):
        self.kind, self.threshold, self.steps = kind, threshold, steps
        self.source, self.offloader = source, offloader
        self.disabled_reason = disabled_reason
        self.block_level = kind in BLOCK_LEVEL
        self.step = -1
        self.hits, self.decisions = [], []
        self.consecutive = 0
        # block level
        self.previous = None   # (audio, video) block-0 residual samples of the last full step
        self.tail = None       # packed: last block output - block 0 output, last full step
        self.indices = None
        # model level
        self.prev_input = None
        self.prev_diff = None
        self.prev_out = None
        self.rate = None
        self.accumulated = 0.

    # ---- shared ------------------------------------------------------------------------------------
    def in_window(self):
        start, end = WINDOWS.get(self.kind, (0., 1.))
        progress = self.step / self.steps
        return start <= progress < end

    def begin(self, video_indices, audio_indices):
        """Called at the start of every transformer forward (one per step)."""
        self.step += 1
        self.indices = (video_indices, audio_indices)

    def stats(self):
        full = self.steps - len(self.hits)
        return dict(kind=self.kind, threshold=self.threshold, steps=self.steps, cached_steps=list(self.hits),
                    full_forwards=full, decisions=self.decisions, source=self.source,
                    disabled_reason=self.disabled_reason)

    # ---- block level: fbcache -----------------------------------------------------------------
    def _samples(self, residual):
        video, audio = self.indices
        rows = residual[0]
        return (rows.index_select(0, audio[::STRIDE])[:, ::STRIDE].float(),
                rows.index_select(0, video[::STRIDE])[:, ::STRIDE].float())

    def before_first_block(self, value):
        self.before = self._samples(value)

    def after_first_block(self, after):
        """Whether to skip the remaining blocks; on a hit `after` becomes the cached output."""
        audio, video = (now - then for now, then in zip(self._samples(after), self.before))
        self.before = None
        hit, scores = False, None
        if self.previous is not None and self.tail is not None and self.in_window() and self.consecutive < 2:
            scores = (_relative(audio, self.previous[0]), _relative(video, self.previous[1]))
            decisive = scores[1]
            hit = decisive <= self.threshold
        self.decisions.append(dict(step=self.step, audio=scores and round(scores[0], 5),
                                   video=scores and round(scores[1], 5), hit=hit))
        if hit:
            self.consecutive += 1
            self.hits.append(self.step)
            after.add_(self.tail)
            return True
        self.consecutive = 0
        self.previous = (audio, video)
        self.anchor = after.clone()
        return False

    def after_last_block(self, output):
        self.tail = output - self.anchor
        self.anchor = None

    # ---- model level: easycache-av --------------------------------------------------------
    def skip_forward(self, video_in, audio_in, sigmas):
        """Cached (video, audio) velocities for this step, or None to run the model."""
        return self._easycache(video_in, audio_in)

    def record_forward(self, video_in, audio_in, sigmas, video_out, audio_out):
        stride = (slice(None), slice(None, None, STRIDE))
        current_in = (video_in[stride].float(), audio_in[stride].float())
        current_out = (video_out[stride].float(), audio_out[stride].float())
        if self.prev_input is not None and self.prev_out is not None:
            rates = []
            for i in range(2):
                change_in = (current_in[i] - self.prev_input[i]).abs().mean()
                change_out = (current_out[i] - self.prev_out[i]).abs().mean()
                rates.append(float(change_out / change_in.clamp_min(1e-8)))
            self.rate = rates
        self.prev_input, self.prev_out = current_in, current_out
        self.prev_norm = [float(o.abs().mean()) for o in current_out]
        self.prev_diff = (video_out.float() - video_in.float(), audio_out.float() - audio_in.float())

    def _easycache(self, video_in, audio_in):
        reuse = False
        if self.rate is not None and self.prev_diff is not None and self.in_window():
            stride = (slice(None), slice(None, None, STRIDE))
            current = (video_in[stride].float(), audio_in[stride].float())
            predicted = [self.rate[i] * float((current[i] - self.prev_input[i]).abs().mean()) / max(self.prev_norm[i], 1e-8)
                         for i in range(2)]
            self.accumulated += max(predicted)
            reuse = self.accumulated < self.threshold
            if not reuse:
                self.accumulated = 0.
        self.decisions.append(dict(step=self.step, accumulated=round(self.accumulated, 5), hit=reuse))
        if not reuse:
            return None
        self.hits.append(self.step)
        return (video_in.float() + self.prev_diff[0], audio_in.float() + self.prev_diff[1])


def begin_pass(transformer, steps, config=None, *, offloader=None, disabled_reason=None):
    """Attach fresh state for this sampling pass; refinement never calls this."""
    wanted = requested(config)
    transformer._freevideo_step_cache = (StepCache(wanted[0], wanted[1], steps, source=wanted[2],
        offloader=offloader, disabled_reason=disabled_reason) if wanted else None)
    return transformer._freevideo_step_cache


def end_pass(transformer):
    cache = getattr(transformer, '_freevideo_step_cache', None)
    transformer._freevideo_step_cache = None
    if cache is None:
        return None
    cache.offloader = None
    return cache.stats()
