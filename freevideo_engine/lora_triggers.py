"""User-entered LoRA triggers, identified by content rather than library location.

All functions here perform synchronous file I/O. HTTP callers use to_thread;
Agent graph construction and chain creation already run on worker threads.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading

from .comfy_assets import resolve_loras
from .launcher_settings import Store
from .long_paths import io_path
from .model_repair import valid_name
from .paths import data_root

MAX_WORDS = 300
CHUNK_BYTES = 1024 * 1024
TRIGGERS_FILE = 'lora-triggers.json'
CACHE_FILE = 'lora-fingerprints.json'
_SHA256 = re.compile(r'[0-9a-f]{64}')
_LOCK = threading.RLock()


def settings_directory():
    # Alongside durable launcher preferences, outside model and runtime folders.
    return Store(data_root()).primary.parent


def _read(path):
    try:
        def invalid_number(value):
            raise ValueError('Invalid JSON number')
        value = json.loads(io_path(path).read_text(encoding='utf-8'), parse_constant=invalid_number)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path, value):
    path = io_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _valid_name(name):
    return (valid_name(name) and '..' not in name
            and not any(c in name for c in (':', '\x00', '\r', '\n'))
            and name.lower().endswith('.safetensors'))


def resolve_name(name, folders=None):
    if not _valid_name(name):
        raise ValueError('Select an existing LoRA filename without path separators or ..')
    if folders is None:
        import folder_paths as folders
    # Reuse the stack's library membership and safetensors validation.
    rows = resolve_loras(json.dumps([dict(name=name)]), folders.get_filename_list('loras'),
                         lambda n: folders.get_full_path_or_raise('loras', n))
    path = io_path(rows[0]['path'])
    if not path.is_file():
        raise ValueError('Select an existing LoRA file')
    return path


def _hash_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint(path, directory=None):
    """Full SHA-256; persistent cache identity is resolved path, size and mtime_ns."""
    path = io_path(Path(path).resolve())
    directory = settings_directory() if directory is None else Path(directory)
    with _LOCK:
        info = path.stat()
        cache = _read(directory / CACHE_FILE).get('files', {})
        cache = cache if isinstance(cache, dict) else {}
        key = str(path)
        row = cache.get(key)
        if (isinstance(row, dict) and type(row.get('size')) is int
                and type(row.get('mtime_ns')) is int
                and (row['size'], row['mtime_ns']) == (info.st_size, info.st_mtime_ns)
                and isinstance(row.get('sha256'), str) and _SHA256.fullmatch(row['sha256'])):
            return row['sha256']
        digest = _hash_file(path)
        after = path.stat()
        if (info.st_size, info.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError('LoRA changed while reading; try again')
        cache[key] = dict(size=info.st_size, mtime_ns=info.st_mtime_ns, sha256=digest)
        _write(directory / CACHE_FILE, dict(version=1, files=cache))
        return digest


def _entries(directory):
    rows = _read(directory / TRIGGERS_FILE).get('triggers', {})
    if not isinstance(rows, dict):
        return {}
    return {key: dict(words=row['words'].strip(), source='user', name=row.get('name', ''))
            for key, row in rows.items()
            if isinstance(key, str) and _SHA256.fullmatch(key) and isinstance(row, dict)
            and isinstance(row.get('words'), str) and 0 < len(row['words'].strip()) <= MAX_WORDS
            and row.get('source') == 'user' and isinstance(row.get('name', ''), str)}


def get_trigger(name, *, folders=None):
    with _LOCK:
        path = resolve_name(name, folders)
        directory = settings_directory()
        digest = fingerprint(path, directory)
        rows = _entries(directory)
        row = rows.get(digest)
        if row is not None and row['name'] != name:
            row['name'] = name
            _write(directory / TRIGGERS_FILE, dict(version=1, triggers=rows))
        return dict(name=name, words=row['words'] if row else '', source='user' if row else '')


def set_trigger(name, words, *, folders=None):
    if not isinstance(words, str):
        raise ValueError('Trigger words must be a string')
    words = words.strip()
    if len(words) > MAX_WORDS:
        raise ValueError('Trigger words must be at most 300 characters')
    with _LOCK:
        path = resolve_name(name, folders)
        directory = settings_directory()
        digest = fingerprint(path, directory)
        rows = _entries(directory)
        if words:
            rows[digest] = dict(name=name, words=words, source='user')
        else:
            rows.pop(digest, None)
        _write(directory / TRIGGERS_FILE, dict(version=1, triggers=rows))
        return dict(name=name, words=words, source='user' if words else '')


def get_trigger_words(name, *, folders=None):
    # No saved triggers means no model-file reads when listing or submitting LoRAs.
    # Subfolder names admitted by existing workflows cannot have an API entry.
    with _LOCK:
        if not _valid_name(name) or not _entries(settings_directory()):
            return ''
        return get_trigger(name, folders=folders)['words']


def _written(prompt, words):
    """Whether the phrase stands on its own in the prompt: sks is not found in asks.

    Only letters and digits at the phrase's own edges are checked, so a phrase in Chinese still matches in running text."""
    ends = ''.join(('(?<![A-Za-z0-9_])' if words[:1].isascii() and (words[:1].isalnum() or words[:1] == '_') else '',
                    re.escape(words),
                    '(?![A-Za-z0-9_])' if words[-1:].isascii() and (words[-1:].isalnum() or words[-1:] == '_') else ''))
    return re.search(ends, prompt, re.IGNORECASE) is not None


def prepend_words(prompt, words):
    if not words or _written(prompt, words):
        return prompt
    return words + ', ' + prompt if prompt else words


def apply_triggers(prompt, loras, *, folders=None):
    for row in loras:
        if row.get('enabled', True) and row.get('strength', 1.) != 0:
            prompt = prepend_words(prompt, get_trigger_words(row['name'], folders=folders))
    return prompt


def list_triggers(folders):
    """Installed LoRA names that have trigger words saved, as {name: words}."""
    found = {}
    for name in sorted(folders.get_filename_list('loras')):
        if name.lower().endswith('.safetensors'):
            words = get_trigger_words(name, folders=folders)
            if words:
                found[name] = words
    return found


def _stack_rows(graph, link, seen=None):
    """Follow only the selected generator's LoRA input and chained previous stacks."""
    seen = set() if seen is None else seen
    if (not isinstance(link, list) or len(link) != 2 or type(link[0]) not in (str, int)
            or link[0] in seen or link[0] not in graph):
        return []
    seen.add(link[0])
    node = graph[link[0]]
    if not isinstance(node, dict) or not isinstance(node.get('inputs'), dict):
        return []
    inputs = node['inputs']
    rows = _stack_rows(graph, inputs.get('previous'), seen)
    if node.get('class_type') == 'FreeVideoLoRAStack':
        serialized = inputs.get('adapters', '[]')
        if not isinstance(serialized, str) or len(serialized) > 128 * 1024:
            raise ValueError('Invalid LoRA selection')
        selected = json.loads(serialized)
        if not isinstance(selected, list) or any(not isinstance(row, dict) for row in selected):
            raise ValueError('Invalid LoRA selection')
        rows.extend(selected)
    elif node.get('class_type') == 'FreeVideoLoRA' and inputs.get('lora') != 'None':
        rows.append(dict(name=inputs.get('lora'), strength=inputs.get('strength', 1.)))
    return rows


def apply_chain_triggers(record, *, folders=None):
    """Supplement a validated chain before its snapshot and block texts are saved."""
    graph = record['snapshot']['prompt']
    inputs = graph[record['node']]['inputs']
    rows = _stack_rows(graph, inputs.get('loras'))
    # Resolve once per chain, then supplement each independently supplied block.
    words = [get_trigger_words(row.get('name'), folders=folders) for row in rows
             if row.get('enabled', True) and row.get('strength', 1.) != 0]
    for item in [inputs, *(block['inputs'] for block in record['blocks'])]:
        if isinstance(item.get('text'), str):
            for phrase in words:
                item['text'] = prepend_words(item['text'], phrase)
    if any(len(block['inputs']['text']) > 20000 for block in record['blocks']):
        raise ValueError('Block text including LoRA triggers must be at most 20000 characters')
