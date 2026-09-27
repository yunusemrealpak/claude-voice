"""Line cues: markers in walkthrough narration that move the emphasis in VS Code.

`voicectl show` highlights a whole function; while it is explained, a marker
such as `{{12-15}}` placed before the words about lines 12-15 puts a stronger
highlight on just those lines at the moment the words are spoken:

    "It starts by {{12-14}} validating the request, and {{16}} only then
    takes the lock."

The markers are removed before synthesis. Each cue keeps the character offset
where it stood in the spoken text, which ElevenLabs' character timings turn
into a moment in the audio.
"""

from __future__ import annotations

import asyncio
import re
from bisect import bisect_right
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from voice.tts import CharTimes

_MARKER = re.compile(r"\s*\{\{\s*(\d+)\s*(?:-\s*(\d+)\s*)?\}\}\s*")
# Punctuation that attaches to the word before it, so no space goes in front.
_CLOSING = ".,;:!?…)]}'\""


@dataclass(frozen=True)
class Cue:
    offset: int  # index in the spoken text of the first character it covers
    start: int  # first line, 1-based
    end: int  # last line, inclusive


def split_cues(text: str) -> tuple[str, list[Cue]]:
    """The text to speak, with the markers removed, and the cues they stood for."""
    spoken: list[str] = []
    length = 0
    cues: list[Cue] = []
    position = 0
    for match in _MARKER.finditer(text):
        before = text[position:match.start()]
        spoken.append(before)
        length += len(before)
        after = text[match.end():match.end() + 1]
        # The marker ate the whitespace on both sides; put one space back
        # between the two words it separated.
        if length and after and after not in _CLOSING:
            spoken.append(" ")
            length += 1
        first = int(match.group(1))
        last = int(match.group(2) or first)
        cues.append(Cue(length, min(first, last), max(first, last)))
        position = match.end()
    spoken.append(text[position:])
    return "".join(spoken), cues


class CueSchedule:
    """Fires each cue when its words come out of the speaker.

    Two things have to meet: when a character is spoken, in ms into the audio
    (from the TTS character timings), and when that part of the audio plays
    (known as each chunk is queued: now plus the speaker's backlog). Timings run
    ahead of the audio, so a cue is scheduled once the chunk that holds it is
    queued; a stall in synthesis then shifts the cue along with the words.
    """

    def __init__(self, cues: list[Cue], fire: Callable[[Cue], None], *, rate: int, lead_s: float = 0.0):
        self._cues = cues
        self._fire = fire
        self._rate = rate
        self._lead_s = lead_s
        self._loop = asyncio.get_running_loop()
        self._audio_ms: dict[int, float] = {}
        self._segments: list[tuple[float, float]] = []  # (ms into the audio, loop time it plays)
        self._queued_ms = 0.0
        self._scheduled: set[int] = set()
        self._handles: list[asyncio.TimerHandle] = []
        self.fired = 0

    def on_times(self, times: CharTimes) -> None:
        for index, cue in enumerate(self._cues):
            k = cue.offset - times.first
            if index not in self._audio_ms and 0 <= k < len(times.starts_ms):
                self._audio_ms[index] = times.starts_ms[k]
        self._schedule()

    def queued(self, pcm_bytes: int, plays_at: float) -> None:
        """A chunk of 16-bit mono audio was queued and starts playing at `plays_at`."""
        self._segments.append((self._queued_ms, plays_at))
        self._queued_ms += pcm_bytes / 2 / self._rate * 1000
        self._schedule()

    def finish(self) -> None:
        """All audio is queued; place whatever is left."""
        self._schedule(final=True)

    def cancel(self) -> None:
        for handle in self._handles:
            handle.cancel()

    def _run(self, cue: Cue) -> None:
        self.fired += 1
        self._fire(cue)

    def _schedule(self, *, final: bool = False) -> None:
        if not self._segments:
            return
        for index, ms in self._audio_ms.items():
            if index in self._scheduled or (ms >= self._queued_ms and not final):
                continue
            position = bisect_right(self._segments, (ms, float("inf"))) - 1
            segment_ms, plays_at = self._segments[max(position, 0)]
            at = plays_at + (ms - segment_ms) / 1000 - self._lead_s
            self._handles.append(self._loop.call_at(at, self._run, self._cues[index]))
            self._scheduled.add(index)
