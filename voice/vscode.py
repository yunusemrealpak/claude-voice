"""Driving the Claude Voice Guide extension in VS Code through its URI handler.

A URI rather than a port: it works with any number of VS Code windows and
needs no server. `open vscode://...` reaches the window that was used last.
"""

from __future__ import annotations

import asyncio
import subprocess
from urllib.parse import urlencode

GUIDE_EXTENSION = "claude-voice.claude-voice-guide"


def guide_uri(action: str, **params) -> str:
    query = urlencode(params)
    return f"vscode://{GUIDE_EXTENSION}/{action}" + (f"?{query}" if query else "")


def open_uri(uri: str) -> None:
    subprocess.run(["open", uri], check=True)


async def focus(start: int | None, end: int | None = None) -> None:
    """Put the strong highlight on lines START-END of the file last shown, or
    remove it when START is None. Leaves the frontmost app alone (`open -g`)."""
    uri = guide_uri("focus", start=start, end=end or start) if start is not None else guide_uri("focus")
    process = await asyncio.create_subprocess_exec(
        "open", "-g", uri, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    await process.wait()
