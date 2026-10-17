const keyEvents = ['keydown', 'keypress', 'keyup', 'copy', 'cut', 'paste'];
const localKeys = new WeakMap();
const isolatedDialogs = new Set();
export const topDialog = () => Array.from(document.querySelectorAll('dialog[open]')).at(-1);

// Document capture shortcuts run before controls inside a modal. Route the
// original event locally, keeping its browser default and trusted state intact.
export function isolateDialogKeys(element, route) {
    const stray = event => {
        if (!element.open) return;
        const inside = element.contains(event.target);
        // Tab can leave focus on body while another modal is still above us.
        // Keep its native cancel default, but don't send a stray key to the host.
        const upper = topDialog();
        if (upper && upper !== element) {
            if (isolatedDialogs.has(upper)) return;
            event.stopImmediatePropagation();
            routeDialogKey(event, upper);
            return;
        }
        event.stopImmediatePropagation();
        route(event, inside);
    };
    isolatedDialogs.add(element);
    for (const type of keyEvents) window.addEventListener(type, stray, true);
    return () => {
        isolatedDialogs.delete(element);
        for (const type of keyEvents) window.removeEventListener(type, stray, true);
    };
}

// Register local handlers for both normal DOM delivery and captured modal keys.
export function listenDialogKey(target, type, handler, options) {
    let listeners = localKeys.get(target);
    if (!listeners) { listeners = []; localKeys.set(target, listeners); }
    const row = {type, handler, capture: options === true || Boolean(options?.capture)};
    listeners.push(row); target.addEventListener(type, handler, options);
    return () => {
        target.removeEventListener(type, handler, options);
        const index = listeners.indexOf(row); if (index >= 0) listeners.splice(index, 1);
    };
}

export function routeDialogKey(event, element) {
    if (!element.contains(event.target)) return;
    const path = [];
    for (let target = event.target; target; target = target.parentNode) {
        path.push(target); if (target === element) break;
    }
    let current, phase, stopped = false, immediate = false;
    // The window gate has stopped native propagation. Local propagation still
    // needs its own flags, currentTarget and phase for nested control handlers.
    const local = new Proxy(event, {
        get(target, name) {
            if (name === 'currentTarget') return current;
            if (name === 'eventPhase') return phase;
            if (name === 'cancelBubble') return stopped;
            if (name === 'stopPropagation') return () => { stopped = true; };
            if (name === 'stopImmediatePropagation') return () => { stopped = immediate = true; };
            const value = Reflect.get(target, name, target);
            return typeof value === 'function' ? value.bind(target) : value;
        },
        set(target, name, value) {
            if (name === 'cancelBubble') { if (value) stopped = true; return true; }
            return Reflect.set(target, name, value, target);
        },
    });
    const deliver = (target, capture) => {
        current = target; phase = target === event.target ? 2 : capture ? 1 : 3;
        if (!capture && target['on' + event.type]?.call(target, local) === false) event.preventDefault();
        for (const row of [...localKeys.get(target) || []]) {
            if (immediate) break;
            if (row.type === event.type && row.capture === capture) row.handler.call(target, local);
        }
    };
    for (const target of [...path].reverse()) { deliver(target, true); if (stopped) return; }
    for (const target of path) { deliver(target, false); if (stopped) break; }
    // Built-in media controls listen inside a browser-owned shadow tree, which
    // the window gate cannot reach. Keep their focused Space action local too.
    const media = event.target;
    if (event.type === 'keydown' && event.key === ' ' && !event.defaultPrevented
        && !event.ctrlKey && !event.metaKey && !event.altKey
        && media.controls && ['VIDEO', 'AUDIO'].includes(media.tagName)) {
        event.preventDefault();
        if (!event.repeat) { if (media.paused) media.play().catch(() => {}); else media.pause(); }
    }
}
