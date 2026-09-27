"""ElevenLabs streaming TTS: a whole text in, PCM out as it is generated.

The text is complete before synthesis starts, so it goes up in one message and
the audio is read back until isFinal.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass
from typing import AsyncIterator, Callable
from urllib.parse import urlencode

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from voice.net import ssl_context

log = logging.getLogger("tts")

BASE_URL = "wss://api.elevenlabs.io/v1/text-to-speech"

# How many characters ElevenLabs buffers before generating. 50 is the documented
# minimum and gives the earliest first audio; later chunks can be longer.
CHUNK_LENGTH_SCHEDULE = [50, 120, 160, 250]
INACTIVITY_TIMEOUT = 180  # seconds; the maximum ElevenLabs allows
MESSAGE_TIMEOUT = 30.0
# A spare socket older than this is replaced before ElevenLabs closes it. A warm
# socket gives first audio in ~250 ms; a cold one measured up to 2.5 s.
WARM_MAX_AGE = 150.0
REFRESH_INTERVAL = 20.0


class TtsError(RuntimeError):
    """Raised when synthesis fails in a way the caller must know about."""


@dataclass(frozen=True)
class CharTimes:
    """When characters of the synthesized text are spoken.

    `first` is the index in the text of the first character covered, and
    `starts_ms[i]` is when character `first + i` starts, in ms from the start of
    the generated audio.
    """

    first: int
    starts_ms: list[float]


class ElevenLabsTts:
    """One WebSocket per generation, with the next one opened in advance.

    The spare connection hides the ~300 ms TLS + handshake cost, so the first
    audio of a reply arrives as soon as ElevenLabs can produce it.

    Docs: https://elevenlabs.io/docs/api-reference/text-to-speech/v-1-text-to-speech-voice-id-stream-input
    """

    def __init__(
        self,
        api_key: str,
        voice_id: str,
        *,
        model: str = "eleven_flash_v2_5",
        output_format: str = "pcm_24000",
        language: str | None = None,
        voice_settings: dict | None = None,
        speed: float = 1.0,
    ):
        self.api_key = api_key
        self.voice_id = voice_id
        self.model = model
        self.output_format = output_format
        self.language = language
        self.voice_settings = voice_settings or {
            "stability": 0.5, "similarity_boost": 0.75, "use_speaker_boost": True,
        }
        self.speed = speed
        self.rate = int(output_format.rsplit("_", 1)[1])
        self._warm: asyncio.Task | None = None
        self._warm_since = 0.0
        self._refresher: asyncio.Task | None = None
        self._closing = False

    @property
    def url(self) -> str:
        params = {
            "model_id": self.model,
            "output_format": self.output_format,
            "inactivity_timeout": str(INACTIVITY_TIMEOUT),
        }
        if self.language:
            params["language_code"] = self.language
        return f"{BASE_URL}/{self.voice_id}/stream-input?{urlencode(params)}"

    async def start(self) -> None:
        self._closing = False
        self._prewarm()
        self._refresher = asyncio.create_task(self._refresh_loop(), name="tts-refresh")

    async def stop(self) -> None:
        self._closing = True
        if self._refresher is not None:
            self._refresher.cancel()
            self._refresher = None
        await self._discard_warm()

    def _prewarm(self) -> None:
        if not self._closing and self._warm is None:
            self._warm = asyncio.create_task(self._open(), name="tts-warm")
            self._warm_since = time.monotonic()

    async def _discard_warm(self) -> None:
        task, self._warm = self._warm, None
        if task is None:
            return
        try:
            socket = await task
        except Exception:  # noqa: BLE001 - nothing to close if it never opened
            return
        await socket.close()

    async def _refresh_loop(self) -> None:
        """Replace the spare socket before ElevenLabs' inactivity timeout closes it."""
        while True:
            await asyncio.sleep(REFRESH_INTERVAL)
            if self._warm is not None and time.monotonic() - self._warm_since > WARM_MAX_AGE:
                await self._discard_warm()
                self._prewarm()

    async def _open(self):
        return await connect(
            self.url,
            additional_headers={"xi-api-key": self.api_key},
            ssl=ssl_context(),
            max_queue=64,
            open_timeout=10,
            close_timeout=1,
            ping_interval=None,
        )

    async def _take_socket(self):
        """Hand out the warm socket and start warming the next one."""
        task, self._warm = self._warm, None
        try:
            socket = await task if task is not None else await self._open()
        except Exception:  # noqa: BLE001 - a failed warm-up just means open a fresh one
            socket = await self._open()
        self._prewarm()
        return socket

    async def _begin(self, socket, text: str) -> None:
        # The initialisation message must carry a single space as its text; the
        # empty text at the end closes the input side and flushes everything.
        await socket.send(json.dumps({
            "text": " ",
            "voice_settings": {**self.voice_settings, "speed": round(float(self.speed), 2)},
            "generation_config": {"chunk_length_schedule": CHUNK_LENGTH_SCHEDULE},
        }))
        await socket.send(json.dumps({"text": text.strip() + " "}, ensure_ascii=False))
        await socket.send(json.dumps({"text": ""}))

    async def synthesize(
        self, text: str, on_times: Callable[[CharTimes], None] | None = None,
    ) -> AsyncIterator[bytes]:
        """Yield PCM as it is generated; report character timings to `on_times`.

        ElevenLabs sends timings in blocks that run ahead of the audio, each
        timed from its own start, and consecutive: a block begins where the
        previous one's last character ends.
        """
        socket = await self._take_socket()
        next_char = 0
        block_start_ms = 0.0
        try:
            try:
                await self._begin(socket, text)
            except ConnectionClosed:
                # The warm socket outlived ElevenLabs' inactivity timeout.
                await socket.close()
                socket = await self._open()
                await self._begin(socket, text)

            while True:
                message = await asyncio.wait_for(socket.recv(), timeout=MESSAGE_TIMEOUT)
                if isinstance(message, bytes):
                    continue
                event = json.loads(message)
                if event.get("error") or event.get("code"):
                    raise TtsError(f"{event.get('code')}: {event.get('message') or event}")
                alignment = event.get("alignment") or {}
                chars = alignment.get("chars") or []
                if chars:
                    starts = alignment.get("charStartTimesMs") or []
                    durations = alignment.get("charDurationsMs") or []
                    if on_times is not None and len(starts) == len(chars):
                        on_times(CharTimes(next_char, [block_start_ms + t for t in starts]))
                    next_char += len(chars)
                    if starts and durations:
                        block_start_ms += starts[-1] + durations[-1]
                if event.get("audio"):
                    yield base64.b64decode(event["audio"])
                if event.get("isFinal"):
                    return
        except ConnectionClosed as exc:
            # ElevenLabs closes the socket after the final audio on some models
            # instead of sending isFinal; anything else is a real failure.
            if exc.rcvd is None or exc.rcvd.code != 1000:
                raise TtsError(f"connection closed: {exc}") from exc
        finally:
            await socket.close()
