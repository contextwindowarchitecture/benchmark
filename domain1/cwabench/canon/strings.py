"""Strings as the spec treats them (conformance/README.md, Blank strings and Ordering)."""
from __future__ import annotations

import re

# ECMAScript whitespace and line terminators: what JavaScript's \s matches, and the set fixture-whitespace/v1 splits
# on. Python's \s differs at U+001C–U+001F and U+FEFF, so it is never used here.
WHITESPACE = (
    "\u0009\u000a\u000b\u000c\u000d   "
    "           "
    "    　﻿"
)
_WS_CLASS = "[" + "".join(f"\\u{ord(c):04x}" for c in WHITESPACE) + "]"
_WS_RUN = re.compile(_WS_CLASS + "+")
_NON_WS_RUN = re.compile("[^" + _WS_CLASS[1:-1] + "]+")


def is_blank(text: str) -> bool:
    return all(c in WHITESPACE for c in text)


def usable_id(value) -> bool:
    """R-2's non-blank string id."""
    return isinstance(value, str) and not is_blank(value)


def utf16_key(text: str) -> bytes:
    """Order by UTF-16 code units, the order RFC 8785 uses for member names and the spec uses for every id."""
    return text.encode("utf-16-be", "surrogatepass")


def collapse(text: str) -> str:
    """The deduplication key (R-24): each run of whitespace becomes one U+0020, then the ends are trimmed of U+0020.
    No Unicode normalization and no case folding."""
    return _WS_RUN.sub(" ", text).strip(" ")


def runs(text: str) -> int:
    """Maximal runs of characters outside the whitespace set."""
    return sum(1 for _ in _NON_WS_RUN.finditer(text))
