"""Which spoken turns count as addressed to the assistant."""

import pytest

from voice.wake import WakeWord

wake = WakeWord(["Cezeri"])


@pytest.mark.parametrize("said, command", [
    ("Cezeri, testleri çalıştır.", "testleri çalıştır."),
    ("Cezeri bir saniye bekle", "bir saniye bekle"),
    ("Hey Cezeri, bu fonksiyonu açıklar mısın?", "bu fonksiyonu açıklar mısın?"),
    ("Tamam Cezeri devam et.", "devam et."),
    ("CEZERİ, dur!", "dur!"),
    ("Cezerî: commit at", "commit at"),
    ("Cezeri.", ""),
    ("Cezeri", ""),
])
def test_speech_that_opens_with_the_name_is_a_command(said, command):
    assert wake.addressed(said) == command


@pytest.mark.parametrize("said", [
    "Yarın toplantı var, unutma.",
    "Cezeri'ye bir şey soracağım.",          # a mention, not an address
    "Ahmet dedi ki Cezeri çok iyi çalışıyor",  # the name, but not at the start
    "Hey hey hey Cezeri",                      # too much before the name
    "",
])
def test_everything_else_is_not_addressed(said):
    assert wake.addressed(said) is None


def test_live_speech_is_checked_the_same_way_for_barge_in():
    assert wake.mentions("Cezeri dur")
    assert wake.mentions("hey Cezeri")
    assert not wake.mentions("bu arada toplantı")


def test_several_names_can_be_accepted():
    assert WakeWord(["Cezeri", "Jarvis"]).addressed("Jarvis, dur") == "dur"


def test_a_misheard_name_is_reported_as_a_near_miss_and_nothing_else_is():
    assert wake.near_miss("Cezari, testleri çalıştır") == "cezari"
    assert wake.near_miss("hey Cezer dur") == "cezer"
    assert wake.near_miss("Yarın toplantı var") is None
