"""End-to-end check against the real services, without microphone or speaker.

ElevenLabs speaks a sentence, the audio is streamed to Deepgram at real-time
pace, and the turn that comes back is compared with what was said. It proves
the keys, the voice, both models and the turn assembly in one go, and measures
the two latencies that matter: first audio, and end of speech to turn.
"""

from __future__ import annotations

import asyncio
import time

from voice.config import Config, Keys
from voice.dsp import Resampler, float_to_pcm16, pcm16_to_float
from voice.stt import DeepgramStt, SttFailed, Turn
from voice.tts import ElevenLabsTts

SAMPLE_RATE = 16000
CHUNK_MS = 20
TRAILING_SILENCE_S = 3.0

DEFAULT_SENTENCE = "Merhaba, ben Claude. Sesli moda geçtik; bir endpoint eklememi ister misin?"


async def roundtrip(cfg: Config, keys: Keys, text: str = DEFAULT_SENTENCE) -> dict:
    tts = ElevenLabsTts(
        keys.elevenlabs, keys.voice_id, model=cfg.tts.model,
        output_format=cfg.tts.output_format, language=cfg.tts.language or None,
        speed=cfg.tts.speed,
    )
    await tts.start()
    started = time.monotonic()
    first_audio_ms = None
    audio = bytearray()
    async for chunk in tts.synthesize(text):
        if first_audio_ms is None:
            first_audio_ms = (time.monotonic() - started) * 1000
        audio += chunk
    await tts.stop()

    speech = float_to_pcm16(Resampler(tts.rate, SAMPLE_RATE).process(pcm16_to_float(bytes(audio))))
    stt = DeepgramStt(
        keys.deepgram, cfg.stt.language, model=cfg.stt.model, sample_rate=SAMPLE_RATE,
        endpointing_ms=cfg.stt.endpointing_ms, utterance_end_ms=cfg.stt.utterance_end_ms,
        turn_grace_ms=cfg.stt.turn_grace_ms,
        turn_grace_incomplete_ms=cfg.stt.turn_grace_incomplete_ms, keyterms=cfg.stt.keyterms,
    )
    await stt.start()
    speech_ended_at: float | None = None
    outcome: dict = {}

    async def collect() -> None:
        async for event in stt:
            if isinstance(event, Turn):
                outcome["heard"] = event.text
                outcome["turn_ms"] = (event.ended_at - speech_ended_at) * 1000 if speech_ended_at else None
                return
            if isinstance(event, SttFailed):
                outcome["error"] = event.reason
                return

    collector = asyncio.create_task(collect())
    for _ in range(100):
        if stt.connected or collector.done():
            break
        await asyncio.sleep(0.1)

    step = SAMPLE_RATE * CHUNK_MS // 1000 * 2
    silence = b"\x00" * int(SAMPLE_RATE * TRAILING_SILENCE_S) * 2
    for offset in range(0, len(speech) + len(silence), step):
        if collector.done():
            break
        if offset >= len(speech) and speech_ended_at is None:
            speech_ended_at = time.monotonic()
        data = speech[offset:offset + step] if offset < len(speech) else silence[:step]
        await stt.send(data)
        await asyncio.sleep(CHUNK_MS / 1000)

    try:
        await asyncio.wait_for(collector, timeout=5.0)
    except asyncio.TimeoutError:
        outcome.setdefault("error", "no turn came back from Deepgram")
    await stt.stop()

    return {
        "said": text,
        "tts_first_audio_ms": round(first_audio_ms or -1),
        "speech_s": round(len(speech) / 2 / SAMPLE_RATE, 1),
        **{k: round(v) if isinstance(v, float) else v for k, v in outcome.items()},
    }
