"""Run the image editor's commands (web/image_commands.js) on the server in QuickJS.

The Agent changes an image document with the functions the image editor page
calls, linked by editor_runtime's bundler. The page's canvas is not there: the
2D DOMMatrix the commands use is supplied here, and text is measured by the
server renderer's measure (edit.measure_text), which draws with the same fonts.
"""
from __future__ import annotations

import json
import threading

from . import edit
from .editor_runtime import WEB, ScriptRuntime, bundle

MODULES = ('fonts', 'image_editor_engine', 'image_commands')
# The undo steps a batch may build before the caller trims them (images.MAX_HISTORY).
HISTORY_LIMIT = 1000
# Text sizes a batch may ask for in turn (a resize measures text at its new size).
MEASURE_ROUNDS = 8

# DOMMatrix and DOMPoint for the affine 2D transforms the commands use, in double
# precision as WebKit computes them; Chromium stores scale-translate matrices in
# float32, so geometry may differ from the page in the last digits.
_MATRIX = r'''
class DOMPoint {
    constructor(x = 0, y = 0, z = 0, w = 1) { this.x = x; this.y = y; this.z = z; this.w = w; }
    matrixTransform(m) { return new DOMMatrix(m).transformPoint(this); }
}
class DOMMatrix {
    constructor(init) {
        const v = init === undefined ? [1, 0, 0, 1, 0, 0]
            : Array.isArray(init) ? init : [init.a, init.b, init.c, init.d, init.e, init.f];
        if (v.length !== 6 || !v.every(Number.isFinite)) throw new TypeError('DOMMatrix: only finite 2D matrices are supported');
        [this.a, this.b, this.c, this.d, this.e, this.f] = v;
    }
    get is2D() { return true; }
    get isIdentity() { return this.a === 1 && this.b === 0 && this.c === 0 && this.d === 1 && this.e === 0 && this.f === 0; }
    multiplySelf(o) {
        const m = o instanceof DOMMatrix ? o : new DOMMatrix(o), {a, b, c, d, e, f} = this;
        this.a = a * m.a + c * m.b; this.b = b * m.a + d * m.b;
        this.c = a * m.c + c * m.d; this.d = b * m.c + d * m.d;
        this.e = a * m.e + c * m.f + e; this.f = b * m.e + d * m.f + f;
        return this;
    }
    preMultiplySelf(o) {
        const m = new DOMMatrix(o instanceof DOMMatrix ? o : new DOMMatrix(o)).multiplySelf(this);
        Object.assign(this, {a: m.a, b: m.b, c: m.c, d: m.d, e: m.e, f: m.f});
        return this;
    }
    translateSelf(tx = 0, ty = 0) { return this.multiplySelf([1, 0, 0, 1, tx, ty]); }
    scaleSelf(sx = 1, sy = sx) { return this.multiplySelf([sx, 0, 0, sy, 0, 0]); }
    rotateSelf(x = 0, y, z) {
        if (y !== undefined || z !== undefined) {
            if (x || y) throw new TypeError('DOMMatrix: only rotation about z is supported');
            x = z || 0;
        }
        const r = x * Math.PI / 180, cos = Math.cos(r), sin = Math.sin(r);
        // The browser's exact values at quarter turns.
        const q = ((x % 360) + 360) % 360, exact = {0: [1, 0], 90: [0, 1], 180: [-1, 0], 270: [0, -1]}[q];
        const [cs, sn] = exact || [cos, sin];
        return this.multiplySelf([cs, sn, -sn, cs, 0, 0]);
    }
    invertSelf() {
        const {a, b, c, d, e, f} = this, det = a * d - b * c;
        if (!det) { [this.a, this.b, this.c, this.d, this.e, this.f] = [NaN, NaN, NaN, NaN, NaN, NaN]; return this; }
        this.a = d / det; this.b = -b / det; this.c = -c / det; this.d = a / det;
        this.e = (c * f - d * e) / det; this.f = (b * e - a * f) / det;
        return this;
    }
    multiply(o) { return new DOMMatrix(this).multiplySelf(o); }
    translate(tx, ty) { return new DOMMatrix(this).translateSelf(tx, ty); }
    scale(sx, sy) { return new DOMMatrix(this).scaleSelf(sx, sy); }
    rotate(x, y, z) { return new DOMMatrix(this).rotateSelf(x, y, z); }
    inverse() { return new DOMMatrix(this).invertSelf(); }
    transformPoint(p = {}) {
        const x = p.x || 0, y = p.y || 0;
        return new DOMPoint(this.a * x + this.c * y + this.e, this.b * x + this.d * y + this.f, p.z || 0, p.w ?? 1);
    }
    get m11() { return this.a; } get m12() { return this.b; } get m21() { return this.c; }
    get m22() { return this.d; } get m41() { return this.e; } get m42() { return this.f; }
}
'''

# One batch: {document, selection, history, future, original, target, operations}
# -> {ok, document, selection, history, future, results} | {ok: false, index, error}.
# history holds the earlier documents (oldest first), future the undone ones
# (nearest last), as the page keeps them; every operation that changes the
# document is one step, a selection change is none, as on the page.
_ENTRY = r'''
const measureKey = text => JSON.stringify([String(text.content ?? ''), text.font ?? null, text.weight ?? null, text.size, text.lineHeight ?? null]);
globalThis.__image_batch = text => {
    const request = JSON.parse(text), c = __load('image_commands'), engine = __load('image_editor_engine');
    // Canvas points (after turns and flips) to document pixels and back, as the page's toD / fromD.
    const toD = (d, [x, y]) => { const p = engine.orientMatrix(d).inverse().transformPoint(new DOMPoint(x, y)); return [p.x, p.y]; };
    const fromD = (d, [x, y]) => { const p = engine.orientMatrix(d).transformPoint(new DOMPoint(x, y)); return [p.x, p.y]; };
    // Text sizes come from the server's measure; one asked for that is not in request.measures
    // is listed in the reply and the batch runs again with it (QuickJS cannot call out under a time limit).
    const measures = request.measures || {}, missing = {};
    const measure = text => {
        const key = measureKey(text);
        if (key in measures) return measures[key];
        missing[key] = text; return {nw: 1, nh: 1};
    };
    const target = request.target ? {width: request.target[0], height: request.target[1]} : null;
    let history = [...request.history, request.document], future = request.future, selection = request.selection ?? null;
    let doc = request.document;
    const results = [];
    const layer = id => doc.layers.find(l => l.id === id) || (() => { throw new c.ImageEditError('not_found', 'Layer not found'); })();
    const commit = next => { ({history, future, doc} = c.commitStep({history, future}, next, %(limit)d)); };
    const follow = (before, after) => {
        if (before.width !== after.width || before.height !== after.height)
            selection = engine.scaleSelection(selection, after.width / before.width, after.height / before.height);
    };
    const geometry = ['cx', 'cy', 'w', 'h', 'rotation', 'flipX', 'flipY'];
    // As the page does: a lock keeps a layer from being deleted or moved, not from being retouched.
    const pixels = (id, command) => {
        if (!layer(id).locked) return command(doc);
        return c.setLayer(command(c.setLayer(doc, id, {locked: false})), id, {locked: true});
    };
    for (let index = 0; index < request.operations.length; index++) {
        const op = request.operations[index], before = new Set(doc.layers.map(l => l.id)), result = {};
        try {
            switch (op.op) {
            case 'document.replace': commit(op.document); break;
            case 'canvas.resize': {
                const next = c.resizeImage(doc, op.width, op.height, op.keep ?? true, measure);
                follow(doc, next); commit(next); break;
            }
            case 'canvas.rotate': commit(c.turn(doc, op.direction, target)); break;
            case 'canvas.flip': commit(c.turn(doc, op.axis === 'horizontal' ? 'flipX' : 'flipY', target)); break;
            case 'canvas.crop': commit({...doc, crop: c.normalizeCrop(doc, op.crop, doc.ratio, target)}); break;
            case 'canvas.extend': commit(c.setCanvas(doc, {color: doc.extend.color, ...op.extend})); break;
            case 'canvas.ratio': commit(c.setRatio(doc, op.ratio, target)); break;
            case 'canvas.pad_to_video': commit(c.padToVideo(doc, target, op.color ?? '#000000')); break;
            case 'layer.add': {
                const spec = op.layer, name = spec.name ?? '';
                if (spec.kind === 'paint') commit(c.addPaintLayer(doc, name));
                else if (spec.kind === 'image') commit(c.addImageLayer(doc, {src: spec.src, nw: spec.nw, nh: spec.nh, name}));
                else if (spec.kind === 'text') {
                    if (!spec.text) throw new c.ImageEditError('unsupported', 'text is required');
                    const r = engine.canvasRect(doc);
                    const text = {font: 'sans', weight: 700, color: '#ffffff', align: 'left', shadow: true, outline: false, lineHeight: 1.25, ...spec.text};
                    commit(c.addTextLayer(doc, {position: spec.position ?? toD(doc, [r.x + r.w / 2, r.y + r.h / 2]), name, text}, measure));
                } else {
                    const shape = spec.shape;
                    if (!shape || !shape.from || !shape.to) throw new c.ImageEditError('unsupported', 'shape with from and to is required');
                    const {from, to, ...rest} = shape, line = (rest.type ?? 'rect') === 'line';
                    // Agents give points in document pixels like every other point; the page's gesture gives canvas ones.
                    commit(c.addShapeLayer(doc, {from: fromD(doc, from), to: fromD(doc, to), name, shape: {type: 'rect', fill: line ? null : '#ffffff',
                        stroke: line ? '#ffffff' : null, width: 6, radius: 0, ...rest}}));
                }
                break;
            }
            case 'layer.copy': commit(c.duplicateLayer(doc, op.layer_id, op.name ?? layer(op.layer_id).name)); break;
            case 'layer.delete': commit(c.deleteLayer(doc, op.layer_id)); break;
            case 'layer.reorder': {
                let next = doc, at = doc.layers.findIndex(l => l.id === layer(op.layer_id).id);
                if (op.index >= doc.layers.length || at < 1) throw new c.ImageEditError('range', 'Layer move out of range');
                while (at !== op.index) { const d = op.index > at ? 1 : -1; next = c.moveLayer(next, op.layer_id, d); at += d; }
                commit(next); break;
            }
            case 'layer.select': commit(c.setActive(doc, op.layer_id)); break;
            case 'layer.update': {
                const patch = op.patch, own = {}, place = {};
                for (const [key, value] of Object.entries(patch))
                    (geometry.includes(key) ? place : key === 'name' ? result : own)[key] = value;
                let next = doc;
                if ('name' in result) { next = c.renameLayer(next, op.layer_id, result.name); delete result.name; }
                if (Object.keys(own).length) next = c.setLayer(next, op.layer_id, own);
                if (Object.keys(place).length) {
                    const current = next.layers.find(l => l.id === op.layer_id);
                    if (current.locked) throw new c.ImageEditError('locked', 'Layer is locked');
                    next = {...next, layers: next.layers.map(l => l.id === op.layer_id ? {...l, ...place} : l)};
                }
                commit(next); break;
            }
            case 'layer.adjust': {
                const current = layer(op.layer_id);
                if (!engine.rasterKinds.has(current.kind)) throw new c.ImageEditError('not_pixel_layer', 'An image or paint layer is required');
                commit(c.setLayer(doc, op.layer_id, {adjust: {...current.adjust, ...op.patch}})); break;
            }
            case 'layer.text': commit(c.updateText(doc, op.layer_id, op.patch, measure)); break;
            case 'layer.shape': commit(c.updateShape(doc, op.layer_id, op.patch)); break;
            case 'selection.set': selection = op.selection.parts.length ? op.selection : null; break;
            case 'selection.add': selection = c.addPart(selection, op.part, op.part.mode ?? 'replace'); break;
            case 'selection.clear': selection = null; break;
            case 'selection.all': selection = c.selectAll(); break;
            case 'selection.invert': selection = c.invertSelection(selection); break;
            // The selection is used up by what the page does with it, as there.
            case 'selection.copy_to_layer':
                commit(c.copyToLayer(doc, op.layer_id, selection, op.name ?? layer(op.layer_id).name)); selection = null; break;
            case 'selection.fill': commit(pixels(op.layer_id, d => c.fillSelection(d, op.layer_id, op.color ?? '#000000', selection))); break;
            case 'selection.erase': commit(pixels(op.layer_id, d => c.clearSelection(d, op.layer_id, selection))); break;
            case 'mask.set': if (!layer(op.layer_id).mask) {
                const next = c.toggleMask(doc, op.layer_id, selection);
                commit(next.doc); if (next.selectionUsed) selection = null;
            } break;
            case 'mask.delete': if (layer(op.layer_id).mask) commit(c.toggleMask(doc, op.layer_id, selection).doc); break;
            case 'mask.from_selection': commit(c.maskFromSelection(doc, op.layer_id, selection)); selection = null; break;
            case 'pixels.apply': {
                const {id, ...pixel} = op.pixel_op, current = layer(op.layer_id);
                if (op.target === 'mask') {
                    // As the page paints on a mask: paint hides, erase shows again.
                    if (!current.mask) throw new c.ImageEditError('unsupported', 'The layer has no mask');
                    if (pixel.type !== 'stroke') throw new c.ImageEditError('unsupported', 'Only strokes paint on a mask');
                    pixel.tool = pixel.tool === 'paint' ? 'hide' : pixel.tool === 'erase' ? 'reveal' : pixel.tool;
                    if (pixel.tool !== 'hide' && pixel.tool !== 'reveal') throw new c.ImageEditError('unsupported', 'Only paint and erase work on a mask');
                } else if (pixel.type === 'stroke' && (pixel.tool === 'hide' || pixel.tool === 'reveal'))
                    throw new c.ImageEditError('unsupported', 'hide and reveal paint on a mask');
                // The page puts paint and gradients for a locked picture on a paint layer of their own; erasing it is allowed.
                if (current.locked && op.target !== 'mask' && (pixel.type === 'gradient' || pixel.tool === 'paint'))
                    throw new c.ImageEditError('locked', 'Paint on a paint layer (layer.add) above the locked picture');
                commit(pixels(op.layer_id, d => c.pushOp(d, op.layer_id, pixel))); break;
            }
            case 'undo': case 'redo': {
                if (op.op === 'undo' ? history.length <= 1 : !future.length) break;
                const next = (op.op === 'undo' ? c.undoStep : c.redoStep)({history, future, doc, selection});
                ({history, future, doc, selection} = next); break;
            }
            case 'reset': selection = null; commit(request.original); break;
            default: throw new c.ImageEditError('unsupported', 'Unknown operation');
            }
        } catch (error) {
            if (!(error instanceof c.ImageEditError)) throw error;
            // A placeholder size may be what failed; measure first, then decide.
            if (Object.keys(missing).length) return JSON.stringify({ok: false, measure: missing});
            return JSON.stringify({ok: false, index, error: {code: error.code, message: error.message}});
        }
        const added = doc.layers.filter(l => !before.has(l.id)).map(l => l.id);
        if (added.length) result.new_layer_ids = added;
        results.push(result);
    }
    if (Object.keys(missing).length) return JSON.stringify({ok: false, measure: missing});
    return JSON.stringify({ok: true, document: doc, selection, history: history.slice(0, -1), future, results});
};
''' % dict(limit=HISTORY_LIMIT)


class ImageRuntime(ScriptRuntime):
    """The image editor's commands (web/image_commands.js)."""

    def __init__(self, web=WEB):
        # fonts.js finds its files from its own address, used only to load them in a page.
        source = bundle(web, MODULES, start='image_commands', entry=_ENTRY, prelude=_MATRIX)
        super().__init__(source.replace('import.meta.url', "'file:///web/fonts.js'"))

    def batch(self, **request):
        """Apply operations to a session's document; see _ENTRY for the reply."""
        measures = {}
        for _ in range(MEASURE_ROUNDS):
            reply = self._entry('__image_batch', dict(request, measures=measures))
            if 'measure' not in reply:
                return reply
            for key, text in reply['measure'].items():
                try:
                    measures[key] = edit.measure_text(text)
                except (KeyError, TypeError, ValueError, OSError):
                    return dict(ok=False, index=None, error=dict(code='range', message='Text cannot be measured'))
        raise RuntimeError('image text measure did not settle')


_RUNTIME = None
_RUNTIME_LOCK = threading.Lock()


def runtime():
    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is None:
            _RUNTIME = ImageRuntime()
        return _RUNTIME
