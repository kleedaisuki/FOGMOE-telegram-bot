"""@brief Dashboard presentation 共用时长语法 / Shared duration syntax for Dashboard presentations."""

from __future__ import annotations

import re
from datetime import timedelta

_DURATION = re.compile(r"(?P<amount>[0-9]+(?:\.[0-9]+)?)(?P<unit>[smhd])\Z")
"""@brief 紧凑时长语法 / Compact duration syntax."""


def parse_duration(value: str) -> timedelta:
    """@brief 解析紧凑正时长 / Parse a compact positive duration.

    @param value 例如 15m、1h、7d / Value such as 15m, 1h, or 7d.
    @return timedelta / timedelta.
    """

    match = _DURATION.fullmatch(value.strip().lower())
    if match is None:
        raise ValueError("window must look like 15m, 1h, or 7d")
    amount = float(match.group("amount"))
    factors = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
    duration = timedelta(seconds=amount * factors[match.group("unit")])
    if duration <= timedelta():
        raise ValueError("window must be positive")
    if duration > timedelta(days=90):
        raise ValueError("window cannot exceed 90 days")
    return duration


__all__ = ["parse_duration"]
