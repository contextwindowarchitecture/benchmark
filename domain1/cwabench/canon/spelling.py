"""Writing one JSON value in many spellings (RFC 8259): key order, whitespace, string escapes and number forms vary,
the value does not, so its RFC 8785 serialization and the snapshot digest stay the same.

A conformant assembler reads every number as the nearest IEEE 754 double (conformance/README.md, Numbers), so
`9007199254740991.0`, `90071992547409910E-1` and `9007199254740991` are one value, and an integer field written
`1000.0` is still the integer the schema asks for (JSON Schema counts a number with a zero fraction as an integer).
MR4 respells seeds this way, and the fuzzer writes some of its snapshots this way.
"""
from __future__ import annotations

import json
import math
import re
from decimal import Decimal

_SPACE = ("", "", " ", "\n", "\t", "\r\n", "  ")
JSON_NUMBER = re.compile(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?")


def number_spellings(value) -> list[str]:
    if isinstance(value, int):
        out = [str(value), f"{value}.0", f"{value}e0", f"{value}0E-1", f"{value}.000E+0"]
        if abs(value) >= 2**52:  # beyond 2^52 a double has no fraction, so these round to the same one
            out += [f"{value}.4", f"{value - 1}.6" if value > 0 else f"{value + 1}.6"]
        return [t for t in out if JSON_NUMBER.fullmatch(t) and float(t) == float(value)]
    text = repr(value)
    if text in ("inf", "-inf", "nan"):
        return [text]
    sign, digits, exponent = Decimal(text).as_tuple()
    mantissa = "".join(map(str, digits))
    lead = "-" if sign else ""
    out = [text, f"{lead}{mantissa}e{exponent}", f"{lead}{mantissa}00E{exponent - 2}", f"{lead}{mantissa}e+{exponent}"
           if exponent >= 0 else f"{lead}{mantissa}e{exponent}"]
    if value.is_integer() and abs(value) >= 2**52 and abs(value) < 2**63:
        whole = int(value)
        out += [f"{whole}.0", f"{whole}.4"]
    if len(mantissa) > 1:
        out.append(f"{lead}{mantissa[0]}.{mantissa[1:]}e{exponent + len(mantissa) - 1}")
    good = []
    for t in out:
        parsed = float(t)
        if JSON_NUMBER.fullmatch(t) and parsed == value and math.copysign(1, parsed) == math.copysign(1, value):
            good.append(t)
    return good or [text]


def _string_spelling(text: str, rng) -> str:
    out = ['"']
    for ch in text:
        code = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif code < 0x20:
            out.append(f"\\u{code:04x}" if rng.random() < 0.5 else json.dumps(ch)[1:-1])
        elif ch == "/" and rng.random() < 0.5:
            out.append("\\/")
        elif code > 0xFFFF and rng.random() < 0.5:
            high, low = 0xD800 + ((code - 0x10000) >> 10), 0xDC00 + ((code - 0x10000) & 0x3FF)
            out.append(f"\\u{high:04x}\\u{low:04X}")
        elif rng.random() < 0.08:
            out.append(f"\\u{code:04X}" if code <= 0xFFFF else ch)
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def spell(value, rng, numbers: bool, integers: bool) -> str:
    """JSON text for `value` with keys shuffled, whitespace sprinkled, strings escaped differently and, when asked,
    numbers respelled: the same JSON value, so the same digest."""
    ws = lambda: rng.choice(_SPACE)  # noqa: E731
    if isinstance(value, dict):
        keys = list(value)
        rng.shuffle(keys)
        members = [f"{ws()}{_string_spelling(k, rng)}{ws()}:{ws()}{spell(value[k], rng, numbers, integers)}{ws()}"
                   for k in keys]
        return "{" + ",".join(members) + "}" if members else "{" + ws() + "}"
    if isinstance(value, list):
        return "[" + ",".join(f"{ws()}{spell(v, rng, numbers, integers)}{ws()}" for v in value) + "]" if value \
            else "[" + ws() + "]"
    if isinstance(value, str):
        return _string_spelling(value, rng)
    if value is True or value is False or value is None:
        return json.dumps(value)
    if isinstance(value, int) and not integers:
        return str(value)
    if isinstance(value, (int, float)) and (numbers or isinstance(value, int)):
        return rng.choice(number_spellings(value))
    return json.dumps(value)


# Keeping spellings through a reduction -------------------------------------------------------------------------------

class FloatText(float):
    """A number read from JSON text, keeping the text: a reduction re-serializes it as written, so a failure that
    depends on how a number was spelled survives minimization."""

    def __new__(cls, text: str):
        value = super().__new__(cls, text)
        value.text = text
        return value


class IntText(int):
    def __new__(cls, text: str):
        value = super().__new__(cls, int(text))
        value.text = text
        return value


def load_keeping(data: bytes):
    """The JSON value, with every number keeping its literal (FloatText, IntText)."""
    return json.loads(data.decode("utf-8"), parse_float=FloatText, parse_int=IntText)


def dump_keeping(value) -> bytes:
    """Compact JSON in which numbers read by load_keeping are written exactly as they were read."""
    def write(v) -> str:
        if isinstance(v, (FloatText, IntText)):
            return v.text
        if isinstance(v, dict):
            return "{" + ",".join(f"{json.dumps(k, ensure_ascii=False)}:{write(x)}" for k, x in v.items()) + "}"
        if isinstance(v, list):
            return "[" + ",".join(write(x) for x in v) + "]"
        return json.dumps(v, ensure_ascii=False)
    return write(value).encode("utf-8")
