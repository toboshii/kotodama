"""Voice loudness normalization.

TTS voices come out of their models at very different levels. In one real
cache the median line of the quietest voice measured 19 dB below the loudest,
while the lines of any one voice stayed within about 1.3 dB of each other. So
the fix is one steady gain per *voice*, not per line. Every voice is brought
to the same target. Everything within a voice (a whispered line, a shouted
one, the shape of each line) passes through untouched.

LoudnessMeter   streaming ITU-R BS.1770 loudness (K-weighting, 400 ms blocks,
                absolute and relative gates) of 16-bit mono PCM.
voice_gain_db   target minus a voice's typical level (the median of its
                recent lines), clamped.
LineGain        the gain for one line, chunk by chunk. Usually constant. For a
                voice heard for the first time it settles slowly toward the
                level being measured. The optional leveler lifts a line whose
                level sags well below how it began.

Everything here measures the unscaled model output, which is also what the
cache stores, so gains can be retuned without re-synthesizing anything.
"""

from __future__ import annotations

import array
import math
import statistics
import sys
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

ABS_GATE = -70.0          # LUFS: blocks below this are silence
REL_GATE = 10.0           # LU below the ungated level: pauses and breaths
MIN_STAT_SECONDS = 1.0    # shorter lines are too noisy to describe a voice
SILENT = -100.0           # stored for a line with no audible speech


def _k_coefficients(rate: int) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """The two BS.1770 pre-filter biquads (high shelf, then high-pass),
    derived for any sample rate (the published values are for 48 kHz)."""
    f0, gain_db, q = 1681.974450955533, 3.999843853973347, 0.7071752369554196
    k = math.tan(math.pi * f0 / rate)
    vh = 10 ** (gain_db / 20.0)
    vb = vh ** 0.4996667741545416
    a0 = 1.0 + k / q + k * k
    shelf = ((vh + vb * k / q + k * k) / a0, 2.0 * (k * k - vh) / a0, (vh - vb * k / q + k * k) / a0,
             2.0 * (k * k - 1.0) / a0, (1.0 - k / q + k * k) / a0)
    f0, q = 38.13547087602444, 0.5003270373238773
    k = math.tan(math.pi * f0 / rate)
    a0 = 1.0 + k / q + k * k
    highpass = (2.0 * (k * k - 1.0) / a0, (1.0 - k / q + k * k) / a0)
    return shelf, highpass


def _lufs(mean_square: float) -> float:
    return -0.691 + 10.0 * math.log10(mean_square) if mean_square > 0 else -math.inf


def _gated(blocks: Iterable[float], floor: float | None = None) -> float | None:
    """BS.1770 gating over block mean squares; None when nothing is audible."""
    loud = [b for b in blocks if _lufs(b) > ABS_GATE]
    if not loud:
        return None
    level = _lufs(sum(loud) / len(loud))
    cut = level - REL_GATE if floor is None else max(level - REL_GATE, floor)
    kept = [b for b in loud if _lufs(b) > cut]
    return _lufs(sum(kept) / len(kept)) if kept else None


class LoudnessMeter:
    """Feed PCM in chunks of any size; read loudness at any time.

    Energy is summed per 100 ms step; a block is four consecutive steps
    (400 ms, 75% overlap), as BS.1770 specifies. Costs about as much CPU as
    the limiter, a few ms per second of audio."""

    STEP_S = 0.1

    def __init__(self, rate: int = 24000):
        self.rate = rate
        (self._b0, self._b1, self._b2, self._a1, self._a2), (self._c1, self._c2) = _k_coefficients(rate)
        self._x1 = self._x2 = self._y1 = self._y2 = self._z1 = self._z2 = 0.0
        self._step_len = max(1, int(round(rate * self.STEP_S)))
        self._acc = 0.0
        self._n = 0
        self._steps: list[float] = []     # mean square of each finished 100 ms step
        self.blocks: list[float] = []     # mean square of each 400 ms block
        self.frames = 0

    @property
    def seconds(self) -> float:
        return self.frames / self.rate

    def feed(self, pcm: bytes) -> None:
        if not pcm:
            return
        samples = array.array("h")
        samples.frombytes(pcm[: len(pcm) // 2 * 2])
        if sys.byteorder == "big":
            samples.byteswap()
        self.frames += len(samples)
        b0, b1, b2, a1, a2, c1, c2 = self._b0, self._b1, self._b2, self._a1, self._a2, self._c1, self._c2
        x1, x2, y1, y2, z1, z2 = self._x1, self._x2, self._y1, self._y2, self._z1, self._z2
        acc, n, step_len = self._acc, self._n, self._step_len
        for s in samples:
            v = s * 3.0517578125e-05
            y = b0 * v + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
            x2 = x1
            x1 = v
            z = y - 2.0 * y1 + y2 - c1 * z1 - c2 * z2
            y2 = y1
            y1 = y
            z2 = z1
            z1 = z
            acc += z * z
            n += 1
            if n == step_len:
                self._close_step(acc / n)
                acc = 0.0
                n = 0
        self._x1, self._x2, self._y1, self._y2, self._z1, self._z2 = x1, x2, y1, y2, z1, z2
        self._acc, self._n = acc, n

    def _close_step(self, mean_square: float) -> None:
        self._steps.append(mean_square)
        if len(self._steps) >= 4:
            self.blocks.append(sum(self._steps[-4:]) / 4.0)
            del self._steps[:-4]

    def integrated(self) -> float | None:
        """Gated loudness of everything fed so far (LUFS), None if silent."""
        return _gated(self.blocks)

    def momentary(self) -> float | None:
        """Loudness of the latest 400 ms block."""
        return _lufs(self.blocks[-1]) if self.blocks else None

    def recent(self, seconds: float, floor: float | None = None) -> float | None:
        """Gated loudness of the last `seconds` (blocks advance every 100 ms)."""
        n = max(1, int(round((seconds - 0.4) / self.STEP_S)) + 1)
        return _gated(self.blocks[-n:], floor) if self.blocks else None


def measure_wav(path: Path) -> tuple[float, float]:
    """(loudness, seconds) of a cached 16-bit mono WAV. Silent -> SILENT."""
    with wave.open(str(path), "rb") as wav:
        meter = LoudnessMeter(wav.getframerate())
        while True:
            data = wav.readframes(24000)
            if not data:
                break
            meter.feed(data)
    level = meter.integrated()
    return (SILENT if level is None else level), meter.seconds


# -- policy ----------------------------------------------------------------

@dataclass
class LevelerSettings:
    max_db: float = 6.0          # most it will lift a line
    deadband_db: float = 2.0     # sag tolerated before it acts
    window_s: float = 3.0        # how much recent speech it compares
    reference_s: float = 1.5     # opening speech that sets the line's level
    rate_db_s: float = 2.0       # how fast the lift moves


@dataclass
class NormalizeSettings:
    enabled: bool = True
    target_lufs: float = -13.0
    max_boost_db: float = 15.0
    max_cut_db: float = 10.0
    voice_window: int = 20       # recent lines that describe a voice
    first_line_adapt: bool = True
    adapt_range_db: float = 6.0  # how far a first line may move from its starting gain
    adapt_rate_db_s: float = 2.0
    lookahead_ms: int = 0
    leveler: bool = False
    leveler_providers: tuple[str, ...] = ()   # empty: every provider
    leveler_settings: LevelerSettings = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.leveler_settings is None:
            self.leveler_settings = LevelerSettings()

    def clamp(self, db: float) -> float:
        return max(-self.max_cut_db, min(self.max_boost_db, db))

    def leveler_for(self, provider: str) -> LevelerSettings | None:
        if not (self.enabled and self.leveler):
            return None
        if self.leveler_providers and provider not in self.leveler_providers:
            return None
        return self.leveler_settings


def typical_level(levels: list[float]) -> float | None:
    """A voice's level: the median, so the odd whisper or shout doesn't move it."""
    usable = [v for v in levels if v is not None and v > ABS_GATE]
    return statistics.median(usable) if usable else None


def voice_gain_db(settings: NormalizeSettings, level: float | None, fallback_db: float) -> float:
    """The steady gain for a voice at `level`; `fallback_db` when unmeasured."""
    if level is None:
        return fallback_db
    return settings.clamp(settings.target_lufs - level)


class LineGain:
    """The gain (dB) for one line, decided chunk by chunk.

    base_db is the voice's steady gain. With `adapt` (a voice with no
    history), once a second of the line has been heard it moves toward
    target - measured level at no more than adapt_rate_db_s. It stays within
    adapt_range_db of where it started. With a leveler, a line that sags more
    than the deadband below its opening level gets lifted (only lifted, and
    held still through pauses)."""

    def __init__(self, settings: NormalizeSettings, rate: int, base_db: float, *, adapt: bool = False,
                 leveler: LevelerSettings | None = None, meter: LoudnessMeter | None = None):
        self.settings = settings
        self.meter = meter or LoudnessMeter(rate)
        self.start_db = self.base_db = base_db
        self.adapt = adapt
        self.leveler = leveler
        self.lift_db = 0.0
        self.reference: float | None = None

    @property
    def db(self) -> float:
        return self.base_db + self.lift_db

    def process(self, pcm: bytes) -> float:
        """Measure this chunk and return the gain to play it at."""
        self.meter.feed(pcm)
        return self.step(len(pcm) / 2 / self.meter.rate)

    def _adapt_goal(self, min_seconds: float = 1.0) -> float | None:
        if not self.adapt or self.meter.seconds < min_seconds:
            return None
        level = self.meter.integrated()
        if level is None:
            return None
        s = self.settings
        goal = s.clamp(s.target_lufs - level)
        return max(self.start_db - s.adapt_range_db, min(self.start_db + s.adapt_range_db, goal))

    def settle(self) -> None:
        """Jump straight to the adapted gain (after a look-ahead buffer, which
        may be shorter than the second settling waits for: one 400 ms block
        is enough to go on)."""
        goal = self._adapt_goal(min_seconds=0.0)
        if goal is not None:
            self.base_db = goal

    def step(self, seconds: float) -> float:
        goal = self._adapt_goal()
        if goal is not None:
            self.base_db = _toward(self.base_db, goal, self.settings.adapt_rate_db_s * seconds)
        if self.leveler is not None:
            self._level(seconds)
        return self.db

    def _level(self, seconds: float) -> None:
        lv, m = self.leveler, self.meter
        if self.reference is None:
            if m.seconds >= lv.reference_s:
                self.reference = m.integrated()
            return
        now = m.momentary()
        if now is None or now < self.reference - 20.0:
            return                                  # a pause: hold
        recent = m.recent(lv.window_s, floor=self.reference - 20.0)
        if recent is None:
            return
        want = max(0.0, min(lv.max_db, self.reference - recent - lv.deadband_db))
        self.lift_db = _toward(self.lift_db, want, lv.rate_db_s * seconds)


def _toward(value: float, goal: float, most: float) -> float:
    if goal > value:
        return min(goal, value + most)
    return max(goal, value - most)
