function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }

// The one place a page says whether an agent is connected: its name while it is, never a client id.
export function connectionLabel(agent, t) {
    if (agent?.connected !== true) return {color: 'gray', text: t('Not connected', '未连接')};
    return {color: 'green', text: String(agent.name || '').trim() || t('Connected', '已连接')};
}

export function connectionSentence(address, t) {
    const url = address + '/freevideo/agent';
    return t(`Connect to FreeVideo: open ${url} and follow the instructions.`, `请连接 FreeVideo：打开 ${url} 并按说明操作。`);
}

// Whether this FreeVideo has the Agent interface on. Any failure counts as off: the surface shows nothing.
export async function agentInterfaceEnabled(api) {
    try {
        const response = await api.fetchApi('/freevideo/agent', {cache: 'no-store'});
        return response.ok && (await response.json()).enabled === true;
    } catch { return false; }
}

const ICONS = {plug: ['M9 7V3M15 7V3M7 7h10v4a5 5 0 0 1-10 0V7zM12 16v5', 1.8], copy: ['M11 9h7a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-7a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2zM5 15V5a2 2 0 0 1 2-2h8', 2]};
function icon(name) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('aria-hidden', 'true');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path'); path.setAttribute('d', ICONS[name][0]);
    svg.setAttribute('stroke-width', String(ICONS[name][1])); svg.append(path);
    return svg;
}

// The card alone: who can connect and one button that copies the sentence to paste to the agent. The sentence
// stays out of sight; it shows, selected, only when the browser refuses the clipboard. shown() says whether the
// user can see the card now; the surface calls sync() whenever that may have changed.
export function agentConnectCard(t, api, shown = () => true) {
    const css = document.createElement('link'); css.rel = 'stylesheet'; css.href = new URL('./agent_connect.css', import.meta.url).href; document.head.append(css);
    const body = el('section', 'fv-agent-connect-body');
    const mark = el('span', 'fv-agent-connect-icon'); mark.append(icon('plug'));
    const title = el('h2', null, t('Connect an agent', '连接 Agent'));
    const subtitle = el('p', 'fv-muted', t('Claude Code, Codex, FreeToken and others', 'Claude Code、Codex、FreeToken 等'));
    const actions = el('div', 'fv-agent-connect-actions');
    const button = el('button', 'fv-primary'); button.type = 'button'; button.disabled = true;
    const label = el('span'); button.append(icon('copy'), label);
    const arrow = el('span', 'fv-agent-connect-arrow', '→'); arrow.setAttribute('aria-hidden', 'true');
    actions.append(button, arrow, el('span', 'fv-agent-connect-hint', t('Paste to your agent', '粘贴给 Agent')));
    const sentence = el('p', 'fv-agent-sentence'); sentence.hidden = true;
    const status = el('p', 'fv-agent-connect-status', t('Getting the connection address…', '正在获取连接地址…')); status.setAttribute('role', 'status');
    body.append(mark, title, subtitle, actions, sentence, status);

    let feedback = false, text = '', failures = 0;
    let timer, feedbackTimer, controller, generation = 0;
    const active = () => body.isConnected && shown();
    const render = () => {
        label.textContent = !text && failures >= 3 ? t('Could not get address. Retry', '获取失败，重试') : feedback ? t('Copied', '已复制') : t('Copy', '复制');
        button.disabled = !text && failures < 3;
        button.title = !text && failures < 3 ? t('Getting the connection address…', '正在获取连接地址…') : '';
        status.hidden = !!text; status.textContent = !text && failures < 3 ? t('Getting the connection address…', '正在获取连接地址…') : '';
    };
    render();

    button.onclick = async () => {
        if (!text) { failures = 0; render(); await load(); return; }
        try { await navigator.clipboard.writeText(text); sentence.hidden = true; }
        catch {
            sentence.textContent = text; sentence.hidden = false;
            const range = document.createRange(); range.selectNodeContents(sentence);
            const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range);
        }
        if (!active()) return;
        feedback = true; render();
        clearTimeout(feedbackTimer);
        feedbackTimer = setTimeout(() => { feedback = false; render(); }, 2000);
    };

    function stop() {
        generation++; clearTimeout(timer); timer = null;
        controller?.abort(); controller = null;
        clearTimeout(feedbackTimer); feedback = false; render();
    }
    // Only the sentence is read here, until it is known; who is connected comes from the surface that holds the card.
    async function load() {
        const current = generation;
        const request = new AbortController(); controller = request;
        try {
            // The sentence carries no parameter: the server gives the agent the language this card showed.
            const response = await api.fetchApi('/freevideo/agent/connect?lang=' + t('en', 'zh'), {cache: 'no-store', signal: request.signal});
            const connect = response.ok ? await response.json() : null;
            if (current !== generation) return;
            if (!connect?.address) throw new Error('Missing address');
            text = connectionSentence(connect.address, t);
            failures = 0; render();
        } catch { if (current === generation) { failures++; render(); } }
        finally {
            if (current === generation) {
                controller = null;
                if (!text && active() && failures < 3) timer = setTimeout(() => { timer = null; void load(); }, 3000);
            }
        }
    }
    function sync() {
        if (!active()) { stop(); return; }
        if (!text && failures < 3 && !controller && !timer) load();
    }
    // Resolves to whether this FreeVideo has the Agent interface on; a surface shows nothing when it is off.
    const enabled = agentInterfaceEnabled(api);
    return {element: body, enabled, sync, stop};
}
