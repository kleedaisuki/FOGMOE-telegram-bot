"""@brief 进程内饱和度周期采样 / Periodic in-process saturation sampling."""

from __future__ import annotations

import asyncio

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

        while not stop_event.is_set():
            self._record()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._interval)
            except TimeoutError:
                continue

    def _record(self) -> None:
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
        self._telemetry.gauge(
            "fogmoe.telemetry.dropped",
            float(buffer.dropped_total),
            unit="{signal}",
        )


__all__ = ["RuntimeMetricsService"]
