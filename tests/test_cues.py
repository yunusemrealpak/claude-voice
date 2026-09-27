"""Line cues in narration: what is spoken, and when the emphasis moves."""

import asyncio

import pytest

from voice.cues import Cue, CueSchedule, split_cues
from voice.tts import CharTimes


def test_markers_are_removed_and_point_at_the_words_after_them():
    spoken, cues = split_cues("It starts by {{12-14}} validating the request, and {{16}} only then locks.")

    assert spoken == "It starts by validating the request, and only then locks."
    assert [(spoken[c.offset:c.offset + 10], c.start, c.end) for c in cues] == [
        ("validating", 12, 14),
        ("only then ", 16, 16),
    ]


@pytest.mark.parametrize("text, spoken, cue", [
    ("{{3}} First the check.", "First the check.", Cue(0, 3, 3)),
    ("the check {{7}}, then", "the check, then", Cue(9, 7, 7)),
    ("backwards {{9-4}} range", "backwards range", Cue(10, 4, 9)),
    ("spaces {{ 5 - 6 }} inside", "spaces inside", Cue(7, 5, 6)),
])
def test_marker_edge_cases(text, spoken, cue):
    assert split_cues(text) == (spoken, [cue])


def test_text_without_markers_is_left_alone():
    assert split_cues("Plain {{words}} and {braces}.") == ("Plain {{words}} and {braces}.", [])


def test_a_cue_fires_when_the_chunk_holding_its_words_plays():
    async def main():
        loop = asyncio.get_running_loop()
        fired = []
        schedule = CueSchedule([Cue(1, 5, 5), Cue(3, 8, 8)], lambda cue: fired.append((cue.start, loop.time())),
                               rate=1000)
        # Timings arrive before any audio: nothing can be placed yet.
        schedule.on_times(CharTimes(0, [0.0, 100.0, 200.0, 300.0]))
        assert fired == []

        start = loop.time()
        schedule.queued(2 * 200, plays_at=start + 0.05)  # 0-200 ms of audio, playing 50 ms from now
        schedule.queued(2 * 200, plays_at=start + 0.40)  # 200-400 ms, after a synthesis stall
        await asyncio.sleep(0.6)

        assert [line for line, _ in fired] == [5, 8]
        assert fired[0][1] - start == pytest.approx(0.15, abs=0.03)  # 50 ms + 100 ms into the audio
        assert fired[1][1] - start == pytest.approx(0.50, abs=0.03)  # the stall shifts it: 400 ms + 100 ms
        assert schedule.fired == 2

    asyncio.run(main())


def test_cancelled_cues_do_not_fire():
    async def main():
        fired = []
        schedule = CueSchedule([Cue(0, 1, 1)], fired.append, rate=1000)
        schedule.on_times(CharTimes(0, [50.0]))
        schedule.queued(2 * 100, plays_at=asyncio.get_running_loop().time())
        schedule.cancel()
        await asyncio.sleep(0.1)
        assert fired == []

    asyncio.run(main())
