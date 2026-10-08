"""Deterministic fidelity checks on a variant against its parent (domain-1-plan.md, 9.3).

They approximate R-18's "no new instruction or unsupported fact" without a model:

    nonempty      the variant has a non-whitespace character
    urls          every URL in the variant appears in the parent
    dates         every date (ISO, or a month name with day and year) is one of the parent's dates
    ids           every ticket-style id (`HX-2041`, `INV-88213`) appears in the parent
    numbers       every other number is one of the parent's, its dates' parts or its ids' digits
                  (thousands separators ignored)
    entities      every run of two or more capitalized words appears in the parent, after leading function words
                  ("The", "In", …) are dropped
    markers       no imperative or role marker the parent lacks: ignore, disregard, you must, you should, do not,
                  a role prefix (`system:`), an angle-bracket tag
    length_ratio  variant tokens ÷ parent tokens within the configured band

A check that finds nothing to compare passes. Every check is reported, not just the first that fails.
"""
from __future__ import annotations

import re

from ..canon.strings import collapse

CHECKS = ("nonempty", "urls", "dates", "ids", "numbers", "entities", "markers", "length_ratio")

MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december")
_MONTH = "(" + "|".join(m.capitalize() for m in MONTHS) + ")"
_URL = re.compile(r"https?://[^\s<>\"'()\[\]]+")
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_PROSE_MDY = re.compile(rf"\b{_MONTH}\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b")
_PROSE_DMY = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+{_MONTH},?\s+(\d{{4}})\b")
_ID = re.compile(r"\b[A-Z]{2,}-\d+\b")
_NUMBER = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")
_ENTITY = re.compile(r"\b[A-Z][A-Za-z]+(?:[ \t]+[A-Z][A-Za-z]+)+")
_FUNCTION = frozenset("The A An This That These Those In On At For By From To Of And But Or After Before During When "
                      "While Its Their Our Both Each Every Some Since Until As If So Then".split())
MARKERS = (
    ("ignore", re.compile(r"\bignor(e|ed|ing)\b", re.IGNORECASE)),
    ("disregard", re.compile(r"\bdisregard", re.IGNORECASE)),
    ("you must", re.compile(r"\byou\s+must\b", re.IGNORECASE)),
    ("you should", re.compile(r"\byou\s+should\b", re.IGNORECASE)),
    ("do not", re.compile(r"\b(do\s+not|don't)\b", re.IGNORECASE)),
    ("role prefix", re.compile(r"\b(system|assistant|developer|user)\s*:", re.IGNORECASE)),
    ("tag", re.compile(r"</?[A-Za-z][A-Za-z0-9_-]*(\s[^<>]*)?/?>")),
)


def _strip_url(url: str) -> str:
    return url.rstrip(".,;:!?")


def _number(text: str) -> str:
    value = text.replace(",", "")
    if "." in value:
        value = value.rstrip("0").rstrip(".")
    return value.lstrip("0") or "0"


class Facts:
    """What a text states that the checks compare: URLs, dates, ids, numbers and entities."""

    def __init__(self, text: str):
        self.text = text
        rest = text
        self.urls = {_strip_url(u) for u in _URL.findall(rest)}
        rest = _URL.sub(" ", rest)
        dates = set()
        for y, m, d in _ISO.findall(rest):
            dates.add((int(y), int(m), int(d)))
        for month, d, y in _PROSE_MDY.findall(rest):
            dates.add((int(y), MONTHS.index(month.lower()) + 1, int(d)))
        for d, month, y in _PROSE_DMY.findall(rest):
            dates.add((int(y), MONTHS.index(month.lower()) + 1, int(d)))
        self.dates = dates
        for pattern in (_ISO, _PROSE_MDY, _PROSE_DMY):
            rest = pattern.sub(" ", rest)
        self.ids = set(_ID.findall(rest))
        rest = _ID.sub(" ", rest)
        self.numbers = {_number(n) for n in _NUMBER.findall(rest)}
        entities = set()
        for match in _ENTITY.finditer(_ID.sub(" ", _URL.sub(" ", text))):  # "Ticket CO-3612" names no entity
            words = match.group(0).split()
            while words and words[0] in _FUNCTION:
                words.pop(0)
            if len(words) >= 2:
                entities.add(" ".join(words))
        self.entities = entities

    def allowed_numbers(self) -> set[str]:
        """Numbers a faithful variant may state: the parent's own, the parts of its dates, and its ids' digits."""
        out = set(self.numbers)
        for y, m, d in self.dates:
            out |= {str(y), str(m), str(d)}
        for found in self.ids:
            out.add(_number(found.split("-", 1)[1]))
        return out


def _check(id: str, extra: list[str]) -> dict:
    if not extra:
        return {"id": id, "status": "pass"}
    return {"id": id, "status": "fail", "detail": "not in the parent: " + ", ".join(sorted(extra))[:300]}


def check(parent: str, variant: str, parent_tokens: int, variant_tokens: int, band: tuple[float, float]) -> list[dict]:
    """Every fidelity check, in CHECKS order."""
    if not variant.strip():
        empty = [{"id": "nonempty", "status": "fail", "detail": "empty or whitespace only"}]
        return empty + [{"id": c, "status": "fail", "detail": "empty variant"} for c in CHECKS[1:]]
    p, v = Facts(parent), Facts(variant)
    flat = collapse(parent)
    out = [{"id": "nonempty", "status": "pass"},
           _check("urls", [u for u in v.urls if u not in p.urls and u not in parent]),
           _check("dates", [f"{y:04d}-{m:02d}-{d:02d}" for y, m, d in v.dates - p.dates]),
           _check("ids", list(v.ids - p.ids)),
           _check("numbers", list(v.numbers - p.allowed_numbers())),
           _check("entities", [e for e in v.entities if e not in flat])]
    added = [name for name, pattern in MARKERS if pattern.search(variant) and not pattern.search(parent)]
    out.append(_check("markers", added) if not added else
               {"id": "markers", "status": "fail", "detail": "markers the parent lacks: " + ", ".join(added)})
    ratio = variant_tokens / parent_tokens if parent_tokens else 0.0
    low, high = band
    out.append({"id": "length_ratio", "status": "pass" if low <= ratio <= high else "fail",
                **({} if low <= ratio <= high else {"detail": f"{ratio:.3f} outside [{low}, {high}]"})})
    return out


def passed(checks: list[dict]) -> bool:
    return all(c["status"] == "pass" for c in checks)
