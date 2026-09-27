"""The voice daemon: owns the microphone, the speaker and both cloud streams.

Claude reaches it over a Unix socket, one JSON request per connection:

    {"cmd": "speak", "text": "...", "wait": false}   queue speech
    {"cmd": "hush"}                                  stop speaking now
    {"cmd": "mute"} / {"cmd": "unmute"}              stop / resume hearing the user
    {"cmd": "listen"}                                stays open; one JSON event per line
    {"cmd": "inject", "text": "..."}                 act as if the user said this
    {"cmd": "status"}
    {"cmd": "shutdown"}

Barge-in never waits for Claude: the moment the transcriber hears words while
speech is playing, playback is cut and the queue is dropped. The words the user
then says become a turn like any other -- or, when the interrupted `speak` asked
to wait, the answer to that call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from voice.stt import SpeechActivity, SttFailed, Turn

log = logging.getLogger("daemon")

PENDING_MAX = 50


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def encode(payload: dict) -> bytes:
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


@dataclass(eq=False)
class SpeakJob:
    text: str
    wait: bool
    done: asyncio.Future  # "done" | "interrupted" | "dropped" | "error: ..."
    interrupted: bool = False
    # For a waiting caller interrupted by the user: resolves to what they said.
    reply: asyncio.Future | None = None


class VoiceDaemon:
    def __init__(
        self,
        *,
        mic,
        speaker,
        stt,
        tts,
        earcon: bytes | None = None,
        lead_in: bytes | None = None,
        barge_in_min_chars: int = 2,
        idle_shutdown_s: float = 120,
        reply_wait_s: float = 20.0,
        transcript_path: Path | None = None,
        watchdog_interval: float = 5.0,
    ):
        self.mic = mic
        self.speaker = speaker
        self.stt = stt
        self.tts = tts
        self.earcon = earcon
        self.lead_in = lead_in
        self.barge_in_min_chars = barge_in_min_chars
        self.idle_shutdown_s = idle_shutdown_s
        self.reply_wait_s = reply_wait_s
        self.transcript_path = transcript_path
        self.watchdog_interval = watchdog_interval

        self.muted = False
        self._stopping = asyncio.Event()
        self._jobs: asyncio.Queue[SpeakJob] = asyncio.Queue()
        self._current: SpeakJob | None = None
        self._current_task: asyncio.Task | None = None
        self._listeners: set[asyncio.StreamWriter] = set()
        self._pending: deque[dict] = deque(maxlen=PENDING_MAX)
        self._reply_waiters: deque[asyncio.Future] = deque()
        self._started_at = time.monotonic()
        self._last_listener_at = time.monotonic()
        self._last_speech_at = 0.0

    # ------------------------------------------------------------------ lifecycle

    def request_stop(self) -> None:
        self._stopping.set()

    async def serve(self, socket_path: Path) -> None:
        socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        socket_path.unlink(missing_ok=True)

        await self.speaker.start()
        await self.mic.start()
        await self.stt.start()
        await self.tts.start()
        server = await asyncio.start_unix_server(self._handle_client, path=str(socket_path))
        os.chmod(socket_path, 0o600)
        self._started_at = self._last_listener_at = time.monotonic()

        tasks = [
            asyncio.create_task(self._pump_mic(), name="pump-mic"),
            asyncio.create_task(self._consume_stt(), name="consume-stt"),
            asyncio.create_task(self._speak_worker(), name="speak-worker"),
            asyncio.create_task(self._watchdog(), name="watchdog"),
        ]
        log.info("ready on %s", socket_path)
        try:
            await self._stopping.wait()
        finally:
            log.info("shutting down")
            self.barge_in(by_user=False)
            for writer in list(self._listeners):
                writer.close()
            server.close()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.mic.stop()
            await self.stt.stop()
            await self.tts.stop()
            await self.speaker.stop()
            try:
                await asyncio.wait_for(server.wait_closed(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            socket_path.unlink(missing_ok=True)
            log.info("stopped")

    async def _pump_mic(self) -> None:
        # While muted nothing leaves the machine; the STT socket stays open on
        # its own KeepAlive frames, so unmuting is instant.
        async for pcm in self.mic:
            if not self.muted:
                await self.stt.send(pcm)

    async def _watchdog(self) -> None:
        """Exit once nobody has listened for a while, so the mic is never left streaming."""
        while True:
            await asyncio.sleep(self.watchdog_interval)
            if self.idle_shutdown_s <= 0 or self._listeners:
                continue
            idle = time.monotonic() - self._last_listener_at
            if idle >= self.idle_shutdown_s:
                log.info("no listener for %.0f s; shutting down", idle)
                self._stopping.set()
                return

    # ------------------------------------------------------------------ hearing

    async def _consume_stt(self) -> None:
        async for event in self.stt:
            if isinstance(event, SpeechActivity):
                self._last_speech_at = time.monotonic()
                if self.speaking and len(event.text) >= self.barge_in_min_chars:
                    self.barge_in(by_user=True)
            elif isinstance(event, Turn):
                self._on_turn(event.text, source="mic")
            elif isinstance(event, SttFailed):
                self._publish({"type": "error", "text": event.reason})
                self._stopping.set()
                return

    def _on_turn(self, text: str, *, source: str) -> None:
        self._record("user", text, source=source)
        if self.earcon:
            self.speaker.write(self.earcon)
        while self._reply_waiters:
            waiter = self._reply_waiters.popleft()
            if not waiter.done():
                waiter.set_result(text)
                return
        self._publish({"type": "user", "text": text})

    def _publish(self, event: dict) -> None:
        event = {"at": now_iso(), **event}
        if not self._listeners:
            self._pending.append(event)
            return
        line = encode(event)
        for writer in list(self._listeners):
            try:
                writer.write(line)
            except Exception:  # noqa: BLE001 - a dead listener is simply dropped
                self._listeners.discard(writer)

    # ------------------------------------------------------------------ speaking

    @property
    def speaking(self) -> bool:
        return self._current is not None or not self._jobs.empty()

    def barge_in(self, *, by_user: bool) -> None:
        """Cut the current speech and drop everything queued behind it."""
        if not self.speaking:
            return
        job = self._current
        if job is not None and not job.interrupted:
            job.interrupted = True
            if by_user and job.wait and job.reply is None:
                job.reply = asyncio.get_running_loop().create_future()
                self._reply_waiters.append(job.reply)
            if self._current_task is not None:
                self._current_task.cancel()
        dropped = 0
        while not self._jobs.empty():
            queued = self._jobs.get_nowait()
            if not queued.done.done():
                queued.done.set_result("dropped")
            dropped += 1
        cut = self.speaker.clear()
        log.info("%s: cut %.1f s of speech, dropped %d queued",
                 "barge-in" if by_user else "hush", cut, dropped)

    async def _speak_worker(self) -> None:
        while True:
            job = await self._jobs.get()
            if job.done.done():
                continue
            self._current = job
            task = asyncio.create_task(self._play(job), name="speak")
            self._current_task = task
            try:
                # wait() rather than await: the task being cancelled by a
                # barge-in must not look like this worker being cancelled.
                await asyncio.wait({task})
            finally:
                if not task.done():
                    task.cancel()
                self._current = None
                self._current_task = None

            if job.interrupted or task.cancelled():
                result = "interrupted"
            elif task.exception() is not None:
                result = f"error: {task.exception()}"
                log.error("speak failed: %s", task.exception())
            else:
                result = "done"
            if not job.done.done():
                job.done.set_result(result)

    async def _play(self, job: SpeakJob) -> None:
        started = time.monotonic()
        if self.lead_in:
            # Written before synthesis starts, so the pause plays while the
            # first audio is on its way and adds almost nothing to the latency.
            self.speaker.write(self.lead_in)
        first = True
        async for pcm in self.tts.synthesize(job.text):
            if first:
                log.info("tts: first audio after %.0f ms", (time.monotonic() - started) * 1000)
                first = False
            self.speaker.write(pcm)
        await self.speaker.drain()

    # ------------------------------------------------------------------ socket

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            request = json.loads(line or b"{}")
            if request.get("cmd") == "listen":
                await self._serve_listener(reader, writer)
                return
            response = await self._dispatch(request, reader)
        except Exception as exc:  # noqa: BLE001 - report it to the client, keep serving
            log.exception("request failed")
            response = {"ok": False, "error": str(exc)}
        try:
            writer.write(encode(response))
            await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()

    async def _dispatch(self, request: dict, reader: asyncio.StreamReader) -> dict:
        cmd = request.get("cmd")
        if cmd == "speak":
            return await self._speak(request, reader)
        if cmd == "hush":
            self.barge_in(by_user=False)
            return {"ok": True}
        if cmd in ("mute", "unmute"):
            self.muted = cmd == "mute"
            log.info("microphone %s", "muted" if self.muted else "unmuted")
            return {"ok": True, "muted": self.muted}
        if cmd == "inject":
            text = str(request.get("text") or "").strip()
            if not text:
                return {"ok": False, "error": "nothing to inject"}
            self.barge_in(by_user=True)
            self._on_turn(text, source="inject")
            return {"ok": True}
        if cmd == "status":
            return self.status()
        if cmd == "shutdown":
            self._stopping.set()
            return {"ok": True}
        return {"ok": False, "error": f"unknown command {cmd!r}"}

    async def _speak(self, request: dict, reader: asyncio.StreamReader) -> dict:
        text = str(request.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "nothing to say"}
        job = SpeakJob(text, bool(request.get("wait")), asyncio.get_running_loop().create_future())
        self._jobs.put_nowait(job)
        self._record("assistant", text)
        if not job.wait:
            return {"ok": True, "queued": self._jobs.qsize()}

        outcome, result = await self._until(job.done, reader)
        if outcome == "hangup":
            # The caller hung up; whatever the user says next must reach the
            # listener rather than a connection nobody reads.
            job.wait = False
            if job.reply is not None:
                job.reply.cancel()
            return {"ok": False, "error": "client disconnected"}

        response = {"ok": True, "result": result}
        if result == "interrupted" and job.reply is not None:
            response["heard"] = await self._await_reply(job.reply, reader)
        return response

    async def _await_reply(self, reply: asyncio.Future, reader: asyncio.StreamReader) -> str | None:
        """The words that interrupted a waiting speak call.

        There is no fixed deadline: someone explaining a problem can talk for
        half a minute. The wait ends once they have been quiet for
        `reply_wait_s` without a turn arriving.
        """
        while True:
            outcome, heard = await self._until(reply, reader, timeout=0.5)
            if outcome == "done":
                return heard
            if outcome == "hangup" or time.monotonic() - self._last_speech_at >= self.reply_wait_s:
                reply.cancel()  # let the words reach the listener instead
                return None

    @staticmethod
    async def _until(future: asyncio.Future, reader: asyncio.StreamReader, timeout: float | None = None):
        """Wait for `future`: ("done", result), ("timeout", None) or ("hangup", None)."""
        hangup = asyncio.ensure_future(reader.read(1))
        try:
            done, _ = await asyncio.wait({future, hangup}, timeout=timeout,
                                         return_when=asyncio.FIRST_COMPLETED)
        finally:
            hangup.cancel()
        if future in done and not future.cancelled():
            return "done", future.result()
        if hangup in done:
            return "hangup", None
        return "timeout", None

    async def _serve_listener(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # One conversation at a time: a session that starts voice mode takes it
        # over, rather than both sessions acting on every spoken word.
        for previous in list(self._listeners):
            self._listeners.discard(previous)
            try:
                previous.write(encode({"at": now_iso(), "type": "handover",
                                       "text": "another session took over voice mode"}))
                previous.close()
            except Exception:  # noqa: BLE001 - it may already be gone
                pass
        self._listeners.add(writer)
        self._last_listener_at = time.monotonic()
        log.info("listener attached (%d)", len(self._listeners))
        while self._pending:
            writer.write(encode(self._pending.popleft()))
        try:
            await reader.read()  # returns at EOF
        except ConnectionError:
            pass
        finally:
            self._listeners.discard(writer)
            self._last_listener_at = time.monotonic()
            log.info("listener detached (%d)", len(self._listeners))
            writer.close()

    def status(self) -> dict:
        return {
            "ok": True,
            "pid": os.getpid(),
            "listening": bool(getattr(self.stt, "connected", False)),
            "muted": self.muted,
            "speaking": self.speaking,
            "queued": self._jobs.qsize(),
            "listeners": len(self._listeners),
            "pending": len(self._pending),
            "mic": getattr(getattr(self.mic, "device", None), "name", None),
            "speaker": getattr(getattr(self.speaker, "device", None), "name", None),
            "uptime_s": round(time.monotonic() - self._started_at),
        }

    def _record(self, role: str, text: str, **extra) -> None:
        if self.transcript_path is None:
            return
        entry = {"at": now_iso(), "role": role, "text": text, **extra}
        with self.transcript_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(entry, ensure_ascii=False) + "\n")
