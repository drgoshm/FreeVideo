"""Subtitle ears engine: sherpa-onnx CTC speech recognition, on the CPU.

One small model per language listens to the sound in 24-second windows (2 s of
overlap, so a word on a seam is heard whole) and returns timed characters or
words. Alignment then lays the given lines over what was heard, in order:
a line is `matched` only when most of its words were really heard there, and a
line nobody said has no time. Recognition groups the heard words into lines at
pauses. The models are optional files under `<models>/subtitles/<lang>/`
(`model.int8.onnx`, `tokens.txt`); without them, or without `sherpa_onnx`, the
engine reports "not installed" and the ears stay off.

An optional shared `subtitles/silero_vad_v5.onnx` gates words by speech time.
It leaves the CTC windows intact; without the verified VAD file the engine
continues with unfiltered recognition. No models are downloaded at runtime.

Recognition with `auto` first names the spoken language (Whisper-tiny language
identification, `subtitles/lid/`, size and SHA-256 pinned). A speech language
without an installed model is refused with the `language` reason; without the
verified identification files `auto` is refused as `no_engine`, so a sound in
another language never becomes a track of wrong words. An explicit language
needs its own model, never another one. The multilingual model
(`subtitles/sensevoice/`, SenseVoice-Small) reads Chinese, Japanese, Korean and
Cantonese; English keeps its own model.
"""
import hashlib
import logging
import os
import re
import threading
import unicodedata

from .paths import model_root
from .subtitle_ears import Cancelled, EarsError, register_engine

RATE = 16000
CORE = 24.0            # seconds each window owns
OVERLAP = 2.0          # heard on both sides of a seam, owned by one side
THRESHOLD = .72        # share of a line's words that must be heard in order
PAUSE = .7             # silence that ends a recognised line (seconds)
LINE_UNITS = {'zh': 24, 'en': 14, 'yue': 24, 'ja': 20, 'ko': 10}
MODELS = {'zh': 'zh', 'en': 'en'}
# SenseVoice-Small (one multilingual model) reads these languages; English keeps its own model.
MULTILINGUAL = 'sensevoice'
MULTILINGUAL_LANGUAGES = ('zh', 'ja', 'ko', 'yue')
VAD_MODEL = 'silero_vad_v5.onnx'
# Official Silero v5.0 export. The native VAD loader exits the process for some
# incompatible exports (including faster-whisper's v6); verify before loading.
VAD_SHA256 = '6b99cbfd39246b6706f98ec13c7c50c6b299181f2474fa05cbc8046acc274396'
VAD_BYTES = 2313101
# Whisper-tiny int8 export (HF csukuangfj/sherpa-onnx-whisper-tiny): only the
# language-identification pass is used. Verified before the native loader sees it.
LID_FOLDER = 'lid'
LID_FILES = {
    'tiny-encoder.int8.onnx': (12937772, 'd24fb083ae3b1041fc24e97971d60e280c9342201fbb67b0ab428a8b4a51a434'),
    'tiny-decoder.int8.onnx': (89855401, 'd2fece8dd42771f1df975c6c0445770d0c292bf7547c2cae04a6c0cc57540925'),
}
LID_SECONDS = 30.      # speech handed to the identifier (its own window)
LID_MIN_SECONDS = .5   # less speech than this is "nobody said anything"
VAD_PAD = .4           # include soft onsets and the delay of CTC token peaks
_LOG = logging.getLogger(__name__)

_WORD = re.compile(r"[㐀-鿿぀-ヿ]|[^\W_㐀-鿿぀-ヿ]+(?:['’][^\W_㐀-鿿぀-ヿ]+)*", re.UNICODE)
_CJK = re.compile(r'[㐀-鿿]')
_KANA = re.compile(r'[぀-ヿ]')
_HANGUL = re.compile(r'[가-힯]')


def written_language(text):
    """The language a given line is written in, from its script alone."""
    if _KANA.search(text):
        return 'ja'
    if _HANGUL.search(text):
        return 'ko'
    return 'zh' if _CJK.search(text) else 'en'


def units(text):
    return _WORD.findall(unicodedata.normalize('NFKC', text))


def norm(text):
    return unicodedata.normalize('NFKC', text).casefold().replace('’', "'")


# ---- matching the lines onto what was heard -------------------------------

def _project_compounds(target, heard):
    """Heard pieces that together spell one camel-case script word become one."""
    compounds = {norm(x) for x in target if re.search(r'[a-z][A-Z]', x)}
    result, j = [], 0
    while j < len(heard):
        width = next((n for n in range(2, min(4, len(heard) - j) + 1)
                      if norm(''.join(x['text'] for x in heard[j:j + n])) in compounds), None) if compounds else None
        if width:
            result.append((j, j + width, {**heard[j], 'text': ''.join(x['text'] for x in heard[j:j + width]),
                                          'end': heard[j + width - 1]['end']}))
            j += width
        else:
            result.append((j, j + 1, heard[j]))
            j += 1
    return result


def _edit_table(expected, observed):
    """Edit distance of the whole line against any stretch of what was heard.

    Free audio before and after, costly gaps in the line. Returns the last row
    and the direction of each cell (0 = both advance, 1 = line word unheard,
    2 = heard word not in the line), tie-broken in that order.
    """
    import numpy as np
    n, m = len(expected), len(observed)
    ids = {}
    left = np.fromiter((ids.setdefault(x, len(ids)) for x in expected), dtype=np.int32, count=n)
    right = np.fromiter((ids.setdefault(x, len(ids)) for x in observed), dtype=np.int32, count=m)
    column = np.arange(m + 1, dtype=np.int32)
    previous = np.zeros(m + 1, dtype=np.int32)
    paths = np.zeros((n + 1, m + 1), dtype=np.uint8)
    paths[0, :] = 0
    for i in range(1, n + 1):
        diagonal = np.full(m + 1, np.iinfo(np.int32).max // 2, dtype=np.int32)
        diagonal[1:] = previous[:-1] + (right != left[i - 1])
        up = previous + 1
        best = np.minimum(diagonal, up)
        best[0] = i
        row = np.minimum.accumulate(best - column) + column
        direction = np.where(row == diagonal, 0, np.where(row == up, 1, 2)).astype(np.uint8)
        direction[0] = 1
        paths[i] = direction
        previous = row
    return previous, paths


def _candidate_spans(target, projected, threshold):
    """Every local alignment of the line that is good enough, one per end."""
    import numpy as np
    n, m = len(target), len(projected)
    if not n or not m:
        return
    expected = [norm(x) for x in target]
    heard = [norm(x[2]['text']) for x in projected]
    last, paths = _edit_table(expected, heard)
    seen = set()
    for endpoint in np.nonzero(last[1:] / n <= 1 - threshold + 1e-9)[0] + 1:
        endpoint = int(endpoint)
        cost = last[endpoint] / n
        pairs, i, j = [], n, endpoint
        while i:
            direction = paths[i, j]
            if j and direction == 0:
                if expected[i - 1] == heard[j - 1]:
                    pairs.append((i - 1, j - 1))
                i -= 1
                j -= 1
            elif direction == 1 or not j:
                i -= 1
            else:
                j -= 1
        pairs.reverse()
        if len(pairs) < min(2, n) or len(pairs) / n < threshold:
            continue
        first, final = projected[pairs[0][1]], projected[pairs[-1][1]]
        begin, end = first[2]['start'], final[2]['end']
        if not 0 <= begin < end or end - begin > max(4.0, n * 1.5):
            continue
        key = (tuple((i, projected[j][0], projected[j][1]) for i, j in pairs), round(float(cost), 8))
        if key in seen:
            continue
        seen.add(key)
        yield first[0], final[1], float(cost), len(pairs) / n, [(i, projected[j][2]) for i, j in pairs]


def match_lines(lines, heard, threshold=THRESHOLD):
    """Lay the lines over the heard words in their given order.

    A line may be missing and extra speech may sit between lines. A near match
    now is preferred over an exact repeat that would lose the lines after it.
    """
    import numpy as np
    m = len(heard)
    costs = np.zeros(m + 1)
    columns = np.arange(m + 1)
    parents, chosen, candidates, unit_lists = [], [], [], []
    for line in lines:
        words = units(line)
        unit_lists.append(words)
        current = costs + (1.0 + len(words) * 1e-6)
        # cheapest earlier state for every start (earliest on ties)
        floor = np.minimum.accumulate(costs)
        fresh = np.ones(m + 1, dtype=bool)
        fresh[1:] = costs[1:] < floor[:-1]
        source = np.maximum.accumulate(np.where(fresh, columns, 0))
        previous = np.arange(m + 1)
        picked = np.zeros(m + 1, dtype=bool)
        by_end = {}
        for start, end, edit, confidence, pairs in _candidate_spans(words, _project_compounds(words, heard), threshold):
            total = floor[start] + edit
            if total < current[end] - 1e-12 or (abs(total - current[end]) <= 1e-12 and picked[end]
                                                and confidence > by_end[end][1]):
                current[end] = total
                previous[end] = source[start]
                picked[end] = True
                by_end[end] = (edit, confidence, pairs)
        costs = current
        parents.append(previous)
        chosen.append(picked)
        candidates.append(by_end)
    state = int(np.argmin(costs))
    result = []
    for index in range(len(lines) - 1, -1, -1):
        found = bool(chosen[index][state])
        entry = dict(index=index, matched=found)
        if found:
            edit, confidence, pairs = candidates[index][state]
            timed = dict(pairs)
            entry.update(start=pairs[0][1]['start'], end=pairs[-1][1]['end'], confidence=round(confidence, 6),
                         words=[dict(text=word, start=timed[i]['start'], end=timed[i]['end'])
                                for i, word in enumerate(unit_lists[index]) if i in timed])
        result.append(entry)
        state = int(parents[index][state])
    result.reverse()
    return result


# ---- listening --------------------------------------------------------------

def _bytes_to_text(pieces):
    """Byte-level pieces (some Chinese models spell UTF-8 bytes) -> characters."""
    chars = (list(range(256, 288)) + list(range(32, 127)) + list(range(288, 306)) + list(range(308, 319))
             + list(range(321, 329)) + list(range(330, 383)) + list(range(384, 423)))
    inverse = {chr(c): i for i, c in enumerate(chars)}
    result, pending = [], []
    for piece in pieces:
        for ch in piece['raw']:
            if ch == '▁':
                # A word boundary also ends a malformed/incomplete byte prefix.
                # Keeping it would consume the next valid character's first
                # byte when UTF-8 decoding rejects the combined sequence.
                pending = []
                result.append({**piece, 'raw': ' '})
                continue
            if ch not in inverse:
                continue
            pending.append((inverse[ch], piece['start'], piece['end']))
            try:
                text = bytes(x[0] for x in pending).decode('utf-8')
            except UnicodeDecodeError as error:
                if error.reason != 'unexpected end of data':
                    pending = []
                continue
            for literal in text:
                result.append({'raw': literal, 'start': pending[0][1], 'end': pending[-1][2]})
            pending = []
    return result


def heard_words(tokens, timestamps, duration, offset=0.0):
    """One window's recogniser output -> [{text, start, end}] (characters or words)."""
    pieces = []
    for index, (token, at) in enumerate(zip(tokens, timestamps)):
        at = float(at)
        if at >= duration or (token.startswith('<') and token.endswith('>')):
            continue
        end = min(duration, float(timestamps[index + 1]) if index + 1 < len(timestamps) else at + .24, at + .4)
        if end <= at:
            end = min(duration, at + .08)
        pieces.append({'raw': token, 'start': offset + at, 'end': offset + end})
    if any(256 <= ord(ch) <= 422 for piece in pieces for ch in piece['raw']):
        pieces = _bytes_to_text(pieces)
    result, buffer, begin, end = [], '', None, None

    def flush():
        nonlocal buffer, begin, end
        if buffer and units(buffer):
            result.append({'text': buffer, 'start': begin, 'end': end})
        buffer, begin, end = '', None, None

    for piece in pieces:
        raw = piece['raw']
        if raw.startswith(('▁', 'Ġ')) or raw in (' ', '|'):
            flush()
        for ch in raw.replace('▁', ' ').replace('Ġ', ' ').replace('|', ' '):
            if '㐀' <= ch <= '鿿' or '぀' <= ch <= 'ヿ':
                flush()
                result.append({'text': ch, 'start': piece['start'], 'end': piece['end']})
            elif ch.isalnum() or ch in "'’":
                if begin is None:
                    begin = piece['start']
                buffer += ch
                end = piece['end']
            else:
                flush()
    flush()
    return result


def windows(count, core=CORE, overlap=OVERLAP):
    """(first, last, own_from, own_to) in samples; each sample is owned by one window."""
    step, pad = int(core * RATE), int(overlap * RATE)
    result, start = [], 0
    while start < count:
        result.append((max(0, start - pad), min(count, start + step + pad), start, min(count, start + step)))
        start += step
    return result


def speech_spans(vad, audio, cancelled=None):
    """Padded speech intervals in window-local seconds; flush even a short tail.

    Drain as we feed the detector so its audio buffer stays bounded. CTC still
    hears the original window, avoiding a different decode at each VAD edge.
    """
    import numpy as np
    vad.reset()
    spans = []
    duration = audio.size / RATE

    def drain():
        while not vad.empty():
            segment = vad.front
            begin = max(0., segment.start / RATE - VAD_PAD)
            end = min(duration, (segment.start + len(segment.samples)) / RATE + VAD_PAD)
            if begin < end:
                if spans and begin <= spans[-1][1]:
                    spans[-1] = (spans[-1][0], max(spans[-1][1], end))
                else:
                    spans.append((begin, end))
            vad.pop()

    for first in range(0, audio.size, 512):
        if cancelled and cancelled():
            raise Cancelled('cancelled')
        chunk = audio[first:first + 512]
        if chunk.size < 512:
            chunk = np.pad(chunk, (0, 512 - chunk.size))
        vad.accept_waveform(chunk)
        drain()
    vad.flush()
    drain()
    return spans


def in_speech(word, spans, offset=0.):
    """Assign each whole word by its midpoint, in the same clock as its window."""
    at = (word['start'] + word['end']) / 2 - offset
    return spans is None or any(begin <= at < end for begin, end in spans)


class Engine:
    name = 'sherpa-ctc'

    def __init__(self, directories, threads, vad_model=None):
        self._directories = directories
        self._threads = threads
        self._recognizers = {}
        self._lock = threading.Lock()
        self._vad_model = vad_model if vad_model is not None else next(iter(directories.values())).parent / VAD_MODEL
        self._vad = None
        self._vad_signature = None
        self._vad_lock = threading.Lock()
        self._lid_folder = next(iter(directories.values())).parent / LID_FOLDER
        self._lid = None
        self._lid_signature = None
        self._lid_lock = threading.Lock()

    def languages(self):
        return sorted(self._directories)

    def _pick(self, language, text=''):
        """The model for a language that is already known (named, or read from given lines)."""
        if language in self._directories:
            return language
        if language == 'auto' and text.strip():
            wanted = written_language(text)
            if wanted in self._directories:
                return wanted
        raise EarsError('no_engine')

    def _lid_ready(self):
        """The identification files, verified by size and SHA-256, or None."""
        try:
            for name, (size, digest) in LID_FILES.items():
                path = self._lid_folder / name
                if path.stat().st_size != size:
                    return None
            signature = tuple((self._lid_folder / name).stat().st_mtime_ns for name in LID_FILES)
            if signature != self._lid_signature:
                self._lid = None
                self._lid_signature = None
                for name, (size, digest) in LID_FILES.items():
                    if hashlib.sha256((self._lid_folder / name).read_bytes()).hexdigest() != digest:
                        _LOG.warning('Subtitle language identification model is unverified')
                        return None
                self._lid_signature = signature
            return self._lid_folder
        except OSError:
            return None

    def _identify(self, audio, cancelled):
        """Spoken language code of the first stretch of speech; None when nobody speaks."""
        import numpy as np
        pieces, total = [], 0.
        for first, last, own_from, own_to in windows(audio.size):
            if cancelled and cancelled():
                raise Cancelled('cancelled')
            spans = self._speech_spans(audio[first:last], cancelled)
            if spans is None:
                spans = [(0., (last - first) / RATE)]
            for begin, end in spans:
                take = min(end - begin, LID_SECONDS - total)
                if take > 0:
                    start = first + int(begin * RATE)
                    pieces.append(audio[start:start + int(take * RATE)])
                    total += take
            if total >= LID_SECONDS:
                break
        if total < LID_MIN_SECONDS:
            return None
        with self._lid_lock:
            folder = self._lid_ready()
            if folder is None:
                raise EarsError('no_engine')
            try:
                if self._lid is None:
                    import sherpa_onnx
                    whisper = sherpa_onnx.SpokenLanguageIdentificationWhisperConfig(
                        encoder=str(folder / 'tiny-encoder.int8.onnx'),
                        decoder=str(folder / 'tiny-decoder.int8.onnx'))
                    self._lid = sherpa_onnx.SpokenLanguageIdentification(
                        sherpa_onnx.SpokenLanguageIdentificationConfig(
                            whisper=whisper, num_threads=min(2, self._threads), provider='cpu'))
                stream = self._lid.create_stream()
                stream.accept_waveform(RATE, np.concatenate(pieces).astype('float32', copy=False))
                return str(self._lid.compute(stream)).strip().lower()
            except Exception:
                self._lid = None
                _LOG.warning('Subtitle language identification failed', exc_info=True)
                raise EarsError('no_engine') from None

    def _spoken(self, audio, language, cancelled):
        """Model language for recognition; `auto` asks the identifier and refuses what it cannot read."""
        if language != 'auto':
            return self._pick(language)
        code = self._identify(audio, cancelled)
        if code is None:
            return sorted(self._directories)[0]
        if code not in self._directories:
            raise EarsError('language')
        return code

    def _recognizer(self, language):
        with self._lock:
            if language not in self._recognizers:
                import sherpa_onnx
                folder = self._directories[language]
                if folder.name == MULTILINGUAL:
                    # One recogniser for all its languages; it names the language of each stretch itself.
                    key = MULTILINGUAL
                    if key not in self._recognizers:
                        self._recognizers[key] = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                            model=str(folder / 'model.int8.onnx'), tokens=str(folder / 'tokens.txt'),
                            num_threads=self._threads, use_itn=True, language='auto', provider='cpu')
                    return self._recognizers[key]
                make = (sherpa_onnx.OfflineRecognizer.from_zipformer_ctc if language == 'zh'
                        else sherpa_onnx.OfflineRecognizer.from_nemo_ctc)
                self._recognizers[language] = make(
                    model=str(folder / 'model.int8.onnx'), tokens=str(folder / 'tokens.txt'),
                    num_threads=self._threads, provider='cpu', decoding_method='greedy_search')
            return self._recognizers[language]

    def _speech_spans(self, audio, cancelled):
        # The detector is stateful: reset/feed/flush belong to one call. The
        # separate lock lets other calls decode while this call checks speech.
        with self._vad_lock:
            try:
                stat = self._vad_model.stat()
                signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
                if signature != self._vad_signature:
                    self._vad = None
                    self._vad_signature = signature
                    data = self._vad_model.read_bytes() if stat.st_size == VAD_BYTES else b''
                    if hashlib.sha256(data).hexdigest() != VAD_SHA256:
                        _LOG.warning('Subtitle speech gate model is unverified; using unfiltered recognition')
                        return None
                    import sherpa_onnx
                    config = sherpa_onnx.VadModelConfig()
                    config.silero_vad.model = str(self._vad_model)
                    config.silero_vad.threshold = .5
                    config.silero_vad.min_speech_duration = .1
                    config.silero_vad.min_silence_duration = .2
                    config.silero_vad.window_size = 512
                    config.silero_vad.max_speech_duration = CORE + 2 * OVERLAP + 1
                    config.sample_rate = RATE
                    config.num_threads = 1
                    config.provider = 'cpu'
                    self._vad = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=60)
                return speech_spans(self._vad, audio, cancelled) if self._vad is not None else None
            except Cancelled:
                raise
            except FileNotFoundError:
                self._vad = None
                self._vad_signature = None
                return None
            except Exception:
                self._vad = None
                _LOG.warning('Subtitle speech gate unavailable; using unfiltered recognition')
                return None

    def _listen(self, audio, language, progress, cancelled):
        recognizer = self._recognizer(language)
        plan = windows(audio.size)
        words = []
        for number, (first, last, own_from, own_to) in enumerate(plan):
            if cancelled and cancelled():
                raise Cancelled('cancelled')
            spans = self._speech_spans(audio[first:last], cancelled)
            if spans == []:
                progress((number + 1) / len(plan))
                continue
            if spans is not None and cancelled and cancelled():
                raise Cancelled('cancelled')
            stream = recognizer.create_stream()
            stream.accept_waveform(RATE, audio[first:last])
            recognizer.decode_stream(stream)
            result = stream.result
            if len(result.timestamps) != len(result.tokens):
                raise RuntimeError('recogniser returned no times')
            for word in heard_words(list(result.tokens), list(result.timestamps), (last - first) / RATE, first / RATE):
                if (own_from / RATE <= (word['start'] + word['end']) / 2 < own_to / RATE
                        and in_speech(word, spans, first / RATE)):
                    words.append(word)
            progress((number + 1) / len(plan))
        return words

    def align(self, audio, lines, language, progress, cancelled):
        language = self._pick(language, ' '.join(lines))
        words = self._listen(audio, language, lambda f: progress(f * .95), cancelled)
        result = match_lines(lines, words)
        progress(1.0)
        return result

    def transcribe(self, audio, language, progress, cancelled):
        language = self._spoken(audio, language, cancelled)
        words = self._listen(audio, language, lambda f: progress(f * .98), cancelled)
        limit = LINE_UNITS[language]
        lines, current = [], []
        for word in words:
            if current and (word['start'] - current[-1]['end'] >= PAUSE or len(current) >= limit):
                lines.append(current)
                current = []
            current.append(word)
        if current:
            lines.append(current)
        progress(1.0)
        found = []
        for group in lines:
            text = ''
            for word in group:
                if text and (word['text'][0].isascii() and word['text'][0].isalnum()) and text[-1].isascii():
                    text += ' '
                text += word['text']
            found.append(dict(text=text, start=group[0]['start'], end=group[-1]['end'], words=group))
        return dict(language=language, lines=found)


_CACHE = {}


def folders(root=None):
    base = (root or model_root()) / 'subtitles'
    found = {}
    for language, name in MODELS.items():
        folder = base / name
        if (folder / 'model.int8.onnx').is_file() and (folder / 'tokens.txt').is_file():
            found[language] = folder
    folder = base / MULTILINGUAL
    if (folder / 'model.int8.onnx').is_file() and (folder / 'tokens.txt').is_file():
        for language in MULTILINGUAL_LANGUAGES:
            found.setdefault(language, folder)
    return found


def factory():
    """The ears engine, or None while the models or the runtime are not installed."""
    try:
        from . import subtitle_pack
        # The speech runtime is downloaded on demand; put it on the path before the import.
        subtitle_pack.activate()
    except Exception:
        pass
    try:
        import sherpa_onnx  # noqa: F401
        import numpy  # noqa: F401
    except ImportError:
        return None
    found = folders()
    if not found:
        return None
    key = tuple(sorted(found.items()))
    if key not in _CACHE:
        _CACHE.clear()
        _CACHE[key] = Engine(found, max(1, min(4, os.cpu_count() or 1)))
    return _CACHE[key]


def _lazy():
    return factory()


register_engine(Engine.name, _lazy, first=True)
