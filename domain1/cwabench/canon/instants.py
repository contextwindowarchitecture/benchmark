"""RFC 3339 timestamps as exact instants (conformance/README.md, Timestamps): compared at full precision, a fraction
of any length, never truncated (R-2)."""
from __future__ import annotations

import re
from fractions import Fraction

_PATTERN = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(?:([Zz])|([+-])(\d{2}):(\d{2}))\Z"
)


class InstantError(ValueError):
    pass


def _days_from_civil(year: int, month: int, day: int) -> int:
    """Days since 1970-01-01 in the proleptic Gregorian calendar (works for year 0000, which datetime cannot hold)."""
    year -= month <= 2
    era = year // 400
    yoe = year - era * 400
    doy = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def _days_in_month(year: int, month: int) -> int:
    if month == 2:
        return 29 if (year % 4 == 0 and year % 100 != 0) or year % 400 == 0 else 28
    return 30 if month in (4, 6, 9, 11) else 31


def parse(text: str) -> Fraction:
    """Seconds since the Unix epoch, exactly."""
    match = _PATTERN.match(text) if isinstance(text, str) else None
    if not match:
        raise InstantError(f"not an RFC 3339 date-time: {text!r}")
    year, month, day, hour, minute, second = (int(g) for g in match.groups()[:6])
    if not 1 <= month <= 12 or not 1 <= day <= _days_in_month(year, month):
        raise InstantError(f"no such date: {text!r}")
    if hour > 23 or minute > 59 or second > 59:
        raise InstantError(f"no such time: {text!r}")
    fraction = Fraction(int(match.group(7)), 10 ** len(match.group(7))) if match.group(7) else Fraction(0)
    offset = 0
    if match.group(9):
        oh, om = int(match.group(10)), int(match.group(11))
        if oh > 23 or om > 59:
            raise InstantError(f"no such offset: {text!r}")
        offset = (oh * 60 + om) * 60 * (1 if match.group(9) == "+" else -1)
    seconds = _days_from_civil(year, month, day) * 86400 + hour * 3600 + minute * 60 + second
    return Fraction(seconds) + fraction - offset


def try_parse(text) -> Fraction | None:
    try:
        return parse(text)
    except InstantError:
        return None
