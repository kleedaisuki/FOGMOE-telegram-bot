"""@brief BTC 模式监控结构化生命周期测试 / BTC pattern-monitor structured-lifecycle tests."""

import asyncio
from datetime import datetime, timezone

import pytest

from fogmoe_bot.application.crypto.market_monitor import (
    BtcPatternMonitor,
    MonitorControlResult,
    PatternScan,
    PatternTrigger,
)


class _Source:
    """@brief 可控模式源 / Controllable pattern source."""

    def __init__(self, scan: PatternScan) -> None:
        """@brief 保存扫描结果 / Store the scan result.

        @param scan 固定扫描结果 / Fixed scan result.
        """

        self.result = scan
        self.scans = 0
        self.evaluations = 0
        self.scan_failures = 0
        self.evaluation_failures = 0
        self.block_scan: asyncio.Event | None = None

    async def scan(self) -> PatternScan:
        """@brief 返回固定扫描 / Return the fixed scan.

        @return 扫描结果 / Scan result.
        """

        self.scans += 1
        if self.block_scan is not None:
            await self.block_scan.wait()
        if self.scan_failures:
            self.scan_failures -= 1
            raise RuntimeError("temporary scan failure")
        return self.result

    async def evaluate(self, trigger: PatternTrigger) -> str:
        """@brief 返回固定复查文本 / Return fixed evaluation text.

        @param trigger 待复查触发 / Trigger to evaluate.
        @return 结果文本 / Result text.
        """

        del trigger
        self.evaluations += 1
        if self.evaluation_failures:
            self.evaluation_failures -= 1
            raise RuntimeError("temporary evaluation failure")
        return "result"


class _Notifications:
    """@brief 记录通知的 sink / Notification-recording sink."""

    def __init__(self) -> None:
        """@brief 初始化记录 / Initialize records."""

        self.messages: list[tuple[int, str]] = []
        self.failures = 0

    async def send(self, chat_id: int, message: str) -> None:
        """@brief 记录通知 / Record a notification.

        @param chat_id 目标 chat / Target chat.
        @param message 文本 / Text.
        @return None / None.
        """

        if self.failures:
            self.failures -= 1
            raise RuntimeError("temporary notification failure")
        self.messages.append((chat_id, message))


def test_monitor_runs_under_one_owned_task_and_evaluates_trigger() -> None:
    """@brief 触发与复查由同一结构化 service 拥有 / One structured service owns scanning and evaluation."""

    async def scenario() -> None:
        """@brief 驱动触发、复查与 shutdown / Drive trigger, evaluation, and shutdown.

        @return None / None.
        """

        trigger = PatternTrigger(100.0, datetime(2026, 1, 1, tzinfo=timezone.utc))
        source = _Source(PatternScan(("triggered",), trigger))
        notifications = _Notifications()
        monitor = BtcPatternMonitor(
            source=source,
            notifications=notifications,
            poll_interval=0.01,
            result_delay=0.01,
        )
        stop = asyncio.Event()
        task = asyncio.create_task(monitor.run(stop))

        assert monitor.start(42) is MonitorControlResult.STARTED
        assert monitor.start(42) is MonitorControlResult.ALREADY_RUNNING
        while source.evaluations == 0:
            await asyncio.sleep(0.001)
        assert notifications.messages[:2] == [(42, "triggered"), (42, "result")]

        stop.set()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(scenario())


def test_stop_fences_scan_result_and_restart_uses_new_chat() -> None:
    """@brief stop fencing 阻止陈旧扫描跨会话投递 / Stop fencing prevents stale cross-session delivery."""

    async def scenario() -> None:
        """@brief 在扫描中停止并重启 / Stop and restart while a scan is in flight.

        @return None / None.
        """

        source = _Source(PatternScan(("stale",), None))
        source.block_scan = asyncio.Event()
        notifications = _Notifications()
        monitor = BtcPatternMonitor(
            source=source,
            notifications=notifications,
            poll_interval=0.01,
            result_delay=0.01,
        )
        runtime_stop = asyncio.Event()
        task = asyncio.create_task(monitor.run(runtime_stop))
        assert monitor.start(1) is MonitorControlResult.STARTED
        while source.scans == 0:
            await asyncio.sleep(0)

        assert monitor.stop() is MonitorControlResult.STOPPED
        assert monitor.stop() is MonitorControlResult.NOT_RUNNING
        assert monitor.start(2) is MonitorControlResult.STARTED
        source.block_scan.set()
        while source.scans < 2:
            await asyncio.sleep(0)
        while not notifications.messages:
            await asyncio.sleep(0)

        assert notifications.messages == [(2, "stale")]
        runtime_stop.set()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(scenario())


def test_external_cancellation_reaps_monitor_wait_tasks() -> None:
    """@brief 强制取消 monitor 会回收内部等待 task / Forced monitor cancellation reaps internal wait tasks."""

    async def scenario() -> None:
        """@brief 取消等待下一轮扫描的 monitor / Cancel a monitor waiting for its next scan.

        @return None / None.
        """

        source = _Source(PatternScan((), None))
        monitor = BtcPatternMonitor(
            source=source,
            notifications=_Notifications(),
            poll_interval=60,
            result_delay=60,
        )
        task = asyncio.create_task(monitor.run(asyncio.Event()))
        assert monitor.start(42) is MonitorControlResult.STARTED
        while source.scans == 0:
            await asyncio.sleep(0)
        while not any(
            pending.get_name() == "btc-monitor-control-change"
            for pending in asyncio.all_tasks()
        ):
            await asyncio.sleep(0)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.1)
        names = {pending.get_name() for pending in asyncio.all_tasks()}
        assert "btc-monitor-control-change" not in names
        assert "btc-monitor-runtime-stop" not in names
        assert "btc-monitor-timer" not in names

    asyncio.run(scenario())


def test_scan_and_notification_failures_do_not_kill_monitor() -> None:
    """@brief 单轮源与通知异常按轮询间隔恢复 / Per-pass source and notification failures recover on the polling interval."""

    async def scenario() -> None:
        source = _Source(PatternScan(("recovered",), None))
        source.scan_failures = 1
        notifications = _Notifications()
        notifications.failures = 1
        monitor = BtcPatternMonitor(
            source=source,
            notifications=notifications,
            poll_interval=0.001,
            result_delay=0.001,
        )
        stop = asyncio.Event()
        task = asyncio.create_task(monitor.run(stop))
        assert monitor.start(42) is MonitorControlResult.STARTED
        for _ in range(200):
            if notifications.messages:
                break
            await asyncio.sleep(0.001)
        assert not task.done()
        assert source.scans >= 3
        assert notifications.messages == [(42, "recovered")]
        stop.set()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(scenario())


def test_evaluation_failure_does_not_kill_monitor() -> None:
    """@brief 单个延迟复查失败不会终止下一轮工作 / One delayed evaluation failure does not terminate later work."""

    async def scenario() -> None:
        trigger = PatternTrigger(100.0, datetime(2026, 1, 1, tzinfo=timezone.utc))
        source = _Source(PatternScan((), trigger))
        source.evaluation_failures = 1
        notifications = _Notifications()
        monitor = BtcPatternMonitor(
            source=source,
            notifications=notifications,
            poll_interval=0.001,
            result_delay=0.001,
        )
        stop = asyncio.Event()
        task = asyncio.create_task(monitor.run(stop))
        assert monitor.start(42) is MonitorControlResult.STARTED
        for _ in range(200):
            if notifications.messages:
                break
            await asyncio.sleep(0.001)
        assert not task.done()
        assert source.evaluations >= 2
        assert notifications.messages == [(42, "result")]
        stop.set()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(scenario())
