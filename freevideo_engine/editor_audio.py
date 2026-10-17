"""Pure timeline audio envelopes; volume is applied by the caller to base samples."""
import math
import re

from .editor_speed import changed, audio_changed, source_sample_start


def clip_fields(raw, kind):
    result = {}
    for name in ('fade_in', 'fade_out', 'crossfade'):
        if name in raw:
            if kind != 'audio' or type(raw[name]) is not int or not 0 <= raw[name] <= 120:
                raise ValueError('Invalid audio envelope')
            result[name] = raw[name]
    if 'detached_from' in raw:
        value = raw['detached_from']
        if (not isinstance(value, str) or not re.fullmatch(r'c[1-9]\d{0,8}', value)
                or value == raw['id'] or raw.get('link')):
            raise ValueError('Invalid detached audio')
        result['detached_from'] = value
    return result


def track_fields(raw):
    result = {}
    for name in ('muted', 'solo', 'locked'):
        if name in raw:
            if type(raw[name]) is not bool:
                raise ValueError('Invalid audio track flag')
            result[name] = raw[name]
    return result


def track_audible(track, tracks):
    return not track.get('muted') and (not any(row.get('solo') for row in tracks) or bool(track.get('solo')))


def clip_audible(clip, track, tracks):
    return (track_audible(track, tracks) and not (clip.get('speed_audio') == 'mute' and changed(clip))
            and not any(row['kind'] == 'video' and row.get('muted')
                        and any(video['id'] == clip.get('link') for video in row['clips']) for row in tracks))


def _samples(frames, rate, fps):
    return math.floor(frames * rate / fps + .5)


def _source_start(clip, rate, fps):
    if audio_changed(clip):
        return math.floor(source_sample_start(clip) * rate / 48000 + .5)
    splice = clip.get('splice')
    return (math.floor((splice['audio_samples'] / 32000 + (clip['in'] - splice['frames']) / fps) * rate + .5)
            if splice else _samples(clip['in'], rate, fps))


def crossfade_reads(track, rate=48000, fps=24):
    """Raw extra read positions/counts at each contiguous audio junction.

    Changed speed or reverse forbids extension on that side. Odd sample counts
    place the extra sample after the junction. Source coordinates include splice.
    """
    result = []
    for left, right in zip(track['clips'], track['clips'][1:]):
        if (not left.get('source') or not right.get('source') or not right.get('crossfade')
                or left['start'] + left['frames'] != right['start']):
            continue
        count = _samples(min(right['crossfade'], 120, left['frames'], right['frames']), rate, fps)
        pre, post = count // 2, count - count // 2
        def extend(clip):
            return not changed(clip)
        tail = (min(post, max(0, _samples(left['length'], rate, fps) - _source_start(left, rate, fps)
                            - _samples(left['frames'], rate, fps))) if extend(left) else 0)
        head = min(pre, max(0, _source_start(right, rate, fps))) if extend(right) else 0
        result.append(dict(left=left['id'], right=right['id'], boundary=_samples(right['start'], rate, fps),
                           pre=pre, post=post, left_tail=tail, right_head=head,
                           left_read_start=_source_start(left, rate, fps) + _samples(left['frames'], rate, fps),
                           right_read_start=_source_start(right, rate, fps) - head))
    return result


def _ramp(position, length):
    import numpy as np
    return (position >= 0).astype(float) if length <= 1 else np.clip(position / (length - 1), 0, 1)


def fade_gain(clip, local, length, rate=48000, fps=24):
    import numpy as np
    local = np.asarray(local)
    gain = np.ones(local.shape, dtype=np.float64)
    if clip.get('fade_in'):
        gain *= _ramp(local, _samples(min(120, clip['frames'] // 2, clip['fade_in']), rate, fps))
    if clip.get('fade_out'):
        gain *= _ramp(length - 1 - local, _samples(min(120, clip['frames'] // 2, clip['fade_out']), rate, fps))
    return gain


def apply_track_audio(block, block_start, track, tracks, *, rate=48000, fps=24, extras=None):
    """Return a new planar C×N block, with the same shape and timeline origin.

    Base block already has volume. extras[right_id] contains raw planar
    left_tail/right_head arrays. Missing extras use a fade out/in at the cut.
    Work depends on block size; no decoding, file access or whole-edit buffer.
    """
    import numpy as np
    if type(block_start) is not int or block_start < 0 or not math.isfinite(rate) or rate <= 0 or not math.isfinite(fps) or fps <= 0:
        raise ValueError('Invalid timeline audio coordinates')
    output = np.array(block, dtype=np.float64, copy=True)
    if output.ndim != 2:
        raise ValueError('Expected planar audio')
    count = output.shape[1]
    if not track_audible(track, tracks):
        output.fill(0)
        return output
    added = np.zeros_like(output)
    plans = []
    for raw in crossfade_reads(track, rate, fps):
        extra = (extras or {}).get(raw['right'], {})
        data = {}
        for name in ('left_tail', 'right_head'):
            value = np.asarray(extra.get(name, np.zeros((output.shape[0], 0))), dtype=np.float64)
            if value.ndim != 2 or value.shape[0] != output.shape[0]:
                raise ValueError('Extra read channel mismatch')
            data[name] = value
        plans.append(dict(raw, tail=min(raw['left_tail'], data['left_tail'].shape[1]),
                          head=min(raw['right_head'], data['right_head'].shape[1]), extra=data))
    for clip in track['clips']:
        start = _samples(clip['start'], rate, fps)
        stop = _samples(clip['start'] + clip['frames'], rate, fps)
        incoming = next((p for p in plans if p['right'] == clip['id']), None)
        outgoing = next((p for p in plans if p['left'] == clip['id']), None)
        lo = start - (incoming['head'] if incoming else 0)
        hi = stop + (outgoing['tail'] if outgoing else 0)
        audible = clip_audible(clip, track, tracks)
        def gain(positions):
            value = fade_gain(clip, positions - lo, hi - lo, rate, fps) if audible else np.zeros(positions.shape)
            for plan, right in ((incoming, True), (outgoing, False)):
                if plan:
                    if plan['head'] + plan['tail']:
                        a, b = plan['boundary'] - plan['head'], plan['boundary'] + plan['tail']
                        weight = _ramp(positions - a, b - a)
                        value *= weight if right else 1 - weight
                    else:
                        value *= (_ramp(positions - start, plan['post']) if right
                                  else _ramp(stop - 1 - positions, plan['pre']))
            return value
        a, b = max(block_start, start), min(block_start + count, stop)
        if b > a:
            output[:, a - block_start:b - block_start] *= gain(np.arange(a, b))[None, :]
        for plan, name, begin, size in ((incoming, 'right_head', lo, incoming['head'] if incoming else 0),
                                        (outgoing, 'left_tail', stop, outgoing['tail'] if outgoing else 0)):
            if not size:
                continue
            a, b = max(block_start, begin), min(block_start + count, begin + size)
            if b > a:
                data = plan['extra'][name]
                offset = data.shape[1] - size if name == 'right_head' else 0
                added[:, a - block_start:b - block_start] += (data[:, offset + a - begin:offset + b - begin]
                    * gain(np.arange(a, b))[None, :] * clip.get('volume', 1))
    return output + added
