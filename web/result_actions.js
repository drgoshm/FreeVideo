import { api } from '../../scripts/api.js';
import { outputDownloadURL } from './output_download.js';
import { shareButton } from './share.js';
import { canExtend, extendText, extendSourceExists } from './studio_extend.js';

const css = document.createElement('link');
css.rel = 'stylesheet'; css.href = new URL('./result_actions.css', import.meta.url).href; document.head.append(css);

const ICONS = {
    back: '<path d="M13 8H3m0 0 4-4M3 8l4 4"/>',
    sidebar: '<rect x="2" y="2.5" width="12" height="11" rx="1.5"/><path d="M9.5 2.5v11"/>',
    download: '<path d="M8 2.5v7.5m0 0 3-3m-3 3-3-3M3 11.5v1a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1v-1"/>',
    share: '<path d="M8 10V2.5m0 0L5.2 5.3M8 2.5l2.8 2.8M4.5 7.5H4a1 1 0 0 0-1 1v4a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1v-4a1 1 0 0 0-1-1h-.5"/>',
    report: '<path d="M9.5 2H5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1V4.5L9.5 2Z"/><path d="M9.5 2v2.5H12M6.5 8h3M6.5 10.5h3"/>',
    chevron: '<path d="m4.5 6.5 3.5 3 3.5-3"/>',
    extend: '<rect x="1.5" y="4" width="8" height="8" rx="1.5"/><path d="M11.5 8h3m0 0-1.5-1.5M14.5 8 13 9.5"/>',
    lock: '<rect x="3.5" y="7" width="9" height="6.5" rx="1.5"/><path d="M5.5 7V5a2.5 2.5 0 0 1 5 0v2"/>',
    close: '<path d="m4 4 8 8M12 4l-8 8"/>',
    expand: '<path d="M9.5 2.5h4v4M13.5 2.5 9 7M6.5 13.5h-4v-4M2.5 13.5 7 9"/>',
    trash: '<path d="M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.6 8.1a1 1 0 0 0 1 .9h3.8a1 1 0 0 0 1-.9l.6-8.1M7 7v4M9 7v4"/>',
};

export function icon(name) {
    const span = document.createElement('span'); span.className = 'fv-icon'; span.setAttribute('aria-hidden', 'true');
    span.innerHTML = `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">${ICONS[name]}</svg>`;
    return span;
}

// Marks a first-pass preview on its picture, with the preview's own size.
export function previewBadge(record, t) {
    const badge = document.createElement('span'); badge.className = 'fv-preview-badge';
    const g = record.preview_geometry;
    badge.textContent = g?.width ? t(`First-pass preview · ${g.width} × ${g.height}`, `一采预览 · ${g.width} × ${g.height}`)
        : t('First-pass preview', '一采预览');
    return badge;
}

function action(tag, label, name, cls) {
    const e = document.createElement(tag); e.className = 'fv-action ' + cls;
    const text = document.createElement('span'); text.textContent = label;
    if (name) e.append(icon(name));
    e.append(text);
    if (tag === 'button') e.type = 'button';
    return e;
}

// A popover above its button that closes on an outside click or Escape, like a
// native menu. Escape must not also reach the dialog, which would close the window.
function popover(menu, summary, onClose = () => {}) {
    const outside = event => { if (menu.open && !menu.contains(event.target)) menu.open = false; };
    menu.addEventListener('toggle', () => {
        if (menu.open) document.addEventListener('pointerdown', outside, true);
        else { document.removeEventListener('pointerdown', outside, true); onClose(); }
    });
    menu.addEventListener('keydown', event => {
        if (event.key !== 'Escape' || !menu.open) return;
        event.preventDefault(); event.stopPropagation(); menu.open = false; summary.focus();
    });
}

// The next step of a first-pass preview. `run` reports its own errors and throws.
export function secondPassAction(t, run) {
    const button = action('button', t('Run second pass', '继续二采'), 'expand', 'fv-action-primary fv-action-second-pass');
    const label = button.lastElementChild;
    button.title = t('Continue from this preview to the full-resolution video.',
        '从这个预览继续生成全分辨率视频。');
    button.onclick = async () => {
        if (button.disabled) return;
        button.disabled = true; button.setAttribute('aria-busy', 'true');
        label.textContent = button.title = t('Adding…', '正在添加…');
        try { await run(); label.textContent = button.title = t('Queued', '已加入队列'); }
        catch {
            button.disabled = false;
            label.textContent = button.title = t('Second pass did not start · Retry', '二采没有开始 · 重试');
        } finally { button.removeAttribute('aria-busy'); }
    };
    return button;
}

// Delete asks in a popover first. `remove` throws an Error whose message is shown there.
function deleteAction(t, remove) {
    const menu = document.createElement('details'); menu.className = 'fv-action-menu fv-action-delete';
    const summary = document.createElement('summary'); summary.className = 'fv-action fv-action-secondary';
    const label = document.createElement('span'); label.textContent = t('Delete', '删除');
    summary.append(icon('trash'), label);
    const panel = document.createElement('div'); panel.className = 'fv-menu fv-confirm';
    const title = document.createElement('strong'); title.textContent = t('Delete this creation?', '删除这个作品？');
    const hint = document.createElement('small');
    hint.textContent = t('The video and its files will be deleted.',
        '视频和相关文件都会删除。');
    const problem = document.createElement('small'); problem.className = 'fv-confirm-problem'; problem.hidden = true;
    const row = document.createElement('div'); row.className = 'fv-confirm-actions';
    const cancel = action('button', t('Cancel', '取消'), null, 'fv-action-secondary');
    const confirm = action('button', t('Delete', '删除'), 'trash', 'fv-action-danger');
    cancel.onclick = () => { menu.open = false; summary.focus(); };
    confirm.onclick = async () => {
        confirm.disabled = cancel.disabled = true; confirm.lastElementChild.textContent = t('Deleting…', '正在删除…');
        try { await remove(); menu.open = false; }
        catch (error) { problem.textContent = error.message; problem.hidden = false; }
        finally { confirm.disabled = cancel.disabled = false; confirm.lastElementChild.textContent = t('Delete', '删除'); }
    };
    row.append(cancel, confirm);
    panel.append(title, hint, problem, row);
    menu.append(summary, panel);
    popover(menu, summary, () => { problem.hidden = true; });
    menu.addEventListener('toggle', () => { if (menu.open) cancel.focus(); });
    return menu;
}

// The video cannot be opened: what works on the file (download, extend, share) goes quiet and cannot be used.
export function actionsUnavailable(bar) {
    if (!bar) return;
    bar.dataset.unavailable = 'true';
    for (const item of bar.querySelectorAll('a.fv-action[download], .fv-action-extend, .fv-share-trigger')) {
        item.setAttribute('aria-disabled', 'true'); item.tabIndex = -1;
        if (item.tagName === 'A') item.removeAttribute('href'); else item.disabled = true;
    }
}

// Download the video first; share and the report follow. A first-pass preview
// leads with `secondPass` instead, and saved creations end with `remove`.
// `diagnostic` downloads the redacted report (and may offer to send it); a
// result without one shows no report entry, because the full generation
// record keeps local paths and is not meant to be shared.
export function resultActions(record, t, {diagnostic = null, secondPass = null, remove = null, extend = null} = {}) {
    const bar = document.createElement('div'); bar.className = 'fv-actions';
    if (secondPass) bar.append(secondPassAction(t, secondPass));
    if (record.video) {
        const download = action('a', t('Download video', '下载视频'), 'download', secondPass ? 'fv-action-secondary' : 'fv-action-primary');
        download.href = outputDownloadURL(api, record.video); download.download = '';
        bar.append(download);
    }
    if (extend && canExtend(record)) {
        const button = action('button', extendText(t, 'action'), 'extend', 'fv-action-secondary fv-action-extend');
        const label = button.lastElementChild;
        const available = exists => {
            label.textContent = exists ? extendText(t, 'action') : t('Video unavailable · retry', '视频不可用 · 重试');
            button.title = '';
        };
        extendSourceExists(api, record).then(available);
        button.onclick = async () => {
            if (button.disabled) return;
            button.disabled = true;
            button.title = t('Checking video…', '正在检查视频…');
            try {
                const exists = await extendSourceExists(api, record); available(exists);
                if (!exists) return;
                if (await extend(record) === null) throw new Error(t('Could not open the editor. Try again.', '无法打开剪辑，请重试。'));
            } catch (error) {
                label.textContent = error.message || t('Could not open the editor. Try again.', '无法打开剪辑，请重试。');
                button.title = label.textContent;
            } finally { button.disabled = false; }
        };
        bar.append(button);
    }
    const share = shareButton(record, t);
    share.className = 'fv-action fv-action-secondary fv-share-trigger';
    const label = document.createElement('span'); label.textContent = share.textContent;
    share.replaceChildren(icon('share'), label);
    bar.append(share);
    if (diagnostic) {
        const reportLabel = t('Diagnostic report', '诊断报告');
        const report = action('button', reportLabel, 'report', 'fv-action-secondary fv-action-report');
        report.title = t('Diagnostic report: hardware, settings and timings', '诊断报告：硬件、设置和耗时');
        const text = report.lastElementChild;
        report.onclick = async () => {
            if (report.disabled) return;
            report.disabled = true; text.textContent = t('Preparing…', '正在整理…');
            try { await diagnostic(); text.textContent = reportLabel; }
            catch { text.textContent = t('Report unavailable · retry', '报告暂不可用 · 重试'); }
            finally { report.disabled = false; }
        };
        bar.append(report);
    }
    if (remove) bar.append(deleteAction(t, remove));
    return bar;
}
