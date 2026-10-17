"""Keep the voice of a long video's parts as loud, and as full, as in the first part.

Each continued part takes its sound context from the part before it, and over
several parts the voice can drift: louder, and fuller in the low range. When the
join is told which parts have spoken lines, it measures the voice in each of them
and, only where a part has drifted more than THRESHOLD_DB from the first one,
brings it back by at most MOST_DB with one gain and one low-range shelf. A part's
whole sound is changed together, so music and background keep their place
against the voice; parts without spoken lines are left exactly as they are. Each
part is filtered on its own, so nothing spills across a seam, and a change eases
in over RAMP_SECONDS on the filtered side of the seam.
"""
import math
import re

RATE = 32000
THRESHOLD_DB = 2.
MOST_DB = 6.
RAMP_SECONDS = .5
# The voice's body (fundamental and first harmonics) against its clear middle range.
LOW, MIDDLE = (60, 300), (300, 3400)
SHELF_HZ = 300.
# A part is never made louder than this peak.
HEADROOM = .95
FRAME, HOP = 4096, 2048
_GATE_DB, _FLOOR_DB, _LEAST = 15., -55., 20

# "A sign on the door says": the writing itself says the words, in the same clause.
_WRITING_SAYS = re.compile(
    r'\b(?:signs?|banners?|captions?|posters?|screens?|labels?|boards?|neon|text|placards?|graffiti|headlines?|'
    r'subtitles?|lettering)\b[^,;:.!?\n]*\b(?:says|said|reads|shows|displays)\s*[:,]?\s*$', re.I)


def voiced(text):
    """Whether a part's description has a spoken line; when unsure, no (the part is then left as it is)."""
    if not isinstance(text, str):
        return False
    from .prompt_vlm.rules import QUOTED, SPEAKER, plain, spoken_lines
    if any(plain(match.group(2)) for match in SPEAKER.finditer(text)):
        return True
    lines = {plain(line) for line in spoken_lines(text)}
    for match in QUOTED.finditer(text):
        words = plain(next(group for group in match.groups() if group is not None))
        if words in lines and not _WRITING_SAYS.search(text[:match.start()]):
            return True
    return False




def _shelf(frequencies):
    return 1. / (1. + (frequencies / SHELF_HZ) ** 2)


def _native(path, cancelled=None):
    import av
    import numpy as np
    chunks = []
    with av.open(str(path)) as container:
        if not container.streams.audio:
            return None
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format='fltp', layout='stereo', rate=RATE)
        for packet in container.demux(stream):
            if cancelled is not None and cancelled():
                return None
            for decoded in packet.decode():
                chunks.extend(frame.to_ndarray() for frame in resampler.resample(decoded))
        chunks.extend(frame.to_ndarray() for frame in resampler.resample(None))
    return np.concatenate(chunks, axis=1) if chunks else None


def measure(path, cancelled=None):
    """The part's average spectrum over its speaking moments and its peak, or None."""
    import numpy as np
    sound = _native(path, cancelled)
    if sound is None or sound.shape[1] < FRAME * 2:
        return None
    window = np.hanning(FRAME)
    count = 1 + (sound.shape[1] - FRAME) // HOP
    frequencies = np.fft.rfftfreq(FRAME, 1 / RATE)
    middle, low = [((frequencies >= a) & (frequencies < b)) for a, b in (MIDDLE, LOW)]
    power = np.empty((count, len(frequencies)))
    for start in range(0, count, 256):
        rows = np.arange(start, min(count, start + 256))
        # Power per channel, then averaged: opposite channels must not cancel out.
        frames = sound[:, rows[:, None] * HOP + np.arange(FRAME)] * window
        power[rows] = (np.abs(np.fft.rfft(frames, axis=2)) ** 2).mean(axis=0)
    power /= (FRAME * window.sum() / 2)
    speech = power[:, middle].sum(axis=1)
    level = 10 * np.log10(speech + 1e-20)
    active = level >= max(np.percentile(level, 95) - _GATE_DB, _FLOOR_DB)
    if active.sum() < _LEAST:
        return None
    spectrum = power[active].mean(axis=0)
    return dict(spectrum=spectrum, frequencies=frequencies, peak=float(np.abs(sound).max()))


def _band(entry, band, shelf_db=0.):
    import numpy as np
    frequencies = entry['frequencies']
    weight = 10 ** (shelf_db * _shelf(frequencies) / 10)
    chosen = (frequencies >= band[0]) & (frequencies < band[1])
    return 10 * math.log10(float((entry['spectrum'] * weight)[chosen].sum()) + 1e-20)


def _pull(difference):
    if not math.isfinite(difference) or abs(difference) <= THRESHOLD_DB:
        return 0.
    return max(-MOST_DB, min(MOST_DB, difference))


def _fit(gain, shelf, room):
    """Lower a boost (gain, then a raised shelf) to at most `room` dB."""
    excess = gain + max(shelf, 0.) - max(room, 0.)
    if excess > 0:
        cut = min(excess, max(gain, 0.))
        gain, shelf = gain - cut, shelf - min(excess - cut, max(shelf, 0.))
    return gain, shelf


def _room(entry):
    """How much the part may be raised before its peak passes HEADROOM, in dB."""
    return 20 * math.log10(HEADROOM / entry['peak']) if entry['peak'] > 0 else MOST_DB


def plan(sources, speaking, cancelled=None):
    """Per part (gain dB, low shelf dB, room dB), None for a part left as it is; or None when no part needs a change.

    The room is how far the part may be raised, also while it eases in from the part before."""
    if speaking is None or len(speaking) != len(sources) or not any(speaking):
        return None
    reference, settings = None, []
    for source, spoken in zip(sources, speaking):
        entry = measure(source, cancelled) if spoken else None
        if entry is None:
            settings.append(None)
            continue
        tilt = _band(entry, LOW) - _band(entry, MIDDLE)
        if reference is None:
            reference = entry, tilt
            settings.append((0., 0., _room(entry)))
            continue
        shelf = _pull(reference[1] - tilt)
        voice = (LOW[0], MIDDLE[1])
        gain = _pull(_band(reference[0], voice) - _band(entry, voice, shelf))
        settings.append(_fit(gain, shelf, _room(entry)) + (_room(entry),))
    return settings if any(setting and any(setting[:2]) for setting in settings) else None


class Shaper:
    """Streaming short-time filter, one part at a time: samples in, the same samples out a little later.

    A part that needs no change (no setting, and no change easing in from the part
    before) is given back exactly as it came. A filtered part sees only its own
    samples; a change eases in over its first RAMP_SECONDS and, when the next part
    is left as it is, back out over its last RAMP_SECONDS.
    """
    def __init__(self, settings, rate, channels):
        import numpy as np
        self.settings, self.rate, self.channels = list(settings), rate, channels
        self.ramp = max(1, round(RAMP_SECONDS * rate))
        self.window = np.sqrt(np.hanning(FRAME + 1)[:FRAME])
        self.shape = _shelf(np.fft.rfftfreq(FRAME, 1 / rate))
        self.index, self.pushed, self.emitted, self.ready = -1, 0, 0, []
        self.filtered = False

    def _setting(self, index):
        return self.settings[index] if 0 <= index < len(self.settings) else None

    def _filters(self, index):
        own, before = self._setting(index), self._setting(index - 1)
        return own is not None and (any(own[:2]) or before is not None and any(before[:2]))

    def mark(self):
        """The next pushed sample starts the next part."""
        self._close(ended=True)
        self.index += 1
        self.filtered = self._filters(self.index)
        if self.filtered:
            import numpy as np
            own, before = self._setting(self.index), self._setting(self.index - 1)
            self.own = tuple(own[:2])
            # Easing in from the part before must not raise this part past its own room either.
            self.start = tuple(before[:2]) if before is not None else (0., 0.)
            if len(own) > 2:
                self.start = _fit(*self.start, own[2])
            self.end = None if self._filters(self.index + 1) else (0., 0.)
            # Half a frame of silence ahead gives the first samples two overlapping frames, as all others have.
            self.buffer = np.zeros((self.channels, FRAME // 2), dtype=np.float64)
            self.offset = -(FRAME // 2)  # the part's position of buffer[:, 0]
            self.added = np.zeros((self.channels, FRAME - HOP), dtype=np.float64)
            self.skip = FRAME // 2
            self.length = self.out = 0  # samples of this part pushed, and given out

    @property
    def pending(self):
        return self.pushed - self.emitted

    def _at(self, position, length):
        """(gain, shelf) at a position in the part; `length` is the part's length once known."""
        weight = min(1., max(0., position / self.ramp))
        gain = self.start[0] + (self.own[0] - self.start[0]) * weight
        shelf = self.start[1] + (self.own[1] - self.start[1]) * weight
        if length is not None and self.end is not None:
            weight = min(1., max(0., (length - position) / self.ramp))
            gain, shelf = self.end[0] + (gain - self.end[0]) * weight, self.end[1] + (shelf - self.end[1]) * weight
        return gain, shelf

    def _frames(self, length=None):
        import numpy as np
        out = []
        # Until the part's end is known, its last RAMP_SECONDS may still ease out: keep them back.
        while self.buffer.shape[1] >= FRAME and (length is not None or self.end is None
                                                 or self.offset + FRAME // 2 < self.length - self.ramp):
            gain, shelf = self._at(self.offset + FRAME // 2, length)
            response = 10 ** ((gain + shelf * self.shape) / 20)
            spectrum = np.fft.rfft(self.buffer[:, :FRAME] * self.window, axis=1) * response
            frame = np.fft.irfft(spectrum, FRAME, axis=1) * self.window
            frame[:, :FRAME - HOP] += self.added
            out.append(frame[:, :HOP])
            self.added = frame[:, HOP:]
            self.buffer = self.buffer[:, HOP:]
            self.offset += HOP
        if not out:
            return
        data = np.concatenate(out, axis=1)
        skip, self.skip = min(self.skip, data.shape[1]), self.skip - min(self.skip, data.shape[1])
        # Less the leading silence, and only the part's own samples: what rang past its end is dropped.
        data = data[:, skip:][:, :self.length - self.out]
        self.out += data.shape[1]
        self.ready.append(np.clip(data, -1., 1.).astype('float32'))

    def _close(self, ended):
        """Finish the current part: `ended` when another part follows it."""
        import numpy as np
        if not self.filtered:
            return
        self.filtered = False
        if not ended:
            self.end = None
        self.buffer = np.concatenate((self.buffer, np.zeros((self.channels, FRAME), dtype=np.float64)), axis=1)
        self._frames(self.length)

    def push(self, data):
        """Add [channels, n] samples; return the finished ones (or None)."""
        import numpy as np
        self.pushed += data.shape[1]
        if not self.filtered:
            self.ready.append(np.asarray(data))
        else:
            self.buffer = np.concatenate((self.buffer, np.asarray(data, dtype=np.float64)), axis=1)
            self.length += data.shape[1]
            self._frames()
        return self._take()

    def flush(self):
        self._close(ended=False)
        return self._take()

    def _take(self):
        import numpy as np
        if not self.ready:
            return None
        data = np.concatenate(self.ready, axis=1) if len(self.ready) > 1 else self.ready[0]
        self.ready = []
        if not data.shape[1]:
            return None
        self.emitted += data.shape[1]
        return data
