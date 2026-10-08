"""The published tokenizers (conformance/README.md, Tokenizers and renderers)."""
from __future__ import annotations

from .strings import runs


def fixture_whitespace(text: str) -> int:
    return runs(text)


def estimate_utf8(text: str) -> int:
    return (len(text.encode("utf-8")) + 3) // 4


TOKENIZERS = {"fixture-whitespace/v1": fixture_whitespace, "estimate-utf8/v1": estimate_utf8}
