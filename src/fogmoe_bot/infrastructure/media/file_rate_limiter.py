"""@brief 跨线程/进程文件滑动窗口限流器 / Cross-thread/process file sliding-window limiter."""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """@brief 一次限流预留结果 / One rate-limit reservation result.

    @param allowed 是否允许 / Whether allowed.
    @param reservation 成功时的时间戳 token / Timestamp token on success.
    @param retry_after_seconds 拒绝时等待秒数 / Retry delay on rejection.
    """

    allowed: bool
    reservation: float | None
    retry_after_seconds: int | None


class FileSlidingWindowLimiter:
    """@brief 无内存全局状态的持久滑动窗口限流器 / Persistent sliding-window limiter without global in-memory state."""

    def __init__(self, root: Path) -> None:
        """@brief 创建限流器 / Create the limiter.

        @param root 状态目录 / State directory.
        """

        self._root = root

    def reserve(
        self,
        key: str,
        *,
        window_seconds: float,
        max_requests: int,
        now: float | None = None,
    ) -> RateLimitDecision:
        """@brief 原子预留窗口槽位 / Atomically reserve one window slot.

        @param key 稳定主体键 / Stable subject key.
        @param window_seconds 滑动窗口 / Sliding window.
        @param max_requests 窗口容量 / Window capacity.
        @param now 可测试 epoch 秒 / Testable epoch seconds.
        @return 限流判定 / Rate-limit decision.
        """

        if window_seconds <= 0 or max_requests <= 0:
            raise ValueError("rate-limit bounds must be positive")
        instant = time.time() if now is None else now
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            timestamps = _read_timestamps(handle)
            cutoff = instant - window_seconds
            active = [value for value in timestamps if value > cutoff]
            if len(active) >= max_requests:
                retry_after = math.ceil(max(1, window_seconds - (instant - active[0])))
                _write_timestamps(handle, active)
                return RateLimitDecision(False, None, retry_after)
            active.append(instant)
            _write_timestamps(handle, active)
            return RateLimitDecision(True, instant, None)

    def release(self, key: str, reservation: float | None) -> None:
        """@brief 失败时释放一次预留 / Release one reservation after failure.

        @param key 稳定主体键 / Stable subject key.
        @param reservation reserve 返回 token / Token returned by reserve.
        @return None / None.
        """

        if reservation is None:
            return
        path = self._path(key)
        try:
            handle = path.open("r+", encoding="utf-8")
        except FileNotFoundError:
            return
        with handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            timestamps = _read_timestamps(handle)
            for index, value in enumerate(timestamps):
                if value == reservation:
                    del timestamps[index]
                    break
            _write_timestamps(handle, timestamps)

    def _path(self, key: str) -> Path:
        """@brief 构造安全状态路径 / Build a safe state path.

        @param key 主体键 / Subject key.
        @return JSON 状态路径 / JSON state path.
        """

        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)[:100] or "anonymous"
        return self._root / f"{safe}.json"


def _read_timestamps(handle: TextIO) -> list[float]:
    """@brief 从已锁 file handle 读取时间戳 / Read timestamps from a locked file handle.

    @param handle 文本 file handle / Text file handle.
    @return 有效浮点时间戳 / Valid float timestamps.
    """

    handle.seek(0)
    raw = handle.read()
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    values: list[float] = []
    for item in payload:
        try:
            values.append(float(item))
        except TypeError, ValueError:
            continue
    return sorted(values)


def _write_timestamps(handle: TextIO, timestamps: list[float]) -> None:
    """@brief 覆写并 fsync 已锁状态 / Overwrite and fsync locked state.

    @param handle 文本 file handle / Text file handle.
    @param timestamps 时间戳 / Timestamps.
    @return None / None.
    """

    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps(timestamps, separators=(",", ":")))
    handle.flush()
    os.fsync(handle.fileno())


__all__ = ["FileSlidingWindowLimiter", "RateLimitDecision"]
