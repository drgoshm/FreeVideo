"""Studio capability limits for Agent generation plans, without hardware probing."""
import json
import time

# A size asked for the first time is worked out in the background in about 0.1 s. The plan that asks first
# waits for it, so it answers what the submit after it will do.
WAIT_SECONDS = 3.


def capability_table(port, spec, *, wait=0., references=None, task=None):
    """references: panel metadata rows (see capabilities.normalize_references) for a layout other than the default."""
    query = dict(width=str(spec['width']), height=str(spec['height']),
                 task=task or spec['task'], adapters='1' if spec['loras'] else '0')
    if references is not None:
        query['references'] = json.dumps(references, separators=(',', ':'))
    deadline = time.monotonic() + wait
    try:
        while True:
            table = port.capability_snapshot(query)
            if table.get('status') != 'computing' or time.monotonic() >= deadline:
                return table
            time.sleep(.05)
    except Exception:
        # The studio cache is advisory. Its failure retains the unknown-limit policy.
        return dict(status='unavailable')


def single_frame_limit(spec, *, port=None, table=None):
    """Frame limit for this normalized size/sampling/task; unknown keeps the 15 s default.

    A submitted spec pins even an unknown limit: a later cache refresh must not
    change the accepted canvas or number of parts. Old receipts also keep their
    unknown limit when no capability source is supplied.
    """
    if 'single_max_frames' in spec:
        return spec['single_max_frames']
    if table is None:
        table = capability_table(port, spec, wait=WAIT_SECONDS) if port is not None else {}
    if table.get('status') != 'ready':
        return None
    tiers = table.get('tiers', {})
    tier = tiers.get(spec['quality'])
    if tier is None:
        # The same plan with the same step cache first. A cache keeps the length a
        # card fits (the planner drops a first-block cache that does not fit),
        # so the same plan with another cache is the next best row.
        same = [row for row in tiers.values()
                if row.get('steps') == spec['base_steps'] and row.get('two_pass') == spec['two_pass']
                and (not spec['two_pass'] or spec['refine_steps'] == 3)]
        tier = next((row for row in same if row.get('step_cache') == spec.get('step_cache')),
                    same[0] if same else {})
    index = next((i for i, row in enumerate(table.get('sizes', []))
                  if (row['width'], row['height']) == (spec['width'], spec['height'])), None)
    if index is None:
        return None
    maxima = tier.get('max_frames', [])
    limit = maxima[index] if isinstance(maxima, list) and index < len(maxima) else None
    return limit if type(limit) is int and limit > 0 else None
