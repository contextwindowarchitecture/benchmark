"""Normalization for the deterministic graders (domain-2-plan.md, 5.2). S0 checks every rule here against planted
answers before any result is trusted.

- Reasoning a model wraps in <think>…</think> is removed before anything is read; an unclosed <think> removes the
  rest of the reply.
- Text compares by key: Unicode NFKC, case-folded, typographic quotes and dashes made plain, Markdown emphasis and
  code marks removed, whitespace collapsed, punctuation at either end dropped, and a leading article dropped.
- A number is digits with an optional sign and fraction, its thousands grouped by commas, underscores, apostrophes,
  spaces or no-break spaces in groups of exactly three ("4,200", "4 200", "4200.0" are one number). A number glued
  to a letter ("4.2k", "A3") is not read as one. Numbers compare by value, so "4200" and "4200.00" are equal.
- A JSON reply is the content of its first ```json fence (or any fence), else the text from its first "{" to its last
  "}"; it must parse strictly and be an object.
"""
from __future__ import annotations

import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation

_THINK = re.compile(r"<think>.*?(?:</think>|\Z)", re.DOTALL | re.IGNORECASE)
_PLAIN = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-",
                        "\u2212": "-"})
_MARKS = re.compile(r"[*_`~]+")
_EDGES = re.compile(r"^[\W_]+|[\W_]+$")
_ARTICLE = re.compile(r"^(?:the|a|an)\s+")
_GROUP = r"[,_' \u00a0\u202f]"
_NUMBER = re.compile(rf"(?<![\w.])-?(?:\d{{1,3}}(?:{_GROUP}\d{{3}})+|\d+)(?:\.\d+)?(?!\w|\.\d)")
_FENCE = re.compile(r"```[a-zA-Z]*\s*\n?(.*?)```", re.DOTALL)


def strip_reasoning(text: str) -> str:
    return _THINK.sub("", text).strip()


def text_key(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_PLAIN)
    text = _MARKS.sub("", text)
    text = " ".join(text.split()).casefold()
    text = _EDGES.sub("", text)
    return _ARTICLE.sub("", text)


def canonical_number(value: Decimal) -> str:
    """A number's canonical text: no grouping, no trailing fractional zeros, no exponent."""
    text = format(value.normalize(), "f")
    return "0" if text in ("-0", "") else text


def numbers(text: str) -> list[str]:
    """The canonical numbers in a text, in order of appearance, without repeats."""
    seen: list[str] = []
    for match in _NUMBER.finditer(unicodedata.normalize("NFKC", text).translate(_PLAIN)):
        raw = re.sub(_GROUP, "", match.group(0))
        try:
            value = canonical_number(Decimal(raw))
        except InvalidOperation:
            continue
        if value not in seen:
            seen.append(value)
    return seen


def json_object(text: str) -> tuple[dict | None, str | None]:
    """The reply's JSON object, or None and why not."""
    fences = _FENCE.findall(text)
    candidate = fences[0] if fences else text
    start, end = candidate.find("{"), candidate.rfind("}")
    if start < 0 or end < start:
        return None, "no JSON object"
    try:
        value = json.loads(candidate[start:end + 1], parse_constant=_reject_constant)
    except (json.JSONDecodeError, ValueError) as error:
        return None, f"not JSON: {error}"
    if not isinstance(value, dict):
        return None, "not a JSON object"
    return value, None


def _reject_constant(name: str):
    raise ValueError(f"{name} is not JSON")
