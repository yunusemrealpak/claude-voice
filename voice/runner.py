"""Wires the real devices and cloud providers into a VoiceDaemon and runs it."""

from __future__ import annotations

import asyncio
import signal
from pathlib import Path

from voice.config import Config, Keys
from voice.daemon import VoiceDaemon
from voice.paths import TRANSCRIPT_PATH
from voice.vscode import focus
from voice.wake import WakeWord


def build(cfg: Config, keys: Keys) -> VoiceDaemon:
    # Imported here so that the client commands never pay for numpy/PortAudio.
    from voice.audio import Microphone, Speaker, earcon, lead_in
    from voice.stt import DeepgramStt
    from voice.tts import ElevenLabsTts

    tts = ElevenLabsTts(
        keys.elevenlabs,
        keys.voice_id,
        model=cfg.tts.model,
        output_format=cfg.tts.output_format,
        language=cfg.tts.language or None,
        voice_settings={
            "stability": cfg.tts.stability,
            "similarity_boost": cfg.tts.similarity_boost,
            "use_speaker_boost": cfg.tts.speaker_boost,
        },
        speed=cfg.tts.speed,
    )
    speaker = Speaker(
        cfg.audio.speaker,
        rate=tts.rate,
        keep_awake_dbfs=cfg.audio.keep_awake_dbfs if cfg.audio.keep_awake else None,
    )
    mic = Microphone(cfg.audio.mic)
    wake = WakeWord(cfg.wake.words) if cfg.wake.enabled else None
    stt = DeepgramStt(
        keys.deepgram,
        cfg.stt.language,
        model=cfg.stt.model,
        sample_rate=mic.rate,
        endpointing_ms=cfg.stt.endpointing_ms,
        utterance_end_ms=cfg.stt.utterance_end_ms,
        turn_grace_ms=cfg.stt.turn_grace_ms,
        turn_grace_incomplete_ms=cfg.stt.turn_grace_incomplete_ms,
        # The name has to be heard reliably, or nothing gets through.
        keyterms=cfg.stt.keyterms + (tuple(cfg.wake.words) if wake else ()),
    )
    return VoiceDaemon(
        mic=mic,
        speaker=speaker,
        stt=stt,
        tts=tts,
        earcon=earcon(tts.rate) if cfg.audio.earcon else None,
        lead_in=lead_in(tts.rate, cfg.audio.lead_in_ms) if cfg.audio.lead_in_ms > 0 else None,
        barge_in_min_chars=cfg.stt.barge_in_min_chars,
        idle_shutdown_s=cfg.daemon.idle_shutdown_s,
        reply_wait_s=cfg.daemon.reply_wait_s,
        transcript_path=TRANSCRIPT_PATH,
        wake=wake,
        wake_window_s=cfg.wake.window_s,
        focus=focus,
        focus_lead_ms=cfg.walkthrough.focus_lead_ms,
    )


async def run(daemon: VoiceDaemon, socket_path: Path) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, daemon.request_stop)
    await daemon.serve(socket_path)
