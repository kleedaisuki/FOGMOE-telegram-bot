"""@brief 类型化 Dashboard 查询端口与用例 / Typed Dashboard query port and use cases."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Protocol

from fogmoe_dashboard.domain.models import (
    ErrorEvent,
    GenAiStats,
    HealthPoint,
    LogEntry,
    MetricStats,
    Overview,
    PipelineStage,
    ResourceInstance,
    RetrievalSnapshot,
    SlowTurn,
    SpanStats,
    TimeWindow,
    TraceDetail,
    TraceSummary,
    TurnLatencyStats,
)

_TRACE_ID = re.compile(r"[0-9a-f]{32}\Z")
"""@brief 规范 trace identifier / Canonical trace identifier."""


class DashboardRepository(Protocol):
    """@brief Dashboard 所需的只读分析端口 / Read-only analytics port required by the Dashboard."""

    async def overview(self, window: TimeWindow) -> Overview:
        """@brief 查询总体健康 / Query overall health."""

        ...

    async def pipeline(self) -> Sequence[PipelineStage]:
        """@brief 查询 durable pipeline / Query the durable pipeline."""

        ...

    async def health_series(
        self,
        window: TimeWindow,
        *,
        buckets: int,
    ) -> Sequence[HealthPoint]:
        """@brief 查询系统健康趋势 / Query system-health trends."""

        ...

    async def spans(
        self,
        window: TimeWindow,
        *,
        name: str | None,
        limit: int,
    ) -> Sequence[SpanStats]:
        """@brief 查询 span RED 聚合 / Query span RED aggregates."""

        ...

    async def errors(
        self,
        window: TimeWindow,
        *,
        limit: int,
    ) -> Sequence[ErrorEvent]:
        """@brief 查询统一错误流 / Query the unified error stream."""

        ...

    async def logs(
        self,
        window: TimeWindow,
        *,
        minimum_severity: int,
        logger_name: str | None,
        limit: int,
    ) -> Sequence[LogEntry]:
        """@brief 查询结构日志 / Query structured logs."""

        ...

    async def traces(
        self,
        window: TimeWindow,
        *,
        errors_only: bool,
        limit: int,
    ) -> Sequence[TraceSummary]:
        """@brief 查询 trace 摘要 / Query trace summaries."""

        ...

    async def trace(self, trace_id: str) -> TraceDetail:
        """@brief 查询完整 trace / Query one complete trace."""

        ...

    async def metrics(
        self,
        window: TimeWindow,
        *,
        name: str | None,
        limit: int,
    ) -> Sequence[MetricStats]:
        """@brief 查询 metric 摘要 / Query metric summaries."""

        ...

    async def gen_ai(
        self,
        window: TimeWindow,
        *,
        limit: int,
    ) -> Sequence[GenAiStats]:
        """@brief 查询 GenAI 使用统计 / Query GenAI usage statistics."""

        ...

    async def retrieval(self, window: TimeWindow) -> RetrievalSnapshot:
        """@brief 查询 Retrieval 性能与队列健康 / Query Retrieval performance and queue health."""

        ...

    async def latency(
        self,
        window: TimeWindow,
    ) -> Sequence[TurnLatencyStats]:
        """@brief 查询 Turn 延迟分布 / Query Turn latency distributions."""

        ...

    async def slow_turns(
        self,
        window: TimeWindow,
        *,
        limit: int,
    ) -> Sequence[SlowTurn]:
        """@brief 查询慢 Turn / Query slow Turns."""

        ...

    async def resources(
        self,
        window: TimeWindow,
        *,
        limit: int,
    ) -> Sequence[ResourceInstance]:
        """@brief 查询资源实例 / Query resource instances."""

        ...

    async def close(self) -> None:
        """@brief 关闭查询资源 / Close query resources."""

        ...


class Dashboard:
    """@brief 强类型、有界的分析查询入口 / Strongly typed, bounded analytics entry point."""

    def __init__(self, repository: DashboardRepository) -> None:
        """@brief 注入只读 repository / Inject a read-only repository.

        @param repository 分析读取端口 / Analytics read port.
        @return None / None.
        """

        self._repository = repository

    async def overview(self, window: TimeWindow) -> Overview:
        """@brief 返回 RED/USE 总览 / Return the RED/USE overview.

        @param window 分析窗口 / Analytics window.
        @return 总览 / Overview.
        """

        return await self._repository.overview(window)

    async def pipeline(self) -> tuple[PipelineStage, ...]:
        """@brief 返回当前 durable pipeline / Return the current durable pipeline.

        @return pipeline 阶段 / Pipeline stages.
        """

        return tuple(await self._repository.pipeline())

    async def health_series(
        self,
        window: TimeWindow,
        *,
        buckets: int = 120,
    ) -> tuple[HealthPoint, ...]:
        """@brief 返回有界健康时间序列 / Return a bounded health time series.

        @param window 分析窗口 / Analytics window.
        @param buckets 期望的最大聚合桶数 / Desired maximum aggregate-bucket count.
        @return 按时间排序的健康点 / Chronologically ordered health points.
        """

        if isinstance(buckets, bool) or not 12 <= buckets <= 360:
            raise ValueError("health-series buckets must be between 12 and 360")
        return tuple(await self._repository.health_series(window, buckets=buckets))

    async def spans(
        self,
        window: TimeWindow,
        *,
        name: str | None = None,
        limit: int = 50,
    ) -> tuple[SpanStats, ...]:
        """@brief 返回操作 RED 聚合 / Return operation RED aggregates."""

        return tuple(
            await self._repository.spans(
                window,
                name=_optional_filter(name),
                limit=_limit(limit),
            )
        )

    async def errors(
        self,
        window: TimeWindow,
        *,
        limit: int = 100,
    ) -> tuple[ErrorEvent, ...]:
        """@brief 返回统一错误流 / Return the unified error stream."""

        return tuple(await self._repository.errors(window, limit=_limit(limit)))

    async def logs(
        self,
        window: TimeWindow,
        *,
        minimum_severity: int = 9,
        logger_name: str | None = None,
        limit: int = 100,
    ) -> tuple[LogEntry, ...]:
        """@brief 返回过滤后的结构日志 / Return filtered structured logs."""

        if not 1 <= minimum_severity <= 24:
            raise ValueError("minimum_severity must be between 1 and 24")
        return tuple(
            await self._repository.logs(
                window,
                minimum_severity=minimum_severity,
                logger_name=_optional_filter(logger_name),
                limit=_limit(limit),
            )
        )

    async def traces(
        self,
        window: TimeWindow,
        *,
        errors_only: bool = False,
        limit: int = 50,
    ) -> tuple[TraceSummary, ...]:
        """@brief 返回 trace 摘要 / Return trace summaries."""

        return tuple(
            await self._repository.traces(
                window,
                errors_only=errors_only,
                limit=_limit(limit),
            )
        )

    async def trace(self, trace_id: str) -> TraceDetail:
        """@brief 返回一个 trace 的 spans 与 logs / Return one trace's spans and logs."""

        normalized = trace_id.strip().lower()
        if _TRACE_ID.fullmatch(normalized) is None:
            raise ValueError("trace_id must contain exactly 32 lowercase hex digits")
        return await self._repository.trace(normalized)

    async def metrics(
        self,
        window: TimeWindow,
        *,
        name: str | None = None,
        limit: int = 100,
    ) -> tuple[MetricStats, ...]:
        """@brief 返回 metric 摘要 / Return metric summaries."""

        return tuple(
            await self._repository.metrics(
                window,
                name=_optional_filter(name),
                limit=_limit(limit),
            )
        )

    async def gen_ai(
        self,
        window: TimeWindow,
        *,
        limit: int = 50,
    ) -> tuple[GenAiStats, ...]:
        """@brief 返回 provider/model 使用统计 / Return provider/model usage statistics."""

        return tuple(await self._repository.gen_ai(window, limit=_limit(limit)))

    async def retrieval(self, window: TimeWindow) -> RetrievalSnapshot:
        """@brief 返回 Retrieval 性能与队列健康 / Return Retrieval performance and queue health.

        @param window 分析窗口 / Analytics window.
        @return Retrieval 组合快照 / Retrieval snapshot.
        """

        return await self._repository.retrieval(window)

    async def latency(
        self,
        window: TimeWindow,
    ) -> tuple[TurnLatencyStats, ...]:
        """@brief 返回 Turn 延迟分布 / Return Turn latency distributions."""

        return tuple(await self._repository.latency(window))

    async def slow_turns(
        self,
        window: TimeWindow,
        *,
        limit: int = 50,
    ) -> tuple[SlowTurn, ...]:
        """@brief 返回最慢 Turn / Return the slowest Turns."""

        return tuple(await self._repository.slow_turns(window, limit=_limit(limit)))

    async def resources(
        self,
        window: TimeWindow,
        *,
        limit: int = 100,
    ) -> tuple[ResourceInstance, ...]:
        """@brief 返回实例生命周期 / Return instance lifecycles."""

        return tuple(await self._repository.resources(window, limit=_limit(limit)))

    async def close(self) -> None:
        """@brief 关闭 repository / Close the repository.

        @return None / None.
        """

        await self._repository.close()


def _limit(value: int) -> int:
    """@brief 校验查询行数上限 / Validate a query row limit."""

    if isinstance(value, bool) or not 1 <= value <= 1000:
        raise ValueError("Dashboard query limit must be between 1 and 1000")
    return value


def _optional_filter(value: str | None) -> str | None:
    """@brief 规范可选精确过滤器 / Normalize an optional exact filter."""

    if value is None:
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > 255:
        raise ValueError("Dashboard filters must contain 1..255 characters")
    return normalized


__all__ = ["Dashboard", "DashboardRepository"]
