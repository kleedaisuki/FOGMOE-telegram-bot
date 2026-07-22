"""@brief 结构化 BTC 模式监控服务 / Structured BTC pattern-monitor service."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

logger = logging.getLogger(__name__)

BTC_MONITOR_DATA_KEY = "fogmoe.btc_monitor"
"""@brief 组合根保存监控服务的稳定键 / Stable composition-root key for the monitor service."""


@dataclass(frozen=True, slots=True)
class PatternTrigger:
    """@brief 一次待复查的价格模式触发 / Price-pattern trigger awaiting evaluation.

    @param price 触发价格 / Trigger price.
    @param occurred_at K 线触发时间 / Candle trigger time.
    """

    price: float
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class PatternScan:
    """@brief 一次市场扫描结果 / Result of one market scan.

    @param messages 应立即通知的诊断消息 / Diagnostic messages to notify immediately.
    @param trigger 可选待复查触发 / Optional trigger awaiting evaluation.
    """

    messages: tuple[str, ...] = ()
    trigger: PatternTrigger | None = None


class PatternSource(Protocol):
    """@brief BTC 模式数据源端口 / BTC pattern-source port."""

    async def scan(self) -> PatternScan:
        """@brief 扫描当前模式 / Scan the current pattern.

        @return 扫描结果 / Scan result.
        """

        ...

    async def evaluate(self, trigger: PatternTrigger) -> str:
        """@brief 复查历史触发结果 / Evaluate a historical trigger.

        @param trigger 待复查触发 / Trigger to evaluate.
        @return 用户可见结果 / User-visible result.
        """

        ...


class MonitorNotificationSink(Protocol):
    """@brief 监控通知投递端口 / Monitor-notification delivery port."""

    async def send(self, chat_id: int, message: str) -> None:
        """@brief 投递一条监控消息 / Deliver one monitor message.

        @param chat_id 目标 Telegram chat ID / Target Telegram chat ID.
        @param message 通知文本 / Notification text.
        @return None / None.
        """

        ...


class MonitorControlResult(StrEnum):
    """@brief 启停命令的穷尽结果 / Exhaustive monitor-control result."""

    STARTED = "started"
    ALREADY_RUNNING = "already_running"
    STOPPED = "stopped"
    NOT_RUNNING = "not_running"


@dataclass(frozen=True, slots=True)
class _MonitorSession:
    """@brief 单次启用会话 / One enabled monitoring session.

    @param generation 防止陈旧扫描在重启后投递 / Generation fencing stale scans after restart.
    @param chat_id 当前通知目标 / Current notification target.
    """

    generation: int
    chat_id: int


@dataclass(frozen=True, slots=True)
class _PendingEvaluation:
    """@brief 单次延迟复查 / One delayed trigger evaluation.

    @param generation 所属启用会话 / Owning session generation.
    @param chat_id 目标 chat / Target chat.
    @param due_at 单调时钟截止点 / Monotonic due time.
    @param trigger 待复查触发 / Trigger to evaluate.
    """

    generation: int
    chat_id: int
    due_at: float
    trigger: PatternTrigger


class BtcPatternMonitor:
    """@brief 无全局状态、无 detached task 的 BTC 监控 / BTC monitor without globals or detached tasks."""

    def __init__(
        self,
        *,
        source: PatternSource,
        notifications: MonitorNotificationSink,
        poll_interval: float = 5.0,
        result_delay: float = 600.0,
    ) -> None:
        """@brief 创建监控服务 / Create the monitor service.

        @param source 市场模式数据源 / Market pattern source.
        @param notifications 通知投递端口 / Notification delivery port.
        @param poll_interval 未触发时的扫描间隔秒数 / Scan interval in seconds while idle.
        @param result_delay 触发后的复查延迟秒数 / Result-evaluation delay in seconds.
        @raise ValueError 时间参数非正时抛出 / Raised for non-positive timing parameters.
        """

        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if result_delay <= 0:
            raise ValueError("result_delay must be positive")
        self._source = source
        self._notifications = notifications
        self._poll_interval = poll_interval
        self._result_delay = result_delay
        self._session: _MonitorSession | None = None
        self._generation = 0
        self._changed = asyncio.Event()
        self._owner_loop: asyncio.AbstractEventLoop | None = None

    @property
    def running(self) -> bool:
        """@brief 是否已由管理员启用 / Whether an administrator enabled monitoring.

        @return 启用状态 / Enabled state.
        """

        return self._session is not None

    def start(self, chat_id: int) -> MonitorControlResult:
        """@brief 启用监控 / Enable monitoring.

        @param chat_id 通知目标 / Notification target.
        @return 启动或已运行 / Started or already-running.
        """

        self._ensure_owner_loop_if_running()
        if self._session is not None:
            return MonitorControlResult.ALREADY_RUNNING
        self._generation += 1
        self._session = _MonitorSession(self._generation, chat_id)
        self._changed.set()
        return MonitorControlResult.STARTED

    def stop(self) -> MonitorControlResult:
        """@brief 停止监控并 fencing 旧工作 / Stop monitoring and fence stale work.

        @return 停止或未运行 / Stopped or not-running.
        """

        self._ensure_owner_loop_if_running()
        if self._session is None:
            return MonitorControlResult.NOT_RUNNING
        self._generation += 1
        self._session = None
        self._changed.set()
        return MonitorControlResult.STOPPED

    async def run(self, stop_event: asyncio.Event) -> None:
        """@brief 运行受 BotRuntime 监督的监控循环 / Run the BotRuntime-supervised monitor loop.

        @param stop_event 阶段停止信号 / Phase-specific stop signal.
        @return None / None.
        """

        self._owner_loop = asyncio.get_running_loop()
        next_scan_at = self._owner_loop.time()
        pending: list[_PendingEvaluation] = []
        try:
            while not stop_event.is_set():
                session = self._session
                if session is None:
                    pending.clear()
                    await self._wait_for_change_or_stop(stop_event, delay=None)
                    next_scan_at = self._owner_loop.time()
                    continue

                now = self._owner_loop.time()
                due = tuple(
                    item
                    for item in pending
                    if item.generation == session.generation and item.due_at <= now
                )
                pending = [item for item in pending if item not in due]
                for item in due:
                    try:
                        result = await self._source.evaluate(item.trigger)
                        if self._is_current(item.generation, item.chat_id):
                            await self._notifications.send(item.chat_id, result)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception(
                            "BTC pattern evaluation failed: chat_id=%s",
                            item.chat_id,
                        )

                now = self._owner_loop.time()
                if now >= next_scan_at and self._is_current(
                    session.generation,
                    session.chat_id,
                ):
                    try:
                        scan = await self._source.scan()
                        if self._is_current(session.generation, session.chat_id):
                            for message in scan.messages:
                                await self._notifications.send(session.chat_id, message)
                            if scan.trigger is not None:
                                pending.append(
                                    _PendingEvaluation(
                                        generation=session.generation,
                                        chat_id=session.chat_id,
                                        due_at=(
                                            self._owner_loop.time() + self._result_delay
                                        ),
                                        trigger=scan.trigger,
                                    )
                                )
                                next_scan_at = (
                                    self._owner_loop.time() + self._result_delay
                                )
                            else:
                                next_scan_at = (
                                    self._owner_loop.time() + self._poll_interval
                                )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception(
                            "BTC pattern scan or notification failed: chat_id=%s",
                            session.chat_id,
                        )
                        next_scan_at = self._owner_loop.time() + self._poll_interval

                candidates = [next_scan_at]
                candidates.extend(
                    item.due_at
                    for item in pending
                    if item.generation == session.generation
                )
                delay = max(0.0, min(candidates) - self._owner_loop.time())
                await self._wait_for_change_or_stop(stop_event, delay=delay)
        finally:
            self._owner_loop = None

    def _is_current(self, generation: int, chat_id: int) -> bool:
        """@brief 检查异步结果是否仍属于当前会话 / Check whether an async result belongs to the current session.

        @param generation 工作代次 / Work generation.
        @param chat_id 工作目标 / Work target.
        @return 当前时返回 True / True when current.
        """

        return self._session == _MonitorSession(generation, chat_id)

    async def _wait_for_change_or_stop(
        self,
        stop_event: asyncio.Event,
        *,
        delay: float | None,
    ) -> None:
        """@brief 等待控制变化、停止或定时器 / Wait for control change, stop, or timer.

        @param stop_event 阶段停止信号 / Phase-specific stop signal.
        @param delay 可选最大等待秒数 / Optional maximum delay in seconds.
        @return None / None.
        """

        self._changed.clear()
        change_task = asyncio.create_task(
            self._changed.wait(),
            name="btc-monitor-control-change",
        )
        stop_task = asyncio.create_task(
            stop_event.wait(),
            name="btc-monitor-runtime-stop",
        )
        tasks: list[asyncio.Task[bool] | asyncio.Task[None]] = [
            change_task,
            stop_task,
        ]
        timer_task: asyncio.Task[None] | None = None
        if delay is not None:
            timer_task = asyncio.create_task(
                asyncio.sleep(delay),
                name="btc-monitor-timer",
            )
            tasks.append(timer_task)
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(change_task, return_exceptions=True)
            await asyncio.gather(stop_task, return_exceptions=True)
            if timer_task is not None:
                await asyncio.gather(timer_task, return_exceptions=True)

    def _ensure_owner_loop_if_running(self) -> None:
        """@brief 运行期间拒绝跨 loop 控制 / Reject cross-loop control while running.

        @return None / None.
        @raise RuntimeError 控制调用不在 owner loop 时抛出 / Raised outside the owner loop.
        """

        if (
            self._owner_loop is not None
            and self._owner_loop is not asyncio.get_running_loop()
        ):
            raise RuntimeError("BTC monitor must be controlled on its owner event loop")


__all__ = [
    "BTC_MONITOR_DATA_KEY",
    "BtcPatternMonitor",
    "MonitorControlResult",
    "MonitorNotificationSink",
    "PatternScan",
    "PatternSource",
    "PatternTrigger",
]
