"""Typed settings from config.toml (+ config.local.toml), credentials from .env.

Everything personal -- API keys and the voice to speak with -- comes from the
environment or the git-ignored .env, never from a tracked file.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from dotenv import load_dotenv

from voice.paths import REPO_ROOT

CONFIG_PATH = REPO_ROOT / "config.toml"
# Machine-specific overrides (device names, levels), git-ignored.
LOCAL_CONFIG_PATH = REPO_ROOT / "config.local.toml"
ENV_PATH = REPO_ROOT / ".env"


@dataclass(frozen=True)
class AudioConfig:
    # Substring of the PortAudio device name; empty means the system default.
    mic: str = ""
    speaker: str = ""
    earcon: bool = True
    # Near-silent pause before each reply; Bluetooth earbuds otherwise swallow
    # the start of the first word. 0 disables it.
    lead_in_ms: int = 300
    # Level of the near-inaudible noise played between replies so Bluetooth
    # audio never falls asleep. Raise towards -60 if first words still get cut.
    keep_awake_dbfs: float = -70.0
    keep_awake: bool = True


@dataclass(frozen=True)
class SttConfig:
    language: str = "tr"
    model: str = "nova-3"
    endpointing_ms: int = 300
    utterance_end_ms: int = 1000
    # Silence after Deepgram's speech_final before the turn is handed to Claude,
    # when the words so far end a sentence...
    turn_grace_ms: int = 700
    # ...and when they stop mid-sentence: a breath, not the end of the request.
    turn_grace_incomplete_ms: int = 2500
    # Interim words this long (in characters) while speaking cut playback off.
    barge_in_min_chars: int = 2
    keyterms: tuple[str, ...] = ()


@dataclass(frozen=True)
class TtsConfig:
    model: str = "eleven_flash_v2_5"
    language: str = "tr"
    output_format: str = "pcm_24000"
    stability: float = 0.5
    similarity_boost: float = 0.75
    speaker_boost: bool = True
    speed: float = 1.0


@dataclass(frozen=True)
class WakeConfig:
    # When enabled, only speech that opens with one of these names is a command.
    enabled: bool = False
    words: tuple[str, ...] = ()
    # After the name alone ("Cezeri."), how long the next turn counts without it.
    window_s: float = 8.0


@dataclass(frozen=True)
class WalkthroughConfig:
    # Line cues fire this much before their words play. Raise it if the
    # emphasis trails the voice, lower it (negative is fine) if it runs ahead.
    focus_lead_ms: int = 0


@dataclass(frozen=True)
class DaemonConfig:
    # With no listener attached for this long the daemon exits, so a closed
    # Claude session does not leave the microphone streaming. 0 disables it.
    idle_shutdown_s: int = 120
    # How long the user must stay quiet before `speak --wait` stops waiting for
    # the words that interrupted it. Not a total: it restarts with every word.
    reply_wait_s: float = 8.0


@dataclass(frozen=True)
class Config:
    audio: AudioConfig = field(default_factory=AudioConfig)
    stt: SttConfig = field(default_factory=SttConfig)
    tts: TtsConfig = field(default_factory=TtsConfig)
    wake: WakeConfig = field(default_factory=WakeConfig)
    walkthrough: WalkthroughConfig = field(default_factory=WalkthroughConfig)
    daemon: DaemonConfig = field(default_factory=DaemonConfig)


@dataclass(frozen=True)
class Keys:
    """Credentials and the voice id: personal, so only ever read from the environment."""

    deepgram: str
    elevenlabs: str
    voice_id: str

    def problems(self) -> list[str]:
        missing = [
            name for name, value in (
                ("DEEPGRAM_API_KEY", self.deepgram),
                ("ELEVENLABS_API_KEY", self.elevenlabs),
                ("ELEVENLABS_VOICE_ID", self.voice_id),
            ) if not value
        ]
        return [f"{name} is not set: add it to {ENV_PATH} (see .env.example)" for name in missing]


def _section(cls, raw: dict, name: str):
    known = {f.name for f in fields(cls)}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ValueError(f"config.toml [{name}]: unknown setting(s) {', '.join(unknown)}")
    values = {key: tuple(value) if isinstance(value, list) else value for key, value in raw.items()}
    return cls(**values)


def _read_toml(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def load_config(path: Path = CONFIG_PATH, local_path: Path = LOCAL_CONFIG_PATH) -> Config:
    data = _read_toml(path)
    for section, values in _read_toml(local_path).items():
        data.setdefault(section, {}).update(values)
    return Config(
        audio=_section(AudioConfig, data.get("audio", {}), "audio"),
        stt=_section(SttConfig, data.get("stt", {}), "stt"),
        tts=_section(TtsConfig, data.get("tts", {}), "tts"),
        wake=_section(WakeConfig, data.get("wake", {}), "wake"),
        walkthrough=_section(WalkthroughConfig, data.get("walkthrough", {}), "walkthrough"),
        daemon=_section(DaemonConfig, data.get("daemon", {}), "daemon"),
    )


def load_keys(env_path: Path = ENV_PATH) -> Keys:
    # Values already in the shell environment win over the .env file.
    load_dotenv(env_path, override=False)
    return Keys(
        deepgram=os.getenv("DEEPGRAM_API_KEY", "").strip(),
        elevenlabs=os.getenv("ELEVENLABS_API_KEY", "").strip(),
        voice_id=os.getenv("ELEVENLABS_VOICE_ID", "").strip(),
    )
