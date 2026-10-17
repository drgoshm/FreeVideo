"""React inside a sampling step when Windows cuts this process's video-memory budget.

Windows moves a process's video memory to system memory while the process
stays over the budget the driver gives it, and every layer then waits for that
memory to come back. The budget was only read between steps, so a cut that
arrived inside a step slowed the whole step several times over: one step took
ten times as long as its neighbours.

Between layers this reads the budget and how long the layer just finished
took. When the pass is over its budget and held up, it first releases what the
pass does not need (idle models), then halves the attention head group for the
layers that follow. Between steps the planned group returns, once per pass,
when every read of a whole step left room for what the pass held with it.

While the budget covers the pass nothing changes: the hooks read two counters
and a clock, no tensor is written and the installed forwards stay as they
were, so outputs are bit-identical. A pass that was narrowed computes its
remaining layers with the narrower group, which rounds differently: the same
seed then gives a slightly different video, as it does on a card whose plan
takes the narrower group from the start.
"""
from contextlib import ExitStack, contextmanager
import gc
import json
import time

MiB, GiB = 1 << 20, 1 << 30
# Readings within this of the budget are not over it: the driver's counters
# and the allocator's pool move by a few MiB between two reads.
OVER_BYTES = 32 * MiB
# A layer is held up when it took this many times as long as the same layer of
# the previous step, and at least this much longer. Layers read from a slow
# drive are slow in every step and never count.
SLOW_FACTOR, SLOW_SECONDS = 3., 5.
# The first step of a pass has no earlier step to compare with. There a layer
# counts when the pass is over its budget by this much and the layer took at
# least SLOW_SECONDS: a layer of a fitting pass takes a fraction of that.
CUT_BYTES = GiB
# Time spent in held-up layers before anything is done, and again before the
# next measure: a budget that dips for a few seconds comes back by itself.
SUSTAIN_SECONDS = 15.
# The planned group returns when the budget stayed this far above what the
# pass used with it, at every read of one whole step.
RESTORE_BYTES = 256 * MiB
# Narrower groups than this were measured several times slower with the
# attention outputs in host memory, and save little.
MIN_HEAD = 4


def emit_event(event):
    print(json.dumps(event), flush=True)


class LayerWatch:
    """One pass's reaction to a budget cut. `source` supplies the readings:

    local()   -> {'budget_bytes', 'usage_bytes'} for this process, or None
    release() -> bytes freed by dropping idle models (may be 0)
    limit()   -> the allocator's current ceiling in bytes, or None
    """
    def __init__(self, engine, source, *, clock=time.monotonic, emit=emit_event, pass_cache=None):
        self.engine, self.source, self.clock, self.emit = engine, source, clock, emit
        self.pass_cache = pass_cache   # the pass's cache record, shared with the sampler
        self.planned = engine.config.get('head_chunk')
        self.stack = None          # open while a narrower group is installed
        self.previous = {}         # layer -> seconds it took in the last completed step
        self.current = {}          # layer -> seconds in the running step
        self.entered = None        # (layer, time) of the layer being dispatched
        self.pressed = 0.          # seconds of held-up layers since the last measure
        self.released = False
        self.exhausted = False
        self.wide_usage = 0        # most this process held with the planned group
        self.wide_peak = 0         # allocator peaks with the planned group, kept across the reset
        self.wide_cache = 0        # largest cache the pass held before its first narrowing
        self.boundary = None       # (lowest budget read, a reading failed) of the step just ended
        self.low_budget = None     # lowest budget read in the running step
        self.unread_step = False   # a reading of the running step failed
        self.pass_allocated = 0    # allocator peaks of the whole pass, kept across every reset
        self.pass_reserved = 0
        self.reset_workspace = False
        self.restored = False      # the planned group came back once in this pass
        self.step = 1
        self.receipt = dict(planned_head_chunk=self.planned, polls=0, unread=0, over_polls=0,
                            held_up_layers=0, actions=[])

    def read(self):
        try:
            sample = self.source.local()
        except (OSError, RuntimeError, AttributeError, ValueError):
            sample = None
        if (not isinstance(sample, dict) or type(sample.get('budget_bytes')) is not int
                or type(sample.get('usage_bytes')) is not int or sample['budget_bytes'] <= 0):
            self.receipt['unread'] += 1
            return None
        self.receipt['polls'] += 1
        return sample

    def enter(self, index):
        """Before layer `index` (1-based) is dispatched."""
        now = self.clock()
        took = None
        if self.entered is not None and self.entered[0] == index - 1:
            took = now - self.entered[1]
            self.current[index - 1] = took
        self.entered = (index, now)
        sample = self.read()
        if sample is None:
            self.pressed = 0.
            self.unread_step = True
            return
        budget, usage = sample['budget_bytes'], sample['usage_bytes']
        self.low_budget = budget if self.low_budget is None else min(self.low_budget, budget)
        if self.stack is None:
            self.wide_usage = max(self.wide_usage, usage)
        over = usage - budget
        if over >= OVER_BYTES:
            self.receipt['over_polls'] += 1
        if over < OVER_BYTES or took is None:
            self.pressed = 0.
            return
        before = self.previous.get(index - 1)
        if before is not None:
            held_up = took >= max(SLOW_FACTOR * before, before + SLOW_SECONDS)
        else:
            held_up = over >= CUT_BYTES and took >= SLOW_SECONDS
        if not held_up:
            self.pressed = 0.
            return
        self.receipt['held_up_layers'] += 1
        self.pressed += took
        if self.pressed >= SUSTAIN_SECONDS:
            self.pressed = 0.
            self.act(index, sample, took)

    def record(self, action, index, sample, **fields):
        row = dict(action=action, step=self.step, layer=index,
                   budget_bytes=sample['budget_bytes'] if sample else None,
                   usage_bytes=sample['usage_bytes'] if sample else None, **fields)
        if len(self.receipt['actions']) < 32:
            self.receipt['actions'].append(row)
        self.emit(dict(event='step_pressure', **row))

    def act(self, index, sample, took):
        if not self.released:
            self.released = True
            try:
                freed = int(self.source.release() or 0)
            except (OSError, RuntimeError, AttributeError, ValueError):
                freed = 0
            if freed > 0:
                self.record('release_idle', index, sample, freed_bytes=freed, layer_seconds=took)
                return
        self.narrow(index, sample, took)

    def narrow(self, index, sample, took):
        config = self.engine.config
        head = config.get('head_chunk')
        if (not config.get('inference_kernels') or type(head) is not int or head // 2 < MIN_HEAD):
            if not self.exhausted:
                self.exhausted = True
                self.record('nothing_left', index, sample, head_chunk=head, layer_seconds=took)
            return
        from .pass_configuration import configuration
        from . import head_chunk
        backend = self.engine.device_backend
        reserved, allocated = backend.max_memory_reserved(), backend.max_memory_allocated()
        if self.stack is None:
            self.stack = ExitStack()
            self.wide_peak = max(self.wide_peak, reserved)
            # The cache of later steps is not part of that peak: remember what
            # the peak held, so the planned group's own workspace is not read
            # as the peak minus a cache that grew beside the narrower group.
            held = (self.pass_cache or {}).get('peak_cache_bytes', 0)
            self.wide_cache = max(self.wide_cache, min(held, reserved))
        # Every reset below forgets what the group before it reached.
        self.pass_reserved = max(self.pass_reserved, reserved)
        self.pass_allocated = max(self.pass_allocated, allocated)
        self.stack.enter_context(configuration(self.engine, dict(head_chunk=head // 2)))
        # The narrower group's first call compiles: collect after each of its
        # groups again, or the frames that compile leaves behind stay beside
        # the next group exactly when this pass must hold less.
        head_chunk.COLLECTED_LENGTHS.clear()
        gc.collect()
        backend.empty_cache()
        # The next step boundary asks for the budget to cover the pass as it now runs.
        backend.reset_peak_memory_stats()
        self.reset_workspace = True
        # Times of the wider group no longer say what a layer should take.
        self.previous, self.current = {}, {}
        self.record('narrow', index, sample, head_chunk_from=head, head_chunk_to=head // 2,
                    layer_seconds=took)

    def restore(self, sample=None):
        stack, self.stack = self.stack, None
        if stack is not None:
            self.restored = True
            stack.close()
            self.previous, self.current = {}, {}
            self.record('restore', 0, sample, head_chunk_to=self.engine.config.get('head_chunk'))

    def step_done(self, pass_cache):
        """At a step boundary, before the budget refresh reads the pass's workspace."""
        self.previous, self.current, self.entered, self.pressed = self.current, {}, None, 0.
        self.step += 1
        self.pass_cache = pass_cache
        if self.reset_workspace:
            # Measured again from the allocator's peak since the group narrowed.
            self.reset_workspace = False
            pass_cache['workspace_peak_bytes'] = 0
        low, self.low_budget = self.low_budget, None
        # A step with a failed reading did not show room at every read.
        unread, self.unread_step = self.unread_step, False
        self.boundary = (low, unread)

    def restore_if_room(self, limit):
        """After the boundary's budget refresh: the planned group returns when the
        step's readings and the refreshed ceiling `limit` both leave room for it.

        Returns the planned group's workspace in bytes when it came back, else
        None. The caller then holds the pass cache to what that workspace leaves.
        """
        boundary, self.boundary = self.boundary, None
        if boundary is None:
            return None
        low, unread = boundary
        # Once per pass: a budget that comes back and is cut again would
        # otherwise switch groups at every boundary, recompiling each time.
        if self.stack is None or low is None or unread or self.restored:
            return None
        if low < self.wide_usage + RESTORE_BYTES:
            return None
        # The ceiling the refresh just set: a cut after the last layer's read
        # is already in it.
        if type(limit) is not int:
            try:
                limit = self.source.limit()
            except (OSError, RuntimeError, AttributeError, ValueError):
                limit = None
        if type(limit) is not int or limit < self.wide_peak:
            return None
        self.restore(dict(budget_bytes=low, usage_bytes=self.wide_usage))
        return max(0, self.wide_peak - self.wide_cache)

    @contextmanager
    def layers(self, blocks):
        handles = []
        try:
            for index, block in enumerate(blocks, 1):
                def entered(module, inputs, index=index):
                    self.enter(index)
                handles.append(block.register_forward_pre_hook(entered))
            yield self
        finally:
            for handle in handles:
                handle.remove()
            stack, self.stack = self.stack, None
            self.receipt['narrowed_at_end'] = stack is not None
            self.receipt['head_chunk_at_end'] = self.engine.config.get('head_chunk')
            if stack is not None:
                stack.close()

    def peaks(self, allocated, reserved):
        """The pass's allocator peaks, including the part before a narrowing reset them."""
        return max(allocated, self.pass_allocated), max(reserved, self.pass_reserved)

    def result(self):
        return dict(self.receipt, wide_peak_reserved_bytes=self.wide_peak or None)
