"""Turn assembly: Deepgram fragments in, one Turn per thing the user said."""

import asyncio

from voice.stt import DeepgramStt, SpeechActivity, Turn, ends_sentence


def results(text: str, *, final: bool = False, speech_final: bool = False) -> dict:
    return {
        "type": "Results",
        "is_final": final,
        "speech_final": speech_final,
        "channel": {"alternatives": [{"transcript": text}]},
    }


def make(grace_ms: int = 50, incomplete_ms: int = 200) -> DeepgramStt:
    return DeepgramStt("key", "tr", turn_grace_ms=grace_ms, turn_grace_incomplete_ms=incomplete_ms)


def drain(stt: DeepgramStt) -> list:
    events = []
    while not stt._out.empty():
        events.append(stt._out.get_nowait())
    return events


def turns(events: list) -> list[str]:
    return [e.text for e in events if isinstance(e, Turn)]


def test_fragments_join_into_one_turn_after_the_grace_period():
    async def scenario():
        stt = make()
        stt.handle(results("Merhaba,", final=True))
        stt.handle(results("bir endpoint ekle.", final=True, speech_final=True))
        assert turns(drain(stt)) == []  # still inside the grace period
        await asyncio.sleep(0.08)
        return turns(drain(stt))

    assert asyncio.run(scenario()) == ["Merhaba, bir endpoint ekle."]


def test_a_pause_mid_sentence_is_a_breath_and_waits_longer():
    async def scenario():
        stt = make()
        stt.handle(results("Ya ben mikrofonu", final=True, speech_final=True))
        stt.handle({"type": "UtteranceEnd"})  # must not cut the sentence short either
        await asyncio.sleep(0.08)
        assert turns(drain(stt)) == []
        stt.handle(results("susturmak istersem ne yapabilirim?", final=True, speech_final=True))
        await asyncio.sleep(0.08)
        return turns(drain(stt))

    assert asyncio.run(scenario()) == ["Ya ben mikrofonu susturmak istersem ne yapabilirim?"]


def test_an_unfinished_sentence_still_closes_after_the_longer_grace():
    async def scenario():
        stt = make()
        stt.handle(results("Ben konuşurken aralarda", final=True, speech_final=True))
        await asyncio.sleep(0.25)
        return turns(drain(stt))

    assert asyncio.run(scenario()) == ["Ben konuşurken aralarda"]


def test_new_words_during_the_grace_period_keep_the_turn_open():
    async def scenario():
        stt = make()
        stt.handle(results("Şunu yap.", final=True, speech_final=True))
        await asyncio.sleep(0.02)
        stt.handle(results("sonra", final=False))  # the user carries on talking
        await asyncio.sleep(0.08)
        assert turns(drain(stt)) == []
        stt.handle(results("sonra testleri çalıştır.", final=True, speech_final=True))
        await asyncio.sleep(0.08)
        return turns(drain(stt))

    assert asyncio.run(scenario()) == ["Şunu yap. sonra testleri çalıştır."]


def test_utterance_end_closes_the_turn_immediately():
    async def scenario():
        stt = make(grace_ms=10_000)
        stt.handle(results("Dur.", final=True))
        stt.handle({"type": "UtteranceEnd"})
        return turns(drain(stt))

    assert asyncio.run(scenario()) == ["Dur."]


def test_interim_words_are_reported_as_activity_but_never_as_a_turn():
    async def scenario():
        stt = make()
        stt.handle(results("bekle", final=False))
        stt.handle(results("", final=False))
        stt.handle({"type": "UtteranceEnd"})
        return drain(stt)

    events = asyncio.run(scenario())
    assert events == [SpeechActivity("bekle")]


def test_stop_flushes_an_unfinished_turn():
    async def scenario():
        stt = make(grace_ms=10_000)
        stt.handle(results("son cümle", final=True))
        await stt.stop()
        return turns(drain(stt))

    assert asyncio.run(scenario()) == ["son cümle"]


def test_keyterms_are_sent_as_repeated_parameters():
    stt = DeepgramStt("key", "tr", keyterms=("endpoint", "Claude Code"))
    assert "keyterm=endpoint" in stt.url
    assert "keyterm=Claude+Code" in stt.url


def test_sentence_ends():
    assert ends_sentence("Tamam.")
    assert ends_sentence("ne yapabilirim?")
    assert ends_sentence('"Bitti."')
    assert not ends_sentence("Ya ben mikrofonu")
    assert not ends_sentence("sürüm 2.")  # a version number, not a full stop
