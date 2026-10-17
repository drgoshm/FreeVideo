const FILE = '/userdata/' + encodeURIComponent('freevideo/guide.json');
const attempted = new Set();
let current = null;
const guides = new Map();
const recordWrites = new WeakMap();

export const REPLAY_TEXT = {
    title: ['Getting started', '新手指引'],
    again: ['Show again', '再看一遍'],
    waiting: ['Waiting for the workspace to be ready. The guide will start automatically.', '正在等待创作面板就绪，随后会自动开始指引。'],
    cancel: ['Cancel', '取消'],
    unavailable: ['Open a creation workflow to view the guide.', '请先打开创作工作流，再查看指引。'],
};

export const CHAPTERS = [
    {id: 'describe', anchor: '[data-fv-guide="describe"]', added: false,
        en: 'Type your prompt here. You can have an agent follow the Skill at the top right, or turn on FreeToken enhancement.',
        zh: '在这里输入提示词。可以让 Agent 参⁠考 Skill，或开启 FreeToken 提示词增强。'},
    {id: 'range', anchor: '[data-fv-guide="range"]', added: true,
        en: 'See the available sizes and lengths here, and how long each takes.', zh: '在这里查看可生成的尺\u2060寸、时长和预计用时。'},
    {id: 'generate', anchor: '[data-fv-guide="generate"]', added: false,
        en: 'Choose "Generate video". Progress and the time left appear in the preview.', zh: '点“生成视频”，进度和剩\u2060余\u2060时\u2060间会显示在预览区。'},
    {id: 'longer', anchor: '[data-fv-guide="longer"]', added: true,
        en: 'For a longer video, or to change part of one, choose "Edit".', zh: '要做更长的视频，或者修\u2060改\u2060其\u2060中\u2060一\u2060段，点“剪辑视图”。'},
    {id: 'agent', anchor: '[data-fv-guide="agent"]', added: true,
        en: 'To have an AI agent such as Claude Code or Codex make videos for you, choose "Autopilot".', zh: '要让 Claude Code、Codex 等 Agent 替你生\u2060成\u2060视\u2060频，点“AI 全自动”。'},
    {id: 'editor-timeline', group: 'editor', anchor: '[data-fv-guide="editor-timeline"]', added: true,
        en: 'Drag a video here.', zh: '把视频拖到这里。'},
    {id: 'editor-trim', group: 'editor', anchor: '[data-fv-guide="editor-trim"]', added: true,
        en: 'Drag the right edge to make this part longer.', zh: '拖右边缘，把这一段拉长。'},
    {id: 'editor-empty', group: 'editor', anchor: '[data-fv-guide="editor-empty"]', added: true,
        en: 'Drag in an empty spot to add a new part.', zh: '在空白处拖出新的一段。'},
    {id: 'editor-describe', group: 'editor', anchor: '[data-fv-guide="editor-describe"]',
        anchors: ['[data-fv-guide="editor-describe"]', '[data-fv-guide="editor-generate"]'], added: true,
        en: 'Describe it, then choose "Generate".', zh: '写一句描述，再点“\u2060生\u2060成\u2060”\u2060。'},
];

const chaptersFor = group => CHAPTERS.filter(c => (c.group || 'studio') === group);

export function guideSteps({record, available = [], returning = false, blockers = {}, all = false, group = 'studio'}) {
    if (Object.values(blockers).some(Boolean)) return [];
    return chaptersFor(group).filter(chapter => available.includes(chapter.id)
        && (all || (!record?.seen.includes(chapter.id) && (record || !returning || chapter.added))));
}

export function guideRecord(record, ids) {
    return {schema: 1, seen: [...new Set([...(record?.seen || []), ...ids])]};
}

export async function readGuideRecord(api) {
    try {
        // Switching workspaces can race a slow acknowledgement from the old
        // guide. Read after its writes so the next group retains those ids.
        await recordWrites.get(api);
        const reply = await api.fetchApi(FILE, {cache: 'no-store'});
        if (reply.status === 404) return {ok: true, record: null};
        if (!reply.ok) return {ok: false};
        const record = await reply.json();
        if (record?.schema !== 1 || !Array.isArray(record.seen) || !record.seen.every(id => typeof id === 'string')) return {ok: false};
        return {ok: true, record};
    } catch { return {ok: false}; }
}

export function saveGuideRecord(api, record) {
    const pending = (recordWrites.get(api) || Promise.resolve()).then(async () => {
        try {
            return (await api.fetchApi(FILE + '?overwrite=true', {method: 'POST', body: JSON.stringify(record), keepalive: true})).ok;
        } catch { return false; }
    });
    recordWrites.set(api, pending);
    return pending;
}

// An empty record checks write access before drawing anything. Never infer a
// new installation from an unreadable library, or consume an absent chapter.
export async function prepareGuide(api, available, group = 'studio') {
    const loaded = await readGuideRecord(api);
    if (!loaded.ok) return null;
    let record = loaded.record, returning = false;
    if (!record) {
        try {
            const reply = await api.fetchApi('/freevideo/library?limit=1');
            if (!reply.ok) return null;
            const data = await reply.json();
            if (!Array.isArray(data.items)) return null;
            returning = data.items.length > 0;
        } catch { return null; }
    }
    const steps = guideSteps({record, available, returning, group});
    record = guideRecord(record, returning ? chaptersFor(group).filter(c => !c.added && available.includes(c.id)).map(c => c.id) : []);
    if (!steps.length) {
        if (returning) await saveGuideRecord(api, record);
        return null;
    }
    if (!await saveGuideRecord(api, record)) return null;
    return {record, steps};
}

export function showGuide({all = true} = {}) {
    current = [...document.querySelectorAll('dialog[open]')].map(dialog => guides.get(dialog)).filter(Boolean).at(-1) || current;
    return (all ? current?.replay() : current?.show()) || Promise.resolve(false);
}

const el = (tag, text, cls) => { const e = document.createElement(tag); if (text != null) e.textContent = text; if (cls) e.className = cls; return e; };
const visible = e => {
    if (!e?.isConnected || e.closest('[hidden], [inert]') || !e.getClientRects().length) return false;
    for (let parent = e; parent; parent = parent.parentElement) {
        const style = getComputedStyle(parent);
        if (style.display === 'none' || style.visibility === 'hidden') return false;
    }
    return true;
};
const clamp = (n, low, high) => Math.max(low, Math.min(high, n));

export function createGuideReplayEntry({dialog, cn, openWorkspace, replay = showGuide}) {
    const text = key => REPLAY_TEXT[key][cn ? 1 : 0];
    const section = el('section', null, 'fv-guide-replay'), row = el('div', null, 'fv-guide-replay-row');
    const button = el('button', text('again')); button.type = 'button';
    const status = el('div', null, 'fv-muted'); status.setAttribute('role', 'status'); status.hidden = true;
    row.append(el('h3', text('title')), button); section.append(row, status);
    button.onclick = async () => {
        button.disabled = true;
        try {
            if (!await openWorkspace()) { status.textContent = text('unavailable'); status.hidden = false; return; }
            // Wait for native focus restoration before focusing the guide.
            await new Promise(resolve => {
                dialog.addEventListener('close', resolve, {once: true});
                dialog.close();
            });
            await replay({all: true});
        } catch { status.textContent = text('unavailable'); status.hidden = false; }
        finally { button.disabled = false; }
    };
    return section;
}

// Custom targets return viewport rectangles on every layout pass. They never
// insert hit targets or change the editor's pointer handling.
export function guideTarget(dialog, chapter, targets = {}) {
    if (targets[chapter.id]) return targets[chapter.id]();
    const nodes = (chapter.anchors || [chapter.anchor]).map(selector => dialog.querySelector(selector));
    if (nodes.some(node => !visible(node))) return null;
    const rects = nodes.map(node => node.getBoundingClientRect());
    const left = Math.min(...rects.map(r => r.left)), top = Math.min(...rects.map(r => r.top));
    const right = Math.max(...rects.map(r => r.right)), bottom = Math.max(...rects.map(r => r.bottom));
    return {node: nodes[0], rect: {left, top, right, bottom, width: right - left, height: bottom - top}};
}

export function createFirstRunGuide({dialog, api, cn, isBusy = () => false, group = 'studio', targets = {}}) {
    const t = (en, zh) => cn ? zh : en;
    const automatic = !attempted.has(group), editor = group === 'editor';
    const chapters = chaptersFor(group);
    if (!document.querySelector('link[data-fv-first-run-guide]')) {
        const css = el('link'); css.rel = 'stylesheet'; css.dataset.fvFirstRunGuide = '';
        css.href = new URL('./first_run_guide.css', import.meta.url).href; document.head.append(css);
    }
    let disposed = false, touched = false, stopped = false, layer = null, bubble = null, ring = null, arrow = null;
    let steps = [], index = 0, record = null, manual = false, restoreFocus = null, pollTimer = null, frame = null;
    let serverBlocked = false, checking = false, preparing = false;
    let waiting = null, replayTimer = null;
    const heldKeys = new Set();
    const target = chapter => dialog.querySelector(chapter.anchor);
    const resolved = chapter => {
        const value = guideTarget(dialog, chapter, targets);
        return value && visible(value.node) && value.rect.width > 0 && value.rect.height > 0 ? value : null;
    };
    const available = () => chapters.filter(c => resolved(c)).map(c => c.id);
    function blocked() {
        return !dialog.open || !dialog.isConnected || dialog.inert || serverBlocked || isBusy()
            // Create stays open underneath Edit. It is an inactive workspace,
            // while settings, export options and other dialogs still block.
            || [...document.querySelectorAll('dialog[open]')].some(e => e !== dialog
                && !(editor && e.matches('dialog.fv-studio:not(.fv-editor)')))
            || [...document.querySelectorAll('[role="dialog"], [aria-modal="true"], .p-dialog-mask, .p-overlay-mask:not(.p-blockui-mask)')]
                .some(e => e !== dialog && !layer?.contains(e) && visible(e))
            || [...document.querySelectorAll('.fv-failure, .fv-health, .fv-update-notice, .fv-prewarm, [data-fv-guide-blocker]')]
                .some(e => visible(e) && (!e.matches('.fv-prewarm') || e.textContent.trim()));
    }
    async function serverState() {
        try {
            const replies = await Promise.all([api.fetchApi('/queue'), api.fetchApi('/freevideo/setup')]);
            if (replies.some(r => !r.ok)) return false;
            const [queue, setup] = await Promise.all(replies.map(r => r.json()));
            return Array.isArray(queue.queue_running) && Array.isArray(queue.queue_pending)
                && !queue.queue_running.length && !queue.queue_pending.length
                && setup.discovery?.ready === true && !setup.task?.busy;
        } catch { return false; }
    }
    function hide(restore = true) {
        const focused = layer?.contains(document.activeElement);
        layer?.remove(); layer = bubble = ring = arrow = null;
        if (restore && focused && restoreFocus?.isConnected) restoreFocus.focus({preventScroll: true});
    }
    function interrupt() { if (automatic && dialog.open) attempted.add(group); stopped = true; clearTimeout(pollTimer); hide(); }
    function remember() {
        if (manual) return;
        // Empty projects acknowledge the optional trim chapter too, so importing
        // later cannot turn a completed three-step guide into a second prompt.
        record = guideRecord(record, editor ? chapters.map(c => c.id) : steps.filter(c => resolved(c)).map(c => c.id));
        // A failed write stays quiet. This page's single attempt is already used.
        saveGuideRecord(api, record).then(ok => { if (!ok && !disposed) interrupt(); });
    }
    function finish() { remember(); stopped = true; clearTimeout(pollTimer); hide(); }
    function position() {
        if (!layer) return;
        if (blocked()) { interrupt(); return; }
        let value = resolved(steps[index]);
        if (value?.reveal) { value.reveal(); value = resolved(steps[index]); }
        if (!value) {
            steps.splice(index, 1);
            if (!steps.length) interrupt();
            else { index = Math.min(index, steps.length - 1); draw(); }
            return;
        }
        const {node, rect: raw} = value, bounds = dialog.getBoundingClientRect();
        const margin = editor ? 12 : 24;
        const xmin = Math.max(margin, bounds.left + 12), ymin = Math.max(margin, bounds.top + 12);
        const xmax = Math.min(innerWidth - margin, bounds.right - 12), ymax = Math.min(innerHeight - margin, bounds.bottom - 12);
        const r = editor ? {left: Math.max(0, raw.left), top: Math.max(0, raw.top),
            right: Math.min(innerWidth, raw.right), bottom: Math.min(innerHeight, raw.bottom)} : raw;
        r.width = r.right - r.left; r.height = r.bottom - r.top;
        if (r.width <= 0 || r.height <= 0) { interrupt(); return; }
        bubble.style.width = `${Math.min(340, xmax - xmin)}px`;
        const b = bubble.getBoundingClientRect(), cx = r.left + r.width / 2, cy = r.top + r.height / 2;
        const x = clamp(cx - b.width / 2, xmin, xmax - b.width), y = clamp(cy - b.height / 2, ymin, ymax - b.height);
        const below = {side: 'below', x, y: r.bottom + 24}, above = {side: 'above', x, y: r.top - b.height - 24};
        const narrow = innerWidth <= 680;
        const candidates = editor
            ? [{...above, x: steps[index].id === 'editor-describe' ? clamp(r.left, xmin, xmax - b.width) : x}]
            : steps[index].id === 'longer'
            ? [{side: 'below', x: clamp(r.right - b.width, xmin, xmax - b.width), y: r.bottom + 24}, above, below]
            : narrow ? [{side: 'below', x, y: ymax - b.height}, above, below]
                : [{side: 'right', x: r.right + 24, y}, {side: 'left', x: r.left - b.width - 24, y}, below, above];
        const fits = c => c.x >= xmin && c.y >= ymin && c.x + b.width <= xmax + .5 && c.y + b.height <= ymax + .5
            && !(c.x < r.right + 8 && c.x + b.width > r.left - 8 && c.y < r.bottom + 8 && c.y + b.height > r.top - 8);
        const place = candidates.find(fits);
        if (!place) { interrupt(); return; }
        const style = getComputedStyle(node);
        const pad = editor ? 0 : 4;
        Object.assign(ring.style, {left: `${r.left - pad}px`, top: `${r.top - pad}px`, width: `${r.width + 2 * pad}px`, height: `${r.height + 2 * pad}px`,
            borderRadius: ['borderTopLeftRadius', 'borderTopRightRadius', 'borderBottomRightRadius', 'borderBottomLeftRadius'].map(n => `${(parseFloat(style[n]) || 0) + pad}px`).join(' ')});
        Object.assign(bubble.style, {left: `${place.x}px`, top: `${place.y}px`}); bubble.dataset.side = place.side;
        arrow.style.left = arrow.style.top = '';
        if (['left', 'right'].includes(place.side)) arrow.style.top = `${clamp(cy - place.y - 6, 20, b.height - 32)}px`;
        else arrow.style.left = `${clamp(cx - place.x - 6, 20, b.width - 32)}px`;
    }
    const follow = () => { if (layer && frame === null) frame = requestAnimationFrame(() => { frame = null; position(); }); };
    function draw() {
        hide(false);
        resolved(steps[index])?.reveal?.();
        while (steps[index] && !resolved(steps[index])) steps.splice(index, 1);
        if (!steps[index]) { interrupt(); return; }
        const {node, rect: r} = resolved(steps[index]), bounds = dialog.getBoundingClientRect();
        const clip = node.closest('.fv-controls, .fv-preview-column')?.getBoundingClientRect() || bounds;
        if (!editor && (innerWidth <= 680 || r.top < clip.top + 8 || r.bottom > clip.bottom - 8))
            node.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
        layer = el('div', null, 'fv-guide-layer'); layer.dataset.chapter = steps[index].id; layer.dataset.group = group;
        ring = el('div', null, 'fv-guide-ring'); ring.setAttribute('aria-hidden', 'true');
        bubble = el('div', null, 'fv-guide-bubble'); bubble.setAttribute('role', 'dialog'); bubble.setAttribute('aria-modal', 'false');
        bubble.setAttribute('aria-label', t('Getting started', '使用指引')); bubble.setAttribute('aria-describedby', 'fv-guide-message');
        const message = el('p', cn ? steps[index].zh : steps[index].en, 'fv-guide-message'); message.id = 'fv-guide-message';
        message.setAttribute('aria-live', 'polite'); message.setAttribute('aria-atomic', 'true');
        const footer = el('div', null, 'fv-guide-footer'), dots = el('div', null, 'fv-guide-dots'), actions = el('div', null, 'fv-guide-actions');
        dots.setAttribute('aria-label', t('Guide progress', '指引进度'));
        steps.forEach((_, i) => { const dot = el('span', null, 'fv-guide-dot' + (i === index ? ' is-current' : '')); if (i === index) dot.setAttribute('aria-current', 'step'); dots.append(dot); });
        if (index < steps.length - 1) {
            const skip = el('button', t('Skip', '跳过'), 'fv-guide-skip'); skip.type = 'button'; skip.onclick = finish; actions.append(skip);
        }
        const next = el('button', index === steps.length - 1 ? t('Got it', '知道了') : t('Next', '下一步'), 'fv-guide-next');
        next.type = 'button'; next.onclick = () => { if (index === steps.length - 1) finish(); else { remember(); index++; draw(); } };
        actions.append(next); footer.append(dots, actions); arrow = el('span', null, 'fv-guide-arrow'); arrow.setAttribute('aria-hidden', 'true');
        bubble.append(message, footer, arrow); layer.append(ring, bubble); dialog.append(layer);
        resizeObserver.disconnect(); resizeObserver.observe(node); resizeObserver.observe(dialog);
        position(); if (layer) next.focus({preventScroll: true});
    }
    async function poll() {
        if (disposed || stopped || checking) return;
        checking = true; serverBlocked = !await serverState(); checking = false;
        if (disposed || stopped) return;
        if (blocked()) interrupt();
        else pollTimer = setTimeout(poll, 1500);
    }
    async function show(all = false, request = null) {
        const cancelled = () => request && waiting !== request;
        if (all || editor) serverBlocked = false;
        if (disposed || preparing || layer || !dialog.open || blocked() || (!all && (touched || stopped))) return false;
        preparing = true; manual = all;
        await document.fonts.ready;
        if (cancelled() || [...document.querySelectorAll('.p-blockui-mask')].some(visible)) { preparing = false; return false; }
        const ready = await serverState();
        if (editor) serverBlocked = !ready;
        let prepared = null;
        if (ready && !disposed && !cancelled() && !blocked() && (all || !touched)) {
            if (all) prepared = {record, steps: guideSteps({available: available(), all: true, group})};
            else prepared = await prepareGuide(api, available(), group);
        }
        preparing = false;
        if (!prepared?.steps.length || disposed || cancelled() || blocked() || (!all && (touched || stopped))) return false;
        stopped = false; serverBlocked = false; record = prepared.record; steps = prepared.steps; index = 0;
        restoreFocus = document.activeElement; draw();
        if (layer) { clearTimeout(pollTimer); pollTimer = setTimeout(poll, 1500); return true; }
        return false;
    }
    function cancelReplay() { clearTimeout(replayTimer); waiting?.remove(); waiting = null; }
    async function retryReplay() {
        const request = waiting;
        if (!request || disposed || !dialog.open) { cancelReplay(); return; }
        const shown = await show(true, request);
        if (waiting !== request || disposed) return;
        if (shown) cancelReplay();
        else replayTimer = setTimeout(retryReplay, 1500);
    }
    async function replay() {
        if (disposed || !dialog.open) return false;
        attempted.add(group); manual = true; clearTimeout(startTimer); interrupt(); cancelReplay();
        waiting = el('div', null, 'fv-guide-wait');
        const message = el('p', t(...REPLAY_TEXT.waiting)); message.setAttribute('role', 'status');
        const cancel = el('button', t(...REPLAY_TEXT.cancel)); cancel.type = 'button'; cancel.onclick = cancelReplay;
        waiting.append(message, cancel); dialog.append(waiting);
        await retryReplay();
        return true;
    }
    const touch = event => {
        if (!dialog.contains(event.target) || layer?.contains(event.target)) return;
        if (automatic) attempted.add(group);
        touched = true;
        if (layer) finish(); else stopped = true;
    };
    const key = event => {
        if (layer) {
            heldKeys.add(event.key); event.stopImmediatePropagation();
            if (event.key === 'Escape' || event.key === 'Enter') {
                event.preventDefault();
                if (event.key === 'Escape') finish(); else bubble.querySelector('.fv-guide-next').click();
            } else if (event.key === 'Tab') {
                event.preventDefault();
                const buttons = [...bubble.querySelectorAll('button')], at = buttons.indexOf(document.activeElement);
                buttons[(at + (event.shiftKey ? -1 : 1) + buttons.length) % buttons.length].focus();
            }
            return;
        }
        if (!dialog.contains(event.target)) return;
        if (!['Tab', 'Shift', 'Control', 'Alt', 'Meta'].includes(event.key)) touch(event);
    };
    const keyEnd = event => {
        if (layer || heldKeys.has(event.key)) event.stopImmediatePropagation();
        if (event.type === 'keyup') heldKeys.delete(event.key);
    };
    const status = event => {
        if (event.detail?.exec_info?.queue_remaining > 0) { serverBlocked = true; if (!editor || layer) interrupt(); }
        else if (layer) poll();
    };
    const execution = () => { serverBlocked = true; if (!editor || layer) interrupt(); };
    const observer = new MutationObserver(rows => {
        if (disposed || stopped) return;
        if (rows.every(row => layer?.contains(row.target))) return;
        if ((!editor || layer) && (blocked() || layer && [...document.querySelectorAll('.p-blockui-mask')].some(visible))) interrupt(); else follow();
    });
    observer.observe(document.body, {subtree: true, childList: true, attributes: true, attributeFilter: ['hidden', 'open', 'class', 'style', 'inert', 'role', 'aria-modal']});
    const resizeObserver = new ResizeObserver(follow);
    document.addEventListener('pointerdown', touch, true); document.addEventListener('click', touch, true);
    document.addEventListener('dragstart', touch, true); document.addEventListener('dragenter', touch, true);
    document.addEventListener('input', touch, true); document.addEventListener('keydown', key, true);
    document.addEventListener('keypress', keyEnd, true); document.addEventListener('keyup', keyEnd, true);
    window.addEventListener('resize', follow); document.addEventListener('scroll', follow, true);
    // The editor can be opened with a bare request helper that has no events.
    const events = api?.addEventListener && api.removeEventListener;
    if (events) { api.addEventListener('status', status); api.addEventListener('execution_start', execution); }
    const handle = {show, replay, suspend() { cancelReplay(); interrupt(); },
        handleKey(event) {
            if (!['keydown', 'keypress', 'keyup'].includes(event.type)) return false;
            const handled = !!layer || heldKeys.has(event.key);
            if (event.type === 'keydown') key(event); else keyEnd(event);
            return handled;
        }, dispose() {
        disposed = true; clearTimeout(startTimer); clearTimeout(pollTimer); cancelAnimationFrame(frame);
        cancelReplay();
        hide(); observer.disconnect(); resizeObserver.disconnect();
        document.removeEventListener('pointerdown', touch, true); document.removeEventListener('click', touch, true);
        document.removeEventListener('dragstart', touch, true); document.removeEventListener('dragenter', touch, true);
        document.removeEventListener('input', touch, true); document.removeEventListener('keydown', key, true);
        document.removeEventListener('keypress', keyEnd, true); document.removeEventListener('keyup', keyEnd, true);
        window.removeEventListener('resize', follow); document.removeEventListener('scroll', follow, true);
        if (events) { api.removeEventListener('status', status); api.removeEventListener('execution_start', execution); }
        guides.delete(dialog);
        if (current === handle) current = [...guides.values()].at(-1) || null;
    }};
    guides.set(dialog, handle);
    const foreground = [...document.querySelectorAll('dialog[open]')].at(-1);
    if (!foreground || foreground === dialog) { current?.suspend(); current = handle; }
    // Steps are fixed when the guide starts, so give an anchor that is still waiting for its data up to 5 s.
    // The Agent tab is not waited for: its discovery has already finished when the guide is created.
    const pending = () => chapters.some(c => c.id !== 'agent' && target(c) && !visible(target(c)));
    function begin(waited = 0) {
        startTimer = setTimeout(() => {
            if (!automatic || attempted.has(group) || manual || layer || disposed || touched) return;
            if (editor) {
                if (!dialog.open || !dialog.isConnected) return;
                serverBlocked = false;
                if (blocked() || pending()) { begin(250); return; }
                show().then(shown => {
                    if (disposed || touched) return;
                    if (!shown && (serverBlocked || blocked())) begin(250);
                    else { attempted.add(group); if (!shown) stopped = true; }
                });
                return;
            }
            if (pending() && waited < 5000) { begin(waited + 250); return; }
            attempted.add(group);
            show().then(shown => { if (!shown) stopped = true; });
        }, waited ? 250 : 1000);
    }
    let startTimer = null;
    begin();
    return handle;
}
