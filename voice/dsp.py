"""Downmixing, resampling and PCM conversion.

Everything is streaming-safe: a Resampler keeps its filter state between chunks
so that a long capture does not drift or click at chunk boundaries.
"""

from __future__ import annotations

from fractions import Fraction

import numpy as np

INT16_SCALE = 32767.0


def pcm16_to_float(data: bytes, channels: int = 1) -> np.ndarray:
    """Interleaved int16 bytes -> float32 array shaped (frames,) or (frames, channels)."""
    samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / INT16_SCALE
    if channels > 1:
        samples = samples.reshape(-1, channels)
    return samples


def float_to_pcm16(samples: np.ndarray) -> bytes:
    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * INT16_SCALE).astype("<i2").tobytes()


def downmix(frames: np.ndarray) -> np.ndarray:
    """Average all channels into mono. Accepts (frames,) or (frames, channels)."""
    if frames.ndim == 1:
        return frames
    return frames.mean(axis=1, dtype=np.float32)


def to_channels(mono: np.ndarray, channels: int) -> np.ndarray:
    """Duplicate a mono signal across `channels` interleaved output channels."""
    if channels == 1:
        return mono
    return np.repeat(mono[:, None], channels, axis=1)


class Resampler:
    """Streaming polyphase rational resampler (numpy only, no scipy dependency)."""

    def __init__(self, src_rate: int, dst_rate: int, taps_per_phase: int = 16):
        if src_rate <= 0 or dst_rate <= 0:
            raise ValueError("sample rates must be positive")
        self.src_rate = src_rate
        self.dst_rate = dst_rate

        ratio = Fraction(dst_rate, src_rate).limit_denominator(2000)
        self.up = ratio.numerator
        self.down = ratio.denominator
        self.passthrough = self.up == 1 and self.down == 1

        if not self.passthrough:
            self.taps = self._design(taps_per_phase)
            self._carry = np.zeros(len(self.taps) - 1, dtype=np.float32)
            self._phase = 0

    def _design(self, taps_per_phase: int) -> np.ndarray:
        """Windowed-sinc low-pass at the lower of the two Nyquist limits.

        Taps are normalised so that zero stuffing by `up` leaves the signal at
        unity gain.
        """
        half = taps_per_phase * max(self.up, self.down)
        n = 2 * half + 1
        t = np.arange(n, dtype=np.float64) - half
        cutoff = 0.5 / max(self.up, self.down)  # normalised to the upsampled rate
        taps = 2 * cutoff * np.sinc(2 * cutoff * t) * np.hamming(n)
        taps = taps / taps.sum() * self.up
        return taps.astype(np.float32)

    def process(self, mono: np.ndarray) -> np.ndarray:
        """Resample one chunk, carrying filter state into the next call."""
        if self.passthrough:
            return mono.astype(np.float32, copy=False)
        if mono.size == 0:
            return np.zeros(0, dtype=np.float32)

        upsampled = np.zeros(mono.size * self.up, dtype=np.float32)
        upsampled[:: self.up] = mono

        buffer = np.concatenate((self._carry, upsampled))
        filtered = np.convolve(buffer, self.taps, mode="valid")
        self._carry = buffer[buffer.size - (self.taps.size - 1):]

        if filtered.size <= self._phase:
            self._phase -= filtered.size
            return np.zeros(0, dtype=np.float32)

        out = filtered[self._phase :: self.down]
        taken = out.size
        self._phase = self._phase + taken * self.down - filtered.size
        return out.astype(np.float32, copy=False)

    def reset(self) -> None:
        if not self.passthrough:
            self._carry[:] = 0.0
            self._phase = 0
