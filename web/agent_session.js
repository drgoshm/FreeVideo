// Page-only control authorization. The Agent bearer token never enters the page.
const sessions = new WeakMap();
export async function agentHeaders(api) {
    let pending = sessions.get(api);
    if (!pending) {
        pending = api.fetchApi('/freevideo/agent/session', {cache: 'no-store', credentials: 'same-origin'})
            .then(async response => {
                if (!response.ok) throw new Error('Agent page session unavailable');
                const value = await response.json();
                return {'X-FreeVideo-CSRF': value.csrf};
            });
        sessions.set(api, pending);
        pending.catch(() => sessions.delete(api));
    }
    return pending;
}

// Status reads use the same page session, acquired only when the card opens.
export async function agentGet(api, path, options = {}) {
    for (let attempt = 0; ; attempt++) {
        const headers = await agentHeaders(api);
        options.signal?.throwIfAborted();
        const response = await api.fetchApi(path, {...options, cache: 'no-store', credentials: 'same-origin', headers});
        if (response.status !== 401 || attempt) return response;
        sessions.delete(api);
    }
}

// A restarted FreeVideo forgets page sessions: fetch a new one once and repeat.
export async function agentPost(api, path, body) {
    return agentSend(api, 'POST', path, body);
}

export async function agentSend(api, method, path, body, headers = {}) {
    for (let attempt = 0; ; attempt++) {
        const response = await api.fetchApi(path, {method, credentials: 'same-origin',
            headers: {'Content-Type': 'application/json', ...headers, ...await agentHeaders(api)},
            ...(body === undefined ? {} : {body: JSON.stringify(body)})});
        if (response.status !== 403 || attempt) return response;
        const value = await response.clone().json().catch(() => null);
        if (value?.error?.code !== 'page_session_required') return response;
        sessions.delete(api);
    }
}

// UI actions retry once, including HTTP refusals. Callers keep the same
// idempotency id/body and show the result where the user clicked.
export async function retryAgentPost(api, path, body, {retry = async () => body, accept = () => true} = {}) {
    for (let attempt = 0; attempt < 2; attempt++) {
        let response = null;
        try {
            response = await agentPost(api, path, body);
            if (response?.ok && await accept(response)) return response;
        } catch { /* Retry below, including an unreadable response body. */ }
        if (attempt) return response?.ok ? null : response;
        body = await retry(response);
        if (!body) return response;
    }
}
