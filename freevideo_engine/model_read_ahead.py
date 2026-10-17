"""Read the video model's weight files while the text encoder works, on a slow drive.

A request that has to load the text encoder from the drive reads it first, then
the video model, with the drive idle in between: the encoder's own compute keeps
the GPU busy for tens of seconds and reads nothing. When the drive is slow that
idle time is read time lost; the model load then starts cold and waits for every
block. This reader fills the gap. It runs from the end of the encoder's device
copy to the end of its compute, reads the root and the resident block files in
the order the engine loads them, and keeps nothing but reclaimable OS cache (no private copy,
CUDA allocation or pinned memory), so the load that follows finds those pages
cached. It stops before keyframe and reference-media encoding, which read the
drive themselves.

It acts only when the drive is slow: the first megabytes (up to 64 MiB) are
timed, and a drive or cache that reads at 1.2 GB/s or faster stops there and
leaves the request otherwise as it was (a fast drive loses nothing waiting, and
copying cached pages would take a core from the encoder). The weights the
engine will pin in RAM and the host work it does beside them are subtracted
from the live RAM before deciding how much to read, so the cache never competes
with them for more than the request leaves. Only files every load reads first
(`model_files`) are read ahead; the blocks it pins, views or streams depend on the
platform and the live memory, and are left to the load.
"""
import os
from pathlib import Path
import threading
import time

from .encoder_prewarm import GiB

# One read; close() waits for the one in flight.
CHUNK = 2**20
# How long the foreground waits for the reader to stop; see ModelReadAhead.close.
CLOSE_WAIT_SECONDS = 1.

# A drive or cache reading at least this fast is not worth a second reader.
FAST_BYTES_PER_SECOND = 1.2e9
PROBE_BYTES = 64 * 2**20
# Live RAM is sampled once per this many bytes.
MEMORY_CHECK_BYTES = 256 * 2**20
SYSTEM_RESERVE = 2 * GiB


def cache_room():
    """Physical RAM that file cache may use, and whether the sample is complete.

    Commit is not part of it: cached pages of a file are not committed memory.
    """
    from .hardware import cgroup_memory
    from .system import system_memory
    memory = system_memory()
    available = memory.get('physical_available_bytes')
    if type(available) is not int or available < 0:
        available = memory['available_bytes']
    group = cgroup_memory()
    if group.get('available_bytes') is not None:
        available = min(available, group['available_bytes'])
    return available, bool(group.get('complete', True))


def probe(file, size, result, cancelled, limit=None, scratch=None, read=None):
    """Read the first bytes of an open file and record how fast they came.

    `read(view)` fills the view and returns the bytes read, or None once reading
    was cancelled. Returns the bytes read, or None when the read was cancelled or
    the file ended early."""
    scratch = bytearray(CHUNK) if scratch is None else scratch
    read = file.readinto if read is None else read
    limit = min(PROBE_BYTES if limit is None else limit, size)
    done = 0
    started = time.monotonic()
    while done < limit:
        if cancelled.is_set():
            return None
        count = read(memoryview(scratch)[:min(CHUNK, limit - done)])
        if not count:
            return None
        done += count
        result['read_bytes'] = result.get('read_bytes', 0) + count
    elapsed = max(time.monotonic() - started, 1e-6)
    result.update(probe_bytes=done, probe_bytes_per_second=done / elapsed)
    return done


def model_files(cache, *, resident_blocks=None, cancelled=None):
    """The weight files every model load reads first, in the order it reads them.

    The root, then the resident blocks in index order (all blocks when
    `resident_blocks` is None). The blocks beyond them are pinned, viewed or
    read at each step by a plan that depends on the platform and the live
    memory, and the AdaLN files are read only when their tables are missing;
    none of those are read ahead. Each step first checks `cancelled` (an Event), and a
    lookup that finds it set returns nothing."""
    import json
    import re
    def stopped():
        return cancelled is not None and cancelled.is_set()
    try:
        cache = Path(cache).resolve()
        if stopped():
            return []
        manifest = json.loads((cache / 'manifest.json').read_text(encoding='utf-8'))
        root, blocks = [], []
        for group in manifest['groups']:
            if stopped():
                return []
            path = cache / group['file']
            found = re.fullmatch(r'blocks/(\d+)\.safetensors', path.relative_to(cache).as_posix())
            if found and path.is_file():
                blocks.append((int(found.group(1)), path))
            elif group.get('group') == 'root' and path.is_file():
                root.append(path)
        blocks.sort(key=lambda row: row[0])
        if resident_blocks is not None:
            blocks = blocks[:max(0, int(resident_blocks))]
        return [str(path) for path in root] + [str(path) for _, path in blocks]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return []


def request_fields(cache, engine, *, budget_is_estimate):
    """What the encode request carries for the read-ahead; nothing when it cannot be formed.

    It touches no file: the reader's own thread resolves the model's files
    (`model_files`), so a drive that does not answer cannot hold up the request."""
    try:
        return dict(model_cache=str(cache), model_resident_blocks=int(engine.get('resident_blocks') or 0),
                    ram_budget_is_estimate=bool(budget_is_estimate),
                    model_headroom_bytes=round(float(engine.get('pin_host_gb') or 0) * 1e9 + _headroom()))
    except Exception:
        return {}


def _headroom():
    from .system import HOST_WEIGHT_HEADROOM
    return HOST_WEIGHT_HEADROOM


# Readers that did not stop in time and may still be inside a call that does not return.
_abandoned = []


def abandoned_readers():
    _abandoned[:] = [reader for reader in _abandoned if not reader.done.is_set()]
    return len(_abandoned)


class ModelReadAhead:
    def __init__(self, paths=(), *, cache=None, resident_blocks=None, headroom_bytes=0, ram_budget_bytes=None,
                 budget_is_estimate=False):
        # Either the files themselves, or the model cache whose files the reader thread looks up.
        self.paths = [Path(item) for item in paths]
        self.cache, self.resident_blocks = cache, resident_blocks
        self.headroom_bytes = max(0, int(headroom_bytes or 0))
        self.ram_budget_bytes = ram_budget_bytes
        self.budget_is_estimate = bool(budget_is_estimate)
        self.cancelled = threading.Event()
        # Taken only to check for cancellation and mark an open or read as in flight, never
        # across the call itself; `inflight` is clear while one is running.
        self.gate = threading.Lock()
        self.inflight = threading.Event()
        self.inflight.set()
        # Set by the reader thread as its last act; the one sure sign that it has ended.
        self.done = threading.Event()
        self.thread = None
        self.closed = None
        self.result = dict(state='not-started', read_bytes=0, elapsed_seconds=0.,
                           gpu_allocation_bytes=0, private_buffer_bytes=CHUNK,
                           scope='Video model root and resident block files; reclaimable OS cache, no GPU residency')

    def start(self):
        if self.thread is not None or self.closed is not None:
            return self
        if abandoned_readers():
            self.result.update(state='skipped', reason='An earlier read-ahead has not returned from the drive')
            return self
        if os.environ.get('FREEVIDEO_MODEL_READ_AHEAD', 'auto').lower() in ('off', '0', 'false'):
            self.result.update(state='skipped', reason='Disabled by FREEVIDEO_MODEL_READ_AHEAD')
            return self
        self.thread = threading.Thread(target=self._run, name='FreeVideo model read-ahead', daemon=True)
        self.thread.start()
        return self

    def _begin(self):
        with self.gate:
            if self.cancelled.is_set():
                return False
            self.inflight.clear()
            return True

    def _read(self, view, file):
        if not self._begin():
            return None
        try:
            return file.readinto(view)
        finally:
            self.inflight.set()

    def _open(self, path):
        if not self._begin():
            return None
        try:
            return path.open('rb', buffering=0)
        finally:
            self.inflight.set()

    def _allowance(self):
        available, complete = cache_room()
        allowance = max(0, available - SYSTEM_RESERVE - self.headroom_bytes)
        if self.ram_budget_bytes is not None and not self.budget_is_estimate:
            from .ram import ProcessMemory
            from .system import memory_sample
            used = memory_sample(ProcessMemory().sample(os.getpid()))
            complete = complete and type(used) is int and used >= 0
            allowance = min(allowance, max(0, int(self.ram_budget_bytes) - (used or 0) - SYSTEM_RESERVE
                                           - self.headroom_bytes))
        return allowance, complete

    def _run(self):
        started = time.monotonic()
        try:
            if self.cache is not None:
                self.paths = [Path(item) for item in model_files(
                    self.cache, resident_blocks=self.resident_blocks, cancelled=self.cancelled)]
            if self.cancelled.is_set():
                self.result.update(state='stopped', reason='Encoding finished before the files were looked up')
                return
            if not self.paths:
                self.result.update(state='skipped', reason='No model files to read ahead')
                return
            stamps = [(path, path.stat()) for path in self.paths]
            total = sum(stamp.st_size for _, stamp in stamps)
            allowance, complete = self._allowance()
            self.result.update(total_bytes=total, allowance_bytes=allowance, headroom_bytes=self.headroom_bytes)
            if not complete or total <= 0 or allowance < min(total, CHUNK):
                self.result.update(state='skipped', reason='Live RAM cannot retain the model beside its pinned weights')
                return
            target = min(total, allowance)
            self.result.update(target_bytes=target)
            scratch = bytearray(CHUNK)
            checked = 0
            for index, (path, stamp) in enumerate(stamps):
                if self.result['read_bytes'] >= target:
                    break
                file = self._open(path)
                if file is None:
                    self.result.update(state='stopped', reason='Encoding finished before the drive was measured')
                    return
                with file:
                    if index == 0:
                        done = probe(file, stamp.st_size, self.result, self.cancelled,
                                     limit=min(target, PROBE_BYTES), scratch=scratch,
                                     read=lambda view: self._read(view, file))
                        if done is None:
                            if not self.cancelled.is_set():
                                raise ValueError('Model file truncated during read-ahead')
                            self.result.update(state='stopped', reason='Encoding finished before the drive was measured')
                            return
                        if self.result['probe_bytes_per_second'] >= FAST_BYTES_PER_SECOND:
                            self.result.update(state='skipped', reason='The drive or cache reads fast; the load reads on demand')
                            return
                        self.result['slow_drive'] = True
                    remaining = min(stamp.st_size - (file.tell() if index == 0 else 0),
                                    target - self.result['read_bytes'])
                    while remaining > 0:
                        pressure = False
                        if self.result['read_bytes'] - checked >= MEMORY_CHECK_BYTES:
                            checked = self.result['read_bytes']
                            available, complete = cache_room()
                            pressure = not complete or available < SYSTEM_RESERVE + self.headroom_bytes + MEMORY_CHECK_BYTES
                        if self.cancelled.is_set() or pressure:
                            self.result.update(state='stopped', reason='Encoding finished, or live RAM pressure')
                            return
                        count = self._read(memoryview(scratch)[:min(CHUNK, remaining)], file)
                        if count is None:
                            self.result.update(state='stopped', reason='Encoding finished, or live RAM pressure')
                            return
                        if not count:
                            raise ValueError('Model file truncated during read-ahead')
                        remaining -= count
                        self.result['read_bytes'] += count
                current = path.stat()
                if (current.st_size, current.st_mtime_ns) != (stamp.st_size, stamp.st_mtime_ns):
                    raise ValueError('Model file changed during read-ahead')
            if self.result['read_bytes'] < total:
                self.result.update(state='partial', reason='Model pages within the live RAM allowance read; the load reads the rest')
            else:
                self.result.update(state='ready', reason='Model files read into the OS cache; the load reuses them')
        except Exception as error:
            # Speculative I/O never replaces the loader's own reads and validation.
            self.result.update(state='failed', reason=type(error).__name__ + ': ' + str(error))
        finally:
            self.result['elapsed_seconds'] = time.monotonic() - started
            self.done.set()

    def close(self):
        """Stop reading and return what was read; later calls return the same result.

        Cancellation is set first, so no open or read starts after it; the
        foreground then waits at most CLOSE_WAIT_SECONDS for the one that had
        started (its call is the only thing that can still be running) and for
        the thread to end. A drive that does not answer in that time leaves the
        thread inside its call: it is abandoned (`abandoned` in the result) and
        ends, closing its file, whenever the call returns. It starts nothing
        more, holds no GPU or pinned memory, and no new read-ahead starts while
        one is abandoned."""
        if self.closed is None:
            self.cancelled.set()
            abandoned = False
            if self.thread is not None:
                deadline = time.monotonic() + CLOSE_WAIT_SECONDS
                try:
                    with self.gate:
                        # Any open or read begun before this point has marked itself in flight.
                        pass
                    self.inflight.wait(max(0., deadline - time.monotonic()))
                    self.done.wait(max(0., deadline - time.monotonic()))
                finally:
                    # Also when the wait is interrupted: a reader that has not ended is registered.
                    abandoned = not self.done.is_set()
                    if abandoned and self not in _abandoned:
                        _abandoned.append(self)
            self.closed = dict(self.result, abandoned=abandoned)
        return self.closed
