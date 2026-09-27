"""`voicectl`: start, drive and stop the voice daemon."""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time

from pathlib import Path
from urllib.parse import urlencode

from voice import client
from voice.client import DaemonNotRunning
from voice.paths import LOG_PATH, REPO_ROOT, SOCKET_PATH, STATE_DIR

START_TIMEOUT = 20.0
GUIDE_EXTENSION = "claude-voice.claude-voice-guide"


def _print_status(status: dict) -> None:
    print(
        f"pid {status['pid']} | listening={status['listening']} muted={status['muted']} "
        f"speaking={status['speaking']} "
        f"listeners={status['listeners']} pending={status['pending']} | "
        f"mic={status['mic']!r} speaker={status['speaker']!r} | up {status['uptime_s']} s"
    )


def _log_tail(lines: int = 20) -> str:
    try:
        return "\n".join(LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except FileNotFoundError:
        return "(no log yet)"


def cmd_start(args) -> int:
    try:
        status = client.request({"cmd": "status"}, timeout=2.0)
        print("already running")
        _print_status(status)
        return 0
    except DaemonNotRunning:
        pass

    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    SOCKET_PATH.unlink(missing_ok=True)
    with LOG_PATH.open("ab") as log_file:
        # A new session, so the daemon outlives the shell that started it.
        process = subprocess.Popen(
            [sys.executable, "-m", "voice", "run"],
            cwd=REPO_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        )

    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            print(f"the daemon exited during start-up (code {process.returncode}):\n{_log_tail()}")
            return 1
        try:
            status = client.request({"cmd": "status"}, timeout=2.0)
        except DaemonNotRunning:
            time.sleep(0.2)
            continue
        if status.get("listening"):
            print("started")
            _print_status(status)
            return 0
        time.sleep(0.2)
    print(f"the daemon is up but Deepgram has not connected after {START_TIMEOUT:.0f} s:\n{_log_tail()}")
    return 1


def cmd_run(args) -> int:
    import asyncio

    from voice.config import load_config, load_keys
    from voice.runner import build, run

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
        force=True,
    )
    keys = load_keys()
    problems = keys.problems()
    if problems:
        for problem in problems:
            logging.error(problem)
        return 2
    daemon = build(load_config(), keys)
    asyncio.run(run(daemon, SOCKET_PATH))
    return 0


def cmd_speak(args) -> int:
    text = " ".join(args.text).strip()
    if not text or text == "-":
        text = sys.stdin.read().strip()
    response = client.request(
        {"cmd": "speak", "text": text, "wait": args.wait},
        timeout=None if args.wait else 10.0,
    )
    if not response.get("ok"):
        print(f"error: {response.get('error')}")
        return 1
    if not args.wait:
        print("queued")
        return 0
    result = response["result"]
    if result == "interrupted" and response.get("heard"):
        print(f"interrupted; the user said: {response['heard']}")
    elif result == "interrupted" and "heard" in response:
        print("interrupted; the user said nothing further in time")
    else:
        print(result)
    return 0


def cmd_simple(cmd: str):
    def handler(args) -> int:
        payload = {"cmd": cmd}
        if cmd == "inject":
            payload["text"] = " ".join(args.text)
        response = client.request(payload)
        if cmd == "status":
            _print_status(response)
        else:
            print("ok" if response.get("ok") else f"error: {response.get('error')}")
        return 0 if response.get("ok") else 1
    return handler


def cmd_listen(args) -> int:
    return client.listen(sys.stdout)


def cmd_show(args) -> int:
    """Open a file in VS Code with a line range highlighted, via the guide extension."""
    if args.clear:
        uri = f"vscode://{GUIDE_EXTENSION}/clear"
    else:
        if not args.file:
            print("show needs FILE [START [END]] or --clear")
            return 2
        path = Path(args.file).expanduser().resolve()
        if not path.is_file():
            print(f"no such file: {path}")
            return 1
        start = args.start or 1
        query = urlencode({"path": str(path), "start": start, "end": args.end or start})
        uri = f"vscode://{GUIDE_EXTENSION}/show?{query}"
    subprocess.run(["open", uri], check=True)
    print("shown")
    return 0


def cmd_devices(args) -> int:
    from voice.devices import list_devices

    for device in list_devices():
        kinds = "/".join(k for k, n in (("in", device.max_input_channels),
                                        ("out", device.max_output_channels)) if n)
        print(f"[{device.index}] {device.name} ({kinds}, {device.default_samplerate:.0f} Hz)")
    return 0


def cmd_selftest(args) -> int:
    import asyncio

    from voice.config import load_config, load_keys
    from voice.selftest import DEFAULT_SENTENCE, roundtrip

    keys = load_keys()
    for problem in keys.problems():
        print(problem)
    if keys.problems():
        return 2
    result = asyncio.run(roundtrip(load_config(), keys, " ".join(args.text) or DEFAULT_SENTENCE))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if "error" in result else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="voicectl", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("start", help="start the daemon in the background").set_defaults(func=cmd_start)
    sub.add_parser("run", help="run the daemon in the foreground").set_defaults(func=cmd_run)
    sub.add_parser("status").set_defaults(func=cmd_simple("status"))
    sub.add_parser("shutdown", help="stop the daemon").set_defaults(func=cmd_simple("shutdown"))
    sub.add_parser("hush", help="stop speaking now").set_defaults(func=cmd_simple("hush"))
    sub.add_parser("mute", help="stop sending the microphone anywhere").set_defaults(func=cmd_simple("mute"))
    sub.add_parser("unmute", help="resume hearing the user").set_defaults(func=cmd_simple("unmute"))
    sub.add_parser("listen", help="print what the user says, one line each").set_defaults(func=cmd_listen)
    sub.add_parser("devices", help="list audio devices").set_defaults(func=cmd_devices)

    speak = sub.add_parser("speak", help="say something (text as arguments, or '-' for stdin)")
    speak.add_argument("--wait", action="store_true",
                       help="block until the speech ends or the user interrupts it")
    speak.add_argument("text", nargs="*")
    speak.set_defaults(func=cmd_speak)

    inject = sub.add_parser("inject", help="act as if the user had said TEXT")
    inject.add_argument("text", nargs="+")
    inject.set_defaults(func=cmd_simple("inject"))

    show = sub.add_parser("show", help="open FILE in VS Code with lines START-END highlighted")
    show.add_argument("file", nargs="?")
    show.add_argument("start", nargs="?", type=int)
    show.add_argument("end", nargs="?", type=int)
    show.add_argument("--clear", action="store_true", help="remove the highlight")
    show.set_defaults(func=cmd_show)

    selftest = sub.add_parser("selftest", help="TTS -> STT round trip against the real services")
    selftest.add_argument("text", nargs="*")
    selftest.set_defaults(func=cmd_selftest)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except DaemonNotRunning as exc:
        print(exc)
        return 3
    except KeyboardInterrupt:
        return 130
