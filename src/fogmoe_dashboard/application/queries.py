"""@brief Dashboard 的封闭查询语言 / Closed query language for the Dashboard."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias, assert_never

from fogmoe_dashboard.application.dashboard import Dashboard
from fogmoe_dashboard.domain.models import (
    ErrorEvent,
    GenAiStats,
    HealthPoint,
    LatencySnapshot,
    LogEntry,
    MetricStats,
    Overview,
    PipelineStage,
    ResourceInstance,
    SpanStats,
    TimeWindow,
    TraceDetail,
    TraceSummary,
)


class DashboardView(StrEnum):
    """@brief 用户可见的分析视图标识 / User-visible analytics-view identity."""

    OVERVIEW = "overview"
    PIPELINE = "pipeline"
    SPANS = "spans"
    ERRORS = "errors"
    LOGS = "logs"
    TRACES = "traces"
    TRACE = "trace"
    METRICS = "metrics"
    AI = "ai"
    LATENCY = "latency"
    RESOURCES = "resources"


@dataclass(frozen=True, slots=True)
class OverviewQuery:
    """@brief 总览查询 / Overview query."""

    window: TimeWindow


@dataclass(frozen=True, slots=True)
class HealthSeriesQuery:
    """@brief 健康趋势查询 / Health-trend query."""

    window: TimeWindow
    buckets: int = 120


@dataclass(frozen=True, slots=True)
class PipelineQuery:
    """@brief Durable pipeline 即时查询 / Instant durable-pipeline query."""


@dataclass(frozen=True, slots=True)
class SpansQuery:
    """@brief Span RED 查询 / Span RED query."""

    window: TimeWindow
    name: str | None = None
    limit: int = 100


@dataclass(frozen=True, slots=True)
class ErrorsQuery:
    """@brief 统一错误流查询 / Unified error-stream query."""

    window: TimeWindow
    limit: int = 200


@dataclass(frozen=True, slots=True)
class LogsQuery:
    """@brief 结构日志查询 / Structured-log query."""

    window: TimeWindow
    minimum_severity: int = 9
    logger_name: str | None = None
    limit: int = 200


@dataclass(frozen=True, slots=True)
class TracesQuery:
    """@brief Trace 摘要查询 / Trace-summary query."""

    window: TimeWindow
    errors_only: bool = False
    limit: int = 100


@dataclass(frozen=True, slots=True)
class TraceQuery:
    """@brief Trace drill-down 查询 / Trace drill-down query."""

    trace_id: str


@dataclass(frozen=True, slots=True)
class MetricsQuery:
    """@brief Metric 摘要查询 / Metric-summary query."""

    window: TimeWindow
    name: str | None = None
    limit: int = 200


@dataclass(frozen=True, slots=True)
class GenAiQuery:
    """@brief GenAI 使用查询 / GenAI-usage query."""

    window: TimeWindow
    limit: int = 100


@dataclass(frozen=True, slots=True)
class LatencyQuery:
    """@brief Turn 延迟组合快照查询 / Combined Turn-latency snapshot query."""

    window: TimeWindow
    slow_turn_limit: int = 100


@dataclass(frozen=True, slots=True)
class ResourcesQuery:
    """@brief 资源实例查询 / Resource-instance query."""

    window: TimeWindow
    limit: int = 200


DashboardQuery: TypeAlias = (
    OverviewQuery
    | HealthSeriesQuery
    | PipelineQuery
    | SpansQuery
    | ErrorsQuery
    | LogsQuery
    | TracesQuery
    | TraceQuery
    | MetricsQuery
    | GenAiQuery
    | LatencyQuery
    | ResourcesQuery
)
"""@brief 所有合法 Dashboard 查询 / Every valid Dashboard query."""

DashboardResult: TypeAlias = (
    Overview
    | tuple[HealthPoint, ...]
    | tuple[PipelineStage, ...]
    | tuple[SpanStats, ...]
    | tuple[ErrorEvent, ...]
    | tuple[LogEntry, ...]
    | tuple[TraceSummary, ...]
    | TraceDetail
    | tuple[MetricStats, ...]
    | tuple[GenAiStats, ...]
    | LatencySnapshot
    | tuple[ResourceInstance, ...]
)
"""@brief 所有合法 Dashboard 查询结果 / Every valid Dashboard query result."""


async def execute_query(
    dashboard: Dashboard,
    query: DashboardQuery,
) -> DashboardResult:
    """@brief 执行封闭查询并保留结果类型 / Execute a closed query while preserving result semantics.

    @param dashboard 有界分析用例 / Bounded analytics use cases.
    @param query 强类型查询值 / Strongly typed query value.
    @return 对应查询结果 / Result corresponding to the query.
    """

    match query:
        case OverviewQuery(window=window):
            return await dashboard.overview(window)
        case HealthSeriesQuery(window=window, buckets=buckets):
            return await dashboard.health_series(window, buckets=buckets)
        case PipelineQuery():
            return await dashboard.pipeline()
        case SpansQuery(window=window, name=name, limit=limit):
            return await dashboard.spans(window, name=name, limit=limit)
        case ErrorsQuery(window=window, limit=limit):
            return await dashboard.errors(window, limit=limit)
        case LogsQuery(
            window=window,
            minimum_severity=minimum_severity,
            logger_name=logger_name,
            limit=limit,
        ):
            return await dashboard.logs(
                window,
                minimum_severity=minimum_severity,
                logger_name=logger_name,
                limit=limit,
            )
        case TracesQuery(window=window, errors_only=errors_only, limit=limit):
            return await dashboard.traces(
                window,
                errors_only=errors_only,
                limit=limit,
            )
        case TraceQuery(trace_id=trace_id):
            return await dashboard.trace(trace_id)
        case MetricsQuery(window=window, name=name, limit=limit):
            return await dashboard.metrics(window, name=name, limit=limit)
        case GenAiQuery(window=window, limit=limit):
            return await dashboard.gen_ai(window, limit=limit)
        case LatencyQuery(window=window, slow_turn_limit=limit):
            summary, slow_turns = await asyncio.gather(
                dashboard.latency(window),
                dashboard.slow_turns(window, limit=limit),
            )
            return LatencySnapshot(summary=summary, slow_turns=slow_turns)
        case ResourcesQuery(window=window, limit=limit):
            return await dashboard.resources(window, limit=limit)
        case _ as unreachable:
            assert_never(unreachable)


def query_view(query: DashboardQuery) -> DashboardView:
    """@brief 返回查询所属的公开视图 / Return the public view owning a query.

    @param query 查询值 / Query value.
    @return 稳定视图标识 / Stable view identity.
    """

    match query:
        case OverviewQuery() | HealthSeriesQuery():
            return DashboardView.OVERVIEW
        case PipelineQuery():
            return DashboardView.PIPELINE
        case SpansQuery():
            return DashboardView.SPANS
        case ErrorsQuery():
            return DashboardView.ERRORS
        case LogsQuery():
            return DashboardView.LOGS
        case TracesQuery():
            return DashboardView.TRACES
        case TraceQuery():
            return DashboardView.TRACE
        case MetricsQuery():
            return DashboardView.METRICS
        case GenAiQuery():
            return DashboardView.AI
        case LatencyQuery():
            return DashboardView.LATENCY
        case ResourcesQuery():
            return DashboardView.RESOURCES
        case _ as unreachable:
            assert_never(unreachable)


__all__ = [
    "DashboardQuery",
    "DashboardResult",
    "DashboardView",
    "ErrorsQuery",
    "GenAiQuery",
    "HealthSeriesQuery",
    "LatencyQuery",
    "LogsQuery",
    "MetricsQuery",
    "OverviewQuery",
    "PipelineQuery",
    "ResourcesQuery",
    "SpansQuery",
    "TraceQuery",
    "TracesQuery",
    "execute_query",
    "query_view",
]
