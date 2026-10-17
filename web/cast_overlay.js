// Shows @mentions of characters in the accent colour. The textarea keeps the text; while a mention is present a copy of
// the text (same font, wrapping and scroll) is drawn over it and the textarea's own glyphs are made transparent.
import {mentionRanges} from './cast_mention.js';

const COPIED = ['fontFamily', 'fontSize', 'fontWeight', 'fontStyle', 'fontVariantLigatures', 'letterSpacing', 'wordSpacing', 'lineHeight',
    'textIndent', 'textTransform', 'tabSize', 'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft', 'direction'];

// `people()` lists the characters present in the media list: [{name}].
export function attachMentionOverlay(input, {people}) {
    const mirror = document.createElement('div'), inner = document.createElement('div');
    mirror.className = 'fv-mention-mirror'; mirror.setAttribute('aria-hidden', 'true'); mirror.hidden = true;
    mirror.append(inner);
    input.parentNode.insertBefore(mirror, input.nextSibling);
    let composing = false, disposed = false;
    function layout() {
        const style = getComputedStyle(input);
        for (const name of COPIED) mirror.style[name] = style[name];
        Object.assign(mirror.style, {left: `${input.offsetLeft + input.clientLeft}px`, top: `${input.offsetTop + input.clientTop}px`,
            width: `${input.clientWidth}px`, height: `${input.clientHeight}px`, opacity: input.disabled ? '.5' : ''});
        inner.style.transform = `translateY(${-input.scrollTop}px)`;
    }
    function refresh() {
        if (disposed) return;
        const text = input.value, ranges = composing ? [] : mentionRanges(text, people());
        const live = ranges.length > 0;
        input.classList.toggle('fv-mention-live', live); mirror.hidden = !live;
        if (!live) { inner.replaceChildren(); return; }
        const parts = []; let at = 0;
        for (const range of ranges) {
            if (range.start > at) parts.push(document.createTextNode(text.slice(at, range.start)));
            const name = document.createElement('span'); name.className = 'fv-mention'; name.textContent = text.slice(range.start, range.end);
            parts.push(name); at = range.end;
        }
        parts.push(document.createTextNode(text.slice(at) + (text.endsWith('\n') ? '​' : '')));
        inner.replaceChildren(...parts);
        layout();
    }
    const listeners = [], listen = (target, name, fn) => { target.addEventListener(name, fn); listeners.push(() => target.removeEventListener(name, fn)); };
    listen(input, 'input', refresh);
    listen(input, 'scroll', () => { if (!mirror.hidden) inner.style.transform = `translateY(${-input.scrollTop}px)`; });
    listen(input, 'compositionstart', () => { composing = true; refresh(); });
    listen(input, 'compositionend', () => { composing = false; refresh(); });
    listen(window, 'resize', () => { if (!mirror.hidden) layout(); });
    const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(() => { if (!mirror.hidden) layout(); }) : null;
    observer?.observe(input);
    return {refresh, dispose() { disposed = true; observer?.disconnect(); listeners.forEach(stop => stop()); mirror.remove(); input.classList.remove('fv-mention-live'); }};
}
