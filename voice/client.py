"""Talking to the daemon. Standard library only, so each command starts fast."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import TextIO

from voice.paths import SOCKET_PATH


class DaemonNotRunning(RuntimeError):
    pass


def _connect(path: Path, timeout: float | None) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(path))
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        sock.close()
        raise DaemonNotRunning("the voice daemon is not running (bin/voicectl start)") from exc
    return sock


def _send(sock: socket.socket, payload: dict) -> None:
    sock.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))


def request(payload: dict, *, path: Path = SOCKET_PATH, timeout: float | None = 10.0) -> dict:
    """Send one request and return the daemon's single-line answer."""
    with _connect(path, timeout) as sock:
        _send(sock, payload)
        with sock.makefile("rb") as stream:
            line = stream.readline()
    if not line:
        raise RuntimeError("the daemon closed the connection without answering")
    return json.loads(line)


def format_event(event: dict) -> str:
    kind = event.get("type")
    text = str(event.get("text", "")).replace("\n", " ")
    if kind == "user":
        return f"🎤 {text}"
    if kind == "error":
        return f"[voice error] {text}"
    return f"[voice {kind}] {text}"


def listen(out: TextIO, *, path: Path = SOCKET_PATH) -> int:
    """Print one line per event until the daemon goes away. Made for Monitor."""
    with _connect(path, None) as sock:
        _send(sock, {"cmd": "listen"})
        with sock.makefile("rb") as stream:
            for line in stream:
                event = json.loads(line)
                out.write(format_event(event) + "\n")
                out.flush()
                if event.get("type") == "handover":
                    return 0
    out.write("[voice stopped] the daemon exited; nothing more will be heard\n")
    out.flush()
    return 1
