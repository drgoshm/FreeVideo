// Short sentences that appear where the user is looking: at the mouse for a refused crossfade length.
let pointer = {x: 0, y: 0}, tracking = false, note = null, timer = null;
function track() {
    if (tracking || !globalThis.document?.addEventListener) return;
    tracking = true;
    document.addEventListener('pointermove', event => { pointer = {x: event.clientX ?? 0, y: event.clientY ?? 0}; }, true);
    document.addEventListener('pointerdown', event => { pointer = {x: event.clientX ?? 0, y: event.clientY ?? 0}; }, true);
}
// Seconds for a sentence: rounded down so the text never promises more than the material holds.
export function seconds(frames, fps = 24) {
    const value = frames / fps, tenths = Math.floor(value * 10) / 10;
    return String(tenths > 0 ? tenths : Math.max(0.01, Math.floor(value * 100) / 100));
}
export function startPointerNotes() { track(); }
// The editor is a modal dialog (top layer): the note must live inside it or it is painted behind it.
export function noteAtPointer(text, host = null) {
    track();
    if (!globalThis.document?.body) return null;
    if (!note) { note = document.createElement('div'); note.className = 'fv-ed-audio-pointer-note'; note.setAttribute('role', 'status'); }
    note.textContent = text; note.hidden = false;
    note.style.left = `${pointer.x + 14}px`; note.style.top = `${pointer.y + 18}px`;
    const parent = host?.closest?.('dialog') || document.body;
    if (note.parentNode !== parent) parent.append(note);
    clearTimeout(timer); timer = setTimeout(() => { if (note) note.hidden = true; }, 2200);
    return note;
}
