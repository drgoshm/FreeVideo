import * as model from './editor_model.js';
function operations() { return operations.value; }
export function connectSpeedOperations(value) { operations.value = value; }

const check = (value, code = 'speed') => { if (!value) throw new model.EditError(code); };
const whole = value => Number.isSafeInteger(value) && value >= 0 && value <= 1e7;
export const percent = clip => Math.round((clip.speed ?? 1) * 100);
export const speedEdited = clip => !!clip && (percent(clip) !== 100 || clip.reverse === true || clip.speed_source_frames !== undefined || (clip.speed_phase ?? 0) !== 0);
export const sourceFrames = clip => clip.speed_source_frames ?? clip.frames;
export const maxFrames = (frames, p, phase = 0) => Math.ceil((frames * 100 - phase) / p);
export function sourceFrame(clip, k) {
    return clip.in + Math.floor(((clip.reverse ? clip.frames - 1 - k : k) * percent(clip) + (clip.speed_phase ?? 0)) / 100);
}
export function speedRange(clip) {
    return whole(clip.length) && clip.length > clip.in
        && clip.in + Math.floor(((clip.frames - 1) * percent(clip) + (clip.speed_phase ?? 0)) / 100) < clip.length;
}
export function parseSpeedData(raw, kind) {
    const data = {};
    if (raw.speed !== undefined) {
        check(typeof raw.speed === 'number' && Number.isFinite(raw.speed) && raw.speed >= .25 && raw.speed <= 4
            && Math.abs(raw.speed * 100 - Math.round(raw.speed * 100)) < 1e-8);
        data.speed = raw.speed;
    }
    if (raw.reverse !== undefined) { check(typeof raw.reverse === 'boolean'); data.reverse = raw.reverse; }
    if (raw.speed_audio !== undefined) {
        check(kind === 'audio' && ['follow', 'mute'].includes(raw.speed_audio)); data.speed_audio = raw.speed_audio;
    }
    if (raw.speed_source_frames !== undefined) {
        check(whole(raw.speed_source_frames) && raw.speed_source_frames > 0 && raw.in + raw.speed_source_frames <= raw.length);
        data.speed_source_frames = raw.speed_source_frames;
    }
    if (raw.speed_phase !== undefined) {
        check(whole(raw.speed_phase) && raw.speed_phase < 100);
        if (raw.speed_phase && percent(raw) !== 100) data.speed_phase = raw.speed_phase;
    }
    check(!speedEdited(raw) || (raw.source !== null && raw.source !== undefined));
    return data;
}
export function validateSpeedPair(a, b) {
    check(percent(a) === percent(b) && !!a.reverse === !!b.reverse && sourceFrames(a) === sourceFrames(b)
        && (a.speed_phase ?? 0) === (b.speed_phase ?? 0), 'link');
}
export function normalizeSpeed(value) {
    check(typeof value === 'number' && Number.isFinite(value) && value >= .25 && value <= 4);
    return Math.round((value + Number.EPSILON) * 100) / 100;
}
function selected(record, id) {
    const clips = model.pair(record, id); check(clips.length, 'missing');
    check(clips.every(clip => clip.source && !clip.pending), 'speed_pending');
    check(record.tracks.every(track => !track.locked || !track.clips.some(clip => clips.includes(clip))), 'speed_locked');
    return clips;
}
function write(clip, speed, reverse, audio) {
    if (speed === 1) delete clip.speed; else clip.speed = speed;
    if (reverse) clip.reverse = true; else delete clip.reverse;
    if (clip.volume !== undefined) {
        if (audio === 'follow') delete clip.speed_audio; else clip.speed_audio = audio;
    }
    if (speed === 1 && !reverse) delete clip.speed_source_frames;
}
function writePhase(clip, phase) {
    if (phase) clip.speed_phase = phase; else delete clip.speed_phase;
}
export function setSpeed(record, id, options = {}) {
    const clips = selected(record, id), primary = clips[0];
    const speed = options.speed === undefined ? primary.speed ?? 1 : normalizeSpeed(options.speed);
    const reverse = options.reverse ?? primary.reverse ?? false;
    check(typeof reverse === 'boolean');
    const sound = clips.find(clip => clip.volume !== undefined);
    const audio = options.speed_audio ?? sound?.speed_audio ?? 'follow'; check(['follow', 'mute'].includes(audio));
    // Phase is a fraction of a source frame, independent of playback speed.
    // At 1x every phase selects the same integer frames, so canonicalize to zero.
    const p = Math.round(speed * 100), phase = p === 100 ? 0 : primary.speed_phase ?? 0;
    const frames = p === percent(primary) ? primary.frames : maxFrames(sourceFrames(primary), p, phase);
    const {copy, finish, makeRoom, detachAttempt, trackOf} = operations();
    const next = copy(record), ids = new Set(clips.map(clip => clip.id));
    const changed = clips.some(clip => percent(clip) !== p || (clip.speed_phase ?? 0) !== phase || !!clip.reverse !== reverse || (clip.volume !== undefined && (clip.speed_audio ?? 'follow') !== audio));
    if (!changed) return record;
    detachAttempt(next, [...ids]);
    // Room is measured on every track the pair occupies, and everything behind the old end yields on all tracks.
    if (frames > primary.frames) makeRoom(next, clips.map(clip => trackOf(next, clip.id).id), primary.start + primary.frames, frames - primary.frames, ids);
    for (const old of clips) {
        const clip = model.find(next, old.id);
        clip.speed_source_frames = sourceFrames(old); clip.frames = frames;
        writePhase(clip, phase);
        write(clip, speed, reverse, audio);
        check(speedRange(clip), 'range'); delete clip.target_frames;
    }
    return finish(next);
}

// Euclidean division keeps the floor sequence, including when revealing media
// before the current in-point. Reverse uses the same forward source interval.
function slice(clip, offset, frames) {
    const p = percent(clip), first = clip.reverse ? clip.frames - offset - frames : offset;
    const numerator = first * p + (clip.speed_phase ?? 0), delta = Math.floor(numerator / 100), phase = numerator - delta * 100;
    const result = {...clip, in: clip.in + delta, frames};
    writePhase(result, phase);
    check(result.in >= 0 && speedRange(result), 'range');
    const last = first + frames;
    const boundary = last === clip.frames ? sourceFrames(clip) : Math.floor((last * p + (clip.speed_phase ?? 0)) / 100);
    result.speed_source_frames = Math.min(result.length - result.in, Math.max(Math.floor(((frames - 1) * p + phase) / 100) + 1, boundary - delta));
    check(result.in + result.speed_source_frames <= result.length, 'range');
    return result;
}
function replaceSlice(clip, offset, frames) {
    const result = slice(clip, offset, frames), delta = result.in - clip.in;
    Object.assign(clip, result);
    writePhase(clip, result.speed_phase ?? 0);
    clip.versions = clip.versions.map(version => ({...version, in: version.in + delta}))
        .filter(version => version.in >= 0 && version.in < version.length);
    delete clip.target_frames;
}
export function trimSpeed(record, id, edge, frame, by = 'user') {
    const clips = selected(record, id), primary = clips[0], minimum = 1;
    const {copy, finish, makeRoom, detachAttempt, trackOf} = operations();
    let delta = frame - (edge === 'start' ? primary.start : primary.start + primary.frames);
    if (edge === 'start') delta = Math.min(primary.frames - minimum, Math.max(-primary.start, delta));
    else delta = Math.max(minimum - primary.frames, delta);
    // Clamp to available media while keeping the requested frame phase intact.
    const p = percent(primary), phase = primary.speed_phase ?? 0, span = Math.min(...clips.map(clip => clip.length - clip.in));
    if (edge === 'end' && !primary.reverse) delta = Math.min(delta, maxFrames(span, p, phase) - primary.frames);
    if (edge === 'start' && primary.reverse) delta = Math.max(delta, primary.frames - maxFrames(span, p, phase));
    if (edge === 'start' && !primary.reverse) delta = Math.max(delta, -Math.floor((primary.in * 100 + phase) / p));
    if (edge === 'end' && primary.reverse) delta = Math.min(delta, Math.floor((primary.in * 100 + phase) / p));
    if (!delta) return record;
    const next = copy(record), ids = new Set(clips.map(clip => clip.id)); detachAttempt(next, [...ids]);
    if (edge === 'end' && delta > 0) makeRoom(next, clips.map(clip => trackOf(next, clip.id).id), primary.start + primary.frames, delta, ids);
    for (const old of clips) {
        const clip = model.find(next, old.id); if (by === 'user') delete clip.text_by;
        replaceSlice(clip, edge === 'start' ? delta : 0, old.frames + (edge === 'start' ? -delta : delta));
        if (edge === 'start') clip.start += delta;
    }
    return finish(next);
}
export function splitSpeed(record, id, frame, by = 'user') {
    const clips = selected(record, id), primary = clips[0], left = frame - primary.start;
    check(left >= 0 && left <= primary.frames, 'range');
    if (left === 0 || left === primary.frames) return record;
    const {copy, finish, detachAttempt, trackOf, freshId, splitAudioFields} = operations();
    const next = copy(record); detachAttempt(next, clips.map(clip => clip.id)); const rights = [];
    for (const old of clips) {
        const clip = model.find(next, old.id); if (by === 'user') delete clip.text_by;
        const right = structuredClone(clip);
        replaceSlice(clip, 0, left); replaceSlice(right, left, old.frames - left);
        right.id = freshId(next); right.start = frame;
        splitAudioFields(clip, right);
        trackOf(next, clip.id).clips.push(right); rights.push(right);
    }
    if (rights.length === 2) { rights[0].link = rights[1].id; rights[1].link = rights[0].id; }
    return finish(next);
}
