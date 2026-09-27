"""Microphone capture and playback that can be cut off mid-sentence."""

from __future__ import annotations

import asyncio
import logging
import threading

import numpy as np
import sounddevice as sd

from voice.devices import find_device
from voice.dsp import float_to_pcm16, pcm16_to_float, to_channels

log = logging.getLogger("audio")

CHUNK_MS = 20
CAPTURE_QUEUE_MAXSIZE = 256  # ~5 s at 20 ms chunks
# Audio from the first moments of a capture stream is dropped: the start-up
# transient reached Deepgram as a phantom "Bu" turn after two of four restarts.
MIC_WARMUP_MS = 500
KEEP_AWAKE_DBFS = -70.0
FADE_MS = 12

# Both streams are opened at the rate the cloud side speaks (16 kHz up, 24 kHz
# down) and CoreAudio converts to the device's own rate in native code. Doing it
# in numpy is not an option on 44.1 kHz devices such as Bluetooth headphones:
# 24000 -> 44100 is a 147/80 ratio, whose filter cost 147 ms of CPU per 100 ms
# of audio and froze the event loop -- and with it barge-in -- while speaking.


class Microphone:
    """Live capture from a PortAudio input device as 16-bit mono PCM chunks."""

    def __init__(self, device_name: str, rate: int = 16000, chunk_ms: int = CHUNK_MS):
        self.rate = rate
        self.device = find_device(device_name, "input")
        self._blocksize = max(1, int(rate * chunk_ms / 1000))
        self._warmup_frames = int(rate * MIC_WARMUP_MS / 1000)
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(CAPTURE_QUEUE_MAXSIZE)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stream: sd.RawInputStream | None = None
        self._dropped = 0

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stream = sd.RawInputStream(
            device=self.device.index,
            channels=1,
            samplerate=self.rate,
            dtype="int16",
            blocksize=self._blocksize,
            callback=self._callback,
        )
        self._stream.start()
        log.info("mic: %s (native %d Hz), captured as mono @ %d Hz",
                 self.device.name, self.device.default_samplerate, self.rate)

    def _callback(self, indata, frames, time_info, status) -> None:
        """PortAudio thread: hand the block over, never block."""
        if self._warmup_frames > 0:
            self._warmup_frames -= frames
            return
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._offer, bytes(indata))

    def _offer(self, pcm: bytes | None) -> None:
        try:
            self._queue.put_nowait(pcm)
        except asyncio.QueueFull:
            # For live speech a gap beats a latency that grows without bound.
            self._dropped += 1
            self._queue.get_nowait()
            self._queue.put_nowait(pcm)
            if self._dropped <= 5 or self._dropped % 100 == 0:
                log.warning("mic: capture queue full, dropped %d chunks", self._dropped)

    async def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        self._offer(None)

    async def __aiter__(self):
        while True:
            pcm = await self._queue.get()
            if pcm is None:
                return
            yield pcm


class Speaker:
    """A continuously open output stream fed from a FIFO that can be emptied at once.

    Keeping the stream open means speech starts without a device start-up delay,
    and `clear()` silences it within one 20 ms block -- which is what makes
    interrupting the assistant feel immediate.

    Between replies it plays near-inaudible noise rather than digital silence.
    Bluetooth audio goes to sleep on silence and takes a moment to wake up; a
    300 ms lead-in alone still left the first word of some replies cut off.
    """

    def __init__(
        self,
        device_name: str,
        rate: int = 24000,
        block_ms: int = CHUNK_MS,
        keep_awake_dbfs: float | None = KEEP_AWAKE_DBFS,
    ):
        self.rate = rate
        self.device = find_device(device_name, "output")
        # Stereo even for mono speech: a mono stream plays in the left ear only.
        self._channels = min(self.device.max_output_channels, 2)
        self._blocksize = max(1, int(rate * block_ms / 1000))
        self._frame_bytes = 2 * self._channels
        self._idle = self._idle_signal(keep_awake_dbfs)
        self._idle_pos = 0
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._stream: sd.RawOutputStream | None = None

    async def start(self) -> None:
        self._stream = sd.RawOutputStream(
            device=self.device.index,
            channels=self._channels,
            samplerate=self.rate,
            dtype="int16",
            blocksize=self._blocksize,
            callback=self._callback,
        )
        self._stream.start()
        log.info("speaker: %s (native %d Hz), played as %d ch @ %d Hz",
                 self.device.name, self.device.default_samplerate, self._channels, self.rate)

    def _idle_signal(self, dbfs: float | None) -> bytes:
        """One second of keep-awake noise, looped between replies; empty when disabled."""
        if dbfs is None:
            return b""
        mono = np.frombuffer(lead_in(self.rate, 1000, dbfs), dtype="<i2").astype(np.float32) / 32767
        return float_to_pcm16(to_channels(mono, self._channels))

    def _fill_idle(self, size: int) -> bytes:
        if not self._idle:
            return b"\x00" * size
        out = bytearray()
        while len(out) < size:
            part = self._idle[self._idle_pos:self._idle_pos + size - len(out)]
            out += part
            self._idle_pos = (self._idle_pos + len(part)) % len(self._idle)
        return bytes(out)

    def _callback(self, outdata, frames, time_info, status) -> None:
        """PortAudio thread: pull from the FIFO and fill the rest with the idle signal."""
        wanted = frames * self._frame_bytes
        with self._lock:
            take = min(wanted, len(self._buffer))
            chunk = bytes(self._buffer[:take])
            del self._buffer[:take]
        outdata[:take] = chunk
        if take < wanted:
            outdata[take:wanted] = self._fill_idle(wanted - take)

    def write(self, pcm: bytes) -> None:
        if not pcm:
            return
        data = float_to_pcm16(to_channels(pcm16_to_float(pcm), self._channels))
        with self._lock:
            self._buffer.extend(data)

    def clear(self) -> float:
        """Drop everything queued, fading the next few milliseconds out to avoid a click.

        Returns how many seconds of audio were dropped.
        """
        fade_bytes = int(self.rate * FADE_MS / 1000) * self._frame_bytes
        with self._lock:
            dropped = len(self._buffer)
            keep = min(fade_bytes, dropped)
            if keep:
                head = np.frombuffer(bytes(self._buffer[:keep]), dtype="<i2")
                head = head.reshape(-1, self._channels).astype(np.float32)
                ramp = np.linspace(1.0, 0.0, head.shape[0], dtype=np.float32)[:, None]
                faded = (head * ramp).astype("<i2").tobytes()
            else:
                faded = b""
            self._buffer[:] = faded
        return (dropped - len(faded)) / self._frame_bytes / self.rate

    @property
    def backlog_seconds(self) -> float:
        with self._lock:
            return len(self._buffer) / self._frame_bytes / self.rate

    async def drain(self) -> None:
        """Wait until everything written so far has reached the device."""
        while self.backlog_seconds > 0:
            await asyncio.sleep(0.02)
        await asyncio.sleep(self._blocksize / self.rate)

    async def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None


def earcon(rate: int = 24000) -> bytes:
    """Two soft rising blips: "heard you, working on it"."""
    parts = []
    for freq, ms in ((660.0, 55), (0.0, 25), (880.0, 70)):
        n = int(rate * ms / 1000)
        if freq == 0.0:
            parts.append(np.zeros(n, dtype=np.float32))
            continue
        t = np.arange(n, dtype=np.float32) / rate
        envelope = np.sin(np.pi * np.arange(n, dtype=np.float32) / n)  # no clicks at the edges
        parts.append(0.12 * envelope * np.sin(2 * np.pi * freq * t))
    return float_to_pcm16(np.concatenate(parts))


def lead_in(rate: int = 24000, ms: int = 300, level_dbfs: float = -70.0) -> bytes:
    """A pause played before every reply, so its first syllable is not lost.

    Bluetooth earbuds mute their output during digital silence and take a moment
    to come back when sound resumes; ElevenLabs audio starts speaking within
    35-80 ms, so without a lead-in the start of the first word goes missing.
    Near-inaudible noise rather than zeros wakes the earbuds during the pause
    instead of during the word.
    """
    n = int(rate * ms / 1000)
    amplitude = 10 ** (level_dbfs / 20)
    noise = np.random.default_rng().uniform(-amplitude, amplitude, n).astype(np.float32)
    return float_to_pcm16(noise)
