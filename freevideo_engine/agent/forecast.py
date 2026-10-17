"""Machine-local timing forecasts shared by planning and durable films."""
import math
from .length import capability_table, single_frame_limit


def generation_estimate(port, spec, chain=None):
    """Return (estimate, capability revision) for a normalized generation spec.

    Every block needs matching history. Keep the plan endpoint's exclusions and
    reasons here so a film never invents a time for unsupported conditioning.
    """
    from ..chain_plan import plan as chain_plan
    table = capability_table(port, spec)
    chain = chain if chain is not None else chain_plan(spec['seconds'], max_frames=single_frame_limit(spec, table=table),
                                                     continued=bool(spec.get('continue_from')))
    seconds = None
    reason = table['status'] if table['status'] in ('computing', 'unavailable') else 'no_matching_estimate'
    if table['status'] == 'ready' and not spec['preview'] and not spec['media']:
        tier = table.get('tiers', {}).get(spec['quality'])
        size = next((i for i, s in enumerate(table.get('sizes', []))
                     if (s['width'], s['height']) == (spec['width'], spec['height'])), None)
        estimates = []
        for block in chain['blocks']:
            length = next((i for i, l in enumerate(table.get('lengths', [])) if l['frames'] == block['frames']), None)
            estimate = None
            if tier and size is not None and length is not None:
                try:
                    estimate = tier['estimate_seconds'][size][length]
                except (IndexError, KeyError, TypeError):
                    pass
            if type(estimate) not in (int, float) or not math.isfinite(estimate) or estimate <= 0:
                break
            estimates.append(estimate)
        if len(estimates) == len(chain['blocks']):
            seconds = sum(estimates)
    known = seconds is not None and seconds > 0
    return (dict(status='ready' if known else 'unavailable', seconds=seconds if known else None,
                 basis=(table.get('diagnostics') or {}).get('basis'),
                 **({} if chain['single'] else {'scope': 'sum_of_blocks'}),
                 **({} if known else {'reason': reason})), table.get('revision'))
