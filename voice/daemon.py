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

With a wake word, only speech that opens with the assistant's name counts: as a
turn and as a barge-in. Everything else is dropped without being recorded.

Speech may carry line cues (`{{12-15}}`, see voice/cues.py): they are removed
from what is said and move the emphasis in VS Code as the words play.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from voice.cues import Cue, CueSchedule, split_cues
from voice.stt import SpeechActivity, SttFailed, Turn
from voice.wake import WakeWord

log = logging.getLogger("daemon")

PENDING_MAX = 50
# An audio stream whose callbacks stop for this long is dead, not quiet: both
# streams run continuously, silence included, one callback every 20 ms.
AUDIO_STALL_S = 3.0
AUDIO_RETRY_MAX_S = 30.0


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def encode(payload: dict) -> bytes:
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


def _log_focus_failure(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.warning("focus: could not reach VS Code: %s", task.exception())


@dataclass(eq=False)
class SpeakJob:
    text: str
    wait: bool
    done: asyncio.Future  # "done" | "interrupted" | "dropped" | "error: ..."
    interrupted: bool = False
    # For a waiting caller interrupted by the user: resolves to what they said.
    reply: asyncio.Future | None = None
    cues: list[Cue] = field(default_factory=list)


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
        audio_check_interval: float = 1.0,
        restart_audio=None,
        wake: WakeWord | None = None,
        wake_window_s: float = 8.0,
        focus: Callable[[int | None, int | None], Awaitable[None]] | None = None,
        focus_lead_ms: float = 0.0,
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
        self.audio_check_interval = audio_check_interval
        self._restart_audio = restart_audio or self._reopen_streams
        self.wake = wake
        self.wake_window_s = wake_window_s
        self.focus = focus
        self.focus_lead_ms = focus_lead_ms
        self._armed_until = 0.0
        self._ignored = 0

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

        # Microphone first: opening a Bluetooth headset's microphone switches the
        # headset to its call profile, which changes the output device's rate;
        # the speaker stream is opened after that switch rather than through it.
        await self.mic.start()
        await self.speaker.start()
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
            asyncio.create_task(self._audio_watchdog(), name="audio-watchdog"),
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

    def _stalled_streams(self) -> list[str]:
        now = time.monotonic()
        return [
            name for name, device in (("mic", self.mic), ("speaker", self.speaker))
            if now - getattr(device, "last_audio_at", now) >= AUDIO_STALL_S
        ]

    async def _audio_watchdog(self) -> None:
        """Reopen the audio streams when one of them stops running.

        A Bluetooth headset that falls back from its call profile during a long
        silence kills the streams without an error reaching us (CoreAudio only
        prints one), and the daemon would go on looking alive while deaf.
        """
        failures = 0
        next_try = 0.0
        while True:
            await asyncio.sleep(self.audio_check_interval)
            stalled = self._stalled_streams()
            if not stalled or time.monotonic() < next_try:
                if not stalled:
                    failures = 0
                continue
            log.warning("audio: no callbacks from %s for %.0f s; reopening the streams",
                        " and ".join(stalled), AUDIO_STALL_S)
            try:
                # Opening a CoreAudio stream can block for a while; keep the loop free.
                await asyncio.to_thread(self._restart_audio)
            except Exception as exc:  # noqa: BLE001 - report it and keep trying
                failures += 1
                next_try = time.monotonic() + min(AUDIO_RETRY_MAX_S, 2 ** failures)
                log.error("audio: reopening failed (attempt %d): %s", failures, exc)
                if failures == 1:
                    self._publish({"type": "audio",
                                   "text": f"the audio device stopped and could not be reopened: {exc}"})
            else:
                log.info("audio: streams reopened")
                if failures:
                    self._publish({"type": "audio", "text": "the audio device is back"})
                failures = 0

    def _reopen_streams(self) -> None:
        from .audio import rescan_devices

        self.mic.close()
        self.speaker.close()
        rescan_devices()
        # Microphone first, as at start-up.
        self.mic.open(rescan=True)
        self.speaker.open(rescan=True)

    # ------------------------------------------------------------------ hearing

    async def _consume_stt(self) -> None:
        async for event in self.stt:
            if isinstance(event, SpeechActivity):
                self._last_speech_at = time.monotonic()
                if (
                    self.speaking
                    and len(event.text) >= self.barge_in_min_chars
                    and (self.wake is None or self.wake.mentions(event.text))
                ):
                    self.barge_in(by_user=True)
            elif isinstance(event, Turn):
                self._on_heard(event.text)
            elif isinstance(event, SttFailed):
                self._publish({"type": "error", "text": event.reason})
                self._stopping.set()
                return

    def _on_heard(self, text: str) -> None:
        """A turn from the microphone: pass it on if it was meant for the assistant."""
        if self.wake is not None:
            now = time.monotonic()
            command = self.wake.addressed(text)
            if command is None:
                if now >= self._armed_until:
                    # Not for us: neither delivered nor recorded.
                    self._ignored += 1
                    near = self.wake.near_miss(text)
                    if near:
                        # Only the first word, and only when it resembles the name:
                        # enough to tune the wake word, not a record of the talk.
                        log.info("ignored a turn opening with %r, close to the wake word (%d so far)",
                                 near, self._ignored)
                    else:
                        log.info("ignored a turn not addressed to the assistant (%d so far)", self._ignored)
                    return
                command = text  # the words that follow a bare "Cezeri."
            self._armed_until = 0.0
            if not command:
                # Only the name: acknowledge, then take the next turn without it.
                self._armed_until = now + self.wake_window_s
                if self.earcon:
                    self.speaker.write(self.earcon)
                log.info("wake word alone; listening for %.0f s", self.wake_window_s)
                return
            text = command
        self._on_turn(text, source="mic")

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
        loop = asyncio.get_running_loop()
        if self.lead_in:
            # Written before synthesis starts, so the pause plays while the
            # first audio is on its way and adds almost nothing to the latency.
            self.speaker.write(self.lead_in)
        schedule = None
        if job.cues and self.focus is not None:
            schedule = CueSchedule(job.cues, self._focus_on, rate=getattr(self.tts, "rate", 24000),
                                   lead_s=self.focus_lead_ms / 1000)
        try:
            first = True
            async for pcm in self.tts.synthesize(job.text, schedule.on_times if schedule else None):
                if first:
                    log.info("tts: first audio after %.0f ms", (time.monotonic() - started) * 1000)
                    first = False
                if schedule is not None:
                    schedule.queued(len(pcm), loop.time() + self.speaker.backlog_seconds)
                self.speaker.write(pcm)
            if schedule is not None:
                schedule.finish()
            await self.speaker.drain()
        finally:
            if schedule is not None:
                # Cut off: the emphasis stays where it was, which is what the
                # user was looking at when they spoke up.
                schedule.cancel()
                if not job.interrupted and schedule.fired:
                    self._focus_on(None)

    def _focus_on(self, cue: Cue | None) -> None:
        """Move the strong highlight to the cue's lines, or remove it."""
        start, end = (cue.start, cue.end) if cue else (None, None)
        task = asyncio.ensure_future(self.focus(start, end))
        task.add_done_callback(_log_focus_failure)

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
        text, cues = split_cues(str(request.get("text") or "").strip())
        if not text.strip():
            return {"ok": False, "error": "nothing to say"}
        job = SpeakJob(text, bool(request.get("wait")), asyncio.get_running_loop().create_future(),
                       cues=cues)
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
            "wake": list(self.wake.words) if self.wake else None,
            "ignored": self._ignored,
            "stalled": self._stalled_streams(),
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
