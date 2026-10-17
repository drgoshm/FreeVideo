import {isolateDialogKeys, listenDialogKey, routeDialogKey} from './dialog_keys.js';

let style;
function loadStyle() {
    if (style) return;
    const href = new URL('editor_expand.css', import.meta.url).href;
    style = [...document.querySelectorAll('link')].find(link => link.href === href);
    if (style) return;
    style = document.createElement('link');
    style.rel = 'stylesheet'; style.href = href;
    document.head.append(style);
}
const el = (tag, cls, text) => {
    const node = document.createElement(tag); node.className = cls;
    if (text != null) node.textContent = text;
    return node;
};

// The supplied textarea owns the text. Both views use its input handler; callers
// changing its value or generation policy programmatically send input or change.
export function createExpandButton({t = en => en, textarea, onGenerate, canGenerate, onDraft = () => {}}) {
    loadStyle();
    const element = el('button', 'fv-ed-expand'); element.type = 'button';
    element.title = t('Expand', '放大'); element.setAttribute('aria-label', element.title);
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('aria-hidden', 'true');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', 'M14 4h6v6M20 4l-7 7M10 20H4v-6M4 20l7-7'); svg.append(path); element.append(svg);
    let reading = {text: null, top: 0, from: 0, to: 0}, dialog = null, large = null, generate = null, notice = null, seen = '', pending = false, disposed = false, cleanups = [];
    const writable = () => !textarea.disabled && !textarea.readOnly;
    const allowed = () => writable() && !pending && !!canGenerate();
    function sync() {
        if (disposed) return;
        element.disabled = !writable();
        if (!dialog) return;
        // The small box changed from outside (another part selected, text restored): the dialog follows.
        if (textarea.value !== seen) { seen = textarea.value; large.value = seen; }
        large.disabled = !writable(); if (generate) generate.disabled = !allowed();
    }
    async function submit() {
        if (!onGenerate || !allowed()) return;
        commit(); notice.textContent = '';
        pending = true; sync();
        let reason = '';
        try { reason = await onGenerate(); }
        finally {
            pending = false;
            // The editor's own reason sits under this dialog, so it is said here too.
            if (notice) { notice.textContent = typeof reason === 'string' ? reason : ''; if (notice.textContent && !large.value.trim()) large.focus(); }
            sync();
        }
    }
    function close() {
        if (!dialog) return;
        commit(); reading = {text: large.value, top: large.scrollTop, from: textarea.selectionStart, to: textarea.selectionEnd};
        const current = dialog; dialog = large = generate = notice = null;
        cleanups.splice(0).forEach(stop => stop());
        current.oncancel = current.onclose = null;
        if (current.open) current.close(); current.remove();
        if (element.isConnected) element.focus({preventScroll: true});
    }
    // The dialog's text reaches the small box as one native edit that leaves its earlier undo history alone;
    // the small box is only written to when the dialog is submitted or closed.
    function commit() {
        if (!dialog || !textarea.isConnected) return;
        if (large.value === textarea.value) { textarea.setSelectionRange?.(large.selectionStart, large.selectionEnd); return; }
        if (!writable()) return;
        const typed = large.value, shown = dialog.open, [from, to] = [large.selectionStart, large.selectionEnd];
        // A modal dialog makes the page behind it inert, so the small box is only editable while it is closed.
        if (shown) dialog.close();
        textarea.focus({preventScroll: true}); textarea.select?.();
        const done = typed ? document.execCommand?.('insertText', false, typed) : document.execCommand?.('delete');
        if (!done || textarea.value !== typed) { textarea.value = typed; textarea.dispatchEvent(new Event('input', {bubbles: true})); }
        textarea.setSelectionRange?.(from, to);
        seen = textarea.value;
        if (shown) { dialog.showModal(); large.focus(); large.setSelectionRange?.(from, to); }
    }
    function open() {
        sync();
        if (disposed || element.disabled || dialog) return;
        dialog = el('dialog', 'fv-ed-expand-dialog');
        dialog.setAttribute('role', 'dialog'); dialog.setAttribute('aria-modal', 'true');
        dialog.setAttribute('aria-label', t('Expand', '放大'));
        seen = textarea.value;
        large = el('textarea', 'fv-ed-expand-text'); large.value = seen;
        if (textarea.maxLength >= 0) large.maxLength = textarea.maxLength;
        large.setAttribute('aria-label', textarea.getAttribute('aria-label') || t('Expand', '放大'));
        notice = el('div', 'fv-ed-expand-note'); notice.setAttribute('role', 'status');
        large.oninput = () => {
            notice.textContent = '';
            // The editor's draft follows every keystroke; the small box itself is written at close or generate.
            if (writable()) onDraft(large.value);
            sync();
        };
        const buttons = el('div', 'fv-ed-expand-buttons');
        // Without a way to generate (a single part cannot be generated alone) only Close is offered.
        if (onGenerate) { generate = el('button', 'fv-primary', t('Generate', '生成')); generate.type = 'button'; generate.onclick = submit; }
        const dismiss = el('button', '', t('Close', '关闭')); dismiss.type = 'button'; dismiss.onclick = close;
        if (generate) buttons.append(generate); buttons.append(dismiss); dialog.append(large, notice, buttons);
        cleanups.push(listenDialogKey(dialog, 'keydown', event => {
            if (event.isComposing || event.keyCode === 229) return;
            if (event.key === 'Escape') { event.preventDefault(); close(); }
            else if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
                event.preventDefault(); if (!event.repeat) submit();
            } else if (event.key === 'Tab') {
                event.preventDefault();
                const controls = [large, generate, dismiss].filter(node => node && !node.disabled);
                const index = controls.indexOf(document.activeElement);
                controls[(index + (event.shiftKey ? -1 : 1) + controls.length) % controls.length]?.focus();
            }
        }));
        cleanups.push(isolateDialogKeys(dialog, event => routeDialogKey(event, dialog)));
        dialog.oncancel = event => { event.preventDefault(); event.stopPropagation(); close(); };
        dialog.onclose = () => { if (dialog && !dialog.open) close(); };
        const parent = textarea.closest('dialog') || document.body;
        parent.addEventListener('close', close); cleanups.push(() => parent.removeEventListener('close', close));
        // A click on the backdrop never takes focus from the text.
        dialog.addEventListener('mousedown', event => { if (event.target === dialog) { event.preventDefault(); large.focus(); } });
        const stop = event => event.stopPropagation();
        for (const type of ['pointerdown', 'pointerup', 'pointermove', 'mousedown', 'mouseup', 'mousemove', 'click', 'dblclick', 'wheel', 'contextmenu']) dialog.addEventListener(type, stop);
        parent.append(dialog); sync(); dialog.showModal();
        // Without a remembered reading position the caret line is centred; focusing alone would jump to the end.
        const from = textarea.selectionStart ?? large.value.length, to = textarea.selectionEnd ?? large.value.length;
        if (reading.text === large.value && reading.from === from && reading.to === to) { large.focus(); large.setSelectionRange?.(from, to); large.scrollTop = reading.top; }
        else {
            const full = large.value; large.value = full.slice(0, to);
            large.scrollTop = 0; const above = large.scrollHeight, view = large.clientHeight; large.value = full;
            const top = above <= view ? 0 : above - view / 2;
            large.focus(); large.setSelectionRange?.(from, to); large.scrollTop = top;
        }
    }
    element.onclick = open;
    textarea.addEventListener('input', sync); textarea.addEventListener('change', sync);
    const observer = typeof MutationObserver === 'function' ? new MutationObserver(sync) : null;
    observer?.observe(textarea, {attributes: true, attributeFilter: ['disabled', 'readonly']});
    sync();
    return {element, open, close, commit, get isOpen() { return !!dialog; }, dispose() {
        close(); disposed = true; observer?.disconnect();
        textarea.removeEventListener('input', sync); textarea.removeEventListener('change', sync);
        element.onclick = null; element.remove();
    }};
}
