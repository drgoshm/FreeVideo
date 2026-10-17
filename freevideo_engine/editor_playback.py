"""Bounded, content-addressed native audio for the local editor player."""
from contextlib import closing, contextmanager, suppress
import hashlib
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from urllib.parse import urlencode

from . import comfy_editor as editor
from . import editor_export as export

MAX_FILES = 64
MAX_BYTES = 256 * 1024 * 1024
_LOCK = threading.Lock()  # One CPU preparation; never one thread per decoder.
_KEY = re.compile(r'[0-9a-f]{64}')


def _folder(root, identity):
    if identity is None:
        return editor._editor_path(root, 'playback-audio')
    parent = editor._project_path(root, identity).parent
    return editor._safe(root, (parent / 'playback-audio').relative_to(Path(root).resolve()).as_posix())


def audio_path(root, identity, key):
    if not isinstance(key, str) or not _KEY.fullmatch(key):
        raise ValueError('Invalid playback key')
    folder = _folder(root, identity)
    return editor._safe(root, (folder / (key + '.wav')).relative_to(Path(root).resolve()).as_posix())


def _files(root):
    base = editor._editor_path(root, 'projects')
    candidates = list(base.glob('*/playback-audio/*.wav'))
    candidates += list(_folder(root, None).glob('*.wav'))
    # Temporary WAVs are not published cache entries.
    return [editor._safe(root, path.relative_to(Path(root).resolve()).as_posix())
            for path in candidates if _KEY.fullmatch(path.stem)]


def _project_exists(root, identity):
    if identity is not None and not editor._project_path(root, identity).is_file():
        raise editor.MissingProject('Playback project no longer exists')


@contextmanager
def _guard(root, cancelled=None):
    # Both threads and service processes serialize publication, eviction and leases.
    while not _LOCK.acquire(timeout=.05):
        export._check(cancelled)
    try:
        base = editor._editor_path(root, '')
        base.mkdir(parents=True, exist_ok=True)
        path = editor._editor_path(root, '.playback.lock')
        with path.open('a+b') as handle:
            if os.name == 'nt':
                import msvcrt
                if handle.tell() == 0:
                    handle.write(b'0'); handle.flush()
                def lock():
                    handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                def unlock():
                    handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                def lock():
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                def unlock():
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            while True:
                export._check(cancelled)
                try:
                    lock(); break
                except (BlockingIOError, PermissionError):
                    time.sleep(.05)
            try:
                yield
            finally:
                unlock()
    finally:
        _LOCK.release()


def _lease_path(root, path):
    return editor._safe(root, path.with_suffix('.lease').relative_to(Path(root).resolve()).as_posix())


def lease_audio(root, identity, key):
    """Pin the file for this read and subsequent Range requests, with an expiry."""
    with _guard(root):
        path = audio_path(root, identity, key)
        size = path.stat().st_size
        os.utime(path, None)
        lease = _lease_path(root, path)
        lease.touch()
        # At least two minutes, or the complete native playback plus one minute.
        expiry = time.time() + max(120, size / (export.SPLICE_RATE * 8) + 60)
        os.utime(lease, (expiry, expiry))
        return path


def _reserve(root, size=0, count=0, protected=()):
    if size > MAX_BYTES or count > MAX_FILES:
        return False
    files = sorted(_files(root), key=lambda p: (p.stat().st_mtime_ns, p.name))
    total, remaining = sum(path.stat().st_size for path in files), len(files)
    for path in files:
        if remaining + count <= MAX_FILES and total + size <= MAX_BYTES:
            return True
        lease = _lease_path(root, path)
        if path in protected or (lease.exists() and lease.stat().st_mtime > time.time()):
            continue
        try:
            length = path.stat().st_size
            path.unlink(missing_ok=True)
        except OSError:
            continue  # A platform may forbid deleting an open media file.
        total -= length; remaining -= 1
        with suppress(OSError):
            lease.unlink(missing_ok=True)
    return remaining + count <= MAX_FILES and total + size <= MAX_BYTES


def _prune(root):
    return _reserve(root)


def _reap_temporary(root):
    # The process lock proves that no participating writer is still active.
    folders = list(editor._editor_path(root, 'projects').glob('*/playback-audio'))
    folders.append(_folder(root, None))
    for folder in folders:
        for path in folder.glob('.preparing-*.wav'):
            path = editor._safe(root, path.relative_to(Path(root).resolve()).as_posix())
            with suppress(OSError):
                path.unlink(missing_ok=True)


def _audio_size(parts):
    duration = Fraction(sum(p['clip']['frames'] for p in parts), editor.FPS)
    offset = Fraction(parts[0].get('skip_output', 0), export.SAMPLE_RATE)
    count = math.ceil((duration + offset) * export.SPLICE_RATE)
    return max(count, sum(part['count'] for part in parts)) * 8 + 128


def _fingerprint(path, cancelled):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while True:
            export._check(cancelled)
            data = stream.read(1024 * 1024)
            if not data:
                return digest.hexdigest()
            digest.update(data)


def _write_audio(path, parts, lead_store, cancelled):
    """Float PCM avoids quantization; retain the export's trim phase prefix."""
    import av
    import numpy as np
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix='.preparing-', suffix='.wav', dir=path.parent, delete=False) as file:
            temporary = Path(file.name)
        # The native clock includes a small prefix for trimmed first clips.
        duration = Fraction(sum(p['clip']['frames'] for p in parts), editor.FPS)
        offset = Fraction(parts[0].get('skip_output', 0), export.SAMPLE_RATE)
        count = math.ceil((duration + offset) * export.SPLICE_RATE)
        if _audio_size(parts) > MAX_BYTES:
            raise ValueError('Playback chain exceeds cache capacity')
        with av.open(str(temporary), 'w', format='wav') as output:
            sound = output.add_stream('pcm_f32le', rate=export.SPLICE_RATE)
            sound.layout = 'stereo'
            written = 0

            def emit(data):
                nonlocal written
                export._check(cancelled)
                data = np.ascontiguousarray(data.T).reshape(1, -1)
                frame = av.AudioFrame.from_ndarray(data, format='flt', layout='stereo')
                frame.sample_rate = export.SPLICE_RATE
                for packet in sound.encode(frame):
                    output.mux(packet)
                written += frame.samples

            with closing(export._JoinedAudioReader.native_chunks(parts, cancelled, lead_store)) as chunks:
                for data in chunks:
                    # Keep the whole native tail, including any samples beyond
                    # the selected timeline, just as the export's resampler does.
                    emit(data)
            while written < count:
                emit(np.zeros((2, min(8192, count - written)), np.float32))
            for packet in sound.encode(None):
                output.mux(packet)
        export._check(cancelled)
        with temporary.open('r+b') as file:
            os.fsync(file.fileno())
        if temporary.stat().st_size > _audio_size(parts):
            raise ValueError('Playback audio exceeded its reserved size')
        temporary.replace(path)
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def prepare(root, record, cancelled=None):
    """Return each eligible track run; a failed preparation keeps legacy audio."""
    import av
    record = editor.validate(record)
    identity = record['id']
    _project_exists(root, identity)
    with _guard(root, cancelled):
        export._check(cancelled)
        _project_exists(root, identity)
        _reap_temporary(root)
        _prune(root)
        folder = _folder(root, identity)
        # Never recreate the parent of a deleted saved project.
        folder.mkdir(parents=identity is None, exist_ok=True)
        protected = set()
        resolved, fingerprints, result = {}, {}, []

        def resolve(key):
            if key not in resolved:
                path = editor.media_path(root, key)
                resolved[key] = dict(audio=path)
            return resolved[key]

        with closing(export._LeadStore(folder)) as leads, av.logging.Capture(local=True):
            for track in record['tracks'][1:]:
                try:
                    planned = export._audio_chains(track['clips'], resolve, cancelled, leads)
                except (export.ExportError, OSError, ValueError, av.FFmpegError):
                    continue
                seen = set()
                for parts in planned.values():
                    if id(parts) in seen:
                        continue
                    seen.add(id(parts))
                    start = parts[0]['clip']['start']
                    end = parts[-1]['clip']['start'] + parts[-1]['clip']['frames']
                    row = dict(track=track['id'], clips=[p['clip']['id'] for p in parts],
                               start=start, frames=end - start, duration=(end - start) / editor.FPS,
                               offset=parts[0].get('skip_output', 0) / export.SAMPLE_RATE,
                               ready=False, audio=None)
                    created = False
                    path = None
                    try:
                        digest = hashlib.sha256(b'editor-native-float-wav-v1')
                        for part in parts:
                            export._check(cancelled)
                            media = part['path']
                            if media not in fingerprints:
                                fingerprints[media] = _fingerprint(media, cancelled)
                            digest.update(fingerprints[media].encode())
                            digest.update(json.dumps(part['clip'], sort_keys=True, separators=(',', ':')).encode())
                            if 'lead_offset' in part:
                                digest.update(leads.load(part['lead_offset'])['samples'].tobytes())
                        key = digest.hexdigest()
                        path = audio_path(root, identity, key)
                        if not path.exists():
                            if not _reserve(root, _audio_size(parts), 1, protected):
                                raise ValueError('Playback cache capacity is in use')
                            _project_exists(root, identity)
                            _write_audio(path, parts, leads, cancelled)
                            created = True
                        # Content changes during planning/decoding must not publish stale audio.
                        for media in {p['path'] for p in parts}:
                            if _fingerprint(media, cancelled) != fingerprints[media]:
                                raise ValueError('Playback source changed')
                        export._check(cancelled)
                        _project_exists(root, identity)
                        os.utime(path, None)
                        protected.add(path)
                        row.update(key=key, ready=True, audio='/freevideo/editor/audio-chain?' +
                                   urlencode(dict(key=key, **({'id': identity} if identity else {}))))
                    except BaseException as error:
                        if created and path is not None:
                            with suppress(OSError):
                                path.unlink(missing_ok=True)
                        if isinstance(error, (export.ExportCancelled, KeyboardInterrupt, SystemExit)):
                            raise
                    result.append(row)
        _prune(root)
        for row in result:
            if row['ready'] and not audio_path(root, identity, row['key']).exists():
                row.update(ready=False, audio=None)
        return dict(chains=result)
