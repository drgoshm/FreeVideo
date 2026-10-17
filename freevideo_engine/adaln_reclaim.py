"""Reclaim the 26 GB of optional AdaLN projection sources that tables made unused.

Before 0.4.0 a slim installation that ran a sampling schedule without a
published table fetched the 50 original projection files (adaln/00..49) and
computed its own tables from them, on the step grid of that version. A newer
engine uses a different grid on some platforms, so those tables are never read
again and the sources only occupy disk. They can be fetched again on demand
(restore_projections), so removing them is recoverable. Everything below must
hold, otherwise nothing is deleted:

1. the installation is a slim export. In a full installation the files are part
   of the model itself;
2. the 50 files are exactly the ones the manifest lists: same names, sizes and
   SHA-256, regular, unshared, reached without links or junctions;
3. at least one complete table of this model, computed on the old step grid,
   is in the cache (its timesteps are the old grid and not the engine grid);
4. no LoRA variant that changes modulation sits beside the cache. Those hardlink
   or copy the projection groups. An unreadable variant counts as one.

Nothing is removed on the engine's own initiative. The background precompile
only checks the conditions and leaves a small offer file; the launcher asks the
user, and the removal runs only when the user agrees. A "later" answer is
recorded against the release line (0.4, 0.5, ...) and the offer comes back with
the next one.

The old-grid tables go after the sources, under the same conditions, one file
at a time and only when the folder holds nothing but that table's files. A
removal that was interrupted resumes where it stopped. Hashing 26 GB is read
only and may be interrupted at any file; it resumes from a receipt of the files
already verified. The removal itself runs under the GPU-work lock, never during
a generation. Nothing here fails a request.
"""
import json
import os
from pathlib import Path
import re
import stat
import time

from . import adaln_assets as assets
from .long_paths import io_path
from .variant_cleanup import SIDECAR, SIDECAR_LIMIT, inside, redirected

RECEIPT = 'adaln-reclaim.json'
OFFER = 'adaln-reclaim-offer.json'
SOURCES = 'adaln'
COUNT = 50
SOURCE_NAME = re.compile(r'adaln/(\d{2})\.safetensors')
VARIANT = re.compile(r'vdn-fp8-lora-[0-9a-f]{24}')
TABLE_FOLDER = re.compile(r'adaln-tables-v2-[0-9a-f]{24}')
TABLE_FILE = re.compile(r'identity\.json|producer\.json|(\d{2})\.(safetensors|json)')
IDENTITY_LIMIT = 65536
BLOCK = 8 * 1024 * 1024


class Stopped(Exception):
    """The caller asked to give way; nothing was deleted."""


def _json(path, limit=IDENTITY_LIMIT):
    path = io_path(path)
    info = path.lstat()
    if redirected(info) or not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise ValueError('Not a small regular file')
    return json.loads(path.read_text(encoding='utf-8'))


def _regular(path):
    try:
        info = io_path(path).lstat()
    except OSError:
        return None
    return None if redirected(info) or not stat.S_ISREG(info.st_mode) else info


def _contained(cache, path):
    """True when path, fully resolved, lies strictly inside this installation's own resolved cache."""
    try:
        root, real = (os.path.realpath(io_path(item)) for item in (cache, path))
        return real != root and os.path.commonpath([root, real]) == root
    except (OSError, ValueError):
        return False


def engine_schedules(steps):
    """(task, old grid rows, engine grid rows) for every task of one step count."""
    from .adaln import schedule_timesteps
    from .media_request import TASKS
    rows = []
    for task in TASKS:
        old, new = (schedule_timesteps(steps, device='cpu', task=task, upstream_grid=grid) for grid in (True, False))
        rows.append((task, [t.float().tolist() for t in old], [t.float().tolist() for t in new]))
    return rows


def _recording(schedules, consulted):
    """`schedules` that also keeps what it answered, for the offer: the launcher has no torch to ask again."""
    def answer(steps):
        rows = schedules(steps)
        consulted[str(steps)] = [[task, old, new] for task, old, new in rows]
        return rows
    return answer


def _offered_schedules(cache):
    """The grids recorded with the standing offer when they were computed by this very engine release, else None."""
    value = offer(cache)
    release, stored = value.get('engine_release'), value.get('schedules')
    if not isinstance(release, str) or release != installed_release() or not isinstance(stored, dict):
        return None
    def answer(steps):
        rows = stored.get(str(steps))
        if not isinstance(rows, list) or not rows:
            raise KeyError(steps)
        return [(task, old, new) for task, old, new in rows]
    return answer


def old_grid_tables(cache, manifest, schedules=None):
    """Folders of this model's tables that sit on the old step grid, as (folder, complete)."""
    schedules = schedules or engine_schedules
    weights = assets.weight_identity(manifest)
    count = len(assets.projection_groups(manifest))
    found, memo = [], {}
    for folder in sorted(io_path(cache).glob('adaln-tables-v2-*')):
        if not TABLE_FOLDER.fullmatch(folder.name) or not inside(cache, folder) or not folder.is_dir():
            continue
        try:
            value = _json(folder / 'identity.json')
            steps = len(value['timesteps'])
            if value.get('weights') != weights or assets.directory(value) != folder.name:
                continue
            if steps not in memo:
                memo[steps] = schedules(steps)
            if any(value['timesteps'] == new for _, _, new in memo[steps]):
                continue  # A table the engine reads: not ours to remove.
            if not any(value['timesteps'] == old != new for _, old, new in memo[steps]):
                continue
            complete = all(_regular(folder / ('%02d.safetensors' % i)) is not None
                           and _regular(folder / ('%02d.json' % i)) is not None for i in range(count))
        except (OSError, ValueError, KeyError, TypeError):
            continue
        found.append((folder, complete))
    return found


def modulation_variants(cache):
    """True when a LoRA variant beside the cache may hold or share the projection groups."""
    parent = io_path(cache).parent
    try:
        entries = list(parent.iterdir())
    except OSError:
        return True
    for entry in entries:
        if not VARIANT.fullmatch(entry.name):
            continue
        try:
            if (entry / SOURCES).exists() or (entry / SOURCES).is_symlink():
                return True
            manifest = _json(entry / 'manifest.json', 64 * 1024 * 1024)
            if any(str(row.get('group', '')).startswith('adaln/') for row in manifest.get('groups', [])):
                return True
        except (OSError, ValueError, TypeError, AttributeError):
            return True  # Cannot tell: keep.
    return False


def release_line(version):
    """'0.4' for '0.4.0' (None when the version is not one): a decline holds for this line."""
    match = re.match(r'(\d{1,4})\.(\d{1,4})(?:\.|$)', version) if isinstance(version, str) else None
    return '%s.%s' % match.groups() if match else None


def installed_release():
    """The product version this engine was released as, or None."""
    try:
        value = _json(Path(__file__).with_name('release_notes.json'), 64 * 1024 * 1024)
    except (OSError, ValueError):
        return None
    return value.get('product_version') if isinstance(value, dict) else None


def offer(cache):
    """The standing offer {'bytes', 'manifest', 'declined'} or {}; small regular file only."""
    try:
        value = _json(Path(cache) / OFFER)
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_offer(cache, **fields):
    from .monitoring import save
    save(Path(cache) / OFFER, dict(format=1, **fields))


def decline(cache, version):
    """Record "later" for this release line; the offer stays visible in the file, not in the dialog."""
    current = {key: value for key, value in offer(cache).items() if key != 'format'}
    current.update(bytes=current.get('bytes', 0), manifest=current.get('manifest'), declined=release_line(version))
    _write_offer(cache, **current)


def _tables_size(folders):
    total = 0
    for folder, _ in folders:
        try:
            total += sum(entry.lstat().st_size for entry in io_path(folder).iterdir() if _regular(entry) is not None)
        except OSError:
            continue
    return total


def _source_paths(cache, manifest):
    rows = assets.projection_groups(manifest)
    names = sorted(row.get('file') for row in rows)
    if names != ['adaln/%02d.safetensors' % i for i in range(COUNT)]:
        return None
    return [(row, io_path(cache) / row['file']) for row in sorted(rows, key=lambda r: r['file'])]


def _hash(path, should_stop):
    import hashlib
    digest = hashlib.sha256()
    with io_path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(BLOCK), b''):
            if should_stop():
                raise Stopped()
            digest.update(block)
    return digest.hexdigest()


def _receipt(cache):
    try:
        value = _json(Path(cache) / RECEIPT)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(cache, value):
    from .monitoring import save
    save(Path(cache) / RECEIPT, value)


def verify_sources(cache, manifest, should_stop=lambda: False):
    """None when the 50 files are exactly the manifest's, otherwise the reason."""
    from .storage import fingerprint
    listed = _source_paths(cache, manifest)
    if listed is None:
        return 'unexpected_manifest'
    receipt, stamps = _receipt(cache), {}
    previous = receipt.get('verified', {}) if receipt.get('manifest') == assets.weight_identity(manifest) else {}
    present = [path for _, path in listed if _regular(path) is not None]
    if not present:
        return 'absent'
    if len(present) != len(listed) and not receipt.get('deleting'):
        return 'partial'
    if not inside(cache, io_path(cache) / SOURCES) or not _contained(cache, io_path(cache) / SOURCES):
        return 'redirected'
    if any(not _contained(cache, path) for _, path in listed if _regular(path) is not None):
        return 'redirected'
    for row, path in listed:
        info = _regular(path)
        if info is None:
            continue  # Removed by an interrupted run of this cleanup.
        if info.st_size != row['bytes'] or info.st_nlink != 1:
            return 'changed' if info.st_size != row['bytes'] else 'shared'
        stamps[row['file']] = dict(fingerprint(path), sha256=row['sha256'])
    verified = {}
    for row, path in listed:
        if _regular(path) is None:
            continue
        stamp = stamps[row['file']]
        saved = previous.get(row['file'])
        if saved is not None and {k: v for k, v in saved.items()} == stamp:
            verified[row['file']] = saved
            continue
        if should_stop():
            raise Stopped()
        if _hash(path, should_stop) != row['sha256']:
            return 'changed'
        verified[row['file']] = stamp
        _save(cache, dict(manifest=assets.weight_identity(manifest), verified=verified,
                          deleting=bool(receipt.get('deleting'))))
    return None


def _remove_sidecars(cache, listed):
    """The small records an earlier download left beside a source (<file>.partial.source.json), so the folder can go."""
    for _, path in listed:
        sidecar = path.with_name(path.name + SIDECAR)
        info = _regular(sidecar)
        if info is not None and info.st_size <= SIDECAR_LIMIT and _contained(cache, sidecar):
            try:
                io_path(sidecar).unlink()
            except OSError:
                pass


def _remove_tables(cache, folders):
    removed = 0
    for folder, _ in folders:
        if not _contained(cache, folder):
            continue
        files = [entry for entry in io_path(folder).iterdir()]
        if any(_regular(entry) is None or not TABLE_FILE.fullmatch(entry.name) or not _contained(cache, entry)
               for entry in files):
            continue  # Anything else in it keeps the whole folder.
        size = 0
        for entry in sorted(files, key=lambda p: p.suffix == '.json'):  # Receipts last.
            size += entry.lstat().st_size
            entry.unlink()
        folder.rmdir()
        removed += size
    return removed


def reclaim(cache, *, should_stop=lambda: False, schedules=None, lock=None, ask=False, release=None):
    """Remove the sources and the old-grid tables when every condition holds.

    Returns {action: 'deleted'|'offered'|'kept', reason, bytes, tables_bytes}. `lock`
    is a context-manager factory held while deleting (the GPU-work lock). With
    `ask` nothing is deleted: the conditions are checked, the offer file is
    written and the result is 'offered'. `release` is the installed version,
    which a recorded "later" is compared against.
    """
    cache = Path(cache)
    kept = lambda reason: dict(action='kept', reason=reason, bytes=0, tables_bytes=0)
    try:
        manifest = _json(cache / 'manifest.json', 64 * 1024 * 1024)
    except (OSError, ValueError):
        return kept('no_manifest')
    if manifest.get('format') not in assets.SLIM_FORMATS:
        return kept('full_install')  # The files are part of the model.
    if ask and release_line(release) is not None and offer(cache).get('declined') == release_line(release):
        return kept('declined')
    try:
        listed = _source_paths(cache, manifest)
        if listed is None:
            return kept('unexpected_manifest')
        if modulation_variants(cache):
            return kept('modulation_lora')
        consulted = {}
        tables = old_grid_tables(cache, manifest, _recording(schedules or engine_schedules, consulted))
        if not any(complete for _, complete in tables):
            return kept('no_old_grid_table')
        reason = verify_sources(cache, manifest, should_stop)
    except Stopped:
        return kept('stopped')
    if reason not in (None, 'absent'):
        if ask:
            _forget_offer(cache)
        return kept(reason)
    if ask:
        present = sum(row['bytes'] for row, path in listed if _regular(path) is not None)
        tables_bytes = _tables_size(tables)
        _write_offer(cache, bytes=present, tables_bytes=tables_bytes, manifest=assets.weight_identity(manifest),
                     declined=offer(cache).get('declined'), engine_release=release, schedules=consulted)
        return dict(action='offered', reason='ask', bytes=present, tables_bytes=tables_bytes)
    freed = 0
    try:
        with (lock() if lock is not None else _nothing()):
            # Nothing may have changed while hashing: decide again under the lock.
            try:
                if should_stop():
                    return kept('stopped')
                if modulation_variants(cache):
                    return kept('modulation_lora')
                if reason is None:
                    again = verify_sources(cache, manifest, should_stop)
                    if again not in (None, 'absent'):
                        return kept(again)
            except Stopped:
                return kept('stopped')
            if reason is None:
                _save(cache, dict(_receipt(cache), manifest=assets.weight_identity(manifest), deleting=True))
                for row, path in listed:
                    info = _regular(path)
                    if info is None:
                        continue
                    if info.st_size != row['bytes'] or info.st_nlink != 1:
                        return kept('changed')
                    if not _contained(cache, path):
                        return kept('redirected')
                    freed += info.st_size
                    io_path(path).unlink()
                _remove_sidecars(cache, listed)
                try:
                    (io_path(cache) / SOURCES).rmdir()
                except OSError:
                    pass  # Other content stays.
            tables_bytes = _remove_tables(cache, old_grid_tables(cache, manifest, schedules))
            io_path(Path(cache) / RECEIPT).unlink(missing_ok=True)
            _forget_offer(cache)
    except BlockingIOError:
        return kept('busy')
    return dict(action='deleted', reason='reclaimed', bytes=freed, tables_bytes=tables_bytes)


def _forget_offer(cache):
    try:
        io_path(Path(cache) / OFFER).unlink(missing_ok=True)
    except OSError:
        pass


class _nothing:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def apply(machine, *, should_stop=lambda: False, schedules=None):
    """The user agreed: check everything again under the installation's GPU-work lock and delete. Never raises.

    The launcher has no torch, so which grids are old comes from the offer the engine wrote; an offer
    from another engine release, or without them, is dropped and made again by the next background check.
    """
    from .locking import runtime_lock
    cache = machine['cache']
    path = Path(machine['root']) / 'engine.lock' if machine.get('root') else None
    schedules = schedules or _offered_schedules(cache)
    if schedules is None:
        _forget_offer(cache)
        return dict(action='kept', reason='stale_offer', bytes=0, tables_bytes=0)
    try:
        result = reclaim(cache, should_stop=should_stop, schedules=schedules,
                         lock=lambda: runtime_lock(path, inherit=False))
    except Exception as error:
        return dict(action='kept', reason='error:' + type(error).__name__, bytes=0, tables_bytes=0)
    if result['action'] != 'deleted' and result['reason'] not in ('busy', 'stopped'):
        _forget_offer(cache)  # The conditions no longer hold: there is nothing to offer.
    return result


def _installed_engine_schedules(machine):
    """engine_schedules for a process that has not put the engine's dependencies on its path (the background check)."""
    ready = []
    def answer(steps):
        if not ready:
            from . import paths
            vdn = Path(machine['root']) / 'vendor' / 'vdn' if machine.get('root') else None
            paths.add_vdn(vdn if vdn is not None and (vdn / 'src/models/hybrid_attention.py').is_file() else None)
            ready.append(True)
        return engine_schedules(steps)
    return answer


def run(machine, *, should_stop=lambda: False):
    """Entry point of the background precompile: check and leave the offer, report one event, never raise."""
    started = time.time()
    try:
        result = reclaim(machine['cache'], should_stop=should_stop, ask=True, release=installed_release(),
                         schedules=_installed_engine_schedules(machine))
    except Exception as error:  # Best effort: the installation works either way.
        result = dict(action='kept', reason='error:' + type(error).__name__, bytes=0, tables_bytes=0)
    if result['action'] == 'kept' and result['reason'] not in ('declined', 'stopped', 'busy') \
            and not result['reason'].startswith('error:'):
        _forget_offer(machine['cache'])  # An offer is only standing while every condition holds.
    result['seconds'] = round(time.time() - started, 1)
    return result
