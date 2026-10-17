"""The quality levels, defined once.

Every entry (editor, Agent API, ComfyUI capabilities, estimates, docs) reads
its level -> plan mapping from here; tests/test_tiers.py checks the entries
that cannot import it (web JS, prose docs) against this table.

- light: two-pass, 8 + 3 steps.
- medium: one pass of 8 steps.
- high: one pass of 20 steps with the audio-video EasyCache.
- max: one pass of 20 steps with the first-block cache. The planner keeps that
  cache only where the card has room for it; otherwise the request samples all
  20 steps in full (engine.json/debug record the cache actually used).

Only a two-pass level samples a second time. Single-pass levels carry
REFINE_STEPS only as the neutral value the `refine_steps` field needs; it is
never run for them. `step_cache` is the engine's cache key: None,
'easycache-av' (audio-video EasyCache) or 'fbcache' (first-block cache).

The caches are not ported to Mac (MPS) yet: there high is one pass of 16
steps and max one pass of 20, both without a cache (TIERS_MAC).
"""
import sys

REFINE_STEPS = 3
STEP_CACHES = (None, 'easycache-av', 'fbcache')

TIERS = {
    'light': dict(steps=8, two_pass=True, step_cache=None),
    'medium': dict(steps=8, two_pass=False, step_cache=None),
    'high': dict(steps=20, two_pass=False, step_cache='easycache-av'),
    'max': dict(steps=20, two_pass=False, step_cache='fbcache'),
}
TIERS_MAC = dict(TIERS, high=dict(steps=16, two_pass=False, step_cache=None),
                 max=dict(steps=20, two_pass=False, step_cache=None))

# Sampling time of a cached pass as a share of the same pass without a cache,
# for estimates that have no run with that cache yet. B200, 20 s clips
# (/mnt/fvbench/eng5/tiers/RESULTS-b200.md): EasyCache-AV sampled 1.8-1.9x and
# the first-block cache 1.4-1.5x faster than plain 20 steps. 10 s clips with the
# GPU held to 8-48 GiB (/mnt/fvbench/remy/tiers-v2/REPORT.md): EasyCache-AV
# 0.48-0.75 of plain 20 steps (median about 0.6); a first-block cache kept at
# 32-48 GiB 0.71-0.82. Under the current keep rule at 8-24 GiB
# (review/quality-tiers-v2/linux-evidence.md) it is kept at 12 and 24 GiB,
# 0.81-1.28 (median 0.87), and dropped at 8 and 16 GiB. The shares are not
# fitted to the median: EasyCache-AV is slightly conservative, the first-block
# cache near the 8-24 GiB median. A first-block cache the planner did not keep
# is plain 20 steps.
SAMPLING_SHARE = {None: 1., 'easycache-av': .64, 'fbcache': .9}


def on_mac():
    """FreeVideo runs on Apple's MPS backend on every Mac."""
    return sys.platform == 'darwin'


def table(mac=False):
    return TIERS_MAC if mac else TIERS


def plan(name, mac=False):
    """(two_pass, base_steps, refine_steps) of a level."""
    tier = table(mac)[name]
    return tier['two_pass'], tier['steps'], REFINE_STEPS


def step_cache(name, mac=False):
    return table(mac)[name]['step_cache']


def name_for(steps, two_pass, refine_steps=REFINE_STEPS, step_cache=None, mac=False):
    """The level with exactly this plan, or None for custom sampling."""
    return next((name for name, tier in table(mac).items()
                 if tier['steps'] == steps and tier['two_pass'] == two_pass
                 and (not two_pass or refine_steps == REFINE_STEPS) and tier['step_cache'] == step_cache), None)


def table_level(steps, two_pass, refine_steps=REFINE_STEPS, step_cache=None, mac=False):
    """The level whose sampling table a plan needs, for naming a missing download.

    Tables follow the step count only. Prefer the exact level (the cache tells
    high from max at 20 steps), then the other platform's table (20 steps
    without a cache is "max", as on Mac; 16 steps is "high"), then the first
    level with that step count.
    """
    found = name_for(steps, two_pass, refine_steps, step_cache, mac) or name_for(
        steps, two_pass, refine_steps, step_cache, not mac)
    return found or next((name for name, tier in TIERS.items()
                          if tier['steps'] == steps and tier['two_pass'] == two_pass
                          and (not two_pass or refine_steps == REFINE_STEPS)), None)


def sampling_share(cache):
    """Estimated sampling time with this effective cache, as a share of the plain pass."""
    return SAMPLING_SHARE.get(cache, 1.)
