import {attemptClip, find} from './editor_model.js';
import {createErrorReport} from './error_panel.js';
import {saveReport, showReportTip, reportIssue} from './report_issue.js';
import {productVersion} from './updates.js';

// Receipts belong to an attempt, not to saved projects or undo history.
export function createEditorFailures({api, state, jobStatuses, reasonText}) {
    const details = new Map(), snapshots = new Map(), progress = new Map();
    let submissionError = null;
    const owns = (job, block) => attemptClip(state.record, block.id)?.job === job.id;
    const key = (job, block) => `${job.id}:${block.id}`;
    function receipt(id) {
        const clip = find(state.record, id) || (id === 'submission' && submissionError ? {id, text: submissionError.text} : null);
        if (!clip) return null;
        const job = jobStatuses.get(clip.job);
        // Redo can restore the original clip (including its audio partner).
        const owned = job?.blocks?.filter(block => owns(job, block)) || [];
        const block = owned.find(block => block.id === clip.id || block.id === clip.link)
            || owned.find(block => block.state === 'failed' && clip.part && block.part === clip.part);
        const live = block && progress.get(key(job, block));
        const current = live && (!block.prompt_id || live.prompt_id === block.prompt_id) ? live : null;
        const submission = id === 'submission' ? submissionError : null;
        const detail = details.get(block?.prompt_id || current?.prompt_id) || submission?.detail;
        const snapshot = snapshots.get(clip.job) || submission?.snapshot, index = job?.blocks?.indexOf(block);
        const value = {reason: block?.reason || current?.reason || clip.reason,
            error: block?.error || current?.error, detail,
            phase: current?.phase, stage: current?.stage, index: index >= 0 ? index : null};
        return {clip, job, block, snapshot, value, text: reasonText(value), attempt: `${clip.job || 'submission'}:${block?.prompt_id || block?.attempts || 0}`,
            reportId: [current?.report_id, block?.report_id].find(id => /^[a-f0-9]{32}$/.test(id || ''))};
    }
    function report(id) {
        const found = receipt(id); if (!found) return null;
        const {clip, job, block, snapshot, value} = found;
        const nodeId = snapshot?.node || job?.node || 'editor', original = snapshot?.prompt?.[nodeId];
        const detail = value.detail || (block?.error && typeof block.error === 'object' ? block.error : null) || {exception_message: block?.error || found.text,
            node_id: nodeId, node_type: original?.class_type || 'FreeVideoGenerate'};
        const inputs = {...original?.inputs, ...detail.current_inputs, ...block?.inputs,
            text: block?.inputs?.text ?? clip.text};
        return createErrorReport(detail, {stage: id === 'submission' ? 'submission' : 'execution', node: {id: nodeId},
            snapshot: {output: {...snapshot?.prompt, [nodeId]: {...original, inputs}}},
            progress: {phase: value.phase || clip.id, stage: value.stage || (value.index != null ? `Part ${value.index + 1}` : ''),
                ...(job ? {done: job.blocks.filter(block => block.state === 'done').length, total: job.blocks.length} : {})}});
    }
    return {
        remember(job, snapshot) { snapshots.set(job.id, snapshot); },
        reconcile(job, previous) {
            for (const block of job.blocks || []) {
                const old = previous?.blocks?.find(value => value.id === block.id), live = progress.get(key(job, block));
                if (old && (old.prompt_id !== block.prompt_id || old.attempts !== block.attempts)
                    && live?.prompt_id === old.prompt_id) progress.delete(key(job, block));
            }
        },
        submission(error, snapshot, text) { submissionError = {detail: error, snapshot, text}; },
        capture(detail) {
            if (!detail?.prompt_id || typeof detail.prompt_id !== 'string') return false;
            for (const job of jobStatuses.values()) {
                if (!job.blocks?.some(block => owns(job, block) && (block.prompt_id === detail.prompt_id
                    || !block.prompt_id && progress.get(key(job, block))?.prompt_id === detail.prompt_id))) continue;
                details.set(detail.prompt_id, structuredClone(detail)); return true;
            }
            return false;
        },
        receive(job, block, message) {
            const previous = progress.get(key(job, block));
            progress.set(key(job, block), {...(previous?.prompt_id === message.prompt_id ? previous : {}), ...message});
        },
        text(id) { return receipt(id)?.text || ''; },
        report,
        async download(id, {bar, anchor, t, current = () => true}) {
            const found = receipt(id); if (!found) return;
            let blob;
            if (found.reportId) {
                const response = await api.fetchApi(`/freevideo/report/${found.reportId}`);
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                blob = await response.blob();
            } else blob = new Blob([JSON.stringify(report(id), null, 2)], {type: 'application/json'});
            const latest = receipt(id);
            if (!current() || latest?.attempt !== found.attempt || latest?.value.detail !== found.value.detail) return;
            await saveReport(blob);
            // Use exactly the video's card and submit flow, including no second download.
            return showReportTip(bar, anchor, t, {mode: 'downloaded',
                submit: () => reportIssue(() => Promise.resolve(blob), productVersion(), {save: false})});
        },
        clear() { details.clear(); snapshots.clear(); progress.clear(); submissionError = null; },
    };
}
