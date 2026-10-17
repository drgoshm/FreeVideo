"""How a requested length becomes one generation or a chain of blocks.

One rule for the creation panel, the editor and the server: under 30 s is one
generation when this device can do it; 30 s and longer, and lengths this device
cannot do at once, are cut into blocks that continue one another. A block
length given explicitly also cuts a video under 30 s.
"""
import math

from .geometry import geometry

FPS, UNIT, OVERLAP = 24, 17, 22
SINGLE_SECONDS, BLOCK_SECONDS, SHORTEST_BLOCK = 30, 15, 5
# A part that continues another is at least this long (the first part is not held to it).
MIN_PART_SECONDS = 10
MIN_PART_FRAMES = geometry(seconds=MIN_PART_SECONDS)['frames']
SHORTEST_FIRST = 2  # whole seconds: what the first part keeps when the continuing parts take the rest
MAX_SECONDS = 7200
MAX_BLOCKS = MAX_SECONDS // SHORTEST_BLOCK


class PlanError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _number(value, name, whole=False):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0 or whole and type(value) is not int:
        raise PlanError('invalid', '%s must be a positive %s' % (name, 'integer' if whole else 'number'))
    return value


def _first_frames(seconds):
    return geometry(seconds=seconds)['frames']


def plan(seconds, single=False, max_frames=None, single_limit_seconds=None, block_seconds=None, continued=False):
    """`max_frames`: the most frames this device generates at once for the current settings (unknown: 15 s).

    `continued`: the video goes on from a saved one, so its first block too spends OVERLAP frames on what
    came before and adds only the rest.
    """
    _number(seconds, 'Length')
    if type(single) is not bool:
        raise PlanError('invalid', 'Single must be true or false')
    try:
        if max_frames is not None:
            limit = _number(max_frames, 'The frame limit', whole=True)
        else:
            limit = _first_frames(BLOCK_SECONDS if single_limit_seconds is None
                                  else _number(single_limit_seconds, 'The single generation limit'))
        if seconds >= SINGLE_SECONDS:
            seconds = math.floor(seconds + .5)  # 30 s and longer are planned in whole seconds
        frames = _first_frames(seconds)
    except PlanError:
        raise
    except ValueError as error:
        raise PlanError('too_short', str(error)) from None
    if seconds > MAX_SECONDS:
        raise PlanError('too_long', 'A video is at most 2 hours long')
    # The longest whole-second block this device can start a video with, and continue one with.
    first = (limit - (limit - 5) % UNIT) // FPS if limit >= 39 else 0
    following = (limit - OVERLAP) // UNIT * UNIT // FPS if limit >= OVERLAP + UNIT else 0
    new = frames
    if continued:
        new = max(1, math.ceil(round(FPS * seconds, 6) / UNIT)) * UNIT
        frames = new + OVERLAP
    block = BLOCK_SECONDS
    if block_seconds is not None:
        block = math.floor(_number(block_seconds, 'The block length') + .5)
        if block < SHORTEST_BLOCK:
            raise PlanError('block_too_short', 'A block is at least %d s long' % SHORTEST_BLOCK)
        if block > first:
            raise PlanError('block_too_long', 'This device cannot generate a block of this length')
    fits = frames <= limit
    alone = dict(seconds=seconds, frames=new, single=True, hide_frames=0,
                 blocks=[dict(seconds=seconds, frames=frames, new_frames=new)])
    head = following if continued else first  # the longest part this video can start with
    # Under 30 s stays one generation unless a block length was asked for; 30 s is two blocks of 15 s.
    if fits and (single or block_seconds is None and seconds < SINGLE_SECONDS or seconds <= min(block, head)):
        return alone
    if block < MIN_PART_SECONDS:
        if fits and seconds < SINGLE_SECONDS:
            return alone  # a block this short cannot continue another: a short video stays one generation
        raise PlanError('block_too_short', 'A block that continues another is at least %d s long' % MIN_PART_SECONDS)
    first, following = min(block, head), min(block, following)
    shortest = MIN_PART_SECONDS if continued else SHORTEST_BLOCK
    if first < shortest or following < MIN_PART_SECONDS:
        if fits and seconds < SINGLE_SECONDS:
            return alone  # a requested block length never makes a part this device cannot do
        raise PlanError('device_limit', 'This device cannot generate this length at this size')
    # Under 30 s the length stays as asked (it may be fractional); from 30 s it is whole seconds already.
    total = seconds
    # Every part that continues another is at least MIN_PART_SECONDS and at most `following`. The part that
    # starts a new video is not held to it; a rest too short for the continuing parts is taken from that part.
    rest = total if continued else total - min(first, total)
    count = math.ceil(round(rest, 6) / following) if rest > 0 else 0
    rest = max(rest, MIN_PART_SECONDS * count)
    whole = math.ceil(round(rest, 6))
    if continued and whole > math.ceil(round(total, 6)) or not continued and total - whole < SHORTEST_FIRST:
        if fits and seconds < SINGLE_SECONDS:
            return alone
        raise PlanError('length_not_splittable', 'This length cannot be cut into parts of the requested length')
    if count > MAX_BLOCKS:
        raise PlanError('too_long', 'This length needs too many blocks')
    lengths = [whole // count + (index < whole % count) for index in range(count)]
    if continued:
        lengths[0] = round(lengths[0] - (whole - total), 6)  # a fractional request keeps its length in the first part
    else:
        lengths.insert(0, round(total - whole, 6))
    frames_all = int(round(FPS * total))
    blocks, shown = [], 0
    for index, length in enumerate(lengths):
        target = int(round(FPS * sum(lengths[:index + 1])))
        if index == 0 and not continued:
            new = requested = _first_frames(length)
        else:
            # Each block takes the shortest step that reaches its whole second; nothing is cut in between.
            new = max(1, math.ceil((target - shown) / UNIT)) * UNIT
            requested = new + OVERLAP
        if requested > limit or length > (first if not index else following):
            raise PlanError('device_limit', 'This device cannot generate this length at this size')
        shown += new
        blocks.append(dict(seconds=length, frames=requested, new_frames=new))
    return dict(seconds=total, frames=frames_all, single=False, hide_frames=shown - frames_all, blocks=blocks)
