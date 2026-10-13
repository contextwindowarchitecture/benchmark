"""A deterministic language detector over a closed list (domain-5-plan.md, 5.4), for MF's translation attacks.

Each language has a set of common function words; a word counts only for the one language whose set holds it, so the
words several of these languages share ("de", "que", "e") decide nothing. A text's language is the one with the most
such words, when it has at least MIN_WORDS of them and strictly more than any other; otherwise none. S0 checks it
against planted sentences in every language (suites/s0_cases.py).
"""
from __future__ import annotations

import re
import unicodedata

WORDS = {
    "en": "the and is are of to it you that with for this was be not have will your from they".split(),
    "es": "el los las y es por para con una del está pero muy también porque usted gracias sí hay su".split(),
    "fr": "le les il et est pour pas dans ce qui avec sont vous nous très merci aussi mais une des du".split(),
    "de": "der die das und ist nicht mit ich sie es ein eine auf für auch zu den dem sind wir danke".split(),
    "it": "il gli e è non per con una sono che della anche molto grazie questo ma nel alla".split(),
    "pt": "os e é não para com uma do da dos das também muito obrigado você mas em ao pelo".split(),
}
LANGUAGES = tuple(WORDS)
MIN_WORDS = 3

_counts: dict[str, int] = {}
for _words in WORDS.values():
    for _w in set(_words):
        _counts[_w] = _counts.get(_w, 0) + 1
UNIQUE = {code: {w for w in words if _counts[w] == 1} for code, words in WORDS.items()}
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def words(text: str) -> list[str]:
    return _WORD.findall(unicodedata.normalize("NFC", text).casefold())


def scores(text: str) -> dict[str, int]:
    found = words(text)
    return {code: sum(w in unique for w in found) for code, unique in UNIQUE.items()}


def detect(text: str) -> str | None:
    ranked = sorted(scores(text).items(), key=lambda kv: -kv[1])
    (best, top), (_, second) = ranked[0], ranked[1]
    return best if top >= MIN_WORDS and top > second else None


def english(text: str) -> int:
    """How many English function words the text holds: how a decoded reply is told from noise."""
    return scores(text)["en"]
