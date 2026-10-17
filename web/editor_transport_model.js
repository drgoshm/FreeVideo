// Transport arithmetic and optional marker data, independent of the DOM.
const validFrame = value => Number.isSafeInteger(value) && value >= 0 && value <= 1e7;
export function markerFields(record) {
    if (record.markers === undefined) return {};
    if (!Array.isArray(record.markers) || record.markers.length > 10000) throw new Error('Invalid markers');
    const rows = new Map();
    for (const marker of record.markers) {
        if (!marker || !validFrame(marker.frame) || typeof marker.name !== 'string'
            || Array.from(marker.name).length > 8000 || /[\uD800-\uDFFF]/u.test(marker.name)) throw new Error('Invalid marker');
        rows.set(marker.frame, {frame: marker.frame, name: marker.name});
    }
    return {markers: [...rows.values()].sort((a, b) => a.frame - b.frame)};
}
export const hasMarker = (record, frame) => (record.markers || []).some(marker => marker.frame === frame);
export function addMarker(record, frame, name = '') {
    if (hasMarker(record, frame)) return record;
    return {...record, ...markerFields({markers: [...record.markers || [], {frame, name}]})};
}
export const removeMarker = (record, frame) => ({...record, markers: (record.markers || []).filter(marker => marker.frame !== frame)});
// A marker never lands on another marker: the move is refused and the record stays as it is.
export function moveMarker(record, from, to) {
    const marker = record.markers?.find(value => value.frame === from);
    if (!marker || from === to || hasMarker(record, to)) return record;
    return {...record, ...markerFields({markers: [...record.markers.filter(value => value.frame !== from), {...marker, frame: to}]})};
}
// The one definition of "the selected range", shared by playback (selected only, loop) and export.
// A range or a gap is its own start and end; selected parts run from the earliest start to the latest end,
// gaps between them included. There is no separate in or out point. Returns {start, end} in frames
// (end exclusive, clipped to `total`), or null when nothing usable is selected.
export function selectedRange(record, selection, total) {
    let start, end;
    if (selection?.kind === 'range' || selection?.kind === 'gap') { start = selection.start; end = start + selection.frames; }
    else if (selection?.kind === 'clip') {
        const clips = record.tracks.flatMap(track => track.clips).filter(clip => selection.ids.includes(clip.id));
        if (!clips.length) return null;
        start = Math.min(...clips.map(clip => clip.start)); end = Math.max(...clips.map(clip => clip.start + clip.frames));
    } else return null;
    end = Math.min(total, end);
    return end > start ? {start, end} : null;
}
// J and L play at 1x in their direction; pressing again never speeds up.
export const shuttleRate = (rate, direction) => direction;
export function createPlaybackClock() {
    let rate = 1, loop = false, range = null, time = 0, frame = 0;
    const bounds = total => ({start: Math.min(total, range?.start ?? 0), end: Math.min(total, range?.end ?? total)});
    return {
        get rate() { return rate; }, set rate(value) { if (![1, -1].includes(value)) throw new Error('Invalid playback rate'); rate = value; },
        get loop() { return loop; }, set loop(value) { loop = !!value; },
        get range() { return range; }, set range(value) { range = value; },
        reset(now, position) { time = now; frame = position; },
        begin(position, total) {
            const {start, end} = bounds(total);
            return rate < 0 ? position >= end || (loop && position <= start) ? Math.max(start, end - 1) : Math.max(start, position)
                : position < start || position >= end ? start : position;
        },
        advance(now, total) {
            const {start, end} = bounds(total), length = end - start;
            const raw = frame + Math.floor(Math.max(0, now - time) * 24 * Math.abs(rate) / 1000) * Math.sign(rate);
            const crossed = rate < 0 ? raw < start : raw >= end;
            if (loop && length > 0 && crossed) return {frame: start + ((raw - start) % length + length) % length, stop: false};
            return {frame: Math.max(start, Math.min(end, raw)), stop: length <= 0 || crossed || (rate < 0 && raw === start && !loop)};
        },
    };
}
const snapStates = new WeakMap();
export const setSnapping = (state, value) => snapStates.set(state, !!value);
export const snappingEnabled = (state, event) => (snapStates.get(state) ?? true) !== !!event?.altKey;
export function transportSnapPoints(state, points, event) {
    return snappingEnabled(state, event) ? [...new Set([...points, ...(state.record.markers || []).map(marker => marker.frame)])] : [];
}
