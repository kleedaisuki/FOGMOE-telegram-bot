"""@brief 应用运行时共享的时间与随机端口 / Shared time and randomness ports for application runtimes."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Protocol


class UtcClock(Protocol):
    """@brief 可替换的 aware UTC 时钟 / Replaceable aware-UTC clock."""

    def now(self) -> datetime:
        """@brief 返回当前 aware UTC 时刻 / Return the current aware UTC instant.

        @return 当前 UTC 时间 / Current UTC time.
        """

        ...


class SystemUtcClock:
    """@brief 基于系统墙钟的 UTC 时钟 / UTC clock backed by the system wall clock."""

    def now(self) -> datetime:
        """@brief 读取系统 UTC 时刻 / Read the system UTC instant.

        @return 当前 aware UTC 时间 / Current aware UTC time.
        """

        return datetime.now(timezone.utc)


type Jitter = Callable[[float, float], float]
"""@brief 在请求闭区间内采样浮点值的随机端口 / Random port sampling a float within a requested closed interval."""


__all__ = ["Jitter", "SystemUtcClock", "UtcClock"]
