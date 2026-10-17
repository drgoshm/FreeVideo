// The editor owns its generation controls; progress can use the optional component.
import {createFirstFrameSwitch} from './editor_first_frame.js';
import {createEditorReferences} from './editor_references.js';
import {createExpandButton} from './editor_expand.js';
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
export const timeEstimate = (seconds, t) => seconds == null ? '' : seconds < 60
    ? t(`about ${Math.max(1, Math.ceil(seconds))} s`, `约 ${Math.max(1, Math.ceil(seconds))} 秒`)
    : t(`about ${Math.ceil(seconds / 60)} minutes`, `约 ${Math.ceil(seconds / 60)} 分钟`);
// After the next frame is on screen; a page nobody sees has no frame to wait for.
const painted = () => typeof document === 'undefined' || document.visibilityState !== 'visible' || typeof requestAnimationFrame !== 'function' ? Promise.resolve()
    : new Promise(resolve => requestAnimationFrame(() => { const {port1, port2} = new MessageChannel(); port1.onmessage = () => { port1.close(); resolve(); }; port2.postMessage(null); }));
export function createEditorGenbar(host, {state, t, actions, text = ''}) {
    const hint = el('span', 'fv-ed-gen-hint'), note = el('span', 'fv-ed-gen-note');
    note.setAttribute('role', 'status'); note.hidden = true;
    const input = el('textarea', 'fv-ed-description'); input.rows = 1; input.value = text;
    const description = el('div', 'fv-ed-description-box'); description.append(input);
    const generate = el('button', 'fv-ed-generate'), cancel = el('button', null, t('Cancel', '取消')), resume = el('button', null, t('Continue', '继续'));
    input.dataset.fvGuide = 'editor-describe'; generate.dataset.fvGuide = 'editor-generate';
    for (const button of [generate, cancel, resume]) button.type = 'button';
    host.append(hint, note, description, generate, cancel, resume);
    const firstFrame = createFirstFrameSwitch(host, {state, t, actions});
    description.after(firstFrame.element);
    const failure = el('div', 'fv-ed-gen-failure fv-result-links'), failureBar = el('div', 'fv-ed-failure-actions fv-actions');
    const failureReason = el('span', 'fv-ed-failure-reason'); failureReason.setAttribute('role', 'status');
    const report = el('button', 'fv-ed-failure-report fv-action fv-action-secondary', t('Download diagnostic report', '下载诊断报告')); report.type = 'button';
    failureBar.append(failureReason, report); failure.append(failureBar); failure.hidden = true; host.append(failure);
    let failureId = null, downloading = false;
    // Reference pictures for a part that starts fresh: thumbnails and a small "+" before the description.
    const references = actions.references ? createEditorReferences({t, source: actions.references.source, size: actions.references.size, error: actions.error, notify: actions.references.notify}) : null;
    if (references) description.before(references.element);
    let status = null, serial = 0, disposed = false, pressing = new Set(), previewSelection = null, estimateCache = null, noteTimer = null, queuedAt = null;
    // Which parts are generating, or how the job ended: a "Queued" line goes once this moves on.
    const progressKey = () => ['done', 'failed', 'cancelled', 'interrupted'].includes(status?.state) ? status.state
        : (status?.blocks || []).filter(block => block.state === 'running').map(block => block.id).join(',');
    async function downloadFailure(id = failureId) {
        if (disposed || downloading || id !== failureId) return;
        downloading = true; report.disabled = true;
        try {
            // A failed part offers its report on the part itself; the submit tip then hangs under this line's sentence.
            await actions.downloadFailureReport?.(id, {bar: failureBar, anchor: report.hidden ? failureReason : report, t,
                current: () => !disposed && failureId === id && !failure.hidden});
        } catch (value) {
            say(t(`Downloading the diagnostic report failed: ${value.message || value}`, `下载诊断报告时出错：${value.message || value}`));
        } finally { downloading = false; report.disabled = false; }
    }
    report.onclick = () => downloadFailure();
    function say(text, queued = false) {
        if (disposed) return;
        queuedAt = text && queued ? progressKey() : null;
        clearTimeout(noteTimer); note.textContent = text || ''; note.hidden = !text;
        if (text) noteTimer = setTimeout(() => { note.hidden = true; }, 6000);
    }
    async function render() {
        if (disposed) return;
        if (queuedAt != null && progressKey() !== queuedAt) say('');
        const current = ++serial, selection = previewSelection || state.selection;
        const selectedFailure = !previewSelection && actions.selectedFailure?.();
        if (failureId !== (selectedFailure?.id || null)) {
            failure.querySelector('.fv-report-tip')?.remove();
            failureId = selectedFailure?.id || null;
        }
        failure.hidden = !selectedFailure;
        failureReason.textContent = selectedFailure?.text || '';
        // Only a press that never became a part has no part to carry the report button.
        report.hidden = selectedFailure?.id !== 'submission';
        const running = ['queued', 'running', 'cancelling'].includes(status?.state);
        const selected = !!selection && (!!previewSelection || (actions.supportedSelection?.() ?? true));
        // While a part generates, choosing another place offers Generate again; it is queued behind.
        // A press is being sent for the parts in `pressing`; only that same selection waits for it.
        const sending = pressing.has(JSON.stringify(state.selection ?? null)), starting = pressing.size > 0 && !running;
        const other = (running || starting) && !sending && selected && !previewSelection && actions.selectionRunning?.() === false;
        // A part pressed while it could not start yet waits in line; its Cancel takes it out of the line.
        const held = !previewSelection && !!actions.selectionHeld?.();
        const busy = sending || held || (running || starting) && !other;
        // The selected part is still being sent: it can only be stopped once it has started.
        const opening = (starting || !!actions.selectionSending?.()) && !held;
        const stopped = ['failed', 'cancelled', 'interrupted'].includes(status?.state);
        host.dataset.mode = busy ? 'running' : selected ? 'selected' : 'idle';
        hint.hidden = !busy && !other;
        // The description and Generate stay where they are while a part is made, greyed, so nothing around them moves.
        input.hidden = generate.hidden = false;
        // A choice that will move to an empty spot can still be described first.
        const writable = selected || !previewSelection && !!selection && !!actions.selectionMovable?.();
        input.disabled = !writable || busy;
        input.placeholder = writable ? t('Describe this part', '描述这一段')
            : '';
        input.setAttribute('aria-label', t('Description', '描述'));
        generate.classList.toggle('fv-primary', selected && !busy);
        // A cancel already accepted is not offered again; the bar says it is stopping until the run ends.
        const cancelling = status?.state === 'cancelling' && !held;
        cancel.hidden = !busy || opening || cancelling; resume.hidden = !stopped;
        const waiting = status?.state === 'queued' && !(status.blocks || []).some(block => ['queued', 'running'].includes(block.state) && block.prompt_id);
        if (opening) hint.textContent = t('Starting…', '正在开始…');
        else if (held || waiting) hint.textContent = t('Queued', '排队中');
        else if (cancelling) hint.textContent = t('Stopping…', '正在停止…');
        else if (running) {
            const blocks = status?.blocks || [], done = blocks.filter(block => block.state === 'done').length;
            const number = Math.min(blocks.length || 1, Math.max(done, status?.chainProgress?.index || 0) + 1), count = blocks.length || 1;
            const remaining = status?.chainProgress?.overall?.remaining_seconds ?? (blocks.every(block => Number.isFinite(block.remaining_seconds)) ? blocks.reduce((sum, block) => sum + block.remaining_seconds, 0) : null);
            const words = status?.words;
            // The creation panel's own sentences, so a long step says what it is and how long it has taken.
            // At a narrow width only the stage sentence is cut; the part number and the time stay.
            // A single part needs no number; with nothing else to say it reads "Preparing".
            // The creation panel's own time when it has one, so the bar and the panel never differ; the job's sum until the panel has one.
            const left = words?.over ? null : words?.left ?? remaining;
            const time = words?.stall || (words?.over ? t('Taking longer than expected', '比预计久，仍在生成') : words?.time || (left != null ? timeEstimate(left, t) : ''));
            const stage = words?.stage || (count === 1 && !time ? t('Preparing', '正在准备') : '');
            const spans = [['fv-ed-gen-part', count > 1 ? t(`Part ${number}/${count}`, `第 ${number}/${count} 段`) : ''],
                ['fv-ed-gen-stage', stage], ['fv-ed-gen-time', time]];
            const shown = spans.filter(([, text]) => text).map(([cls, text], index) => [cls, index ? ` · ${text}` : text]);
            hint.replaceChildren(...shown.map(([cls, text]) => el('span', cls, text)));
            hint.title = shown.map(([, text]) => text).join('');
        }
        firstFrame.update(busy);
        references?.update(busy, busy ? undefined : actions.references.visible() || false);
        generate.textContent = t('Generate', '生成');
        // Never disabled for a reason the press itself can explain or fix.
        generate.disabled = busy || !!previewSelection;
        resizeDescription();
        input.dispatchEvent(new Event('change'));
        generate.title = '';
        if (selected && !busy) {
            const key = JSON.stringify(selection);
            if (estimateCache?.key === key) generate.textContent += estimateCache.label;
            const seconds = await actions.estimate(selection);
            if (disposed || current !== serial) return;
            const label = seconds == null ? '' : ' · ' + timeEstimate(seconds, t);
            estimateCache = {key, label};
            generate.textContent = t('Generate', '生成') + label;
        }
    }
    function resizeDescription() {
        // 18 px lines + 16 px padding + 2 px border: one line is still 36 px, five lines 108 px.
        input.style.height = '0px';
        const height = Math.max(36, (input.scrollHeight || 34) + 2);
        input.style.height = `${Math.min(108, height)}px`;
        input.style.overflowY = height > 108 ? 'auto' : 'hidden';
        host.closest('.fv-editor')?.style.setProperty('--fv-ed-description-growth', `${Math.min(108, height) - 36}px`);
    }
    let width = null;
    const resize = typeof ResizeObserver === 'function' ? new ResizeObserver(entries => {
        const next = entries[0]?.contentRect.width;
        if (next === width) return;
        width = next; resizeDescription();
    }) : null;
    resize?.observe(description);
    input.oninput = () => { state.setRangeText(input.value); render(); };
    input.onkeydown = event => {
        // The former text input did not submit or insert a line on Enter.
        if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) event.preventDefault();
    };
    generate.onclick = async () => {
        const key = JSON.stringify(state.selection ?? null);
        if (pressing.has(key)) return;
        pressing.add(key); say(''); render();
        const typed = input.value;
        let reason = '';
        try {
            // The press shows before any planning, however long that takes.
            await painted();
            const result = actions.press ? await actions.press(input.value) : await actions.generate({target: 'both', text: input.value});
            // Never over what the user typed meanwhile, for another part.
            if (result?.text != null && result.text !== input.value && input.value === typed) input.value = result.text;
            if (result?.say) { say(result.say, !!result.queued); reason = result.say; }
            if (result?.focus) { pressing.delete(key); render(); input.disabled = false; input.focus(); }
        }
        // A specific reason is said once; the planning limits are already shown above.
        catch (error) { const result = actions.error(error, 'generate'); reason = result?.say || '';
            if (result?.say && !['chain_too_long', 'chain_part_too_short'].includes(result.reason)) say(result.say);
            else if (!result && !note.textContent) { reason = t('Could not start. Try again.', '没能开始，请重试。'); say(reason); } }
        finally { pressing.delete(key); render(); }
        return reason;
    };
    const expand = createExpandButton({t, textarea: input, onGenerate: () => generate.onclick(), canGenerate: () => !generate.disabled, onDraft: text => state.setRangeText(text)});
    description.append(expand.element);
    cancel.onclick = () => (!previewSelection && actions.selectionHeld?.() ? Promise.resolve(actions.unhold()).then(render) : actions.cancel()).catch(actions.error);
    resume.onclick = () => actions.resume().catch(value => actions.error(value, 'generate'));
    const unsubscribe = state.subscribe(event => {
        if (event.type === 'selection' && state.selection?.kind === 'range') {
            if (state.rangeText === null) state.setRangeText(input.value);
            else if (input.value !== state.rangeText) input.value = state.rangeText;
        }
        if (event.type === 'selection' && state.selection?.kind === 'clip') {
            const selected = state.record.tracks.flatMap(track => track.clips).find(clip => state.selection.ids.includes(clip.id) && clip.pending);
            // A part with no description of its own keeps what is typed, as a new range does.
            if (selected?.text) input.value = selected.text;
        }
        if (event.type === 'selection' || event.type === 'record') render();
    });
    render();
    return {element: host, say, downloadFailure, getText() { expand.commit(); return input.value; }, update(value) { status = value; render(); }, preview(selection) { previewSelection = selection; render(); }, setText(value) { input.value = value || ''; state.setRangeText(input.value); render(); },
        dispose() { disposed = true; ++serial; clearTimeout(noteTimer); unsubscribe(); resize?.disconnect(); expand.dispose(); references?.dispose(); }};
}

// A missing optional module leaves the editor's existing progress in place.
export async function loadEditorComponents(load = path => import(path)) {
    const results = await Promise.allSettled([load('./block_progress.js')]);
    return {blocks: results[0].status === 'fulfilled' ? results[0].value : null};
}

// What a part's progress reads from each message: Studio's displayed percentage while it
// shows this part, never a stage's own fraction (one loading step or one pass), which
// read as the part's showed a part still generating as finished.
export function blockMessages(messages, snapshot = () => null) {
    return messages.map(message => {
        const part = {...message}, shown = snapshot(message); delete part.display_fraction;
        return shown ? {...part, ...shown} : part;
    });
}
export function editorBlockStates(job, messages = [], resolve = null) {
    if (resolve) return resolve(job, messages);
    const states = {pending: 'waiting', queued: 'waiting', running: 'generating', done: 'done',
        failed: 'failed', cancelled: 'stopped', interrupted: 'stopped'};
    // A part that never started in a run that was stopped or failed is stopped too, not still queued.
    // Once a cancel is accepted (cancelling), such a part can only end cancelled, so it reads stopped at once.
    const ended = ['failed', 'cancelled', 'interrupted', 'cancelling'].includes(job.state);
    return Object.fromEntries((job.blocks || []).map(block => {
        if (ended && ['pending', 'queued'].includes(block.state)) return [block.id, {state: 'stopped', fraction: 0, remainingSeconds: null}];
        const live = messages.find(message => message.chain?.id === job.id && message.chain?.block === block.id);
        const running = block.state === 'running' || block.state === 'queued';
        const fraction = block.state === 'done' ? 1 : running && live
            ? Number.isFinite(live.percent) ? live.percent / 100 : live.bar ?? live.overall?.fraction ?? block.fraction
            : block.fraction;
        return [block.id, {state: running && live ? 'generating' : states[block.state] || 'waiting',
            ...(['pending', 'queued'].includes(block.state) && !live ? {queued: true} : {}),
            fraction: Math.max(0, Math.min(1, fraction || 0)),
            remainingSeconds: running && live ? live.remaining_seconds ?? live.overall?.remaining_seconds ?? block.remaining_seconds ?? null
                : block.remaining_seconds ?? null}];
    }));
}
