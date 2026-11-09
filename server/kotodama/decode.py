"""Turn whatever a TTS backend returns into 16-bit mono PCM at one rate.

The player and cache take raw s16le mono at the provider's sample rate.
Replicate models hand back finished files (usually WAV, sometimes MP3),
OpenAI-compatible servers stream PCM or WAV. PCMDecoder accepts the bytes in
any chunking and emits converted PCM as soon as it can:

  - WAV: RIFF chunks walked incrementally; a data size of 0 or 0xFFFFFFFF
    (streaming servers don't know it up front) means "until the end";
    16/24/32-bit integer and 32-bit float samples;
  - raw PCM: s16le mono at a declared rate;
  - MP3/FLAC/Ogg: decoded by ffmpeg at the end, if ffmpeg is installed.

Stereo is averaged to mono; other rates are resampled linearly.
"""

from __future__ import annotations

import io
import shutil
import struct
import subprocess
import sys
import wave
from array import array


class DecodeError(ValueError):
    pass


_ARRAY_FMT = "h"
_BIG = sys.byteorder == "big"


def sniff(head: bytes) -> str:
    """'wav', 'mp3', 'flac', 'ogg' or 'unknown' from the first bytes."""
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:3] == b"ID3" or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return "mp3"
    if head[:4] == b"fLaC":
        return "flac"
    if head[:4] == b"OggS":
        return "ogg"
    return "unknown"


class _Resampler:
    """Streaming linear interpolation, continuous across chunks."""

    def __init__(self, src: int, dst: int):
        self.step = src / dst
        self.t = 0.0
        self.prev: int | None = None

    def process(self, samples: array) -> array:
        buf = samples if self.prev is None else array(_ARRAY_FMT, [self.prev]) + samples
        out = array(_ARRAY_FMT)
        n = len(buf)
        t, step = self.t, self.step
        while t < n - 1:
            i = int(t)
            a = buf[i]
            out.append(int(a + (buf[i + 1] - a) * (t - i)))
            t += step
        if n:
            self.t = t - (n - 1)
            self.prev = buf[-1]
        return out


class PCMDecoder:
    def __init__(self, target_rate: int, *, kind: str = "auto", declared_rate: int | None = None):
        self.target = int(target_rate)
        self.kind = kind                     # auto | wav | pcm | mp3 | flac | ogg
        self.declared = int(declared_rate or target_rate)
        self._buf = bytearray()
        self._compressed = bytearray()
        self._in_data = False
        self._data_left: int | None = None   # None: until the end
        self._channels = 1
        self._width = 2
        self._float = False
        self._rate = self.declared
        self._resampler: _Resampler | None = None
        self._carry = b""
        if kind == "pcm":
            self._start_data(None, rate=self.declared, channels=1, width=2, is_float=False)

    # -- public -----------------------------------------------------------

    def feed(self, data: bytes) -> bytes:
        if not data:
            return b""
        if self.kind in ("mp3", "flac", "ogg"):
            self._compressed += data
            return b""
        if self._in_data:
            return self._convert(data)
        self._buf += data
        if self.kind == "auto":
            if len(self._buf) < 12:
                return b""
            self.kind = sniff(bytes(self._buf[:12]))
            if self.kind in ("mp3", "flac", "ogg"):
                self._compressed += self._buf
                self._buf.clear()
                return b""
            if self.kind != "wav":
                raise DecodeError("unrecognised audio format (expected WAV, raw PCM, MP3 or FLAC)")
        return self._parse_wav_header()

    def finish(self) -> bytes:
        if self.kind in ("mp3", "flac", "ogg"):
            return self._ffmpeg(bytes(self._compressed))
        if not self._in_data:
            if self._buf:
                raise DecodeError("audio ended before any sample data")
            return b""
        self._carry = b""                    # a partial frame at the very end is dropped
        return b""

    # -- WAV --------------------------------------------------------------

    def _parse_wav_header(self) -> bytes:
        buf = self._buf
        pos = 12
        fmt = None
        while True:
            if len(buf) < pos + 8:
                return b""
            cid = bytes(buf[pos:pos + 4])
            size = struct.unpack_from("<I", buf, pos + 4)[0]
            body = pos + 8
            if cid == b"fmt ":
                if len(buf) < body + 16:
                    return b""
                tag, channels, rate, _, _, bits = struct.unpack_from("<HHIIHH", buf, body)
                if tag == 0xFFFE and size >= 40 and len(buf) >= body + 40:
                    tag = struct.unpack_from("<H", buf, body + 24)[0]   # WAVE_FORMAT_EXTENSIBLE subformat
                if tag not in (1, 3):
                    raise DecodeError(f"unsupported WAV encoding (format tag {tag})")
                if bits not in (8, 16, 24, 32) or (tag == 3 and bits != 32):
                    raise DecodeError(f"unsupported WAV sample size ({bits}-bit)")
                fmt = (channels, rate, bits // 8, tag == 3)
            elif cid == b"data":
                if fmt is None:
                    raise DecodeError("WAV data before its format chunk")
                channels, rate, width, is_float = fmt
                streaming = size in (0, 0xFFFFFFFF)
                rest = bytes(buf[body:])
                buf.clear()
                self._start_data(None if streaming else size, rate=rate, channels=channels, width=width,
                                 is_float=is_float)
                return self._convert(rest)
            if size == 0xFFFFFFFF:
                raise DecodeError("malformed WAV chunk size")
            pos = body + size + (size & 1)

    def _start_data(self, size: int | None, *, rate: int, channels: int, width: int, is_float: bool) -> None:
        self._in_data = True
        self._data_left = size
        self._rate, self._channels, self._width, self._float = rate, max(1, channels), width, is_float
        self._resampler = _Resampler(rate, self.target) if rate != self.target else None

    # -- conversion -------------------------------------------------------

    def _convert(self, data: bytes) -> bytes:
        if self._data_left is not None:
            data = data[:self._data_left]
            self._data_left -= len(data)
        data = self._carry + data
        frame = self._width * self._channels
        cut = len(data) - len(data) % frame
        data, self._carry = data[:cut], data[cut:]
        if not data:
            return b""
        samples = self._to_int16(data)
        if self._channels > 1:
            ch = self._channels
            samples = array(_ARRAY_FMT, (sum(samples[i:i + ch]) // ch for i in range(0, len(samples), ch)))
        if self._resampler is not None:
            samples = self._resampler.process(samples)
        if _BIG:
            samples.byteswap()
        return samples.tobytes()

    def _to_int16(self, data: bytes) -> array:
        w = self._width
        if w == 2 and not self._float:
            a = array(_ARRAY_FMT)
            a.frombytes(data)
            if _BIG:
                a.byteswap()
            return a
        if self._float:
            vals = struct.unpack(f"<{len(data) // 4}f", data)
            return array(_ARRAY_FMT, (max(-32768, min(32767, int(v * 32767))) for v in vals))
        if w == 1:                                               # unsigned 8-bit
            return array(_ARRAY_FMT, ((b - 128) << 8 for b in data))
        if w == 3:
            return array(_ARRAY_FMT, (int.from_bytes(data[i:i + 3], "little", signed=True) >> 8
                                      for i in range(0, len(data), 3)))
        vals = struct.unpack(f"<{len(data) // 4}i", data)        # 32-bit int
        return array(_ARRAY_FMT, (v >> 16 for v in vals))

    # -- compressed -------------------------------------------------------

    def _ffmpeg(self, data: bytes) -> bytes:
        exe = shutil.which("ffmpeg")
        if not exe:
            raise DecodeError(f"the model returned {self.kind.upper()} audio and ffmpeg isn't installed: "
                              "set the model's audio format to wav in its adapter, or install ffmpeg")
        try:
            proc = subprocess.run([exe, "-v", "error", "-i", "pipe:0", "-f", "s16le", "-ac", "1",
                                   "-ar", str(self.target), "pipe:1"],
                                  input=data, capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DecodeError(f"ffmpeg failed: {exc}") from exc
        if proc.returncode != 0:
            raise DecodeError(f"ffmpeg could not decode the audio: {proc.stderr.decode(errors='replace')[:200]}")
        out = proc.stdout
        return out[:len(out) - len(out) % 2]


def decode_all(data: bytes, target_rate: int, *, kind: str = "auto", declared_rate: int | None = None) -> bytes:
    dec = PCMDecoder(target_rate, kind=kind, declared_rate=declared_rate)
    return dec.feed(data) + dec.finish()


def wav_bytes(pcm: bytes, rate: int) -> bytes:
    """Wrap s16le mono PCM in a WAV container."""
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return out.getvalue()
