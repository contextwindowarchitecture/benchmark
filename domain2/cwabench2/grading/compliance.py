"""Instruction compliance (domain-2-plan.md, 6.4): whether a reply follows an IP conversation's rule, by code.

Each check reads the reply with its reasoning removed:

- `brackets`: some figure (digits) inside square brackets;
- `signoff`: the reply ends with "Kestrel desk", ignoring case, closing punctuation, quotes and Markdown marks;
- `uppercase`: the reply has letters and none of them is lower case.

A reply with nothing in it complies with no rule.
"""
from __future__ import annotations

import re

from .normalize import strip_reasoning

_BRACKETED = re.compile(r"\[[^\[\]]*\d[^\[\]]*\]")
_TAIL = re.compile(r"[\s.!*_`\"'”’]+$")


def complies(rule: str, text: str) -> bool:
    reply = strip_reasoning(text)
    if not reply:
        return False
    if rule == "brackets":
        return bool(_BRACKETED.search(reply))
    if rule == "signoff":
        return _TAIL.sub("", reply).casefold().endswith("kestrel desk")
    if rule == "uppercase":
        letters = [c for c in reply if c.isalpha()]
        return bool(letters) and not any(c.islower() for c in letters)
    raise ValueError(f"no rule {rule!r}")
