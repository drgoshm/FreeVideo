// "Replace audio" is a command, never the side effect of a drop. Once it is chosen
// (context menu or the Audio tab) the next media pick or drop on that piece replaces it.
// Leaving the mode without a pick changes nothing and leaves no history.
import * as model from './editor_model.js';
const modes = new WeakMap();
export function replaceMode(state) {
    if (!modes.has(state)) {
        const listeners = new Set(); let id = null;
        const announce = () => { for (const listener of [...listeners]) listener(); };
        const mode = {listeners, pick: null, get id() { return id; },
            start(next) { if (id !== next) { id = next; announce(); } },
            cancel() { if (id === null) return false; id = null; announce(); return true; }};
        modes.set(state, mode);
        // Choosing anything else, or losing the piece, ends the mode without an edit.
        state.subscribe(event => {
            if (id === null) return;
            if (event.type === 'selection' || event.type === 'record' && !model.find(state.record, id)) mode.cancel();
        });
    }
    return modes.get(state);
}
const payloadOf = (key, metadata) => {
    const separator = key?.indexOf(':');
    if (!(separator > 0) || !metadata?.has_audio) return null;
    return {source: {kind: key.slice(0, separator), id: key.slice(separator + 1)}, frames: metadata.frames, hasAudio: true, hasVideo: false};
};
// The library cards keep their own gestures; while the mode is on, "add" gestures pick instead.
export function attachReplacePicker({state, library, store, actions, t, error = () => {}}) {
    const mode = replaceMode(state);
    async function pick(key) {
        const id = mode.id;
        await store.getSources([key]);
        if (mode.id !== id || id === null) return;
        const metadata = store.sources.get(key), payload = payloadOf(key, metadata);
        // A pick that cannot replace says why; the mode stays on for another pick.
        if (!payload) { error(metadata && !metadata._fallback ? {reason: 'no_audio'} : new Error('source')); return; }
        if (actions.replaceAudio(id, payload).ok !== false) mode.cancel();
    }
    const choose = event => {
        if (mode.id === null) return;
        const button = event.target.closest?.('.fv-ed-source-card')?.querySelector('.fv-ed-source');
        if (!button || button.disabled) return;
        event.preventDefault(); event.stopImmediatePropagation();
        pick(button.dataset.key).catch(value => error(value));
    };
    const onClick = event => { if (event.target.closest?.('.fv-ed-source-add')) choose(event); };
    // The keys a card answers to: Enter on the card, Enter or Space on its add button.
    mode.pick = event => {
        const card = event.key === 'Enter' && event.target.closest?.('.fv-ed-source') || event.target.closest?.('.fv-ed-source-add');
        if (mode.id === null || !card) return false;
        choose(event); return true;
    };
    const paint = () => {
        library.dataset.picking = String(mode.id !== null);
        if (mode.id !== null) library.dataset.open = 'true';
    };
    library.addEventListener('dblclick', choose, true); library.addEventListener('click', onClick, true);
    mode.listeners.add(paint);
    return () => {
        library.removeEventListener('dblclick', choose, true); library.removeEventListener('click', onClick, true);
        mode.listeners.delete(paint); mode.cancel(); mode.pick = null; delete library.dataset.picking;
    };
}
