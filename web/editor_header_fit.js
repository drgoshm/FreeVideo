// The view switch stays centred in the editor's header while the project name next to the logo stays readable.
// When it would not, the logo first takes its phone size, then gives its place to the name; if that is still too tight, the switch moves to a second row.
// The feedback entry leaves before the switch moves down, and on two rows when the name is still tight.
// About six Chinese characters or ten Latin letters of the name itself, without the menu arrow.
const READABLE = 120, COMPACT_MARK = 90;

export function fitEditorHeader(header, brand, title, navigation, actions) {
    if (typeof ResizeObserver !== 'function' || typeof getComputedStyle !== 'function' || typeof matchMedia !== 'function') return {check() {}, dispose() {}};
    const px = value => parseFloat(value) || 0;
    function check() {
        const span = title.firstElementChild, mark = brand.firstElementChild;
        if (!header.isConnected || !span || !header.clientWidth) return;
        // Measure the plain layout, so the result never depends on the one applied before.
        delete header.dataset.fit;
        const box = getComputedStyle(header), gap = px(box.columnGap), inner = header.clientWidth - px(box.paddingLeft) - px(box.paddingRight);
        const side = (inner - navigation.offsetWidth - 2 * gap) / 2, spacing = px(getComputedStyle(brand).columnGap);
        const own = getComputedStyle(title), chevron = span.nextElementSibling, text = document.createRange(); text.selectNodeContents(span);
        const frame = px(own.paddingLeft) + px(own.paddingRight) + px(own.borderLeftWidth) + px(own.borderRightWidth) + (chevron ? chevron.getBoundingClientRect().width + px(own.columnGap) : 0);
        const wanted = Math.min(READABLE, text.getBoundingClientRect().width) + frame;
        // The actions' own width: the box itself may already be squeezed below its buttons.
        const shown = [...actions.children].filter(child => child.offsetWidth > 0), apart = px(getComputedStyle(actions).columnGap);
        const need = Math.max(actions.offsetWidth, shown.reduce((sum, child) => sum + child.getBoundingClientRect().width, 0) + apart * Math.max(0, shown.length - 1));
        const fits = markWidth => side >= need && side - markWidth - spacing >= wanted;
        const compact = Math.min(COMPACT_MARK, mark.offsetWidth);
        // On two rows the name shares the first row with the actions only; the logo shrinks too when that is still tight.
        const row = markWidth => inner - need - gap - markWidth - spacing;
        // The feedback entry is the first action to leave this header (it stays in the other views).
        const feedback = actions.querySelector('.fv-feedback-entry'), spare = feedback ? feedback.offsetWidth + apart : 0;
        // From 681 px up the switch has one place in every view, so the logo, then the feedback entry, leave before the switch moves down.
        const wide = !matchMedia('(max-width:680px)').matches, bare = room => wide && side >= room && side >= wanted;
        const fit = fits(mark.offsetWidth) ? '' : fits(compact) ? 'compact' : bare(need) ? 'bare' : spare && bare(need - spare) ? 'bare lean'
            : row(mark.offsetWidth) >= wanted ? 'wrap' : row(compact) >= wanted || !spare ? 'wrap compact' : 'wrap compact lean';
        if (fit) header.dataset.fit = fit;
    }
    const observer = new ResizeObserver(check);
    observer.observe(header); observer.observe(navigation); observer.observe(actions);
    return {check, dispose: () => observer.disconnect()};
}
