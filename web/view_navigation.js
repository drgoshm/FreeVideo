import { app } from '../../scripts/app.js';
import { openSetup } from './setup.js';
import { feedbackButton } from './feedback.js';
import { wordmark } from './wordmark.js';
import { createUpdateNotice, createVersionInfo } from './updates.js';
import { makeMovable } from './toolbar_position.js';
import { openEditorView } from './editor_loader.js';

const languageOverride = typeof location !== 'undefined'
    ? new URLSearchParams(location.search).get('freevideo_lang') : null;
const cn = languageOverride === 'zh' || (languageOverride !== 'en'
    && String(navigator.language || '').toLowerCase().startsWith('zh'));
const t = (en, zh) => cn ? zh : en;
const preference = 'freevideo.view';
let toolbar, open, selected, currentView = 'nodes', agentTab = null, opening = null;
const style = document.createElement('link');
style.rel = 'stylesheet'; style.href = new URL('./view_navigation.css', import.meta.url).href;
document.head.append(style);

export function preferredView() {
    try { return localStorage.getItem(preference); } catch { return null; }
}

let collapsing = false;

export function viewChanged(view, node) {
    currentView = view;
    if (node) selected = node;
    // Making way for the user's own workflow is not a choice of view: the FreeVideo workflow reopens as it was left.
    if (!collapsing) try { localStorage.setItem(preference, view); } catch { /* Private browsing still works. */ }
    refreshNavigation();
}

// Whether this FreeVideo has the Agent interface on. The surfaces that open first (the toolbar, the studio) ask
// and tell it here; a view switch shows its fourth tab only once the answer is yes.
export function agentTabReady(answer) {
    agentTab = Promise.resolve(answer).then(on => on === true, () => false);
    return agentTab;
}

// The Autopilot page loads on demand, as the editing view does; a failure leaves the current view as it is.
export async function openAutopilot(node, options) {
    try { return (await import('./autopilot.js')).openAutopilotView(node, options); }
    catch (error) { console.warn('FreeVideo autopilot view:', error); return null; }
}

export function viewSwitch(view, onStudio, onNodes, onEditor, onAutopilot) {
    const group = document.createElement('div'); group.className = 'fv-view-switch';
    group.setAttribute('role', 'group'); group.setAttribute('aria-label', t('FreeVideo view', 'FreeVideo 视图'));
    for (const [id, label, action] of [['studio', t('Create', '创作面板'), onStudio], ['nodes', t('Nodes', '节点视图'), onNodes], ['editor', t('Edit', '剪辑视图'), onEditor],
        ['autopilot', t('Autopilot', 'AI 全自动'), onAutopilot]]) {
        const button = document.createElement('button'); button.type = 'button';
        const text = document.createElement('span'); text.textContent = label; button.append(text);
        // Browser translation must leave the product name as written.
        if (id === 'autopilot') text.setAttribute('translate', 'no');
        button.dataset.label = label; button.dataset.view = id;
        if (id === 'editor') button.dataset.fvGuide = 'longer';
        button.setAttribute('aria-pressed', String(view === id)); button.onclick = action;
        if (id === 'autopilot') {
            button.hidden = view !== id;
            // Settled once the tab is shown or known to stay away; the first-run guide waits for it.
            group.ready = (agentTab || Promise.resolve(false)).then(on => { if (on) { button.hidden = false; button.dataset.fvGuide = 'agent'; } });
        }
        group.append(button);
    }
    return group;
}

const generateNodes = () => (app.graph?._nodes || []).filter(n => n.comfyClass === 'FreeVideoGenerate' || n.type === 'FreeVideoGenerate');

// A workflow without a FreeVideo node is the user's own: nothing of ours stays over it.
// Each workspace listens for this and closes itself.
export function collapseWorkspaces() {
    collapsing = true; setTimeout(() => { collapsing = false; }, 1000);
    window.dispatchEvent(new Event('freevideo-collapse'));
    for (const dialog of document.querySelectorAll('dialog.fv-studio[open]')) dialog.close();
}

export function refreshNavigation() {
    if (!toolbar) return;
    const nodes = generateNodes();
    if (!nodes.includes(selected)) selected = nodes[0];
    toolbar.hidden = !app.graph || !nodes.length || currentView !== 'nodes';
    for (const button of toolbar.querySelectorAll('[data-view]')) {
        button.setAttribute('aria-pressed', String(button.dataset.view === currentView));
    }
}

export function openCreativeWorkspace() {
    // Settings replay belongs to the visible editing workspace too.
    if (currentView === 'editor' && document.querySelector('dialog.fv-editor[open]')) return true;
    refreshNavigation();
    const focused = Object.values(app.canvas?.selected_nodes || {}).find(n => n.comfyClass === 'FreeVideoGenerate' || n.type === 'FreeVideoGenerate');
    const node = currentView === 'studio' ? selected : focused || selected;
    if (!node || !open) return false;
    open(node);
    return true;
}

export function installNavigation(openStudio, agent = false, {openWorkflow = null} = {}) {
    open = openStudio;
    if (!toolbar) agentTabReady(agent);
    if (toolbar) { refreshNavigation(); return; }
    toolbar = document.createElement('aside'); toolbar.className = 'fv-view-navigation';
    toolbar.setAttribute('aria-label', t('FreeVideo workspace', 'FreeVideo 工作区'));
    const row = document.createElement('div'); row.className = 'fv-view-row';
    // The whole bar drags like a window; these dots say so on hover.
    const grip = document.createElement('span'); grip.className = 'fv-view-grip'; grip.setAttribute('aria-hidden', 'true');
    const brand = wordmark();
    const choose = (action, create = false) => {
        const focused = Object.values(app.canvas?.selected_nodes || {}).find(n => (app.graph?._nodes || []).includes(n)
            && (n.comfyClass === 'FreeVideoGenerate' || n.type === 'FreeVideoGenerate'));
        refreshNavigation();
        let node = focused || selected;
        if (!node && create) {
            // The user's own workflow stays as it is: FreeVideo gets a workflow of its own.
            return (async () => {
                try {
                    // Clicks while the workflow is opening share that one workflow.
                    const first = !opening;
                    opening ||= Promise.resolve(openWorkflow?.()).finally(() => { opening = null; });
                    node = await opening;
                    if (!node) throw new Error('FreeVideo workflow unavailable');
                    selected = node;
                    return await action(node, first);
                } catch (error) {
                    console.warn('FreeVideo edit project:', error);
                    app.extensionManager?.toast?.add({severity: 'error', summary: 'FreeVideo',
                        detail: t('Could not complete. Try again.', '没有完成，请重试'), life: 9000});
                    return null;
                }
            })();
        }
        if (node) return action(node, false);
    };
    const settings = document.createElement('button'); settings.type = 'button';
    settings.textContent = t('Settings', '设置'); settings.className = 'fv-view-settings'; settings.onclick = openSetup;
    row.append(grip, brand, viewSwitch('nodes', () => choose(node => { open?.(node); }, true), () => {}, () => choose(async (node, created) => {
        const handle = await openEditorView(node);
        if (created) handle?.notify(t('Created an editing project for you.', '已为你新建一个剪辑工程'));
        return handle;
    }, true), () => choose(node => openAutopilot(node), true)), createVersionInfo(cn).element, feedbackButton(t, 'fv-view-settings'), settings); toolbar.append(row);
    toolbar.append(createUpdateNotice(cn).element);
    document.body.append(toolbar); refreshNavigation();
    makeMovable(toolbar);
    // health.js asks for this before it reports the toolbar missing. A
    // workspace removed without closing must not keep the toolbar hidden.
    globalThis.window?.addEventListener('freevideo-navigation-refresh', () => {
        if (currentView !== 'nodes' && !document.querySelector('dialog.fv-studio[open]')) currentView = 'nodes';
        refreshNavigation();
    });
}
