"""Wake word: only speech that addresses the assistant by name reaches Claude.

The microphone also hears the user talking to other people. With a wake word,
a turn counts as a command only when it starts with the assistant's name
("Cezeri, run the tests"), optionally after a filler ("hey", "tamam"). The name
is stripped before the text is handed on; everything else is dropped.

Matching is on whole words after case and diacritic folding, so "CEZERİ" and
"Cezerî" match while an inflected mention in conversation ("Cezeri'ye sorarım")
does not.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

# How alike a word must be to the name to be logged as a near miss.
NEAR_MISS_RATIO = 0.6

# Words that may come before the name without making it a mention in passing.
LEADING_FILLERS = frozenset({"hey", "hei", "ey", "hi", "ok", "okay", "tamam", "evet", "peki"})
MAX_LEADING = 2

_WORD = re.compile(r"[\w']+")
_SEPARATORS = " \t,.;:!?-–—…"


def fold(word: str) -> str:
    """Lowercase and strip diacritics: "CEZERİ" and "Cezerî" both become "cezeri"."""
    decomposed = unicodedata.normalize("NFKD", word.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


class WakeWord:
    def __init__(self, words: tuple[str, ...] | list[str]):
        self.words = tuple(words)
        self._folded = frozenset(fold(w) for w in words)
        if not self._folded:
            raise ValueError("a wake word filter needs at least one word")

    def _find(self, text: str) -> re.Match | None:
        """The match of the wake word, if the text opens by addressing the assistant."""
        for position, match in enumerate(_WORD.finditer(text)):
            word = fold(match.group())
            if word in self._folded:
                return match
            if position >= MAX_LEADING or word not in LEADING_FILLERS:
                return None
        return None

    def addressed(self, text: str) -> str | None:
        """The command with the name stripped; "" when only the name was said;
        None when the speech was not addressed to the assistant."""
        match = self._find(text)
        if match is None:
            return None
        return text[match.end():].lstrip(_SEPARATORS).strip()

    def mentions(self, text: str) -> bool:
        """Whether live, still-changing speech opens with the name (for barge-in)."""
        return self._find(text) is not None

    def near_miss(self, text: str) -> str | None:
        """The opening word, folded, when it resembles the name without matching it.

        Lets a misheard wake word ("cezari", "cezeriy") be spotted in the log
        without recording what was said to someone else.
        """
        for position, match in enumerate(_WORD.finditer(text)):
            word = fold(match.group())
            if any(SequenceMatcher(None, word, name).ratio() >= NEAR_MISS_RATIO for name in self._folded):
                return word
            if position >= MAX_LEADING or word not in LEADING_FILLERS:
                return None
        return None
