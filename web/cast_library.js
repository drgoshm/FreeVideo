import {agentGet, agentPost, agentSend} from './agent_session.js';
import {agentInterfaceEnabled} from './agent_connect.js';

// The optional generator owns its UI and job lifecycle; keep its contract here.
async function mountGenerator(options) {
    try {
        const {mountCastGenerate, castGenerateSupported} = await import('./cast_generate.js');
        if (typeof mountCastGenerate !== 'function' || (castGenerateSupported && !castGenerateSupported())) return null;
        return await mountCastGenerate(options);
    } catch { return null; }
}

const el = (tag, className, value) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined) node.textContent = value;
    return node;
};
const button = (className, label, action) => {
    const node = el('button', className, label); node.type = 'button'; node.onclick = action; return node;
};
const path = id => '/freevideo/agent/cast/' + encodeURIComponent(id);

export function createCastLibrary({api, text, viewURL, uploadOne, onChange = () => {}}) {
    if (!document.querySelector('link[data-freevideo-cast]')) {
        const css = el('link'); css.rel = 'stylesheet'; css.dataset.freevideoCast = '';
        css.href = new URL('./cast_library.css', import.meta.url).href; document.head.append(css);
    }
    let rows = [], mentioned = new Set(), disposed = false, request = 0, current = null, broadcasting = false;
    const subscribers = new Set();
    const element = el('div', 'fv-cast-library');
    // The row shows, and the page session is asked for, only once /freevideo/agent says the interface is on.
    let available = false; element.hidden = true;
    const probe = agentInterfaceEnabled(api).then(on => { available = on && !disposed; element.hidden = !available; return available; });
    const heading = el('span', 'fv-cast-heading', text('Characters', '角色'));
    const avatars = el('div', 'fv-cast-avatars');
    const add = button('fv-cast-add', '+', () => openDialog());
    add.setAttribute('aria-label', text('New character', '新建角色'));
    element.append(heading, avatars, add);

    async function value(response) {
        const data = await response.json().catch(() => null);
        if (!response.ok) {
            const error = new Error(data?.error?.message || (typeof data?.error === 'string' && data.error)
                || data?.message || response.statusText || text('Did not finish. Try again.', '没有完成，请重试。'));
            error.status = response.status; throw error;
        }
        return data;
    }
    // The front picture always comes first: the @ popup and the edit dialog both rely on it.
    const get = async id => {
        const card = await value(await agentGet(api, path(id)));
        const images = card.images || [], front = images.find(image => image.label === 'front');
        return {...card, images: front ? [front, ...images.filter(image => image !== front)] : images};
    };
    function render() {
        avatars.replaceChildren(); heading.hidden = !rows.length;
        for (const row of rows) {
            const avatar = button('fv-cast-avatar', '', () => openDialog(row.id));
            avatar.classList.toggle('fv-cast-mentioned', mentioned.has(row.id));
            avatar.title = row.name; avatar.setAttribute('aria-label', row.name);
            const image = row.thumbnail?.file ? el('img') : el('span', 'fv-cast-initial', row.name.slice(0, 1));
            if (row.thumbnail?.file) { image.src = viewURL(row.thumbnail.file); image.alt = ''; image.loading = 'lazy'; }
            avatar.append(image); avatars.append(avatar);
        }
    }
    async function refresh() {
        if (disposed || !await probe) return rows;
        const serial = ++request;
        const data = await value(await agentGet(api, '/freevideo/agent/cast?kind=character&limit=200'));
        if (!disposed && serial === request) {
            rows = data.cards; render(); onChange(rows);
            for (const fn of subscribers) fn(rows);
        }
        return rows;
    }
    const changed = () => {
        if (!broadcasting) refresh().catch(error => current?.message(error.message));
    };
    async function saved() {
        broadcasting = true;
        try { window.dispatchEvent(new CustomEvent('freevideo-cast-changed')); }
        finally { broadcasting = false; }
        await refresh();
    }

    function openDialog(id = null) {
        if (disposed || !available) return;
        current?.close();
        let closed = false, loading = !!id, saving = false, revision, active = 'upload';
        let generator = null, aiState = {busy: false, done: false}, deleteTimer = null, confirming = false, frontless = false;
        const slots = Array(6).fill(null), slotUI = [];
        const dialog = el('dialog', 'fv-cast-dialog');
        const top = el('div', 'fv-cast-top');
        const title = el('h2', '', id ? text('Edit character', '编辑角色') : text('New character', '新建角色'));
        dialog.setAttribute('aria-label', title.textContent);
        const closeButton = button('fv-cast-close', '×', () => close());
        closeButton.setAttribute('aria-label', text('Close', '关闭')); top.append(title, closeButton);
        const name = el('input', 'fv-cast-name'); name.type = 'text'; name.maxLength = 120;
        name.placeholder = text('Name', '名字'); name.setAttribute('aria-label', text('Name', '名字'));
        name.oninput = () => sync();
        name.onkeydown = event => { if (event.key === 'Enter') event.preventDefault(); };
        const tabs = el('div', 'fv-cast-tabs'); tabs.setAttribute('role', 'tablist');
        tabs.setAttribute('aria-label', text('Images', '图片'));
        const uploadTab = button('', text('Upload images', '上传图片'), () => select('upload'));
        const aiTab = button('', text('Generate with AI', 'AI 生成'), () => select('ai')); aiTab.hidden = tabs.hidden = true;
        for (const tab of [uploadTab, aiTab]) tab.setAttribute('role', 'tab');
        // Tab panels are dialog-local, so multiple material panels can coexist.
        const uploadPane = el('div', 'fv-cast-upload'), views = el('div', 'fv-cast-views');
        const aiPane = el('div', 'fv-cast-generate'); aiPane.hidden = true;
        const status = el('div', 'fv-cast-message'); status.setAttribute('role', 'status'); status.hidden = true;
        const actions = el('div', 'fv-cast-actions');
        const remove = button('fv-cast-delete', text('Delete character', '删除角色'), () => deleteCard()); remove.hidden = !id;
        const cancel = button('', text('Cancel', '取消'), () => close());
        const save = button('fv-cast-save', text('Save', '保存'), () => saveCard());
        actions.append(remove, cancel, save);
        dialog.append(top, name, tabs, uploadPane, aiPane, status, actions); tabs.append(uploadTab, aiTab);

        // A refresh notice is plain secondary text; only failures are shown as errors.
        const message = (content, notice = false) => {
            if (closed) return;
            status.textContent = content; status.hidden = !content; status.classList.toggle('fv-cast-notice', notice);
        };
        const aiBusy = () => aiState.busy || !!generator?.isBusy();
        // What is still missing before Save works ('' when nothing is); the button says so on hover and when pressed.
        let missing = '';
        const missingNow = () => !name.value.trim() ? text('Enter a name first', '先填写名字')
            : aiBusy() ? text('Wait for the five pictures to finish', '等待五张图生成完成')
            : active === 'upload' ? (frontless ? slots.some(Boolean) : slots[0]) ? '' : frontless ? text('Add an image first', '先添加图片') : text('Add a front image first', '先添加正面图片')
            : aiState.done ? '' : text('Generate the images first', '先生成角色图片');
        function sync() {
            const busy = loading || saving;
            name.disabled = busy; remove.disabled = busy;
            uploadTab.disabled = aiTab.disabled = busy;
            const before = missing;
            missing = busy ? '' : missingNow();
            save.disabled = busy;
            if (missing) { save.setAttribute('aria-disabled', 'true'); save.title = missing; }
            else { save.removeAttribute('aria-disabled'); save.removeAttribute('title'); }
            if (before && before !== missing && status.textContent === before) message('');
            for (let index = 0; index < slotUI.length; index++) {
                slotUI[index].choose.disabled = busy; slotUI[index].remove.disabled = busy;
            }
        }
        function select(tab) {
            active = tab;
            uploadPane.hidden = tab !== 'upload'; aiPane.hidden = tab !== 'ai';
            for (const [key, node] of [['upload', uploadTab], ['ai', aiTab]]) {
                node.setAttribute('aria-selected', String(key === tab)); node.tabIndex = key === tab ? 0 : -1;
            }
            sync();
        }
        tabs.onkeydown = event => {
            if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key) || aiTab.hidden || saving || loading) return;
            event.preventDefault();
            select(event.key === 'Home' ? 'upload' : event.key === 'End' ? 'ai' : active === 'upload' ? 'ai' : 'upload');
            (active === 'upload' ? uploadTab : aiTab).focus();
        };
        function release(index) {
            if (slots[index]?.preview) URL.revokeObjectURL(slots[index].preview);
            slots[index] = null;
        }
        function paint(index) {
            const {choose, remove} = slotUI[index], picture = slots[index];
            choose.replaceChildren(); let image = null;
            if (picture) {
                image = el('img'); image.alt = '';
                // The remove button waits for the picture, so a slot still loading never looks like a filled one.
                const shown = () => { if (slots[index] === picture) remove.hidden = false; };
                image.onload = image.onerror = shown;
                image.src = picture.preview || viewURL(typeof picture.media === 'string' ? picture.media : picture.media.file);
                choose.append(image);
            } else {
                choose.append(el('span', 'fv-cast-slot-plus', '+'));
                if (!index) choose.append(el('span', '', text('Front image', '正面图片')));
            }
            remove.hidden = true; if (image?.complete) remove.hidden = false; sync();
        }
        function chooseFiles(index, files) {
            if (loading || saving || closed) return;
            for (const file of Array.from(files).slice(0, 6 - index)) {
                release(index);
                slots[index] = {file, label: index ? 'other' : 'front', preview: URL.createObjectURL(file)};
                paint(index++);
            }
            message('');
        }
        for (let index = 0; index < 6; index++) {
            const slot = el('div', 'fv-cast-slot' + (index ? '' : ' fv-cast-front'));
            const input = el('input'); input.type = 'file'; input.accept = 'image/*'; input.hidden = true;
            const label = index ? text(`View ${index}`, `视角 ${index}`) : text('Front image', '正面图片');
            const choose = button('fv-cast-choose', '', () => input.click()); choose.setAttribute('aria-label', label);
            const remove = button('fv-cast-remove', '×', () => { release(index); paint(index); });
            remove.setAttribute('aria-label', text('Remove image', '移除图片'));
            input.onchange = () => { chooseFiles(index, input.files); input.value = ''; };
            slot.ondragover = event => { event.preventDefault(); event.stopPropagation(); };
            slot.ondrop = event => { event.preventDefault(); event.stopPropagation(); chooseFiles(index, event.dataTransfer.files); };
            slot.append(choose, remove, input); slotUI.push({choose, remove});
            (index ? views : uploadPane).append(slot); paint(index);
        }
        uploadPane.append(views);
        function resetDelete() {
            clearTimeout(deleteTimer); deleteTimer = null; confirming = false;
            remove.textContent = text('Delete character', '删除角色'); remove.classList.remove('fv-cast-confirm');
        }
        async function load() {
            loading = true; sync();
            try {
                const card = await get(id);
                if (closed) return;
                revision = card.revision; name.value = card.name;
                for (let i = 0; i < 6; i++) release(i);
                // A card may have no front picture (the API does not require one): all its pictures stay, in order, and
                // saving does not ask for a front; the front slot is only reserved while there is a front to keep first.
                frontless = card.images.length > 0 && card.images[0].label !== 'front';
                card.images.slice(0, 6).forEach((picture, index) => { slots[index] = {...picture}; });
                slotUI.forEach((_, index) => paint(index));
                generator?.cancel(); aiState = {busy: false, done: false}; select('upload'); resetDelete();
            } finally { loading = false; if (!closed) sync(); }
        }
        async function failure(error) {
            if (closed) return;
            if (error.status === 409 && id) {
                try {
                    await load();
                    message(text('Changed elsewhere. Showing the latest version.', '已在别处修改，已显示最新内容'), true);
                } catch (error) { message(error.message); }
            } else message(error.message);
        }
        async function saveCard() {
            if (closed || save.disabled) return;
            if (missing) { message(missing, true); return; }
            saving = true; resetDelete(); message(''); sync();
            try {
                const pictures = active === 'upload' ? slots.filter(Boolean) : generator.getPictures().map((picture, index) => ({
                    ...(typeof picture.file === 'string' ? {media: picture.file} : {file: picture.file}),
                    label: picture.label || (index ? 'other' : 'front'),
                }));
                const frontNeeded = active === 'ai' || !frontless;
                if (!pictures.length || pictures.length > 6 || (frontNeeded && !pictures.some(picture => picture.label === 'front')))
                    throw new Error(text('Choose one front image and up to 5 more angles', '选一张正面图片，再加最多 5 张其他角度的图片'));
                const images = [];
                for (const picture of pictures) {
                    if (closed) return;
                    // Cache uploads on the slot so retrying a refused save does not upload twice.
                    if (picture.file && !picture.media) picture.media = (await uploadOne(picture.file)).file;
                    images.push({media: picture.media, label: picture.label});
                }
                if (closed) return;
                const body = {name: name.value.trim(), images};
                await value(await (id ? agentSend(api, 'PATCH', path(id), {revision, ...body})
                    : agentPost(api, '/freevideo/agent/cast', {kind: 'character', ...body})));
                close(); await saved();
            } catch (error) { await failure(error); }
            finally { saving = false; if (!closed) sync(); }
        }
        async function deleteCard() {
            if (loading || saving || closed || revision === undefined) return;
            if (!confirming) {
                confirming = true; remove.textContent = text('Confirm delete', '确认删除'); remove.classList.add('fv-cast-confirm');
                deleteTimer = setTimeout(resetDelete, 3000); return;
            }
            saving = true; resetDelete(); message(''); sync();
            try {
                await value(await agentSend(api, 'DELETE', path(id), {}, {'If-Match': String(revision)}));
                close(); await saved();
            } catch (error) { await failure(error); }
            finally { saving = false; if (!closed) sync(); }
        }
        function close() {
            if (closed) return;
            closed = true; clearTimeout(deleteTimer);
            generator?.cancel(); generator?.destroy();
            for (let index = 0; index < 6; index++) release(index);
            if (dialog.open) dialog.close(); dialog.remove();
            if (current?.element === dialog) current = null;
            add.focus();
        }
        // A click anywhere else in the dialog takes the second step back.
        dialog.onpointerdown = event => { if (confirming && event.target !== remove) resetDelete(); };
        dialog.oncancel = event => { event.preventDefault(); close(); };
        dialog.onclose = close;
        current = {element: dialog, message, close};
        name.value = ''; select('upload'); document.body.append(dialog); dialog.showModal(); name.focus();
        if (id) load().catch(error => message(error.message));
        mountGenerator({container: aiPane, api, text, viewURL, onState: state => {
            if (closed) return;
            aiState = state; sync();
        }}).then(instance => {
            if (closed) { instance?.cancel(); instance?.destroy(); return; }
            generator = instance; aiTab.hidden = tabs.hidden = !instance; sync();
        });
    }

    // The @ popup reports which characters the prompt currently mentions; they get an accent ring.
    const mentions = event => {
        mentioned = new Set(Array.isArray(event.detail) ? event.detail : []); render();
    };
    window.addEventListener('freevideo-cast-mentioned', mentions);
    window.addEventListener('focus', changed);
    window.addEventListener('freevideo-cast-changed', changed);
    render(); changed();
    return {element, list: () => rows, get, refresh,
        subscribe(fn) { subscribers.add(fn); return () => subscribers.delete(fn); },
        openNew: () => openDialog(), openEdit: id => openDialog(id),
        destroy() {
            if (disposed) return;
            disposed = true; request++; current?.close(); subscribers.clear(); element.remove();
            window.removeEventListener('freevideo-cast-mentioned', mentions);
            window.removeEventListener('focus', changed);
            window.removeEventListener('freevideo-cast-changed', changed);
        }};
}
