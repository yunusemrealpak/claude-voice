"""Settings layering and where credentials may come from."""

from voice.config import load_config, load_keys


def test_local_config_overrides_the_tracked_one_section_by_section(tmp_path):
    tracked = tmp_path / "config.toml"
    local = tmp_path / "config.local.toml"
    tracked.write_text('[audio]\nmic = ""\nearcon = false\n[stt]\nlanguage = "tr"\n')
    local.write_text('[audio]\nmic = "Built-in"\n')

    cfg = load_config(tracked, local)

    assert cfg.audio.mic == "Built-in"
    assert cfg.audio.earcon is False  # untouched keys of an overridden section survive
    assert cfg.stt.language == "tr"


def test_missing_credentials_and_voice_are_reported(tmp_path, monkeypatch):
    for name in ("DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID"):
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / ".env"
    env.write_text("DEEPGRAM_API_KEY=dg\n")

    problems = load_keys(env).problems()

    assert [p.split()[0] for p in problems] == ["ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID"]


def test_the_voice_id_comes_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "voice-from-env")
    assert load_keys(tmp_path / "missing.env").voice_id == "voice-from-env"
