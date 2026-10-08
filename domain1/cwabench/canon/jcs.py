"""RFC 8785 (JSON Canonicalization Scheme) for the JSON a snapshot or payload can hold.

Written from the RFC and ECMAScript's Number::toString, not from any assembler, so it can check them.
"""
from __future__ import annotations

import math

from .strings import utf16_key

_SHORT_ESCAPES = {0x08: "\\b", 0x09: "\\t", 0x0A: "\\n", 0x0C: "\\f", 0x0D: "\\r", 0x22: '\\"', 0x5C: "\\\\"}
_MAX_SAFE = 2**53


class CanonicalizationError(ValueError):
    pass


def string(text: str) -> str:
    out = ['"']
    for char in text:
        code = ord(char)
        if code in _SHORT_ESCAPES:
            out.append(_SHORT_ESCAPES[code])
        elif code < 0x20:
            out.append(f"\\u{code:04x}")
        elif 0xD800 <= code <= 0xDFFF:
            raise CanonicalizationError("a lone surrogate has no RFC 8785 serialization")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def number(value: float) -> str:
    """ECMAScript Number::toString of a finite double.

    repr() gives the shortest digits that round-trip, which is what ECMAScript picks too; only the placement of the
    decimal point and the exponent differ, and those are applied here by the ECMAScript rules.
    """
    if not math.isfinite(value):
        raise CanonicalizationError(f"{value} is not a finite number")
    if value == 0:
        return "0"  # also -0
    sign = "-" if value < 0 else ""
    text = repr(abs(value))
    mantissa, _, exp_text = text.partition("e")
    exponent = int(exp_text) if exp_text else 0
    whole, _, fraction = mantissa.partition(".")
    if fraction == "0":
        fraction = ""
    digits = (whole + fraction).lstrip("0")
    # The value is 0.digits × 10^n: n counts the places the point sits after the first significant digit.
    leading_zeros = len(whole + fraction) - len((whole + fraction).lstrip("0"))
    n = len(whole) + exponent - leading_zeros
    digits = digits.rstrip("0")
    k = len(digits)
    if k <= n <= 21:
        out = digits + "0" * (n - k)
    elif 0 < n <= 21:
        out = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        out = "0." + "0" * (-n) + digits
    else:
        mantissa_out = digits[0] + ("." + digits[1:] if k > 1 else "")
        e = n - 1
        out = f"{mantissa_out}e{'+' if e > 0 else '-'}{abs(e)}"
    return sign + out


def serialize(value) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        # RFC 8785 numbers are IEEE 754 doubles; an integer beyond 2^53 rounds to the double JavaScript reads.
        if -_MAX_SAFE <= value <= _MAX_SAFE:
            return str(value)
        try:
            return number(float(value))
        except OverflowError:
            raise CanonicalizationError("an integer outside the double range") from None
    if isinstance(value, float):
        return number(value)
    if isinstance(value, str):
        return string(value)
    if isinstance(value, list):
        return "[" + ",".join(serialize(v) for v in value) + "]"
    if isinstance(value, dict):
        keys = sorted(value, key=utf16_key)
        return "{" + ",".join(string(k) + ":" + serialize(value[k]) for k in keys) + "}"
    raise CanonicalizationError(f"{type(value).__name__} is not JSON")


def serialize_bytes(value) -> bytes:
    return serialize(value).encode("utf-8")
