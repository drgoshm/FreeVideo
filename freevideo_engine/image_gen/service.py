"""One active image and one waiting job, with a worker leased until idle or video work."""
import atexit
from collections import OrderedDict
from copy import deepcopy
import json
import math
from pathlib import Path
import queue
import re
import secrets
import subprocess
import threading
import time
import uuid

from . import image_pack
from .failure import Context, ERRORS, FailureError, MESSAGE_ZH, NO_REPORT, STAGES, classify_exception

BODY_LIMIT = 64 * 1024
CANCEL_GRACE = 5.
MAX_IMAGES = 4
REPORT_LIMIT = 16
INFERENCE_TIMEOUT = 600.
QUEUE_TIMEOUT = 600.
QUEUE_POLL = .1
INVALID_ERRORS = frozenset(('invalid_request', 'invalid_prompt', 'invalid_dimensions', 'invalid_seed',
                            'invalid_media', 'too_many_images'))


class WorkerIdleExit(RuntimeError):
    """The worker closed itself after its idle timeout before it answered anything."""


FAILURE_KINDS = tuple((name, re.compile(pattern, re.I)) for name, pattern in (
    ('panic', r'\bpanick?ed\b'), ('CUDA error', r'\bcuda\b[^\n]{0,40}\berror\b|\bcublas\w*_status|\bcudnn\w*_status'),
    ('out of memory', r'out of memory|\bOOM\b'), ('MemoryError', r'\bMemoryError\b'),
    ('access violation', r'access violation|segmentation fault|\bSIGSEGV\b'),
    ('DLL load failed', r'DLL load failed|cannot open shared object'), ('ImportError', r'\b(Import|ModuleNotFound)Error\b')))


class WorkerEarlyExit(WorkerIdleExit):
    """The worker process ended with an error before it answered anything: nothing ran."""


class Worker:
    """One reader owns each pipe; stderr is drained without retaining user text."""
    def __init__(self, process):
        self.process = process
        self.rows = queue.Queue()
        self.writer_mutex = threading.Lock()
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.error_reader = threading.Thread(target=self.read_errors, daemon=True)
        self.tail = ''
        self.reader.start()
        self.error_reader.start()

    def read(self):
        try:
            for line in self.process.stdout:
                if len(line) > 128 * 1024:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    self.rows.put(row)
        finally:
            self.rows.put(None)

    def read_errors(self):
        # Only the last words are kept, for the report of a worker that ended abnormally.
        for chunk in iter(lambda: self.process.stderr.read(1024), ''):
            self.tail = (self.tail + chunk)[-1500:]

    def send(self, **row):
        with self.writer_mutex:
            self.process.stdin.write(json.dumps(row, ensure_ascii=True) + '\n')
            self.process.stdin.flush()

    def close(self):
        self.reader.join(timeout=5)
        self.error_reader.join(timeout=5)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if not stream.closed:
                stream.close()


class Service:
    def __init__(self, installation, input_directory, *, video_busy=None, idle_seconds=45.):
        self.installation = installation
        self.input_directory = input_directory
        self.video_busy = video_busy or self._video_busy
        self.idle_seconds = idle_seconds
        self.mutex = threading.RLock()
        self.stop = threading.Event()
        self.session_stop = threading.Event()
        self.requests = queue.Queue()
        self.thread = None
        self.process = None
        self.worker = None
        self.key = None
        self.state = {}
        self._busy = False
        self.closed = False
        self.yielding = False
        self.reports = OrderedDict()
        self.context = None
        self.last_machine = {}
        self.worker_restarts = 0
        self.worker_started = False
        self.queued = None
        self.queue_thread = None
        self.queue_wake = threading.Event()
        self.finished = OrderedDict()
        atexit.register(self.close)

    @staticmethod
    def _video_busy():
        from ..resident_process import OWNER
        return OWNER.active

    def busy(self):
        return self._busy

    def info(self):
        engine_root, machine = self.installation()
        spec = image_pack.catalog()
        ready = image_pack.ready(machine['model_root'])
        return dict(ready=ready, model=spec['repo'],
                    checking=not ready and image_pack.checking(machine['model_root']),
                    downloading=not ready and image_pack.downloading(machine['model_root'], engine_root),
                    bytes=sum(row['bytes'] for row in spec['groups']['image']), busy=self.busy(),
                    max_images=MAX_IMAGES)

    def request(self, value):
        if not isinstance(value, dict):
            raise ValueError('invalid_request')
        prompt = value.get('prompt')
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 2000:
            raise ValueError('invalid_prompt')
        dimensions = [value.get(name) for name in ('width', 'height')]
        if any(type(size) is not int or not 256 <= size <= 2048 for size in dimensions):
            raise ValueError('invalid_dimensions')
        seed = value.get('seed')
        if seed is not None and (type(seed) is not int or not -2**63 <= seed < 2**64):
            raise ValueError('invalid_seed')
        images = value.get('images')
        if images is None:
            images = []
        if not isinstance(images, list):
            raise ValueError('invalid_media')
        if len(images) > MAX_IMAGES:
            raise ValueError('too_many_images')
        directory = Path(self.input_directory()).resolve()
        paths = []
        for name in images:
            # A file in the input directory, or one of our own results as freevideo-image/<name>.
            folder, _, leaf = name.rpartition('/') if isinstance(name, str) else ('', '', '')
            if (not isinstance(name, str) or not leaf or leaf in ('.', '..')
                    or folder not in ('', 'freevideo-image')
                    or any(char in name for char in ('\\', ':', '\0'))):
                raise ValueError('invalid_media')
            parent = directory / folder if folder else directory
            path = parent / leaf
            if (parent.is_symlink() or path.is_symlink() or parent.resolve() != parent
                    or path.resolve().parent != parent or not path.is_file()):
                raise ValueError('invalid_media')
            paths.append(str(path))
        output = directory / 'freevideo-image'
        if output.is_symlink() or output.resolve().parent != directory:
            raise ValueError('invalid_media')
        output.mkdir(exist_ok=True)
        return dict(prompt=prompt, width=dimensions[0] // 32 * 32, height=dimensions[1] // 32 * 32,
                    steps=8, seed=seed, images=paths, path=str(output / (uuid.uuid4().hex + '.png')))

    def start(self, action, value):
        if action not in ('generate', 'compose'):
            raise ValueError('invalid_request')
        context = Context(action, value)
        with self.mutex:
            if self.closed:
                raise ValueError('closed')
            if self.queued is not None:
                raise ValueError('busy')
            if self.yielding:
                context.set_machine(self.last_machine)
                raise FailureError(self.failed('busy_video', context, video_busy=True))
            video = self.video_busy()
            waiting = (video or self.busy() or self.thread is not None
                       and self.thread.is_alive() and self.session_stop.is_set())
            raw = deepcopy(value)
            root, machine, prepared = self.prepare(action, raw, context, waiting=waiting)
            key = secrets.token_urlsafe(24)
            if waiting:
                self.queued = dict(key=key, action=action, value=raw, context=context,
                                   state=dict(phase='queued', queued_behind='video' if video else 'image'),
                                   video_since=time.monotonic() if video else None)
                if self.queue_thread is None or not self.queue_thread.is_alive():
                    self.queue_thread = threading.Thread(target=self.wait_queued,
                                                        name='freevideo-image-queue', daemon=True)
                    self.queue_thread.start()
            else:
                self.begin(key, root, machine, prepared, context)
            return dict(job=key)

    def prepare(self, action, value, context, *, waiting=False):
        """Validate at admission and again at dispatch, against the original request."""
        try:
            root, machine = self.installation()
        except (OSError, ValueError) as error:
            raise FailureError(self.failed('pack_missing', context, error=error)) from None
        context.set_machine(machine)
        self.last_machine = machine
        try:
            if not image_pack.ready(machine['model_root']):
                raise ValueError('pack_missing')
            requested = {name: value.get(name) for name in ('width', 'height')} if isinstance(value, dict) else {}
            if action == 'compose':
                from . import compose
                value = compose.build(value, self.input_directory())
            value = self.request(value)
            value['kind'] = context.kind
            context.update_request(value)
            context.plan = self.preflight(machine, value, fit=not waiting)
            # Live free memory while another job owns the GPU is advisory. Keep
            # the original request for dispatch, when the owner has released it.
            if not waiting and context.plan is not None and not context.plan['feasible']:
                raise ValueError('no_memory')
            if 'fitted' in value:
                value['fitted']['requested'] = requested
                context.update_request(value)
            return root, machine, value
        except ValueError as error:
            if str(error) in INVALID_ERRORS:
                raise
            code = str(error) if str(error) in ERRORS else classify_exception(error)
            raise FailureError(self.failed(code, context, error=error)) from None
        except Exception as error:
            raise FailureError(self.failed(classify_exception(error), context, error=error)) from None

    def remember(self, key, state):
        self.finished[key] = deepcopy(state)
        while len(self.finished) > REPORT_LIMIT:
            self.finished.popitem(last=False)

    def begin(self, key, root, machine, value, context):
        if self.key is not None:
            self.remember(self.key, self.state)
        self.key = key
        self.stop.clear()
        self.state = dict(phase='starting', stage='start')
        if 'fitted' in value:
            self.state['fitted'] = deepcopy(value['fitted'])
        self.context = context
        self._busy = True
        if self.thread is None or not self.thread.is_alive():
            self.session_stop = threading.Event()
            self.requests = queue.Queue()
            self.thread = threading.Thread(target=self.work, args=(root, machine),
                                           name='freevideo-image-gen', daemon=True)
            self.requests.put(value)
            self.thread.start()
        else:
            self.requests.put(value)

    def discard_queued(self, state):
        self.remember(self.queued['key'], state)
        self.queued = None
        self.queue_wake.set()

    def wait_queued(self):
        while True:
            with self.mutex:
                pending = self.queued
                if pending is None:
                    # Clearing under the lock lets a new admission start a reader.
                    self.queue_thread = None
                    return
                if self.closed:
                    self.discard_queued(dict(phase='cancelled'))
                    continue
                try:
                    video = self.yielding or self.video_busy()
                except Exception as error:
                    self.discard_queued(self.failed(classify_exception(error), pending['context'], error=error))
                    continue
                pending['state']['queued_behind'] = 'video' if video else 'image'
                if video:
                    if pending['video_since'] is None:
                        pending['video_since'] = time.monotonic()
                    if time.monotonic() - pending['video_since'] >= QUEUE_TIMEOUT:
                        state = self.failed('timeout', pending['context'],
                                            error=TimeoutError('video queue timeout'), video_busy=True)
                        self.discard_queued(state)
                        continue
                else:
                    pending['video_since'] = None
                    stopping = (self.thread is not None and self.thread.is_alive()
                                and self.session_stop.is_set())
                    if not self.busy() and not stopping:
                        try:
                            root, machine, value = self.prepare(pending['action'], pending['value'],
                                                                pending['context'])
                        except FailureError as error:
                            self.discard_queued(error.status)
                        except Exception as error:
                            # A previously valid reference may disappear while waiting.
                            code = ('generation_failed' if str(error) in INVALID_ERRORS
                                    else classify_exception(error))
                            self.discard_queued(self.failed(code, pending['context'], error=error))
                        else:
                            self.queued = None
                            self.begin(pending['key'], root, machine, value, pending['context'])
                        continue
                self.queue_wake.clear()
            self.queue_wake.wait(QUEUE_POLL)

    def memory_budget(self, machine):
        """Bound the driver query, without importing Torch or starting a worker."""
        if machine.get('device_backend', 'cuda') != 'cuda':
            return None
        from ..background_room import Gpu, family
        from ..background_compile import COMPILE
        from ..gpu_budget import open_bounded
        from ..resident_process import OWNER

        def read():
            gpu = Gpu(machine.get('gpu_uuid'))
            try:
                memory = gpu.memory()
                if memory is None:
                    return None
                used, total = memory
                if not total or not 0 <= used <= total:
                    return None
                # work() releases idle video/compile models; the image worker
                # reuses its own allocator. Never credit an unrelated process.
                owned = set()
                for process in (self.process, None if OWNER.active else OWNER.process, COMPILE.process):
                    if process is not None and process.poll() is None:
                        owned.update(family(process.pid))
                rows = gpu.processes() if owned else {}
                if owned and (rows is None or not owned.intersection(rows)
                              or any(rows.get(pid) is None for pid in owned if pid in rows)):
                    # An unreported owned pool must not cause a false refusal.
                    # The worker still checks live capacity before inference.
                    return total
                credit = sum(size for pid, size in rows.items() if pid in owned)
                return min(total, total - used + credit)
            finally:
                gpu.close()

        try:
            return open_bounded(read, .5)
        except (OSError, RuntimeError, AttributeError, ValueError):
            return None

    def preflight(self, machine, value, *, fit=True):
        budget = self.memory_budget(machine)
        if budget is None:
            return
        from PIL import Image
        from .image_policy import plan_image
        reference_pixels = 0
        for path in value['images']:
            with Image.open(path) as image:
                scale = (1024**2 / (image.width * image.height)) ** .5
                reference_pixels += round(image.width * scale) * round(image.height * scale)
        plan = plan_image(gpu_budget_bytes=budget, width=value['width'], height=value['height'],
                          reference_pixels=reference_pixels)
        if fit and not plan.feasible:
            width, height = value['width'], value['height']
            landscape = width >= height
            long, short = max(width, height), min(width, height)
            # At most 48 probes for the accepted 2048-pixel limit. Round the
            # proportional short side to its nearest 32-pixel grid point.
            for side in range(long - 32, 511, -32):
                other = (short * side + long * 16) // (long * 32) * 32
                if other < 512:
                    break
                fitted_width, fitted_height = (side, other) if landscape else (other, side)
                candidate = plan_image(gpu_budget_bytes=budget, width=fitted_width, height=fitted_height,
                                       reference_pixels=reference_pixels)
                if candidate.feasible:
                    value.update(width=fitted_width, height=fitted_height,
                                 fitted=dict(width=fitted_width, height=fitted_height,
                                             requested=dict(width=width, height=height)))
                    return candidate.to_dict()
        return plan.to_dict()

    def failed(self, code, context, *, row=None, error=None, video_busy=None):
        """One safe failure status, used by admission, worker, HTTP and Agent callers."""
        code = code if isinstance(code, str) and code in ERRORS else 'generation_failed'
        if code == 'cancelled':
            return dict(phase='cancelled')
        stage = row.get('stage') if isinstance(row, dict) else None
        stage = stage if stage in STAGES else context.timing.stage
        result = dict(phase='failed', error=code, report_id=None, stage=stage)
        if code in MESSAGE_ZH:
            result['message_zh'] = MESSAGE_ZH[code]
        if code not in NO_REPORT:
            if video_busy is None:
                try:
                    video_busy = self.yielding or self.video_busy()
                except Exception:
                    video_busy = False
            if context.pack is None and context.machine.get('model_root'):
                try:
                    context.pack = image_pack.status(context.machine['model_root'])
                except Exception:
                    # A missing/broken pack must not prevent reporting the original error.
                    pass
            report = context.report(code, row=row, error=error, video_busy=video_busy,
                                    worker_restarts=self.worker_restarts)
            with self.mutex:
                report_id = uuid.uuid4().hex
                self.reports[report_id] = report
                while len(self.reports) > REPORT_LIMIT:
                    self.reports.popitem(last=False)
            result['report_id'] = report_id
        return result

    def report(self, report_id):
        if (not isinstance(report_id, str) or len(report_id) != 32
                or any(char not in '0123456789abcdef' for char in report_id)):
            return None
        with self.mutex:
            report = self.reports.get(report_id)
            # Callers cannot mutate a retained report through an in-process reference.
            return json.loads(json.dumps(report, allow_nan=False)) if report is not None else None

    def status(self, key):
        with self.mutex:
            if isinstance(key, str):
                def matches(candidate):
                    return candidate is not None and secrets.compare_digest(key.encode('utf-8'), candidate.encode('utf-8'))
                if matches(self.key):
                    return deepcopy(self.state)
                if self.queued is not None and matches(self.queued['key']):
                    return deepcopy(self.queued['state'])
                for candidate, state in self.finished.items():
                    if matches(candidate):
                        return deepcopy(state)
            raise ValueError('unknown_job')

    def cancel(self, key):
        with self.mutex:
            row = self.status(key)
            if row['phase'] == 'queued':
                self.discard_queued(dict(phase='cancelled'))
                return dict(phase='cancelled')
            if key != self.key or not self.busy():
                return row
            self.stop.set()
            self.state['phase'] = 'cancelling'
            return dict(phase='cancelling')

    def stop_for_generation(self, only_idle=False):
        """Reap even an in-flight spawn before video acquires the runtime lease.

        ``only_idle`` is for other users of the lease (the prompt model): they get it
        back from an idle worker but never cut off an image that is being made.
        """
        with self.mutex:
            if only_idle and (self._busy or self.thread is None or not self.thread.is_alive()):
                return False
            self.yielding = True
            self.stop.set()
            self.session_stop.set()
            if self.queued is not None:
                self.queued['state']['queued_behind'] = 'video'
                if self.queued['video_since'] is None:
                    self.queued['video_since'] = time.monotonic()
            thread = self.thread
        try:
            if thread is not None and thread is not threading.current_thread():
                thread.join()
        finally:
            with self.mutex:
                self.yielding = False
                self.queue_wake.set()
        return True

    def close(self):
        with self.mutex:
            self.closed = True
            if self.queued is not None:
                self.discard_queued(dict(phase='cancelled'))
            queue_thread = self.queue_thread
        self.stop_for_generation()
        if queue_thread is not None and queue_thread is not threading.current_thread():
            queue_thread.join()

    def environment(self, root):
        from ..comfy_bridge import source_root
        from ..comfy_environment import isolated_environment
        from .. import processes, resident_process
        env = isolated_environment(root, source_root())
        for name in ('FREEVIDEO_RUNTIME_LOCK_FD', processes.HANDLE_ENV, resident_process.ENV,
                     'FREEVIDEO_PROMPT_PRESTART_WORKER'):
            env.pop(name, None)
        return env

    def spawn(self, root, machine, env, descriptor):
        from .. import processes
        command = processes.module_command('freevideo_engine.image_gen.worker', image_pack.base(machine['model_root']),
                                           '--device', machine.get('device_backend', 'cuda'),
                                           '--idle-seconds', self.idle_seconds)
        command[0] = machine['comfy_python']
        env = dict(env, FREEVIDEO_RUNTIME_LOCK_FD=str(descriptor))
        return processes.popen(command, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace',
                               pass_fds=(descriptor,), supervise=True, start_new_session=True)

    def work(self, root, machine):
        from ..locking import runtime_lock
        from .. import processes, resident_process
        session = None
        failure = 'worker_exit'
        failure_error = None
        try:
            from ..background_compile import COMPILE
            from ..encoder_prewarm import IDLE
            COMPILE.stop()
            if self.video_busy() or resident_process.OWNER.active:
                raise BlockingIOError('busy_video')
            resident_process.OWNER.stop_prewarm()
            IDLE.stop()
            with runtime_lock(Path(root) / 'engine.lock', inherit=False) as descriptor:
                try:
                    if self.session_stop.is_set():
                        return
                    env = self.environment(root)
                    owner = resident_process.OWNER
                    if owner.process is not None and owner.process.poll() is None and owner.endpoint:
                        env[resident_process.ENV] = str(owner.endpoint)
                        try:
                            resident_process.release_idle_cache(env, descriptor)
                            resident_process.OWNER.record(action='released', reason='image_model')
                        except resident_process.SessionRestarted:
                            pass
                        env.pop(resident_process.ENV, None)
                    if self.session_stop.is_set():
                        return
                    if self.video_busy():
                        raise BlockingIOError('busy_video')
                    def respawn():
                        nonlocal session
                        session.process.wait()
                        session.close()
                        session = Worker(self.spawn(root, machine, env, descriptor))
                        with self.mutex:
                            self.worker = session
                            self.process = session.process
                            self.worker_restarts += 1

                    session = Worker(self.spawn(root, machine, env, descriptor))
                    with self.mutex:
                        self.worker = session
                        self.process = session.process
                        self.worker_restarts += int(self.worker_started)
                        self.worker_started = True
                    while not self.session_stop.is_set():
                        try:
                            value = self.requests.get(timeout=.05)
                        except queue.Empty:
                            with self.mutex:
                                if session.process.poll() is not None and self.requests.empty():
                                    self.session_stop.set()
                                    break
                            continue
                        if session.process.poll() == 0:
                            respawn()
                        try:
                            self.infer(session, value)
                        except WorkerIdleExit as error:
                            # The request reached a worker that was already unloading after
                            # its idle timeout and was never read. Nothing ran: send it to a
                            # fresh worker once; a second exit is reported as a failure.
                            if self.stop.is_set() or self.session_stop.is_set():
                                raise
                            earlier = str(error)
                            respawn()
                            try:
                                self.infer(session, value)
                            except RuntimeError as second:
                                if earlier.startswith('worker_exit (') and str(second).startswith('worker_exit'):
                                    raise RuntimeError('%s; first try: %s' % (second, earlier[len('worker_exit '):])) from None
                                raise
                finally:
                    # Keep the parent's lease until the whole owned process tree exits.
                    if session is not None:
                        try:
                            if session.process.poll() is None:
                                session.send(action='close')
                                session.process.wait(timeout=5)
                        except (OSError, subprocess.TimeoutExpired):
                            pass
                        processes.stop(session.process, grace=5)
                        session.process.wait()
                        session.close()
        except BlockingIOError as error:
            failure = 'busy_video'
            failure_error = error
        except TimeoutError as error:
            failure = 'timeout'
            failure_error = error
        except Exception as error:
            failure = 'worker_exit'
            failure_error = error
        finally:
            with self.mutex:
                self.process = self.worker = None
                self.session_stop.set()
                if self.busy():
                    context = self.context or Context('generate', {})
                    fitted = self.state.get('fitted')
                    self.state = (dict(phase='cancelled') if self.stop.is_set()
                                  else self.failed(failure, context, error=failure_error,
                                                   video_busy=failure == 'busy_video'))
                    if fitted is not None:
                        self.state['fitted'] = fitted
                    self._busy = False
                    self.context = None
                self.queue_wake.set()

    def shrink(self, value, row, context):
        """After a memory error while working on the picture, step the size down for one more try.

        The ladder is the one start-up fitting uses (32-pixel grid, 512 pixels at the least);
        the step grows with the size so a big canvas needs few tries. False at the smallest size.
        """
        if (row.get('code') != 'no_memory' or row.get('stage') in ('load', 'text', 'encode')
                or self.stop.is_set() or self.session_stop.is_set()):
            return False
        width, height = value['width'], value['height']
        long, short = max(width, height), min(width, height)
        # A worker that had to load the model again costs seconds per try; take bigger steps then.
        side = long - max(32, long // (8 if row.get('reloaded') else 16) // 32 * 32)

        def fitted(length):
            return (short * length + long * 16) // (long * 32) * 32

        if side < 512 or fitted(side) < 512:
            # The step would jump past the least size: try that size itself before giving up.
            side = next((n for n in range(512, long, 32) if fitted(n) >= 512), None)
            if side is None:
                return False
        other = fitted(side)
        value['width'], value['height'] = (side, other) if width >= height else (other, side)
        with self.mutex:
            fitted = self.state.get('fitted') or {}
            requested = fitted.get('requested') or dict(width=width, height=height)
            self.state['fitted'] = dict(width=value['width'], height=value['height'], requested=requested)
            self.state.update(phase='progress', stage='start', fraction=0)
        context.update_request(value)
        return True

    @staticmethod
    def worker_gone(session, heard):
        """The error for a worker pipe that ended; it carries the exit code for the report."""
        try:
            code = session.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            code = None
        if code == 0 and not heard:
            return WorkerIdleExit('worker_exit (exit code 0, before any answer)')
        if code is None:
            return RuntimeError('worker_exit')
        session.error_reader.join(timeout=1)
        # What the worker wrote can hold a prompt or a user's folder: only a fixed word for
        # the kind of failure leaves this function, never any of the text.
        kind = next((name for name, pattern in FAILURE_KINDS if pattern.search(session.tail)), '')
        session.tail = ''
        message = 'worker_exit (exit code %d%s)' % (code, ', ' + kind if kind else '')
        # A worker that ended with an error before answering never ran the request; one
        # fresh worker gets it, and a second exit is reported with its own last words.
        return (WorkerEarlyExit if not heard and code > 0 else RuntimeError)(message)

    def infer(self, session, value):
        context = self.context or Context('generate', value)
        heard = False
        try:
            session.send(action='generate', **value)
        except OSError:
            raise self.worker_gone(session, heard)
        cancelled_at = None
        deadline = time.monotonic() + INFERENCE_TIMEOUT
        while True:
            if self.stop.is_set() and cancelled_at is None:
                session.send(action='cancel')
                cancelled_at = time.monotonic()
            if cancelled_at is not None and time.monotonic() - cancelled_at > CANCEL_GRACE:
                raise TimeoutError('cancelled')
            if self.session_stop.is_set():
                return
            if time.monotonic() > deadline:
                raise TimeoutError('timeout')
            try:
                row = session.rows.get(timeout=.05)
            except queue.Empty:
                continue
            if row is None:
                raise self.worker_gone(session, heard)
            heard = True
            phase = row.get('phase')
            if phase == 'progress':
                stage, fraction = row.get('stage'), row.get('fraction')
                if (stage in STAGES and type(fraction) in (int, float)
                        and math.isfinite(fraction) and 0 <= fraction <= 1):
                    # The limit is for a worker that stops reporting, not for a slow first load.
                    deadline = time.monotonic() + INFERENCE_TIMEOUT
                    with self.mutex:
                        if self.state.get('stage') == stage and type(self.state.get('fraction')) in (int, float):
                            fraction = max(fraction, self.state['fraction'])
                        context.timing.enter(stage)
                        self.state.update(phase='cancelling' if self.stop.is_set() else 'progress',
                                          stage=stage, fraction=fraction)
            elif phase in ('result', 'error'):
                if self.stop.is_set() or row.get('code') == 'cancelled':
                    result = dict(phase='cancelled')
                elif phase == 'error' and self.shrink(value, row, context):
                    # The memory estimate was optimistic for this machine: same worker, next size down.
                    session.send(action='generate', **value)
                    deadline = time.monotonic() + INFERENCE_TIMEOUT
                    continue
                elif phase == 'error':
                    code = row.get('code')
                    result = self.failed(code, context, row=row)
                else:
                    path = Path(value['path'])
                    if row.get('path') != str(path) or path.is_symlink() or not path.is_file():
                        raise RuntimeError('worker_exit')
                    from PIL import Image
                    with Image.open(path) as image:
                        if image.format != 'PNG' or image.size != (value['width'], value['height']):
                            raise RuntimeError('worker_exit')
                        image.verify()
                    seconds = row.get('seconds')
                    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
                        raise RuntimeError('worker_exit')
                    result = dict(phase='complete', file='freevideo-image/' + path.name,
                                  seconds=seconds, plan=row.get('plan'))
                with self.mutex:
                    if 'fitted' in self.state:
                        result['fitted'] = self.state['fitted']
                    self.state = result
                    self._busy = False
                    self.context = None
                    self.queue_wake.set()
                return


def stop_prestart_for_generation(server):
    service = getattr(server, '_freevideo_image_gen', None)
    if service is not None:
        service.stop_for_generation()


def get_service(server=None):
    """The image service of this server, for callers in the same process (the Agent).

    They call ``start('generate', {...})``, ``status(job)``, ``cancel(job)`` and
    ``info()`` directly with the same arguments, results and error codes as the
    HTTP routes; only the page token is not needed. None before ``register()``.
    """
    if server is None:
        from server import PromptServer
        server = PromptServer.instance
    return getattr(server, '_freevideo_image_gen', None)


def release_idle_worker(server):
    """Hand engine.lock back from a worker that is only waiting for its idle exit."""
    service = getattr(server, '_freevideo_image_gen', None)
    if service is not None:
        service.stop_for_generation(only_idle=True)


def register():
    from aiohttp import web
    from server import PromptServer
    import folder_paths
    from ..comfy_bridge import installation
    server = PromptServer.instance
    if server is None or getattr(server, '_freevideo_image_gen', None) is not None:
        return

    def video_busy():
        running, pending = server.prompt_queue.get_current_queue()
        return bool(running or pending) or Service._video_busy()

    service = Service(installation, folder_paths.get_input_directory, video_busy=video_busy)
    server._freevideo_image_gen = service
    token = secrets.token_urlsafe(24)

    @server.routes.get('/freevideo/image-gen')
    async def info(request):
        try:
            return web.json_response(dict(service.info(), token=token))
        except (OSError, ValueError):
            return web.json_response(dict(error='setup_required'), status=400)

    @server.routes.get('/freevideo/image-gen/report/{report_id}')
    async def report(request):
        value = service.report(request.match_info['report_id'])
        headers = {'Cache-Control': 'no-store'}
        if value is None:
            return web.json_response(dict(error='unknown_report'), status=404, headers=headers)
        from datetime import datetime, timezone
        stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
        headers['Content-Disposition'] = 'attachment; filename="FreeVideo-report-%s.json"' % stamp
        return web.json_response(value, headers=headers)

    @server.routes.post('/freevideo/image-gen/{action}')
    async def action(request):
        header = request.headers.get('X-FreeVideo-Image', '')
        if not secrets.compare_digest(header.encode('utf-8'), token.encode('utf-8')):
            return web.json_response(dict(error='reload'), status=403)
        try:
            if request.content_length and request.content_length > BODY_LIMIT:
                raise ValueError('invalid_request')
            body = bytearray()
            while True:
                chunk = await request.content.read(min(8192, BODY_LIMIT + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > BODY_LIMIT:
                    raise ValueError('invalid_request')
            value = json.loads(body)
            if not isinstance(value, dict):
                raise ValueError('invalid_request')
            name = request.match_info['action']
            if name == 'cast-views':
                from .cast_views import page_views
                result = page_views(value.get('description'))
            elif name == 'status':
                result = service.status(value.get('job'))
            elif name == 'cancel':
                result = service.cancel(value.get('job'))
            else:
                result = service.start(name, value)
            return web.json_response(result)
        except FailureError as error:
            return web.json_response(error.status, status=400)
        except (ValueError, OSError) as error:
            code = str(error)
            if code in ('unknown_job', 'closed'):
                result = dict(error=code)
            else:
                code = code if code in ERRORS else 'invalid_request'
                result = dict(phase='failed', error=code, report_id=None, stage='start')
                if code in MESSAGE_ZH:
                    result['message_zh'] = MESSAGE_ZH[code]
            return web.json_response(result, status=400)
