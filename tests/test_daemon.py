"""The daemon's socket protocol, barge-in and delivery, with fake audio and providers."""

import asyncio
import json
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from voice.daemon import VoiceDaemon
from voice.stt import SpeechActivity, SttFailed, Turn
from voice.wake import WakeWord


class FakeMic:
    device = None

    def __init__(self):
        self._stopped = asyncio.Event()

    async def start(self):
        pass

    async def stop(self):
        self._stopped.set()

    async def __aiter__(self):
        await self._stopped.wait()
        return
        yield  # pragma: no cover - makes this an async generator


class FakeStt:
    connected = True

    def __init__(self):
        self.events: asyncio.Queue = asyncio.Queue()

    async def start(self):
        pass

    async def stop(self):
        self.events.put_nowait(None)

    async def send(self, pcm):
        pass

    def emit(self, event):
        self.events.put_nowait(event)

    async def __aiter__(self):
        while True:
            event = await self.events.get()
            if event is None:
                return
            yield event


class FakeTts:
    """Yields `chunks` pieces of audio, `delay` seconds apart."""

    def __init__(self, chunks=5, delay=0.02):
        self.chunks = chunks
        self.delay = delay
        self.spoken: list[str] = []

    async def start(self):
        pass

    async def stop(self):
        pass

    async def synthesize(self, text):
        self.spoken.append(text)
        for _ in range(self.chunks):
            await asyncio.sleep(self.delay)
            yield b"\x00\x00" * 240


class FakeSpeaker:
    device = None

    def __init__(self):
        self.written = 0
        self.cleared = 0
        self.chunks: list[bytes] = []

    async def start(self):
        pass

    async def stop(self):
        pass

    def write(self, pcm):
        self.written += len(pcm)
        self.chunks.append(pcm)

    def clear(self):
        self.cleared += 1
        return 0.0

    async def drain(self):
        await asyncio.sleep(0.01)


@pytest.fixture
def socket_dir():
    # macOS caps Unix socket paths at 104 bytes; pytest's tmp_path is longer.
    path = Path(tempfile.mkdtemp(prefix="cv", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


async def call(sock: Path, payload: dict) -> dict:
    reader, writer = await asyncio.open_unix_connection(str(sock))
    writer.write((json.dumps(payload) + "\n").encode())
    await writer.drain()
    line = await reader.readline()
    writer.close()
    return json.loads(line)


async def attach_listener(sock: Path):
    """Returns (reader, writer). Keep the writer referenced: once it is garbage
    collected the connection closes and the daemon detaches the listener."""
    reader, writer = await asyncio.open_unix_connection(str(sock))
    writer.write(b'{"cmd": "listen"}\n')
    await writer.drain()
    return reader, writer


async def next_event(reader, timeout=1.0) -> dict:
    return json.loads(await asyncio.wait_for(reader.readline(), timeout))


def run_with_daemon(socket_dir, scenario, *, tts=None, **options):
    """Start a daemon with fakes, run `scenario(daemon, sock, stt, speaker, tts)`, stop it."""

    async def main():
        stt, speaker, tts_ = FakeStt(), FakeSpeaker(), tts or FakeTts()
        daemon = VoiceDaemon(mic=FakeMic(), speaker=speaker, stt=stt, tts=tts_,
                             earcon=b"\x00\x00", **options)
        sock = socket_dir / "v.sock"
        server = asyncio.create_task(daemon.serve(sock))
        for _ in range(100):
            if sock.exists():
                break
            await asyncio.sleep(0.01)
        try:
            return await scenario(daemon, sock, stt, speaker, tts_)
        finally:
            daemon.request_stop()
            await asyncio.wait_for(server, 5)

    return asyncio.run(main())


def test_speak_without_wait_answers_at_once_and_plays(socket_dir):
    async def scenario(daemon, sock, stt, speaker, tts):
        response = await call(sock, {"cmd": "speak", "text": "Merhaba"})
        await asyncio.sleep(0.3)
        return response, tts.spoken, speaker.written

    response, spoken, written = run_with_daemon(socket_dir, scenario)
    assert response["ok"] is True
    assert spoken == ["Merhaba"]
    assert written > 0


def test_speak_wait_reports_done(socket_dir):
    async def scenario(daemon, sock, stt, speaker, tts):
        return await call(sock, {"cmd": "speak", "text": "Kısa", "wait": True})

    assert run_with_daemon(socket_dir, scenario) == {"ok": True, "result": "done"}


def test_user_words_interrupt_a_waiting_speak_and_become_its_answer(socket_dir):
    async def scenario(daemon, sock, stt, speaker, tts):
        listener, listener_writer = await attach_listener(sock)
        pending = asyncio.create_task(call(sock, {"cmd": "speak", "text": "Uzun anlatım", "wait": True}))
        await asyncio.sleep(0.1)
        stt.emit(SpeechActivity("bir"))
        stt.emit(Turn("bir saniye, bunu neden böyle yaptın?", 0.0))
        response = await asyncio.wait_for(pending, 2)
        # The words went to the waiting caller, so the listener must not get them too.
        with pytest.raises(asyncio.TimeoutError):
            await next_event(listener, timeout=0.2)
        return response, speaker.cleared

    response, cleared = run_with_daemon(socket_dir, scenario, tts=FakeTts(chunks=100))
    assert response == {"ok": True, "result": "interrupted", "heard": "bir saniye, bunu neden böyle yaptın?"}
    assert cleared >= 1


def test_barge_in_drops_queued_speech(socket_dir):
    async def scenario(daemon, sock, stt, speaker, tts):
        await call(sock, {"cmd": "speak", "text": "birinci"})
        second = asyncio.create_task(call(sock, {"cmd": "speak", "text": "ikinci", "wait": True}))
        await asyncio.sleep(0.1)
        stt.emit(SpeechActivity("dur"))
        response = await asyncio.wait_for(second, 2)
        return response, tts.spoken

    response, spoken = run_with_daemon(socket_dir, scenario, tts=FakeTts(chunks=100))
    assert response == {"ok": True, "result": "dropped"}
    assert spoken == ["birinci"]


def test_activity_while_silent_is_not_a_barge_in(socket_dir):
    async def scenario(daemon, sock, stt, speaker, tts):
        stt.emit(SpeechActivity("merhaba"))
        await asyncio.sleep(0.05)
        return speaker.cleared

    assert run_with_daemon(socket_dir, scenario) == 0


def test_turns_reach_the_listener_and_wait_for_one_when_nobody_listens(socket_dir):
    async def scenario(daemon, sock, stt, speaker, tts):
        stt.emit(Turn("ilk söz", 0.0))
        await asyncio.sleep(0.05)
        assert daemon.status()["pending"] == 1
        listener, listener_writer = await attach_listener(sock)
        first = await next_event(listener)
        stt.emit(Turn("ikinci söz", 0.0))
        second = await next_event(listener)
        return first, second

    first, second = run_with_daemon(socket_dir, scenario)
    assert (first["type"], first["text"]) == ("user", "ilk söz")
    assert (second["type"], second["text"]) == ("user", "ikinci söz")


def test_a_new_listener_takes_over_from_the_previous_one(socket_dir):
    async def scenario(daemon, sock, stt, speaker, tts):
        old, old_writer = await attach_listener(sock)
        await asyncio.sleep(0.05)
        new, new_writer = await attach_listener(sock)
        handover = await next_event(old)
        assert await asyncio.wait_for(old.readline(), 1) == b""  # closed after the notice
        stt.emit(Turn("sadece yeni oturuma", 0.0))
        return handover, await next_event(new), daemon.status()["listeners"]

    handover, event, listeners = run_with_daemon(socket_dir, scenario)
    assert handover["type"] == "handover"
    assert event["text"] == "sadece yeni oturuma"
    assert listeners == 1


def test_inject_interrupts_speech_and_is_delivered(socket_dir):
    async def scenario(daemon, sock, stt, speaker, tts):
        listener, listener_writer = await attach_listener(sock)
        await call(sock, {"cmd": "speak", "text": "uzun"})
        await asyncio.sleep(0.05)
        await call(sock, {"cmd": "inject", "text": "test mesajı"})
        return await next_event(listener), speaker.cleared

    event, cleared = run_with_daemon(socket_dir, scenario, tts=FakeTts(chunks=100))
    assert event["text"] == "test mesajı"
    assert cleared >= 1


def test_a_caller_that_hangs_up_does_not_swallow_the_next_turn(socket_dir):
    async def scenario(daemon, sock, stt, speaker, tts):
        listener, listener_writer = await attach_listener(sock)
        reader, writer = await asyncio.open_unix_connection(str(sock))
        writer.write(b'{"cmd": "speak", "text": "uzun", "wait": true}\n')
        await writer.drain()
        await asyncio.sleep(0.1)
        writer.close()  # e.g. the Bash tool timed out
        await asyncio.sleep(0.05)
        stt.emit(SpeechActivity("hey"))
        stt.emit(Turn("hey, dinliyor musun", 0.0))
        return await next_event(listener)

    event = run_with_daemon(socket_dir, scenario, tts=FakeTts(chunks=100))
    assert event["text"] == "hey, dinliyor musun"


def test_stt_failure_is_reported_and_stops_the_daemon(socket_dir):
    async def scenario(daemon, sock, stt, speaker, tts):
        listener, listener_writer = await attach_listener(sock)
        stt.emit(SttFailed("Deepgram rejected the request: HTTP 400"))
        event = await next_event(listener)
        await asyncio.wait_for(daemon._stopping.wait(), 1)
        return event

    event = run_with_daemon(socket_dir, scenario)
    assert event["type"] == "error"


def test_the_daemon_exits_when_nobody_listens(socket_dir):
    async def scenario(daemon, sock, stt, speaker, tts):
        await asyncio.wait_for(daemon._stopping.wait(), 2)
        return True

    assert run_with_daemon(socket_dir, scenario, idle_shutdown_s=0.2, watchdog_interval=0.05)


def test_muted_audio_never_reaches_the_transcriber():
    class CountingStt(FakeStt):
        def __init__(self):
            super().__init__()
            self.sent = 0

        async def send(self, pcm):
            self.sent += 1

    class ThreeChunkMic(FakeMic):
        async def __aiter__(self):
            for _ in range(3):
                yield b"\x00\x00"

    async def main():
        stt = CountingStt()
        daemon = VoiceDaemon(mic=ThreeChunkMic(), speaker=FakeSpeaker(), stt=stt, tts=FakeTts())
        daemon.muted = True
        await daemon._pump_mic()
        while_muted = stt.sent
        daemon.muted = False
        await daemon._pump_mic()
        return while_muted, stt.sent

    assert asyncio.run(main()) == (0, 3)


def test_mute_and_unmute_over_the_socket(socket_dir):
    async def scenario(daemon, sock, stt, speaker, tts):
        muted = await call(sock, {"cmd": "mute"})
        state = (await call(sock, {"cmd": "status"}))["muted"]
        unmuted = await call(sock, {"cmd": "unmute"})
        return muted, state, unmuted

    muted, state, unmuted = run_with_daemon(socket_dir, scenario)
    assert muted == {"ok": True, "muted": True}
    assert state is True
    assert unmuted == {"ok": True, "muted": False}


def test_every_reply_starts_with_the_lead_in(socket_dir):
    lead = b"\x01\x00" * 10

    async def scenario(daemon, sock, stt, speaker, tts):
        await call(sock, {"cmd": "speak", "text": "bir", "wait": True})
        await call(sock, {"cmd": "speak", "text": "iki", "wait": True})
        return speaker.chunks

    chunks = run_with_daemon(socket_dir, scenario, tts=FakeTts(chunks=2), lead_in=lead)
    assert chunks[0] == lead and chunks[3] == lead  # lead, audio, audio, lead, ...
    assert chunks.count(lead) == 2


def test_a_long_interruption_is_waited_for_while_the_user_keeps_talking(socket_dir):
    async def scenario(daemon, sock, stt, speaker, tts):
        pending = asyncio.create_task(call(sock, {"cmd": "speak", "text": "anlatım", "wait": True}))
        await asyncio.sleep(0.1)
        for _ in range(8):  # 0.8 s of talking, well past reply_wait_s
            stt.emit(SpeechActivity("uzun bir açıklama"))
            await asyncio.sleep(0.1)
        stt.emit(Turn("uzun bir açıklama yapıyorum", 0.0))
        return await asyncio.wait_for(pending, 3)

    response = run_with_daemon(socket_dir, scenario, tts=FakeTts(chunks=100), reply_wait_s=0.3)
    assert response == {"ok": True, "result": "interrupted", "heard": "uzun bir açıklama yapıyorum"}


def test_a_waiting_speak_gives_up_once_the_user_goes_quiet(socket_dir):
    async def scenario(daemon, sock, stt, speaker, tts):
        listener, listener_writer = await attach_listener(sock)
        pending = asyncio.create_task(call(sock, {"cmd": "speak", "text": "anlatım", "wait": True}))
        await asyncio.sleep(0.1)
        stt.emit(SpeechActivity("öhm"))
        response = await asyncio.wait_for(pending, 3)
        stt.emit(Turn("sonradan gelen söz", 0.0))  # too late for the caller
        return response, await next_event(listener)

    response, event = run_with_daemon(socket_dir, scenario, tts=FakeTts(chunks=100), reply_wait_s=0.3)
    assert response == {"ok": True, "result": "interrupted", "heard": None}
    assert event["text"] == "sonradan gelen söz"


CEZERI = WakeWord(["Cezeri"])


def test_with_a_wake_word_only_addressed_speech_gets_through(socket_dir):
    async def scenario(daemon, sock, stt, speaker, tts):
        listener, listener_writer = await attach_listener(sock)
        stt.emit(Turn("Yarın toplantı var, unutma.", 0.0))
        stt.emit(Turn("Cezeri, testleri çalıştır.", 0.0))
        event = await next_event(listener)
        return event, daemon.status()["ignored"]

    event, ignored = run_with_daemon(socket_dir, scenario, wake=CEZERI)
    assert event["text"] == "testleri çalıştır."
    assert ignored == 1


def test_the_name_alone_opens_a_short_window_for_the_next_sentence(socket_dir):
    async def scenario(daemon, sock, stt, speaker, tts):
        listener, listener_writer = await attach_listener(sock)
        stt.emit(Turn("Cezeri.", 0.0))
        stt.emit(Turn("bir saniye bekle", 0.0))
        within = await next_event(listener)
        await asyncio.sleep(0.25)  # the window has closed
        stt.emit(Turn("bu da başka biriyle konuşma", 0.0))
        with pytest.raises(asyncio.TimeoutError):
            await next_event(listener, timeout=0.2)
        return within

    event = run_with_daemon(socket_dir, scenario, wake=CEZERI, wake_window_s=0.1)
    assert event["text"] == "bir saniye bekle"


def test_with_a_wake_word_only_the_name_interrupts_speech(socket_dir):
    async def scenario(daemon, sock, stt, speaker, tts):
        await call(sock, {"cmd": "speak", "text": "uzun anlatım"})
        await asyncio.sleep(0.05)
        stt.emit(SpeechActivity("merhaba, nasılsın"))  # someone else in the room
        await asyncio.sleep(0.05)
        before = speaker.cleared
        stt.emit(SpeechActivity("Cezeri dur"))
        await asyncio.sleep(0.05)
        return before, speaker.cleared

    before, after = run_with_daemon(socket_dir, scenario, tts=FakeTts(chunks=100), wake=CEZERI)
    assert before == 0
    assert after >= 1


def test_speech_that_was_not_for_the_assistant_is_not_recorded(socket_dir, tmp_path):
    transcript = tmp_path / "transcript.jsonl"

    async def scenario(daemon, sock, stt, speaker, tts):
        stt.emit(Turn("Bu akşam yemeğe gelecek misin?", 0.0))
        stt.emit(Turn("Cezeri, durum nedir?", 0.0))
        await asyncio.sleep(0.05)

    run_with_daemon(socket_dir, scenario, wake=CEZERI, transcript_path=transcript)
    recorded = [json.loads(line)["text"] for line in transcript.read_text().splitlines()]
    assert recorded == ["durum nedir?"]


def test_a_stream_that_stops_running_is_reopened_and_the_listener_told_if_that_fails(socket_dir):
    attempts = []

    def restart_audio():
        attempts.append(time.monotonic())
        if len(attempts) == 1:
            raise RuntimeError("device busy")
        speaker.last_audio_at = time.monotonic()

    async def scenario(daemon, sock, stt, speaker_, tts):
        nonlocal speaker
        speaker = speaker_
        reader, _writer = await attach_listener(sock)
        speaker.last_audio_at = time.monotonic() - 10  # no callbacks for 10 s

        failed = await next_event(reader, timeout=3)
        assert failed["type"] == "audio" and "device busy" in failed["text"]
        back = await next_event(reader, timeout=5)
        assert back == {**back, "type": "audio", "text": "the audio device is back"}
        assert len(attempts) == 2
        assert (await call(sock, {"cmd": "status"}))["stalled"] == []

    speaker = None
    run_with_daemon(socket_dir, scenario, audio_check_interval=0.05, restart_audio=restart_audio)


def test_streams_that_keep_running_are_left_alone(socket_dir):
    attempts = []

    async def scenario(daemon, sock, stt, speaker, tts):
        speaker.last_audio_at = time.monotonic()
        await asyncio.sleep(0.3)
        assert attempts == []

    run_with_daemon(socket_dir, scenario, audio_check_interval=0.05,
                    restart_audio=lambda: attempts.append(1))
