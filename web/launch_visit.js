// The launcher opens ComfyUI with freevideo=launch and then removes it from the address. The tab remembers
// that, so a refresh of that tab behaves like the visit did; a new tab or the user's own ComfyUI does not.
const MARK = 'freevideo.launched';

export function isLaunchVisit(search, storage) {
    const launched = new URLSearchParams(search || '').get('freevideo') === 'launch';
    try {
        if (launched) storage?.setItem(MARK, '1');
        return launched || storage?.getItem(MARK) === '1';
    } catch { return launched; }
}
