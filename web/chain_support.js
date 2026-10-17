// Pure helpers the creative panel's long-video planning shares with the editor store; kept out of
// editor_store.js so the panel does not load the editing implementation.
import { stepCacheOf } from './sampling_effort.js';
const clockDefault = {now: () => Date.now(), setTimeout: (fn, ms) => setTimeout(fn, ms), clearTimeout: id => clearTimeout(id)};
export const chainRoute = (id, action) => '/freevideo/chain' + (id ? '/' + encodeURIComponent(id) : '') + (action ? '/' + action : '');

// No overlapping polls and no delivery after disposal, including a slow response.
export function createPoller(read, deliver, {clock = clockDefault, interval = 2000, onError = () => {}} = {}) {
    let active = true, timer = null, running = null;
    async function poll() {
        if (!active || running) return running;
        running = (async () => {
            try { const value = await read(); if (active && await deliver(value) === false) active = false; }
            catch (error) { if (active) onError(error); }
        })().finally(() => {
            running = null;
            if (active) timer = clock.setTimeout(() => { timer = null; poll(); }, interval);
        });
        return running;
    }
    poll();
    return {poll, dispose() { active = false; clock.clearTimeout(timer); }};
}

export function interpolate(points, seconds) {
    const rows = points.filter(([x, y]) => Number.isFinite(x) && Number.isFinite(y) && y >= 0).sort((a, b) => a[0] - b[0]);
    if (!rows.length) return null;
    if (rows.length === 1) return rows[0][1];
    let i = rows.findIndex(([x]) => x >= seconds);
    if (i < 1) i = seconds <= rows[0][0] ? 1 : rows.length - 1;
    const [a, b] = [rows[i - 1], rows[i]];
    return b[0] === a[0] ? b[1] : Math.max(0, a[1] + (seconds - a[0]) * (b[1] - a[1]) / (b[0] - a[0]));
}
// The reply groups its numbers by quality level. An edit keeps the panel's steps,
// not a level's name, so the level is the one those steps describe (as the
// creative panel's range view picks it); other steps have no numbers.
function capabilityTier(reply, settings) {
    const tiers = reply.tiers;
    if (!tiers || typeof tiers !== 'object') return reply;
    if (tiers[settings?.tier]) return tiers[settings.tier];
    const steps = Number(settings?.base_steps), twoPass = !!settings?.two_pass, cache = stepCacheOf(settings?.step_cache);
    return Object.values(tiers).find(tier => tier.steps === steps && tier.two_pass === twoPass
        && (!twoPass || Number(settings?.refine_steps) === 3) && stepCacheOf(tier.step_cache) === cache) || {};
}
export function capabilityPoints(reply, {width, height, settings = {}}) {
    const tier = capabilityTier(reply, settings);
    const i = reply.sizes?.findIndex(size => size.width === width && size.height === height) ?? -1;
    const max = Array.isArray(tier.max_seconds) ? tier.max_seconds[i] : tier.max_seconds;
    const known = Array.isArray(tier.max_frames) ? tier.max_frames[i] : tier.max_frames;
    const maxRequestFrames = Number.isSafeInteger(known) && known >= 0 ? known
        : Number.isFinite(max) ? Math.max(39, 5 + 17 * Math.ceil((24 * max - 5) / 17)) : null;
    const longest = continues => {
        if (maxRequestFrames === null) return 15;
        for (let seconds = 30; seconds >= 2; seconds--) {
            const frames = continues ? 17 * Math.ceil(seconds * 24 / 17) + 22 : Math.max(39, 5 + 17 * Math.ceil((seconds * 24 - 5) / 17));
            if (frames <= maxRequestFrames) return seconds;
        }
        return 2; // The final request check reports too_long when even 2s cannot fit.
    };
    const estimates = i >= 0 && Array.isArray(tier.estimate_seconds?.[i]) ? tier.estimate_seconds[i] : tier.estimate_seconds;
    const lengths = reply.lengths || reply.seconds || [5, 10, 15];
    return {maxSeconds: longest(false), maxFollowSeconds: longest(true), maxRequestFrames,
        points: lengths.map((length, j) => [Number(length.seconds ?? length),
            Array.isArray(estimates) ? estimates[j] : estimates?.[length.seconds ?? length]])};
}
