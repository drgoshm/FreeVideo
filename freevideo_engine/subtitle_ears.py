"""Subtitle "ears": the timeline's sound -> timed text (alignment or recognition).

FreeVideo only listens here. Proofreading, translation, segmentation and
layout are the brain's job (an Agent, or FreeVideo AI), which then writes the
subtitle track with ordinary editor commands.

* align:      the lines are known; return when each is spoken. A line that
              cannot be found comes back `matched: false` with no time.
* transcribe: the lines are unknown; return what is said, with times.

Times are timeline frames (24 fps), given with seconds for reading. The sound
analysed is the saved timeline's own mix (cuts, moves, volumes and mutes
included), so frames map straight back onto the project. A model engine is
pluggable: any object with `align` and/or `transcribe` registered below.
"""
import copy
import logging
import re
import shutil
import threading
import time
import uuid
from collections import deque
from contextlib import nullcontext
from pathlib import Path

from . import editor_export as export
from . import editor_export_options

FPS = 24
RATE = 16000
MAX_SECONDS = 2 * 3600          # longest sound analysed in one job
MAX_LINES = 1000
MAX_LINE_CHARS = 2000
MAX_JOBS = 200
_JOB_ID = re.compile(r's-[0-9a-f]{32}')
_LANGUAGE = re.compile(r'auto|[a-z]{2,3}(-[A-Za-z0-9]{2,8})?')
_REASONS = ('no_engine', 'no_sound', 'language', 'too_long', 'source_missing', 'unreadable', 'disk_full', 'failed')


class EarsError(Exception):
    """A public reason code; engines and decoders never leak their own messages."""
    def __init__(self, reason='failed'):
        self.reason = reason if reason in _REASONS else 'failed'
        super().__init__(self.reason)


class Cancelled(InterruptedError):
    pass


_ENGINES = {}


def register_engine(name, factory, *, first=False):
    """`factory()` returns an engine, or None when its model is not installed.

    An engine offers `align(audio, lines, language, progress, cancelled)` and/or
    `transcribe(audio, language, progress, cancelled)` and returns seconds on
    the 16 kHz mono float32 `audio` it was given.
    """
    _ENGINES[name] = (factory, first)


def unregister_engine(name):
    _ENGINES.pop(name, None)


def engine_for(kind, name=None):
    order = sorted(_ENGINES, key=lambda n: (not _ENGINES[n][1], n))
    for candidate in ([name] if name else order):
        if candidate not in _ENGINES:
            continue
        engine = _ENGINES[candidate][0]()
        if engine is not None and callable(getattr(engine, kind, None)):
            return engine
    raise EarsError('no_engine')


def available():
    """Which jobs can run now: {'align': bool, 'transcribe': bool}."""
    result = {}
    for kind in ('align', 'transcribe'):
        try:
            engine_for(kind)
            result[kind] = True
        except EarsError:
            result[kind] = False
    return result


def _whole(value, minimum=0):
    return type(value) is int and minimum <= value <= 2 ** 53 - 1


def validate_request(body):
    """Wire body -> clean request dict. ValueError on anything unexpected."""
    if not isinstance(body, dict) or set(body) - {'kind', 'id', 'revision', 'source', 'lines', 'language', 'range'}:
        raise ValueError('Invalid request')
    kind = body.get('kind')
    if kind not in ('align', 'transcribe'):
        raise ValueError('Invalid kind')
    language = body.get('language', 'auto')
    if not isinstance(language, str) or not _LANGUAGE.fullmatch(language):
        raise ValueError('Invalid language')
    request = dict(kind=kind, language=language)
    if ('id' in body) == ('source' in body):
        raise ValueError('Give a project or a source')
    if 'id' in body:
        if not isinstance(body['id'], str) or not export._PROJECT_ID.fullmatch(body['id']):
            raise ValueError('Invalid project')
        if 'revision' in body and not _whole(body['revision']):
            raise ValueError('Invalid revision')
        request.update(id=body['id'], revision=body.get('revision'))
    else:
        source = body['source']
        if (not isinstance(source, dict) or set(source) != {'work'} or not isinstance(source['work'], str)
                or not export._IMPORT_ID.fullmatch(source['work'])):
            raise ValueError('Invalid source')
        request['source'] = dict(source)
    if 'range' in body:
        span = body['range']
        if (not isinstance(span, dict) or set(span) != {'start', 'frames'}
                or not _whole(span['start']) or not _whole(span['frames'], 1)):
            raise ValueError('Invalid range')
        request['range'] = dict(start=span['start'], frames=span['frames'])
    lines = body.get('lines')
    if kind == 'align':
        if (not isinstance(lines, list) or not 1 <= len(lines) <= MAX_LINES
                or any(not isinstance(t, str) or len(t) > MAX_LINE_CHARS or re.search(r'[\x00-\x08\x0b-\x1f\x7f]', t)
                       for t in lines) or not any(t.strip() for t in lines)):
            raise ValueError('Invalid lines')
        request['lines'] = list(lines)
    elif lines is not None:
        raise ValueError('Lines belong to alignment only')
    return request


# ---- sound ---------------------------------------------------------------

def decode_mono(path, *, cancelled=None):
    """Any audio/video file -> float32 mono at 16 kHz (the whole sound)."""
    import av
    import numpy as np
    pieces, total = [], 0
    limit = MAX_SECONDS * RATE
    try:
        with av.open(str(path)) as container:
            stream = next((s for s in container.streams if s.type == 'audio'), None)
            if stream is None:
                raise EarsError('no_sound')
            resampler = av.AudioResampler(format='fltp', layout='mono', rate=RATE)

            def take(frames):
                nonlocal total
                for converted in frames:
                    data = converted.to_ndarray().reshape(-1)
                    pieces.append(data)
                    total += data.size

            for frame in container.decode(stream):
                if cancelled and cancelled():
                    raise Cancelled('cancelled')
                take(resampler.resample(frame))
                if total > limit:
                    raise EarsError('too_long')
            take(resampler.resample(None) or [])
    except (EarsError, Cancelled):
        raise
    except FileNotFoundError:
        raise EarsError('source_missing') from None
    except Exception:
        raise EarsError('unreadable') from None
    if not total:
        raise EarsError('no_sound')
    return np.concatenate(pieces).astype('float32', copy=False)


def _silent(audio):
    import numpy as np
    return audio.size == 0 or float(np.max(np.abs(audio))) < 1e-4


# ---- results -------------------------------------------------------------

def _frame(seconds, offset, bound):
    return max(offset, min(offset + bound, offset + int(seconds * FPS + .5)))


def _timed(item, offset, bound):
    """One engine span {start,end,...} in seconds -> frames plus seconds, or None."""
    try:
        start, end = float(item['start']), float(item['end'])
    except (KeyError, TypeError, ValueError):
        return None
    if not (0 <= start < end) or start * FPS >= bound:
        return None
    first = _frame(start, offset, bound)
    last = max(first + 1, _frame(end, offset, bound))
    return dict(start=first, end=last, start_seconds=round(offset / FPS + start, 3),
                end_seconds=round(offset / FPS + min(end, bound / FPS), 3))


def _words(items, offset, bound):
    result, end = [], offset
    for item in items or ():
        text = item.get('text') if isinstance(item, dict) else None
        timed = _timed(item, offset, bound) if text else None
        if timed is None or timed['start'] < end:
            continue
        end = timed['end']
        result.append(dict(text=str(text), **timed))
    return result


def shape_align(lines, found, offset, bound):
    """Engine output -> one entry per given line, text untouched, order kept."""
    by_index = {}
    for item in found or ():
        if isinstance(item, dict) and _whole(item.get('index')) and item['index'] < len(lines):
            by_index.setdefault(item['index'], item)
    result, end = [], offset
    for index, text in enumerate(lines):
        entry = dict(index=index, text=text, matched=False)
        item = by_index.get(index)
        timed = _timed(item, offset, bound) if item and item.get('matched', True) and text.strip() else None
        if timed is not None and timed['start'] >= end:
            end = timed['end']
            entry.update(matched=True, words=_words(item.get('words'), offset, bound), **timed)
        result.append(entry)
    return result


def shape_transcript(found, offset, bound):
    result, end = [], offset
    for item in found or ():
        text = str(item.get('text', '')).strip() if isinstance(item, dict) else ''
        timed = _timed(item, offset, bound) if text else None
        if timed is None or timed['start'] < end:
            continue
        end = timed['end']
        result.append(dict(index=len(result), text=text, matched=True,
                           words=_words(item.get('words'), offset, bound), **timed))
    return result


# ---- the job -------------------------------------------------------------

def _mix(project, resolver, root, request, progress, cancelled):
    """Render the saved timeline's sound range to a scratch wav and decode it."""
    width, height, frames, _ = export._layout(project)
    span = request.get('range') or dict(start=0, frames=frames)
    options = dict(only='sound', sound_format='wav', range=span)
    settings = editor_export_options.validate(options, width, height, frames)
    scratch = Path(root) / 'FreeVideo' / '.editor' / 'ears' / uuid.uuid4().hex
    target = scratch / 'mix.wav'
    try:
        export._checked_target(target, root)
        try:
            export.render(project, resolver, target, progress=progress, cancelled=cancelled, options=settings)
        except export.ExportCancelled:
            raise Cancelled('cancelled') from None
        except export.ExportError as error:
            raise EarsError(error.reason) from None
        return decode_mono(target, cancelled=cancelled), span['start'], span['frames']
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def run_job(request, *, root, loader=None, resolver=None, mixer=None, engine=None,
            progress=None, cancelled=None, gate=None):
    """Validated request -> result dict. Raises EarsError / Cancelled.

    `gate()` returns a context manager held while the engine runs, so a model
    waits its turn behind video generation (the integration supplies the lock).
    """
    progress = progress or (lambda fraction: None)

    def check():
        if cancelled and cancelled():
            raise Cancelled('cancelled')

    kind = request['kind']
    engine = engine or engine_for(kind)
    resolver = resolver or export.resolve_source
    if 'source' in request:
        key = 'work:' + request['source']['work']
        try:
            files = resolver(key)
        except FileNotFoundError:
            raise EarsError('source_missing') from None
        except ValueError:
            raise EarsError('source_missing') from None
        path = files.get('audio') or files.get('video')
        if path is None:
            raise EarsError('no_sound')
        audio = decode_mono(path, cancelled=cancelled)
        offset, bound = 0, int(audio.size * FPS / RATE + .5)
        if 'range' in request:
            span = request['range']
            first = int(span['start'] / FPS * RATE)
            audio = audio[first:first + int(span['frames'] / FPS * RATE)]
            offset, bound = span['start'], min(span['frames'], int(audio.size * FPS / RATE + .5))
    else:
        loader = loader or export.load_project
        project = copy.deepcopy(loader(request['id'], request.get('revision')))
        if mixer is not None:
            audio, offset, bound = mixer(project, resolver, request, lambda f: progress(f * .3), cancelled)
        else:
            audio, offset, bound = _mix(project, resolver, root, request, lambda f: progress(f * .3), cancelled)
    progress(.3)
    check()
    if _silent(audio):
        raise EarsError('no_sound')
    if audio.size > MAX_SECONDS * RATE:
        raise EarsError('too_long')
    started = time.monotonic()
    with (gate() if gate else nullcontext()):
        check()
        report = lambda fraction: progress(.3 + .69 * max(0., min(1., float(fraction))))
        try:
            if kind == 'align':
                found = engine.align(audio, list(request['lines']), request['language'], report, cancelled)
                language = request['language']
                lines = shape_align(request['lines'], found, offset, bound)
            else:
                found = engine.transcribe(audio, request['language'], report, cancelled)
                language = request['language']
                if isinstance(found, dict):
                    language = found.get('language') or language
                    found = found.get('lines')
                lines = shape_transcript(found, offset, bound)
        except (EarsError, Cancelled):
            raise
        except MemoryError:
            raise EarsError('failed') from None
        except Exception:
            logging.getLogger(__name__).exception('Subtitle ears (%s) failed', kind)
            raise EarsError('failed') from None
    check()
    return dict(kind=kind, language=language, fps=FPS, range=dict(start=offset, frames=bound),
                lines=lines, matched=sum(1 for l in lines if l['matched']), total=len(lines),
                engine=str(getattr(engine, 'name', 'engine')), seconds=round(time.monotonic() - started, 2))


class EarsQueue:
    """One worker, FIFO, cancellable; the same shape as the export queue."""
    def __init__(self, root, *, runner=None, **kwargs):
        self.root = Path(root).absolute()
        self.runner = runner or run_job
        self.kwargs = kwargs
        self.lock = threading.RLock()
        self.jobs = {}
        self.pending = deque()
        self.worker = None

    @staticmethod
    def _view(job):
        return {key: job[key] for key in ('id', 'kind', 'state', 'fraction', 'reason', 'result')}

    def submit(self, body):
        request = validate_request(body)
        job = dict(id='s-' + uuid.uuid4().hex, kind=request['kind'], state='running', fraction=0.,
                   reason=None, result=None, request=request, stop=threading.Event())
        with self.lock:
            if len(self.jobs) >= MAX_JOBS:
                for old in list(self.jobs):
                    if self.jobs[old]['state'] != 'running':
                        del self.jobs[old]
                        if len(self.jobs) < MAX_JOBS:
                            break
            if len(self.jobs) >= MAX_JOBS:
                raise EarsError('failed')
            self.jobs[job['id']] = job
            self.pending.append(job['id'])
            initial = self._view(job)
            if self.worker is None:
                self.worker = threading.Thread(target=self._run, name='freevideo-subtitle-ears', daemon=True)
                self.worker.start()
            return initial

    def get(self, identity):
        if not isinstance(identity, str) or not _JOB_ID.fullmatch(identity):
            raise ValueError('Invalid job')
        with self.lock:
            if identity not in self.jobs:
                raise KeyError(identity)
            return self._view(self.jobs[identity])

    def cancel(self, identity):
        with self.lock:
            self.get(identity)
            job = self.jobs[identity]
            if job['state'] == 'running':
                job['stop'].set()
                if identity in self.pending:
                    self.pending.remove(identity)
                    job['state'] = 'cancelled'
            return self._view(job)

    def _run(self):
        while True:
            with self.lock:
                if not self.pending:
                    self.worker = None
                    return
                job = self.jobs[self.pending.popleft()]
                if job['stop'].is_set():
                    job['state'] = 'cancelled'
                    continue

            def progress(fraction):
                with self.lock:
                    job['fraction'] = max(job['fraction'], min(.99, float(fraction)))

            try:
                result = self.runner(job['request'], root=self.root, progress=progress,
                                     cancelled=job['stop'].is_set, **self.kwargs)
                with self.lock:
                    if job['stop'].is_set():
                        job.update(state='cancelled')
                    else:
                        job.update(state='done', fraction=1., result=result)
            except Exception as error:
                with self.lock:
                    if isinstance(error, Cancelled) or job['stop'].is_set():
                        job.update(state='cancelled', reason=None)
                    elif isinstance(error, export.ExportConflict):
                        job.update(state='failed', reason='failed')
                    else:
                        job.update(state='failed', reason=export._reason(error) if not isinstance(error, EarsError)
                                   else error.reason)


def register():
    """Register the ears endpoints once on the existing ComfyUI server."""
    from . import subtitle_sherpa  # noqa: F401  (registers the speech-model engine)
    import asyncio
    from aiohttp import web
    from server import PromptServer
    server = PromptServer.instance
    if server is None or getattr(server, '_freevideo_subtitle_ears', False):
        return
    queue = EarsQueue(export._output_root())
    server._freevideo_subtitle_ears = queue

    def response(value):
        return web.json_response({'job': value}, headers={'Cache-Control': 'no-store'})

    @server.routes.post('/freevideo/editor/ears')
    async def start(request):
        try:
            body = await request.json()
            return response(await asyncio.to_thread(queue.submit, body))
        except export.ExportConflict as error:
            return web.json_response({'revision': error.revision}, status=409, headers={'Cache-Control': 'no-store'})
        except (ValueError, TypeError, KeyError):
            return web.json_response({'reason': 'failed'}, status=400)
        except EarsError as error:
            return web.json_response({'reason': error.reason}, status=503)

    @server.routes.get('/freevideo/editor/ears')
    async def status(request):
        identity = request.query.get('id')
        if identity is None:
            return web.json_response(dict(available=available()), headers={'Cache-Control': 'no-store'})
        try:
            return response(queue.get(identity))
        except KeyError:
            return web.json_response({'reason': 'failed'}, status=404)
        except ValueError:
            return web.json_response({'reason': 'failed'}, status=400)

    @server.routes.post('/freevideo/editor/ears/cancel')
    async def cancel(request):
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {'id'}:
                raise ValueError('Invalid request')
            return response(queue.cancel(body['id']))
        except KeyError:
            return web.json_response({'reason': 'failed'}, status=404)
        except (ValueError, TypeError):
            return web.json_response({'reason': 'failed'}, status=400)

    from . import subtitle_pack
    installer = subtitle_pack.Installer()
    server._freevideo_subtitle_pack = installer

    def pack_state():
        return dict(subtitle_pack.status(), install=installer.state())

    @server.routes.get('/freevideo/editor/subtitle-pack')
    async def pack_status(request):
        value = await asyncio.to_thread(pack_state)
        return web.json_response(value, headers={'Cache-Control': 'no-store'})

    @server.routes.post('/freevideo/editor/subtitle-pack')
    async def pack_install(request):
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) - {'action', 'language'}:
                raise ValueError('Invalid request')
            action = body.get('action')
            if action == 'cancel':
                installer.cancel()
            elif action == 'install':
                value = await asyncio.to_thread(subtitle_pack.status)
                if not value['supported']:
                    return web.json_response({'reason': 'unsupported'}, status=409)
                if not value['published']:
                    return web.json_response({'reason': 'not_published'}, status=409)
                groups = ('runtime', 'speech', 'language') if body.get('language') is True else ('runtime', 'speech')
                if body.get('language') not in (None, True, False):
                    raise ValueError('Invalid request')
                installer.start(groups)
            else:
                raise ValueError('Invalid request')
        except (ValueError, TypeError):
            return web.json_response({'reason': 'failed'}, status=400)
        return web.json_response(await asyncio.to_thread(pack_state), headers={'Cache-Control': 'no-store'})
