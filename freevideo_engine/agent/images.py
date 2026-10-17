"""Image editing sessions for agents: the image editor's document, changed with the page's commands.

A session holds one version 2 document (web/image_editor_engine.js) of a frozen
source picture, the selection, and an undo history. Operations run the page's
own commands (web/image_commands.js) in QuickJS (image_runtime), so the Agent
adds layers, heals and crops the way the page does; every change is saved with a
revision check, and render makes a PNG with the server renderer (edit.py).
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import logging
from pathlib import Path
import secrets
import threading
import time

from . import edit, errors, schema, spec
from .jobs import JobError

MAX_OPERATIONS = 64
MAX_RECEIPTS = 256
MAX_HISTORY = 32
MAX_HISTORY_BYTES = 8 * 2**20
MAX_SESSIONS = 512
# Command codes that describe the document's state; the rest are request errors.
STATE_CODES = {'locked': 'layer_locked'}
COMMAND_EXPECTED = {
    'not_found': 'a layer_id of this revision',
    'not_pixel_layer': 'an image or paint layer',
    'no_selection': 'a selection (selection.set first)',
    'range': 'values inside the documented ranges',
    'unsupported': 'an op named in the operations schema with its fields',
}


def now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


class ImageError(JobError):
    """An image session error; a conflict carries the current session so the caller can retry from it."""

    def __init__(self, code, message=None, *, image=None, operation=None):
        super().__init__(code, message or errors.ERRORS[code][2])
        self.status = errors.ERRORS[code][0]
        self.image, self.operation = image, operation

    def view(self):
        value = errors.build(self.code, validation=True, details=[], message=self.message,
                             next_action=errors.ERRORS[self.code][3])
        if self.operation is not None:
            value['details'].append(dict(operation=self.operation))
        if self.image is not None:
            value['image'] = deepcopy(self.image)
            value['current_revision'] = self.image['revision']
        return value


def _invalid(path, expected, received=None):
    return spec.SpecError([schema.diagnostic(path, expected, received)])


def _digest(scope, payload):
    return hashlib.sha256(json.dumps(dict(scope=scope, body=payload), sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def _bytes(value):
    return len(json.dumps(value, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode())


def _layers(document):
    return {layer['id']: layer for layer in document['layers']}


def changed_layer_ids(before, after):
    """Layers added, removed or changed, in the result's order, then removed ones."""
    old, new = _layers(before), _layers(after)
    return [i for i, layer in new.items() if old.get(i) != layer] + [i for i in old if i not in new]


def trim_history(history, future):
    """At most MAX_HISTORY undo steps and MAX_HISTORY_BYTES of stored steps; the oldest go first."""
    truncated = False
    history, future = list(history), list(future)
    while len(history) > MAX_HISTORY:
        history.pop(0)
        truncated = True
    while history and _bytes(history) + _bytes(future) > MAX_HISTORY_BYTES:
        history.pop(0)
        truncated = True
    while future and _bytes(future) > MAX_HISTORY_BYTES:
        future.pop()
        truncated = True
    return history, future, truncated


class Images:
    def __init__(self, jobs, directory):
        self.jobs, self.port = jobs, jobs.port
        self.directory = Path(directory)
        self.lock = threading.RLock()
        self.receipts = {}  # (client, ref) -> {digest, result}; in memory, bounded.

    # Storage -------------------------------------------------------------------------------------------
    def _path(self, identity):
        if not isinstance(identity, str) or not schema.re.fullmatch(schema.IMAGE['pattern'], identity):
            raise ImageError('not_found', 'The image session does not exist.')
        return self.directory / (identity + '.json')

    def _load(self, identity):
        path = self._path(identity)
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            raise ImageError('not_found', 'The image session does not exist.') from None
        except (OSError, ValueError) as error:
            logging.getLogger(__name__).warning('Image session %s is unreadable', identity)
            raise ImageError('storage_unavailable') from error
        if not isinstance(record, dict) or record.get('id') != identity or not isinstance(record.get('document'), dict):
            raise ImageError('storage_unavailable')
        return record

    def _store(self, record):
        from ..comfy_editor import _write
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            _write(self._path(record['id']), record)
        except OSError as error:
            raise ImageError('storage_unavailable') from error

    def view(self, record):
        return dict(id=record['id'], revision=record['revision'], document=deepcopy(record['document']),
                    source=dict(record['source']), target=record.get('target'), selection=deepcopy(record.get('selection')),
                    created_at=record['created_at'], updated_at=record['updated_at'],
                    history=dict(undo_depth=len(record.get('history', [])), redo_depth=len(record.get('future', []))))

    def _receipt(self, scope, payload, ref, client=None):
        if ref is None:
            return None, None
        digest = _digest(scope, payload)
        with self.lock:
            old = self.receipts.get((client, ref))
        if old is not None:
            if old['digest'] != digest:
                raise JobError('idempotency_conflict', 'Key already used')
            return digest, deepcopy(old['result'])
        return digest, None

    def _remember(self, ref, digest, result, client=None):
        if ref is None:
            return
        with self.lock:
            self.receipts[client, ref] = dict(digest=digest, result=deepcopy(result))
            while len(self.receipts) > MAX_RECEIPTS:
                self.receipts.pop(next(iter(self.receipts)))

    # Documents -----------------------------------------------------------------------------------------
    def _resolve(self, name):
        return self.port.input_path(name)

    def check_document(self, document, sources, at='/document'):
        """The published shape, then the renderer's cross checks, then every picture a layer names."""
        try:
            self._check_document(document, sources)
        except spec.SpecError as error:
            raise spec.SpecError([schema.prefix_diagnostic(e, at) if e['path'].startswith('/') or not e['path']
                                  else e for e in error.errors]) from None

    def _check_document(self, document, sources):
        schema.validate(schema.image_document(), document)
        try:
            edit._check(document)
        except edit.EditError as error:
            raise _invalid(error.path if error.path != '/' else '', error.message) from None
        ids = [layer['id'] for layer in document['layers']]
        if len(set(ids)) != len(ids):
            raise _invalid('/layers', 'unique layer ids')
        if document.get('active', 'base') not in ids:
            raise _invalid('/active', 'the id of one of the layers', document.get('active'))
        by = {layer['id']: layer for layer in document['layers']}
        for index, layer in enumerate(document['layers']):
            copy = layer.get('copyOf')
            if copy:
                # A copy draws the first upto steps of another layer; that one must not be a copy of it.
                source, chain = by.get(copy['layer']), {layer['id']}
                if source is None:
                    raise _invalid('/layers/%d/copyOf/layer' % index, 'the id of another layer', copy['layer'])
                if copy.get('upto', 0) > len(source.get('ops') or []):
                    raise _invalid('/layers/%d/copyOf/upto' % index, 'at most the number of steps of that layer', copy['upto'])
                while source is not None and source.get('copyOf'):
                    if source['id'] in chain:
                        raise _invalid('/layers/%d/copyOf' % index, 'copies that do not copy themselves')
                    chain.add(source['id'])
                    source = by.get(source['copyOf']['layer'])
                continue
            src = layer.get('src')
            if layer['kind'] == 'image' and src not in sources:
                path = self.port.input_path(src) if isinstance(src, str) else None
                if path is None or not path.is_file():
                    raise _invalid('/layers/%d/src' % index, 'an input file returned by media upload', src)

    def _freeze(self, identity, item):
        """The source picture as an input file of this session, and its digest."""
        from PIL import Image, ImageOps
        row = dict(id=identity)
        relative = self.jobs.media_file(row, item, 'source')
        if relative == item.get('file'):
            # An uploaded file may be replaced later; the session keeps its own copy.
            copy = 'freevideo/agent/%s/source%s' % (identity, Path(relative).suffix.lower())
            self.port.import_file(self.port.input_path(relative), self.port.input_path(copy, create=True))
            relative = copy
        path = self.port.input_path(relative)
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b''):
                digest.update(chunk)
        try:
            with Image.open(path) as image:
                width, height = ImageOps.exif_transpose(image).size
        except (OSError, ValueError, Image.DecompressionBombError) as error:
            raise JobError('invalid_media', 'The source is not a picture FreeVideo can read.') from error
        if width > 8192 or height > 8192 or width * height > schema.IMAGE_MAX_PIXELS:
            raise JobError('image_size', 'The source picture is larger than 8192 pixels on a side.')
        return dict(file=relative, digest='sha256:' + digest.hexdigest(), width=width, height=height)

    # Sessions ------------------------------------------------------------------------------------------
    def get(self, identity):
        return dict(image=self.view(self._load(identity)))

    def create(self, value, *, ref=None, client=None):
        schema.validate(schema.image_schema('create'), value)
        ref = value.get('ref', ref)
        payload = {k: v for k, v in value.items() if k != 'ref'}
        digest, replay = self._receipt('images:create', payload, ref, client)
        if replay is not None:
            return replay
        item = spec.media_item(value['source'], '/source', image=True)
        identity = 'i-' + secrets.token_hex(16)
        source = self._freeze(identity, item)
        if value.get('document') is not None:
            document = deepcopy(value['document'])
            self.check_document(document, {source['file']})
        else:
            document = edit.new_document(source['file'], source['width'], source['height'])
        stamp = now()
        record = dict(id=identity, revision=1, document=document, original=deepcopy(document),
                      source=dict(file=source['file'], digest=source['digest']),
                      target=list(value['target']) if value.get('target') else None, selection=None,
                      history=[], future=[], created_at=stamp, updated_at=stamp)
        with self.lock:
            self._store(record)
        result = dict(image=self.view(record))
        self._remember(ref, digest, result, client)
        return result

    def _current(self, identity, revision):
        record = self._load(identity)
        if record['revision'] != revision:
            raise ImageError('image_changed', image=self.view(record))
        return record

    def write(self, identity, value, *, ref=None, client=None):
        schema.validate(schema.image_schema('write'), value)
        ref = value.get('ref', ref)
        payload = dict({k: v for k, v in value.items() if k != 'ref'}, id=identity)
        digest, replay = self._receipt('images:write', payload, ref, client)
        if replay is not None:
            return replay
        return self.operations(identity, dict(revision=value['revision'],
            operations=[dict(op='document.replace', document=value['document'])]), _receipt=(ref, digest), _write=True, client=client)

    def operations(self, identity, value, *, client=None, ref=None, _receipt=None, _write=False):
        if _receipt is None:
            schema.validate(schema.image_schema('operations'), value)
            ref = value.get('ref', ref)
            payload = dict({k: v for k, v in value.items() if k != 'ref'}, id=identity)
            digest, replay = self._receipt('images:operations', payload, ref, client)
            if replay is not None:
                return replay
        else:
            ref, digest = _receipt
        from .image_runtime import runtime
        with self.lock:
            record = self._current(identity, value['revision'])
            operations = [self._prepared(index, op, record) for index, op in enumerate(value['operations'])]
            reply = runtime().batch(document=record['document'], selection=record.get('selection'),
                                    history=record.get('history', []), future=record.get('future', []),
                                    original=record['original'], target=record.get('target'),
                                    operations=operations)
            if not reply['ok']:
                self._rejected(reply, record, value['operations'])
            document = reply['document']
            # What the commands made is held to the same published shape as an upload.
            self.check_document(document, {record['source']['file']})
            history, future, truncated = trim_history(reply['history'], reply['future'])
            changed = changed_layer_ids(record['document'], document)
            moved = (document != record['document'] or reply['selection'] != record.get('selection')
                     or history != record.get('history', []) or future != record.get('future', []))
            if moved:
                record.update(document=document, selection=reply['selection'], history=history, future=future,
                              revision=record['revision'] + 1, updated_at=now())
                self._store(record)
        result = dict(image=self.view(record), changed_layer_ids=changed)
        if reply.get('results'):
            result['results'] = reply['results']
        if truncated:
            result['warnings'] = [dict(code='history_truncated',
                                       message='The oldest undo steps were dropped; the document did not change.')]
        if _write:
            result.pop('changed_layer_ids')
            result.pop('results', None)
        self._remember(ref, digest, result, client)
        return result

    def _prepared(self, index, op, record):
        """An operation with what the commands need from files: a whole document is checked, a new picture layer gets its size."""
        if op['op'] == 'document.replace':
            self.check_document(op['document'], {record['source']['file']}, '/operations/%d/document' % index)
        if op['op'] == 'layer.add' and op['layer']['kind'] == 'image':
            src = op['layer'].get('src')
            if src is None:
                raise _invalid('/operations/%d/layer/src' % index, 'an input file returned by media upload')
            width, height = self._picture_size(src, '/operations/%d/layer/src' % index)
            return dict(op, layer=dict(op['layer'], nw=width, nh=height))
        return op

    def _picture_size(self, src, path):
        from PIL import Image, ImageOps
        file = self.port.input_path(src)
        if file is None or not file.is_file():
            raise _invalid(path, 'an input file returned by media upload', src)
        try:
            with Image.open(file) as image:
                return ImageOps.exif_transpose(image).size
        except (OSError, ValueError, Image.DecompressionBombError):
            raise _invalid(path, 'a picture FreeVideo can read', src) from None

    def _rejected(self, reply, record, operations):
        error, index = reply['error'], reply.get('index')
        if error['code'] in STATE_CODES:
            raise ImageError(STATE_CODES[error['code']], error.get('message') or None, image=self.view(record),
                             operation=index)
        path = '/operations' + ('/%d' % index if index is not None else '')
        received = operations[index] if index is not None and index < len(operations) else None
        raise spec.SpecError([schema.diagnostic(path, COMMAND_EXPECTED.get(error['code'], 'an operation this image allows'),
                                                received)])

    def render(self, identity, value, *, jobs, ref=None, client=None):
        schema.validate(schema.image_schema('render'), value)
        ref = value.get('ref', ref)
        payload = dict({k: v for k, v in value.items() if k != 'ref'}, id=identity)
        digest, replay = self._receipt('images:render', payload, ref, client)
        if replay is not None:
            return replay
        self._current(identity, value['revision'])
        job = jobs.submit(dict(kind='image_edit', image=identity, revision=value['revision']), client=client)[0]
        result = dict(job=jobs.view(job), jobs=[jobs.view(job)])
        self._remember(ref, digest, result, client)
        return result


def run_render(jobs, row):
    """The image_edit job: render the session's revision to a PNG input file."""
    images = jobs.images
    spec_ = row['spec']
    record = images._load(spec_['image'])
    if record['revision'] != spec_['revision']:
        raise JobError('image_changed', 'The image changed since you read it (now revision %d)' % record['revision'])
    folder = 'freevideo/agent/' + row['id']
    relative = folder + '/image.png'
    out = jobs.port.input_path(relative, create=True)
    try:
        made = edit.save(record['document'], images._resolve, out)
    except edit.EditError as error:
        raise JobError('invalid_edit', str(error), path=getattr(error, 'path', None)) from None
    out.with_suffix('.edit.json').write_text(json.dumps(record['document']), encoding='utf-8')
    return dict(file=relative, width=made['width'], height=made['height'], document_version=2,
                image_id=record['id'], image_revision=record['revision'])
