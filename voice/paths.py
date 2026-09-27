"""Where the daemon keeps its socket, log and transcript."""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Unix socket paths are limited to 104 bytes on macOS, so the state directory
# lives directly under the home directory rather than somewhere deep.
STATE_DIR = Path(os.environ.get("CLAUDE_VOICE_HOME", Path.home() / ".claude-voice")).expanduser()
SOCKET_PATH = STATE_DIR / "voice.sock"
LOG_PATH = STATE_DIR / "daemon.log"
TRANSCRIPT_PATH = STATE_DIR / "transcript.jsonl"
