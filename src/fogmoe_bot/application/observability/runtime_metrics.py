"""@brief 进程内饱和度周期采样 / Periodic in-process saturation sampling."""

from __future__ import annotations

import asyncio
import os
import resource
from pathlib import Path

from fogmoe_bot.application.runtime.keyed_mailbox import KeyedMailboxRuntime

from .telemetry import Telemetry, TelemetryRuntime


class RuntimeMetricsService:
    """@brief 采样 mailbox 与 exporter 健康 / Sample mailbox and exporter health."""

    def __init__(
        self,
        *,
        telemetry: Telemetry,
        exporter: TelemetryRuntime,
        execution: KeyedMailboxRuntime,
        interval: float,
    ) -> None:
        """@brief 注入可直接读取的运行时 / Inject directly observable runtimes.

        @param telemetry metric recorder / Metric recorder.
        @param exporter 遥测导出 runtime / Telemetry export runtime.
        @param execution keyed mailbox runtime / Keyed mailbox runtime.
        @param interval 采样秒数 / Sampling interval in seconds.
        """

        if interval <= 0:
            raise ValueError("Runtime metric interval must be positive")
        self._telemetry = telemetry
        self._exporter = exporter
        self._execution = execution
        self._interval = interval

    async def run(self, stop_event: asyncio.Event) -> None:
        """@brief 采样至停止 / Sample until stopped.

        @param stop_event 停止信号 / Stop signal.
        @return None / None.
        """

        loop = asyncio.get_running_loop()
        expected = loop.time()
        while not stop_event.is_set():
            now = loop.time()
            self._record(loop_lag_seconds=max(0.0, now - expected))
            expected = now + self._interval
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._interval)
            except TimeoutError:
                continue

    def _record(self, *, loop_lag_seconds: float = 0.0) -> None:
        """@brief 记录一个一致的进程内快照 / Record one coherent in-process snapshot."""

        mailbox = self._execution.snapshot()
        for metric_name, value in (
            ("fogmoe.runtime.mailboxes", mailbox.mailbox_count),
            ("fogmoe.runtime.pending", mailbox.pending_count),
            ("fogmoe.runtime.active", mailbox.active_count),
            ("fogmoe.runtime.queued", mailbox.queued_count),
            ("fogmoe.runtime.ready_mailboxes", mailbox.ready_mailbox_count),
        ):
            self._telemetry.gauge(metric_name, float(value), unit="{item}")
        buffer = self._telemetry.snapshot()
        self._telemetry.gauge(
            "fogmoe.telemetry.queue.size",
            float(buffer.queued),
            unit="{signal}",
        )
        self._telemetry.gauge(
            "fogmoe.telemetry.queue.capacity",
            float(buffer.capacity),
            unit="{signal}",
        )
        self._telemetry.gauge(
            "fogmoe.telemetry.export.failures",
            float(self._exporter.export_failures),
            unit="{failure}",
        )
        self._telemetry.gauge(
            "fogmoe.telemetry.exported",
            float(self._exporter.exported_signals),
            unit="{signal}",
        )
        for signal_kind, accepted in buffer.accepted_by_signal.items():
            self._telemetry.gauge(
                "fogmoe.telemetry.accepted",
                float(accepted),
                unit="{signal}",
                attributes={"telemetry.signal.type": signal_kind},
            )
        for signal_kind, dropped in buffer.dropped_by_signal.items():
            self._telemetry.gauge(
                "fogmoe.telemetry.dropped",
                float(dropped),
                unit="{signal}",
                attributes={"telemetry.signal.type": signal_kind},
            )
        self._telemetry.gauge(
            "fogmoe.runtime.event_loop.lag",
            loop_lag_seconds,
            unit="s",
        )
        self._telemetry.gauge(
            "process.memory.usage",
            float(_rss_bytes()),
            unit="By",
        )
        self._telemetry.gauge(
            "process.cpu.time",
            _process_cpu_seconds(),
            unit="s",
        )
        self._telemetry.gauge(
            "process.open_file_descriptors",
            float(_open_file_descriptors()),
            unit="{fd}",
        )
        load = _load_average_1m()
        if load is not None:
            self._telemetry.gauge("system.cpu.load_average.1m", load, unit="1")


def _rss_bytes() -> int:
    """@brief 读取当前 RSS 字节数 / Read current resident-set size in bytes.

    @return Linux ``/proc`` 可用时的当前 RSS，否则返回资源上界近似值 /
        Current RSS on Linux ``/proc``, otherwise a resource-limit approximation.
    """

    try:
        pages = Path("/proc/self/statm").read_text(encoding="utf-8").split()[1]
        return int(pages) * os.sysconf("SC_PAGE_SIZE")
    except IndexError, OSError, ValueError:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        return int(usage.ru_maxrss) * 1024


def _process_cpu_seconds() -> float:
    """@brief 读取进程累计 CPU 时间 / Read cumulative process CPU time.

    @return user 与 system CPU 秒数 / Sum of user and system CPU seconds.
    """

    usage = resource.getrusage(resource.RUSAGE_SELF)
    return float(usage.ru_utime + usage.ru_stime)


def _open_file_descriptors() -> int:
    """@brief 读取已打开文件描述符数 / Read open file-descriptor count.

    @return Linux ``/proc`` 不可用时为零 / Zero when Linux ``/proc`` is unavailable.
    """

    try:
        return sum(1 for _ in Path("/proc/self/fd").iterdir())
    except OSError:
        return 0


def _load_average_1m() -> float | None:
    """@brief 读取一分钟系统负载 / Read one-minute system load average.

    @return 支持时的一分钟 load average，否则为 None / One-minute load average when supported.
    """

    try:
        return float(os.getloadavg()[0])
    except OSError:
        return None


__all__ = ["RuntimeMetricsService"]
