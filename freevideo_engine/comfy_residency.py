"""Cooperate with ComfyUI's native memory manager without changing its models."""
from contextlib import contextmanager
from functools import wraps
import threading

from .resident_process import OWNER

_local = threading.local()


@contextmanager
def keep_engine_cache():
    old = getattr(_local, 'keep', False)
    _local.keep = True
    try:
        yield
    finally:
        _local.keep = old


def install(memory):
    if getattr(memory.free_memory, '_freevideo_resident_cooperation', False) is True:
        return
    original = memory.free_memory

    @wraps(original)
    def free_memory(memory_required, device, *args, **kwargs):
        # FreeVideo's own handoff releases native Comfy models only. Other
        # nodes, and Comfy's explicit unload command, can reclaim our idle
        # process before choosing a lower-memory native loading strategy.
        if (not getattr(_local, 'keep', False) and not OWNER.active and
                OWNER.process is not None and OWNER.process.poll() is None):
            if device is None or memory_required > memory.get_free_memory(device):
                OWNER.release('comfy_model')
        return original(memory_required, device, *args, **kwargs)

    free_memory._freevideo_resident_cooperation = True
    memory.free_memory = free_memory
