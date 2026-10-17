import { app } from '../../scripts/app.js';
import { api } from '../../scripts/api.js';
import { restorePromptDraft } from './prompt_draft.js';
import { takePortMarker } from './port_notice.js';

// health.js names what failed; files loaded before it queue their reports.
const health = item => (window.__freevideoHealth ||= []).push(item);
const studioWorkflow = graph => (graph?._nodes || graph?.nodes || []).some(n => n.comfyClass === 'FreeVideoGenerate' || n.type === 'FreeVideoGenerate')
    && !!graph?.extra?.freevideo_studio;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
let configured = performance.now();

// ComfyUI's own session storage holds the last project's tabs and drafts under
// several keys that differ between frontend versions; any of them naming one of
// our nodes (the generate node may have been removed) means a project from the previous visit is waiting to be restored.
function cachedStudioProject() {
    for (const area of ['localStorage', 'sessionStorage']) {
        try {
            const store = globalThis[area];
            for (let index = 0; index < (store?.length || 0); index++) {
                const key = store.key(index);
                if (key !== 'workflow' && !key?.startsWith('Comfy.')) continue;
                if (/FreeVideo(Generate|Media)|freevideo_studio/.test(store.getItem(key) || '')) return true;
            }
        } catch { /* Storage blocked: open the bundled workflow right away. */ }
    }
    return false;
}

async function existingStudioWorkflow(target, bundled) {
    // Restored background tabs are lazy: load() reads the host's saved draft,
    // whereas the workflow store's openWorkflow() only selects the tab record.
    await target.load?.();
    const saved = target.changeTracker?.activeState;
    // Leave the current graph intact; the existing startup check reports an
    // unopened workflow without replacing the user's project.
    if (!saved) return null;
    const graph = structuredClone(saved);
    if (!graph.nodes?.some(node => node.type === 'FreeVideoGenerate')) {
        const node = structuredClone(bundled.nodes.find(node => node.type === 'FreeVideoGenerate'));
        node.id = Math.max(graph.last_node_id || 0, ...graph.nodes.map(node => Number(node.id) || 0)) + 1;
        graph.last_node_id = node.id;
        node.pos = [Math.max(0, ...graph.nodes.map(node => (node.pos?.[0] || 0) + (node.size?.[0] || 400))) + 80, 100];
        for (const input of node.inputs || []) input.link = null;
        for (const output of node.outputs || []) output.links = null;
        graph.nodes.push(node);
        // Reconnect a single unambiguous media source, leaving every existing
        // node and link intact. Multiple media sources keep their own topology.
        const media = graph.nodes.filter(node => node.type === 'FreeVideoMedia');
        const input = node.inputs?.findIndex(input => input.name === 'media') ?? -1;
        const output = media.length === 1 ? media[0].outputs?.findIndex(output => output.type === node.inputs?.[input]?.type) ?? -1 : -1;
        if (input >= 0 && output >= 0 && graph.version === .4) {
            graph.links ||= [];
            const id = Math.max(graph.last_link_id || 0, ...graph.links.map(link => link[0])) + 1;
            graph.links.push([id, media[0].id, output, node.id, input, node.inputs[input].type]);
            (media[0].outputs[output].links ||= []).push(id);
            node.inputs[input].link = id;
            graph.last_link_id = id;
        }
    }
    graph.extra = {...graph.extra, freevideo_studio: true};
    return restorePromptDraft(api, graph, target.path);
}

// Opens FreeVideo's own workflow in a tab of its own and returns its generate node. The workflow in front
// is left as it is: an existing FreeVideo tab comes back with its project, otherwise a new one opens.
export async function openFreeVideoWorkflow() {
    const response = await api.fetchApi('/freevideo/launcher/workflow');
    if (!response.ok) throw new Error(`FreeVideo workflow: HTTP ${response.status}`);
    const workflow = await restorePromptDraft(api, await response.json());
    const store = app.extensionManager?.workflow;
    const existing = store?.getWorkflowByPath?.('workflows/FreeVideo.json')
        || store?.openWorkflows?.find(row => studioWorkflow(row.changeTracker?.activeState));
    const restored = existing ? await existingStudioWorkflow(existing, workflow) : null;
    if (restored) await app.loadGraphData(restored, true, true, existing);
    else await app.loadGraphData(structuredClone(workflow), true, true, 'FreeVideo');
    return (app.graph?._nodes || []).find(n => n.comfyClass === 'FreeVideoGenerate' || n.type === 'FreeVideoGenerate') || null;
}

// ComfyUI restores the previous session's workflow and tabs after every
// extension's setup, so that restore can bring another workflow to the front
// over ours. Once startup is quiet, bring our tab back into view even if a
// click or key arrived during startup. Never awaited from setup: ComfyUI restores only after
// setup returns.
async function keepInFront(workflow, opened) {
    let touched = false;
    const touch = event => { if (!event.target?.closest?.('.fv-health')) touched = true; };
    addEventListener('pointerdown', touch, true); addEventListener('keydown', touch, true);
    try {
        const begin = performance.now();
        while (performance.now() - begin < 15000
            && (app.extensionManager?.spinner || performance.now() - configured < 600)) await sleep(100);
        const store = app.extensionManager?.workflow;
        // Opening the bundled graph before session restore overwrites the cached
        // FreeVideo tab, including its media and connections. Reuse that project.
        if (studioWorkflow(app.graph)) {
            const current = app.graph.serialize(), target = store?.activeWorkflow;
            const restored = await restorePromptDraft(api, structuredClone(current), target?.path || '__existing__');
            if (JSON.stringify(restored) !== JSON.stringify(current))
                await app.loadGraphData(restored, true, true, target || 'FreeVideo');
            return;
        }
        // The user kept working in our own tab: its edits stay, even without the generate node.
        if (touched && opened && store?.activeWorkflow === opened) return;
        const existing = store?.getWorkflowByPath?.('workflows/FreeVideo.json')
            || store?.openWorkflows?.find(row => studioWorkflow(row.changeTracker?.activeState));
        if (existing) {
            const restored = await existingStudioWorkflow(existing, workflow);
            if (restored) await app.loadGraphData(restored, true, true, existing);
            return;
        }
        // Our own tab when ComfyUI still has it; a new one otherwise.
        const target = opened && store?.getWorkflowByPath?.(opened.path) === opened ? opened : 'FreeVideo';
        await app.loadGraphData(structuredClone(workflow), true, true, target);
    } finally {
        removeEventListener('pointerdown', touch, true); removeEventListener('keydown', touch, true);
    }
}

// Explicit launcher navigation only; ordinary ComfyUI visits restore their own
// graph. Remove the flag before loading so refreshing never resets user edits.
app.registerExtension({
    name: 'FreeVideo.Launcher',
    afterConfigureGraph() { configured = performance.now(); },
    async setup() {
        takePortMarker();
        const url = new URL(window.location.href);
        if (url.searchParams.get('freevideo') !== 'launch') return;
        url.searchParams.delete('freevideo');
        window.history.replaceState(window.history.state, '', url);
        let workflow;
        const cached = cachedStudioProject();
        try {
            const response = await api.fetchApi('/freevideo/launcher/workflow');
            if (!response.ok) throw new Error(`FreeVideo workflow: HTTP ${response.status}`);
            // A fresh copy of the bundled workflow, with the prompt last written.
            workflow = await restorePromptDraft(api, await response.json());
            // Opening the bundled graph now would overwrite the cached project's
            // media and connections before ComfyUI restores them; keepInFront
            // brings the restored tab forward instead.
            if (!cached) await app.loadGraphData(structuredClone(workflow), true, true, 'FreeVideo');
        } catch (error) {
            console.error('FreeVideo launcher:', error);
            health({type: 'launch', error});
            return;
        }
        const opened = cached ? undefined : app.extensionManager?.workflow?.activeWorkflow;
        keepInFront(workflow, opened).then(() => health({type: 'launch'}), error => {
            console.error('FreeVideo launcher:', error);
            health({type: 'launch', error});
        });
    },
});
