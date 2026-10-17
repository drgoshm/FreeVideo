// Where the caret of a textarea is on screen, for popups that should sit under the line being typed.
const COPIED = ['boxSizing', 'borderTopWidth', 'borderRightWidth', 'borderBottomWidth', 'borderLeftWidth',
    'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft', 'fontFamily', 'fontSize', 'fontStyle', 'fontWeight',
    'fontVariant', 'letterSpacing', 'lineHeight', 'textTransform', 'textIndent', 'tabSize', 'wordSpacing', 'direction'];
export function caretRect(input, index = input.selectionStart) {
    const box = input.getBoundingClientRect(), style = getComputedStyle(input);
    const mirror = document.createElement('div');
    for (const name of COPIED) mirror.style[name] = style[name];
    Object.assign(mirror.style, {position: 'fixed', left: '-9999px', top: '0', visibility: 'hidden',
        whiteSpace: 'pre-wrap', overflowWrap: 'break-word', width: `${input.clientWidth + parseFloat(style.borderLeftWidth) + parseFloat(style.borderRightWidth)}px`,
        height: 'auto', overflow: 'hidden'});
    mirror.textContent = String(input.value).slice(0, index);
    const marker = document.createElement('span'); marker.textContent = '​';
    mirror.append(marker); document.body.append(mirror);
    const top = marker.offsetTop, left = marker.offsetLeft, height = marker.offsetHeight || parseFloat(style.fontSize) * 1.3;
    mirror.remove();
    const border = parseFloat(style.borderTopWidth) || 0;
    // The line the caret is on, clipped to the visible part of the box when the text is scrolled.
    const lineTop = Math.min(Math.max(box.top + border + top - input.scrollTop, box.top), box.bottom - height);
    return {left: box.left + (parseFloat(style.borderLeftWidth) || 0) + left, top: lineTop, bottom: lineTop + height};
}
