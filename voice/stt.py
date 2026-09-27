"""Deepgram streaming transcription, assembled into whole spoken turns.

A turn is everything the user says before they stop talking, rather than one
segment per sentence, because it becomes one message to Claude.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from urllib.parse import urlencode

from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from voice.net import ssl_context

log = logging.getLogger("stt")

DEEPGRAM_URL = "wss://api.deepgram.com/v1/listen"
KEEPALIVE_INTERVAL = 5.0
AUDIO_QUEUE_MAXSIZE = 512
BACKOFF_START = 0.5
BACKOFF_MAX = 10.0

# Deepgram rejects utterance_end_ms below this with HTTP 400.
MIN_UTTERANCE_END_MS = 1000

SENTENCE_ENDINGS = ".?!…"

# A 4xx means the request itself is wrong; reconnecting cannot fix it.
# 429 is the exception -- it is a rate limit and does clear on its own.
PERMANENT_STATUSES = frozenset({400, 401, 402, 403, 404, 405, 409, 413, 414, 415, 422})


@dataclass(frozen=True)
class SpeechActivity:
    """Words heard while the user is still talking. Drives barge-in."""

    text: str


@dataclass(frozen=True)
class Turn:
    """Everything the user said before going quiet."""

    text: str
    ended_at: float  # monotonic time the turn was closed


@dataclass(frozen=True)
class SttFailed:
    """Deepgram refused the request; retrying cannot help."""

    reason: str


def is_permanent_failure(exc: BaseException) -> bool:
    return isinstance(exc, InvalidStatus) and exc.response.status_code in PERMANENT_STATUSES


def ends_sentence(text: str) -> bool:
    """True when a committed fragment reads as a finished sentence.

    A digit before the full stop is a decimal or a version number rather than a
    sentence end, so "sürüm 2." keeps the turn open.
    """
    stripped = text.rstrip().rstrip("\"')]}”’")
    if not stripped or stripped[-1] not in SENTENCE_ENDINGS:
        return False
    if stripped[-1] == "." and len(stripped) >= 2 and stripped[-2].isdigit():
        return False
    return True


class DeepgramStt:
    """Streams PCM to Deepgram and yields SpeechActivity and Turn events.

    When Deepgram reports silence, the punctuation it added decides how long to
    wait. A fragment that ends a sentence closes the turn `turn_grace_ms` after
    speech_final, or at once on UtteranceEnd, which only comes after a full
    second of silence. A fragment that stops mid-sentence ("Ya ben mikrofonu")
    is a breath, not the end, and waits `turn_grace_incomplete_ms`. Any new words
    cancel the pending close, so a pause does not split one request into two
    messages.

    Docs: https://developers.deepgram.com/reference/speech-to-text-api/listen-streaming
    """

    def __init__(
        self,
        api_key: str,
        language: str,
        *,
        model: str = "nova-3",
        sample_rate: int = 16000,
        endpointing_ms: int = 300,
        utterance_end_ms: int = 1000,
        turn_grace_ms: int = 700,
        turn_grace_incomplete_ms: int = 2500,
        keyterms: tuple[str, ...] = (),
    ):
        if utterance_end_ms < MIN_UTTERANCE_END_MS:
            raise ValueError(
                f"[stt] utterance_end_ms must be at least {MIN_UTTERANCE_END_MS} "
                f"(got {utterance_end_ms}); Deepgram rejects lower values with HTTP 400"
            )
        self.api_key = api_key
        self.language = language
        self.model = model
        self.sample_rate = sample_rate
        self.endpointing_ms = endpointing_ms
        self.utterance_end_ms = utterance_end_ms
        self.turn_grace = turn_grace_ms / 1000
        self.turn_grace_incomplete = turn_grace_incomplete_ms / 1000
        self.keyterms = keyterms

        self.connected = False
        self._audio: asyncio.Queue[bytes | None] = asyncio.Queue(AUDIO_QUEUE_MAXSIZE)
        self._out: asyncio.Queue[SpeechActivity | Turn | SttFailed | None] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._closing = False
        self._pending: list[str] = []
        self._close_timer: asyncio.TimerHandle | None = None
        self._dropped = 0

    @property
    def url(self) -> str:
        params = [
            ("model", self.model),
            ("language", self.language),
            ("encoding", "linear16"),
            ("sample_rate", str(self.sample_rate)),
            ("channels", "1"),
            ("interim_results", "true"),
            ("punctuate", "true"),
            ("smart_format", "true"),
            ("endpointing", str(self.endpointing_ms)),
            ("utterance_end_ms", str(self.utterance_end_ms)),
        ]
        params += [("keyterm", term) for term in self.keyterms]
        return f"{DEEPGRAM_URL}?{urlencode(params)}"

    async def start(self) -> None:
        self._closing = False
        self._task = asyncio.create_task(self._run(), name="stt-run")

    async def send(self, pcm: bytes) -> None:
        if self._closing or not pcm:
            return
        try:
            self._audio.put_nowait(pcm)
        except asyncio.QueueFull:
            self._dropped += 1
            if self._dropped <= 5 or self._dropped % 100 == 0:
                log.warning("stt: upload queue full, dropped %d chunks", self._dropped)

    async def stop(self) -> None:
        self._closing = True
        self._close_turn()
        self._close_upload_queue()
        task, self._task = self._task, None
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
        self._out.put_nowait(None)

    async def __aiter__(self):
        while True:
            item = await self._out.get()
            if item is None:
                return
            yield item

    # ---------------------------------------------------------------- turn logic

    def handle(self, message: dict) -> None:
        """Process one Deepgram message. Public so tests can drive it directly."""
        kind = message.get("type")
        if kind == "Results":
            self._handle_results(message)
        elif kind == "UtteranceEnd":
            self._end_of_speech(utterance_end=True)
        elif kind == "Error":
            log.error("stt: %s", message)

    def _handle_results(self, message: dict) -> None:
        alternatives = message.get("channel", {}).get("alternatives", [])
        text = ((alternatives[0].get("transcript") if alternatives else "") or "").strip()

        if text:
            # The user is still talking: whatever close was pending is premature.
            self._cancel_close()
            self._out.put_nowait(SpeechActivity(text))

        if not message.get("is_final"):
            return
        if text:
            self._pending.append(text)
        if message.get("speech_final"):
            self._end_of_speech(utterance_end=False)

    def _end_of_speech(self, *, utterance_end: bool) -> None:
        """Deepgram heard silence: close the turn now, soon, or after a breath."""
        if not self._pending:
            return
        if ends_sentence(self._pending[-1]):
            if utterance_end:
                self._close_turn()  # a full second of silence has already passed
            else:
                self._schedule_close(self.turn_grace)
        elif self._close_timer is None:
            self._schedule_close(self.turn_grace_incomplete)

    def _schedule_close(self, delay: float) -> None:
        self._cancel_close()
        if delay <= 0:
            self._close_turn()
            return
        self._close_timer = asyncio.get_running_loop().call_later(delay, self._close_turn)

    def _cancel_close(self) -> None:
        if self._close_timer is not None:
            self._close_timer.cancel()
            self._close_timer = None

    def _close_turn(self) -> None:
        self._cancel_close()
        if not self._pending:
            return
        text = " ".join(self._pending).strip()
        self._pending.clear()
        self._out.put_nowait(Turn(text, time.monotonic()))

    # ----------------------------------------------------------------- transport

    def _close_upload_queue(self) -> None:
        """Queue the close sentinel, making room for it if a stalled socket filled the queue."""
        while True:
            try:
                self._audio.put_nowait(None)
                return
            except asyncio.QueueFull:
                try:
                    self._audio.get_nowait()
                except asyncio.QueueEmpty:
                    return

    async def _run(self) -> None:
        backoff = BACKOFF_START
        while not self._closing:
            try:
                async with connect(
                    self.url,
                    additional_headers={"Authorization": f"Token {self.api_key}"},
                    ssl=ssl_context(),
                    max_queue=64,
                    open_timeout=10,
                    ping_interval=None,  # Deepgram wants its own KeepAlive frames
                ) as socket:
                    log.info("stt: connected (%s, %s)", self.model, self.language)
                    self.connected = True
                    backoff = BACKOFF_START
                    await self._session(socket)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - transient failures must retry
                if self._closing:
                    break
                if is_permanent_failure(exc):
                    log.error("stt: Deepgram rejected the request (%s); not retrying", exc)
                    self._closing = True
                    self._out.put_nowait(SttFailed(f"Deepgram rejected the request: {exc}"))
                    return
                log.warning("stt: connection lost (%s); retrying in %.1fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
            finally:
                self.connected = False
        log.info("stt: stopped")

    async def _session(self, socket) -> None:
        helpers = [
            asyncio.create_task(self._sender(socket), name="stt-send"),
            asyncio.create_task(self._keepalive(socket), name="stt-keepalive"),
        ]
        try:
            async for message in socket:
                if isinstance(message, bytes):
                    continue
                self.handle(json.loads(message))
        finally:
            for task in helpers:
                task.cancel()
            await asyncio.gather(*helpers, return_exceptions=True)

    async def _sender(self, socket) -> None:
        while True:
            pcm = await self._audio.get()
            if pcm is None:
                await socket.send(json.dumps({"type": "CloseStream"}))
                return
            await socket.send(pcm)

    async def _keepalive(self, socket) -> None:
        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            await socket.send(json.dumps({"type": "KeepAlive"}))
