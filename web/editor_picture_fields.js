// Sparse wire fields; normalization matches editor_geometry/editor_picture._fields.
export const PICTURE_DEFAULTS = Object.freeze({fit: 'cover', crop_left: 0, crop_top: 0, crop_right: 0, crop_bottom: 0,
    flip_h: false, flip_v: false, rotate: 0, scale: 1, offset_x: 0, offset_y: 0});
export function pictureFields(value = {}) {
    value = value && typeof value === 'object' ? value : {};
    const result = {fit: value.fit === 'contain' ? 'contain' : 'cover'};
    for (const [key, fallback] of Object.entries(PICTURE_DEFAULTS)) {
        if (key === 'fit') continue;
        const v = value[key], [low, high] = key.startsWith('crop_') ? [0, .9] : key === 'scale' ? [.1, 10] : key === 'rotate' ? [-180, 180] : [-2, 2];
        result[key] = typeof fallback === 'boolean' ? v === true : typeof v !== 'number' || Number.isNaN(v) ? fallback : Math.min(high, Math.max(low, v));
    }
    for (const [a, b] of [['crop_left', 'crop_right'], ['crop_top', 'crop_bottom']]) {
        const sum = result[a] + result[b];
        if (sum > .9) { result[a] = result[a] * .9 / sum; result[b] = result[b] * .9 / sum; }
    }
    return Object.fromEntries(Object.entries(result).filter(([k, v]) => v !== PICTURE_DEFAULTS[k]));
}
export function replacePicture(target, fields) {
    for (const key of Object.keys(PICTURE_DEFAULTS)) delete target[key];
    Object.assign(target, pictureFields(fields));
    return target;
}

// The existing immutable model supplies its validation and transaction finish.
export function pictureEditor({copy, trackBy, check, pictureFit, finish}) {
    const end = clip => clip.start + clip.frames;
    return function (record, id, fields) {
        check(fields !== null && typeof fields === 'object' && !Array.isArray(fields), 'invalid');
        const next = copy(record), clip = trackBy(next, 'v1').clips.find(c => c.id === id);
        check(clip && clip.source, 'missing');
        if (fields.fit !== undefined) pictureFit(fields.fit);
        replacePicture(clip, {...clip, ...fields});
        for (const tx of next.replacements || []) if (tx.rolledBack) {
            const old = tx.originals[0].clips.find(c => c.id === id);
            if (old) { replacePicture(old, clip); for (const c of tx.clips[0].clips)
                if (c.start >= old.start && end(c) <= end(old)) replacePicture(c, clip); }
        }
        return finish(next);
    };
}
