"""Subtitle cues: timed words -> frame-exact cues, checks, and SRT/WebVTT text.

Pure functions. No models, no files, no network. Times from the alignment or
recognition step arrive in seconds; cues leave as integer frames on the 24 fps
editor timeline, ready for the subtitle track.
"""
import re

FPS = 24
WRAP = .86          # editor_text.wrap_text wraps at this share of the picture width
MIN_FRAMES = 24     # a cue stays up for a second when the next one leaves room
GAP_BREAK = .35     # a pause this long (seconds) is a natural place to start a new cue
MAX_CUES = 500      # the subtitle track holds at most this many clips

_CJK = re.compile(r'[\u2e80-\u9fff\u3400-\u4dbf\uf900-\ufaff\uff00-\uffef\u3000-\u303f\U00020000-\U0003ffff]')
_SOFT_END = '，,、；;：:。.!?！？…'
_TRAILING = '，,、；;：:。.'


def is_cjk(char):
    return bool(_CJK.fullmatch(char))


def frame_of(seconds, fps=FPS):
    return int(seconds * fps + .5)


def measurer(font):
    """Width of a line the way editor_text lays it out: per-character advances."""
    return lambda text: sum(font.getlength(char) for char in text)


def _join(parts):
    """Chinese runs without spaces, Latin words with one space."""
    result = ''
    for part in parts:
        if result and not (is_cjk(result[-1]) or is_cjk(part[:1])) and not result.endswith(' '):
            result += ' '
        result += part
    return result


def words_of(text, start, end):
    """Evenly spread words over one sentence when a model gave no word times."""
    parts = []
    for chunk in re.findall(r'\S+', text):
        run = ''
        for char in chunk:
            if is_cjk(char):
                if run:
                    parts.append(run)
                    run = ''
                parts.append(char)
            else:
                run += char
        if run:
            parts.append(run)
    weights = [max(1, len(p)) for p in parts]
    total = sum(weights)
    result, done = [], 0
    for part, weight in zip(parts, weights):
        result.append(dict(text=part, start=start + (end - start) * done / total,
                           end=start + (end - start) * (done + weight) / total))
        done += weight
    return result


def _clean(text):
    text = text.strip()
    while text and text[-1] in _TRAILING:
        text = text[:-1].rstrip()
    return text


def _segments(words, measure, limit):
    """Indexes where each cue starts; greedy fill, preferring pauses and punctuation."""
    cuts, begin, best = [0], 0, None
    for index in range(1, len(words)):
        previous = words[index - 1]
        line = _join(w['text'] for w in words[begin:index + 1])
        soft = previous['text'][-1:] in _SOFT_END or words[index]['start'] - previous['end'] >= GAP_BREAK
        if soft and index - begin >= 2:
            best = index
        if measure(_clean(line)) > limit:
            cut = best if best is not None and best > begin else index
            cuts.append(cut)
            begin, best = cut, None
            if cut != index:
                # Re-check what stayed behind the cut with the current word.
                again = _join(w['text'] for w in words[begin:index + 1])
                if measure(_clean(again)) > limit:
                    cuts.append(index)
                    begin = index
    return cuts


def build_cues(sentences, measure, width, *, fps=FPS, total_frames=None, min_frames=MIN_FRAMES):
    """Sentences with times -> [{start, frames, text, sentence}], non-overlapping.

    `sentences`: dicts with text, start, end (seconds), optional words
    [{text,start,end}] and matched (False sentences are skipped; no time is
    invented for them). `measure(text)` and `width` come from the picture:
    each cue fits one line at the project's real font and size.
    """
    limit = width * WRAP
    cues = []
    for number, sentence in enumerate(sentences):
        if sentence.get('matched', True) is False or not str(sentence.get('text', '')).strip():
            continue
        words = [w for w in (sentence.get('words') or []) if str(w.get('text', '')).strip()] \
            or words_of(sentence['text'], sentence['start'], sentence['end'])
        cuts = _segments(words, measure, limit) + [len(words)]
        groups = [words[first:last] for first, last in zip(cuts, cuts[1:]) if words[first:last]]
        # A one- or two-character tail reads badly alone: fold it back when it fits,
        # otherwise hand it words from the piece before it.
        if len(groups) > 1 and len(_join(w['text'] for w in groups[-1]).replace(' ', '')) <= 2:
            joined = groups[-2] + groups[-1]
            if measure(_clean(_join(w['text'] for w in joined))) <= limit:
                groups[-2:] = [joined]
            else:
                while len(groups[-2]) > 3 and len(_join(w['text'] for w in groups[-1]).replace(' ', '')) <= 2:
                    groups[-1].insert(0, groups[-2].pop())
        pieces = [(_clean(_join(w['text'] for w in g)), g[0]['start'], g[-1]['end']) for g in groups]
        for text, start, end in pieces:
            if text:
                first_frame = frame_of(start, fps)
                cues.append(dict(start=first_frame, frames=max(1, frame_of(end, fps) - first_frame), text=text, sentence=number))
    return settle(cues, total_frames=total_frames, min_frames=min_frames)


def settle(cues, *, total_frames=None, min_frames=MIN_FRAMES):
    """Order, remove overlap, hold each cue at least `min_frames` when there is room."""
    cues = sorted((dict(c) for c in cues), key=lambda c: (c['start'], c['frames']))
    result = []
    for cue in cues:
        if result:
            last = result[-1]
            if cue['start'] < last['start'] + last['frames']:
                last['frames'] = max(1, cue['start'] - last['start'])
        result.append(cue)
    for index, cue in enumerate(result):
        room = result[index + 1]['start'] if index + 1 < len(result) else (total_frames if total_frames is not None else 10 ** 7)
        if cue['frames'] < min_frames:
            cue['frames'] = max(cue['frames'], min(min_frames, room - cue['start']))
    if total_frames is not None:
        result = [c for c in result if c['start'] < total_frames]
        for cue in result:
            cue['frames'] = min(cue['frames'], total_frames - cue['start'])
    return result[:MAX_CUES]


def check_cues(cues, measure, width, *, total_frames=None):
    """Problems a writer should fix before the track is saved. Empty means fine."""
    limit = width * WRAP
    problems, end = [], 0
    if len(cues) > MAX_CUES:
        problems.append(dict(index=MAX_CUES, reason='too_many'))
    for index, cue in enumerate(cues):
        if not (isinstance(cue.get('start'), int) and isinstance(cue.get('frames'), int)) or cue['start'] < 0 or cue['frames'] < 1:
            problems.append(dict(index=index, reason='time'))
            continue
        if cue['start'] < end:
            problems.append(dict(index=index, reason='overlap'))
        end = cue['start'] + cue['frames']
        if total_frames is not None and end > total_frames:
            problems.append(dict(index=index, reason='after_end'))
        text = cue.get('text')
        if not isinstance(text, str) or not text.strip() or len(text) > 8000:
            problems.append(dict(index=index, reason='text'))
            continue
        lines = text.split('\n')
        if len(lines) > 2:
            problems.append(dict(index=index, reason='too_many_lines'))
        if any(measure(line) > limit for line in lines):
            problems.append(dict(index=index, reason='too_wide'))
    return problems


def _clock(frame, fps, comma):
    millis = (frame * 1000 * 2 + fps) // (2 * fps)      # nearest millisecond, ties up
    hours, millis = divmod(millis, 3600000)
    minutes, millis = divmod(millis, 60000)
    seconds, millis = divmod(millis, 1000)
    return '%02d:%02d:%02d%s%03d' % (hours, minutes, seconds, ',' if comma else '.', millis)


def _body(text):
    return '\n'.join(line for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n') if line.strip())


def to_srt(cues, *, fps=FPS, offset=0):
    """SubRip with a UTF-8 byte order mark, which every common player accepts."""
    blocks = []
    for number, cue in enumerate(cues, 1):
        start = cue['start'] - offset
        blocks.append('%d\n%s --> %s\n%s\n' % (number, _clock(start, fps, True),
                                              _clock(start + cue['frames'], fps, True), _body(cue['text'])))
    return '\ufeff' + '\n'.join(blocks)


def to_vtt(cues, *, fps=FPS, offset=0):
    blocks = ['WEBVTT\n']
    for cue in cues:
        start = cue['start'] - offset
        text = _body(cue['text']).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        blocks.append('%s --> %s\n%s\n' % (_clock(start, fps, False), _clock(start + cue['frames'], fps, False), text))
    return '\n'.join(blocks)


def from_srt(text, *, fps=FPS):
    """Parse SubRip back into cues (the same limits as the page's importer)."""
    if not isinstance(text, str) or len(text) > 4 * 1024 * 1024:
        raise ValueError('Invalid subtitles')
    blocks = re.split(r'\n\s*\n', text.lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n').strip())
    if not blocks or len(blocks) > MAX_CUES:
        raise ValueError('Invalid subtitles')
    cues, end = [], 0
    for block in blocks:
        lines = block.split('\n')
        if re.fullmatch(r'\d+', lines[0]):
            lines.pop(0)
        match = re.fullmatch(r'(\d{2,3}):([0-5]\d):([0-5]\d)[,.](\d{3}) --> (\d{2,3}):([0-5]\d):([0-5]\d)[,.](\d{3})', lines.pop(0)) if lines else None
        if not match or not lines:
            raise ValueError('Invalid subtitles')
        def at(base):
            h, m, s, ms = (int(match.group(base + i)) for i in range(4))
            return frame_of(h * 3600 + m * 60 + s + ms / 1000, fps)
        start, stop = at(1), at(5)
        if start < end or stop <= start or stop > 10 ** 7:
            raise ValueError('Invalid subtitles')
        end = stop
        cues.append(dict(start=start, frames=stop - start, text='\n'.join(lines)))
    return cues
