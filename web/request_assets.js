// One muted line above Generate: what this request will download first.
// LoRA file names this request would load, through the whole `previous` chain.
export function enabledLoras(start, {upstream, widget, value}) {
    const names = [];
    for (let current = start, depth = 0; current && depth < 16; current = upstream(current, 'previous'), depth++) {
        try {
            if (widget(current, 'adapters')) {
                for (const row of JSON.parse(value(current, 'adapters') || '[]')) {
                    if (row.enabled !== false && Number(row.strength ?? 1) !== 0 && typeof row.name === 'string') names.push(row.name);
                }
            } else if (widget(current, 'lora')) {
                const name = value(current, 'lora');
                if (name && name !== 'None' && Number(value(current, 'strength') ?? 1) !== 0) names.push(name);
            }
        } catch {}
    }
    return names;
}

// Reference files as `kind:file`, so generation's own rule can tell whether a video has audio.
// A reference that is not a file in the input folder makes the task unknowable here: send none.
export function referenceFiles(refs) {
    return refs.length && refs.every(r => typeof r.file === 'string' && r.kind) ? refs.map(r => `${r.kind}:${r.file}`) : [];
}

// Keep the extension and the end of a long file name; shorten the middle.
const short = name => name.length <= 28 ? name
    : name.slice(0, 12) + '…' + name.slice(-15);

export function createRequestAssets({t, api, request, onError = () => {}}) {
    const element = document.createElement('div');
    element.className = 'fv-request-assets fv-muted';
    element.hidden = true;
    element.setAttribute('aria-live', 'polite');
    let timer = 0, controller = null, key = '', disposed = false;
    const size = bytes => bytes >= 2 ** 30 ? (bytes / 2 ** 30).toFixed(1) + ' GB' : Math.max(1, Math.round(bytes / 2 ** 20)) + ' MB';
    function lines(items) {
        const rows = [];
        // One line names what Generate will fetch first: the two-pass model, then the sampling data.
        const names = {upscaler: t('two-pass model', '二次采样模型'), sampling: t('sampling data', '采样数据')};
        const bytes = {};
        for (const item of items) {
            if (item.action !== 'auto') continue;
            const kind = names[item.kind] ? item.kind : 'other';
            bytes[kind] = (bytes[kind] || 0) + item.bytes;
        }
        const parts = [['upscaler', names.upscaler], ['sampling', names.sampling], ['other', t('other model files', '其他模型文件')]]
            .filter(([kind]) => bytes[kind]).map(([kind, name]) => `${name} ${size(bytes[kind])}`);
        if (parts.length) rows.push(t(`Downloads before generating: ${parts.join(', ')}`, `生成前会先下载：${parts.join('、')}`));
        for (const item of items) {
            if (item.kind === 'prompt-vlm') {
                rows.push(t(`First prompt rewrite downloads ${size(item.bytes)}`, `首次改写提示词会先下载 ${size(item.bytes)}`));
            } else if (item.kind === 'lora') {
                const names = (item.names || []).map(short);
                rows.push(t(`LoRA not found: ${names.join(', ')}`, `找不到 LoRA：${names.join('、')}`));
            }
        }
        return rows;
    }
    function show(items) {
        const rows = lines(items);
        element.replaceChildren(...rows.map(text => Object.assign(document.createElement('div'), {textContent: text})));
        element.hidden = rows.length === 0;
    }
    function refresh(force = false) {
        const query = request();
        if (!query) { show([]); return; }
        const next = query.toString();
        if (next === key && !force) return;
        key = next;
        clearTimeout(timer); controller?.abort();
        timer = setTimeout(async () => {
            const current = controller = new AbortController();
            try {
                const reply = await api.fetchApi('/freevideo/request-assets?' + next, {signal: current.signal});
                if (!reply.ok) return;
                const result = await reply.json();
                if (disposed || current.signal.aborted || key !== next) return;
                show(Array.isArray(result.items) ? result.items : []);
            } catch (error) { if (error?.name !== 'AbortError') onError(error); } // Advisory; never blocks Generate.
        }, 300);
    }
    return {element, refresh, dispose() { disposed = true; clearTimeout(timer); controller?.abort(); }};
}
