// Gesture rules for the sound group: putting the original sound back, explicit replacement.
import * as model from './editor_model.js';
import {trackOf, putAudio} from './editor_audio_model.js';
const fail = code => { throw new model.EditError(code); };
const end = clip => clip.start + clip.frames;
const busy = clip => ['queued', 'running'].includes(clip.pending);
const overlaps = (clip, start, stop) => clip.start < stop && end(clip) > start;
// The fields a linked pair must share, copied from the picture to its sound.
const SHARED = ['start', 'frames', 'in', 'length', 'speed', 'reverse', 'speed_source_frames', 'speed_phase', 'splice'];

// The button works as long as the picture exists and is waiting for its sound.
export function canRestoreOriginal(record, id) {
    const picture = model.find(record, id);
    return !!(picture && picture.source && !picture.link && picture.detached_from && trackOf(record, id).kind === 'video');
}
// The picture's sound comes back: the detached piece when it is still the same original,
// otherwise a new piece, and whatever replaced it stays where it is.
export function restoreOriginal(record, id) {
    if (!canRestoreOriginal(record, id)) fail('restore_audio');
    const next = model.serialize(record), picture = model.find(next, id), old = model.find(next, picture.detached_from);
    const original = !!old && old.detached_from === picture.id && !old.link && trackOf(next, old.id).kind === 'audio'
        && JSON.stringify(old.source) === JSON.stringify(picture.source);
    for (const clip of [picture, ...(original ? [old] : [])]) {
        if (trackOf(next, clip.id).locked) fail('locked');
        if (busy(clip)) fail('busy');
    }
    // The lane it came from first, then the other one. Nothing is pushed: the picture stays where it is.
    const lanes = ['a1', 'a2'].map(name => next.tracks.find(track => track.id === name) || {id: name, kind: 'audio', clips: []});
    const home = original ? trackOf(next, old.id).id : 'a1';
    lanes.sort((a, b) => (b.id === home) - (a.id === home));
    const lane = lanes.find(track => !track.locked && !track.clips.some(clip => !(original && clip.id === old.id) && overlaps(clip, picture.start, end(picture))));
    if (!lane) fail('audio_full');
    let sound = old;
    if (original) trackOf(next, old.id).clips = trackOf(next, old.id).clips.filter(clip => clip.id !== old.id);
    else {
        sound = {id: `c${++next.serial}`, source: structuredClone(picture.source), link: null, text: '', part: null, pending: null, reason: null, job: null,
            extend: picture.extend, versions: [], volume: 1};
    }
    for (const name of SHARED) if (picture[name] !== undefined) sound[name] = structuredClone(picture[name]); else delete sound[name];
    // Fades and crossfades follow the new position and length.
    delete sound.crossfade;
    for (const name of ['fade_in', 'fade_out']) if (sound[name]) {
        const value = Math.min(sound[name], Math.floor(sound.frames / 2));
        if (value) sound[name] = value; else delete sound[name];
    }
    sound.link = picture.id; picture.link = sound.id; delete picture.detached_from; delete sound.detached_from;
    if (!next.tracks.some(track => track.id === lane.id)) next.tracks.push(lane);
    const row = next.tracks.find(track => track.id === lane.id);
    row.clips.push(sound); row.clips.sort((a, b) => a.start - b.start);
    return model.parse(next);
}

// "Replace audio" is a command of its own: the piece keeps its place, a shorter
// replacement shortens it, a longer one keeps the old length.
export function canReplaceAudio(record, id) {
    const clip = model.find(record, id);
    return !!(clip && clip.source && clip.detached_from && !clip.link && trackOf(record, id).kind === 'audio');
}
export function replaceAudio(record, id, payload) {
    if (!canReplaceAudio(record, id)) fail('link');
    return putAudio(record, {...payload, hasVideo: false}, trackOf(record, id).id, model.find(record, id).start, id);
}
