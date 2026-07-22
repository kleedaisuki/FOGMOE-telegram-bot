"""@brief 不可变分析视图模型 / Immutable analytics view models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TypeAlias
from uuid import UUID


JsonScalar: TypeAlias = str | bool | int | float | None
JsonObject: TypeAlias = dict[str, "JsonValue"]
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | JsonObject

RESOURCE_STALE_AFTER = timedelta(seconds=90)
"""@brief 资源心跳失联阈值 / Resource-heartbeat staleness threshold."""


@dataclass(frozen=True, slots=True)
class TimeWindow:
    """@brief 有界 UTC 分析窗口 / Bounded UTC analytics window.

    @param start 包含的开始时间 / Inclusive start timestamp.
    @param end 不包含的结束时间 / Exclusive end timestamp.
    """

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        """@brief 校验并规范 UTC / Validate and normalize to UTC.

        @return None / None.
        """

        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("Dashboard time windows must be timezone-aware")
        start = self.start.astimezone(UTC)
        end = self.end.astimezone(UTC)
        duration = end - start
        if duration <= timedelta():
            raise ValueError("Dashboard time window must be positive")
        if duration > timedelta(days=90):
            raise ValueError("Dashboard time window cannot exceed 90 days")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    @classmethod
    def last(
        cls,
        duration: timedelta,
        *,
        now: datetime | None = None,
    ) -> TimeWindow:
        """@brief 构造截至当前的窗口 / Build a window ending now.

        @param duration 回看时长 / Lookback duration.
        @param now 可替换当前时间 / Replaceable current timestamp.
        @return 规范化窗口 / Normalized window.
        """

        end = (now or datetime.now(UTC)).astimezone(UTC)
        return cls(end - duration, end)


@dataclass(frozen=True, slots=True)
class PipelineStage:
    """@brief Durable pipeline 阶段健康 / Durable-pipeline stage health."""

    stage: str
    pending: int
    processing: int
    retrying: int
    failed_final: int
    oldest_ready_at: datetime | None
    expired_leases: int


@dataclass(frozen=True, slots=True)
class Overview:
    """@brief RED 与核心业务总览 / RED and core-business overview."""

    generated_at: datetime
    window: TimeWindow
    spans: int
    error_spans: int
    traces: int
    logs: int
    error_logs: int
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    input_tokens: int
    output_tokens: int
    tool_calls: int
    pipeline: tuple[PipelineStage, ...]

    @property
    def span_error_rate(self) -> float:
        """@brief 计算 span 错误率 / Calculate the span error rate.

        @return 0..1 错误率 / Error rate in the range 0..1.
        """

        return self.error_spans / self.spans if self.spans else 0.0


@dataclass(frozen=True, slots=True)
class HealthPoint:
    """@brief 系统健康时间序列点 / System-health time-series point.

    @param observed_at 聚合桶开始时间 / Aggregate-bucket start timestamp.
    @param span_rate_per_second 每秒 span 数 / Spans per second.
    @param span_error_rate 0..1 span 错误率 / Span error rate in the range 0..1.
    @param p95_ms span 的 p95 延迟 / Span p95 latency.
    @param error_logs 错误日志数 / Error-log count.
    @param input_tokens 输入 token 数 / Input-token count.
    @param output_tokens 输出 token 数 / Output-token count.
    """

    observed_at: datetime
    span_rate_per_second: float
    span_error_rate: float
    p95_ms: float | None
    error_logs: int
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class SpanStats:
    """@brief 按操作聚合的 RED 统计 / RED statistics grouped by operation."""

    name: str
    kind: str
    calls: int
    rate_per_second: float
    errors: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    average_ms: float
    maximum_ms: float

    @property
    def error_rate(self) -> float:
        """@brief 计算错误率 / Calculate the error rate.

        @return 0..1 错误率 / Error rate in the range 0..1.
        """

        return self.errors / self.calls if self.calls else 0.0


@dataclass(frozen=True, slots=True)
class RetrievalQueueStats:
    """@brief 一个 Embedding Space 的实时队列健康 / Live queue health for one embedding space.

    @param space_id Embedding Space identity / Embedding-space identity.
    @param model Provider model identity / Provider-model identity.
    @param dimensions 向量维度 / Vector dimensions.
    @param pending 待处理任务 / Pending jobs.
    @param processing 已领取任务 / Processing jobs.
    @param retrying 等待重试任务 / Retry-wait jobs.
    @param completed 已完成向量 / Completed vectors.
    @param failed_final 最终失败任务 / Finally failed jobs.
    @param oldest_ready_at 最早就绪时间 / Oldest ready timestamp.
    @param oldest_ready_age_seconds 最老积压秒数 / Oldest backlog age in seconds.
    @param expired_leases 已过期租约 / Expired leases.
    """

    space_id: str
    model: str
    dimensions: int
    pending: int
    processing: int
    retrying: int
    completed: int
    failed_final: int
    oldest_ready_at: datetime | None
    oldest_ready_age_seconds: float | None
    expired_leases: int


@dataclass(frozen=True, slots=True)
class RetrievalSnapshot:
    """@brief Retrieval 性能与饱和度组合快照 / Combined Retrieval performance and saturation snapshot.

    @param operations 时间窗内 RED 操作统计 / Operation RED statistics in the window.
    @param queues 当前 Embedding Space 队列 / Current embedding-space queues.
    @param metrics Retrieval 指标摘要 / Retrieval metric summaries.
    """

    operations: tuple[SpanStats, ...]
    queues: tuple[RetrievalQueueStats, ...]
    metrics: tuple[MetricStats, ...]

    @property
    def ready(self) -> int:
        """@brief 返回所有空间待处理与重试总数 / Return total pending and retry-wait jobs."""

        return sum(queue.pending + queue.retrying for queue in self.queues)

    @property
    def failed(self) -> int:
        """@brief 返回最终失败总数 / Return total finally failed jobs."""

        return sum(queue.failed_final for queue in self.queues)

    @property
    def expired_leases(self) -> int:
        """@brief 返回过期租约总数 / Return total expired leases."""

        return sum(queue.expired_leases for queue in self.queues)


@dataclass(frozen=True, slots=True)
class ErrorEvent:
    """@brief 跨 span 与 log 的统一错误事件 / Unified error event across spans and logs."""

    occurred_at: datetime
    source: str
    name: str
    message: str
    trace_id: str | None
    turn_id: UUID | None


@dataclass(frozen=True, slots=True)
class LogEntry:
    """@brief 可关联的结构日志行 / Correlatable structured-log row."""

    occurred_at: datetime
    severity_number: int
    severity_text: str
    logger_name: str
    event_name: str | None
    body: str
    trace_id: str | None
    span_id: str | None
    turn_id: UUID | None


@dataclass(frozen=True, slots=True)
class TraceSummary:
    """@brief Trace 列表摘要 / Trace-list summary."""

    trace_id: str
    started_at: datetime
    ended_at: datetime
    duration_ms: float
    span_count: int
    error_count: int
    root_operations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TraceSpan:
    """@brief Trace 中的单个 span / A single span in a trace."""

    span_id: str
    parent_span_id: str | None
    name: str
    kind: str
    status: str
    started_at: datetime
    ended_at: datetime
    duration_ms: float
    status_message: str | None
    attributes: JsonObject


@dataclass(frozen=True, slots=True)
class TraceLog:
    """@brief Trace 关联日志 / Trace-correlated log."""

    occurred_at: datetime
    span_id: str | None
    severity_text: str
    logger_name: str
    event_name: str | None
    body: str


@dataclass(frozen=True, slots=True)
class TraceDetail:
    """@brief 完整 trace drill-down / Complete trace drill-down."""

    trace_id: str
    spans: tuple[TraceSpan, ...]
    logs: tuple[TraceLog, ...]


@dataclass(frozen=True, slots=True)
class MetricStats:
    """@brief Metric 时间窗摘要 / Metric window summary."""

    name: str
    kind: str
    unit: str
    attributes: JsonObject
    points: int
    latest_at: datetime
    latest: float
    minimum: float
    maximum: float
    average: float
    total: float | None
    rate_per_second: float | None


@dataclass(frozen=True, slots=True)
class GenAiStats:
    """@brief Provider/model 推理统计 / Provider/model inference statistics."""

    provider: str
    model: str
    calls: int
    errors: int
    input_tokens: int
    output_tokens: int
    p50_ms: float
    p95_ms: float


@dataclass(frozen=True, slots=True)
class TurnLatencyStats:
    """@brief Turn 状态与延迟分布 / Turn-state and latency distribution."""

    state: str
    turns: int
    p50_end_to_end_ms: float | None
    p95_end_to_end_ms: float | None
    p95_inference_ms: float | None
    p95_delivery_ms: float | None
    average_inference_attempts: float
    average_delivery_attempts: float


@dataclass(frozen=True, slots=True)
class SlowTurn:
    """@brief 慢 Turn drill-down / Slow-Turn drill-down."""

    turn_id: UUID
    update_id: int | None
    state: str
    received_at: datetime | None
    end_to_end_ms: float | None
    inference_ms: float | None
    delivery_ms: float | None
    inference_attempts: int
    delivery_attempts: int


@dataclass(frozen=True, slots=True)
class LatencySnapshot:
    """@brief 同一时间窗的 Turn 延迟组合快照 / Combined Turn-latency snapshot for one time window."""

    summary: tuple[TurnLatencyStats, ...]
    slow_turns: tuple[SlowTurn, ...]


class ResourceState(StrEnum):
    """@brief 截至查询时刻的资源存活状态 / Resource liveness state as of a query instant."""

    ACTIVE = "active"
    """@brief 心跳新鲜且未停止 / Heartbeat is fresh and the resource is not stopped."""

    STALE = "stale"
    """@brief 未收到停止信号但心跳过期 / No stop signal was received, but the heartbeat is stale."""

    STOPPED = "stopped"
    """@brief 已在查询时刻前显式停止 / Explicitly stopped by the query instant."""

    @classmethod
    def at(
        cls,
        *,
        last_seen_at: datetime,
        stopped_at: datetime | None,
        observed_at: datetime,
        stale_after: timedelta = RESOURCE_STALE_AFTER,
    ) -> ResourceState:
        """@brief 基于心跳而非空停止时间判定状态 / Classify state from a heartbeat rather than a null stop timestamp.

        @param last_seen_at 最后心跳时刻 / Latest heartbeat instant.
        @param stopped_at 显式停止时刻 / Explicit stop instant.
        @param observed_at 查询截止时刻 / Query observation instant.
        @param stale_after 心跳新鲜度阈值 / Heartbeat freshness threshold.
        @return 截至查询时刻的状态 / State as of the query instant.
        """

        timestamps = (last_seen_at, observed_at)
        if any(value.tzinfo is None for value in timestamps) or (
            stopped_at is not None and stopped_at.tzinfo is None
        ):
            raise ValueError("Resource liveness timestamps must be timezone-aware")
        if stale_after <= timedelta():
            raise ValueError("Resource stale threshold must be positive")
        observation = observed_at.astimezone(UTC)
        if stopped_at is not None and stopped_at.astimezone(UTC) <= observation:
            return cls.STOPPED
        if last_seen_at.astimezone(UTC) >= observation - stale_after:
            return cls.ACTIVE
        return cls.STALE


@dataclass(frozen=True, slots=True)
class ResourceInstance:
    """@brief 遥测资源实例生命周期 / Telemetry-resource instance lifecycle."""

    resource_id: UUID
    service_name: str
    service_version: str
    environment: str
    instance_id: str
    started_at: datetime
    last_seen_at: datetime
    stopped_at: datetime | None
    state: ResourceState
    attributes: JsonObject

    def __post_init__(self) -> None:
        """@brief 守住资源时间线不变式 / Enforce resource-timeline invariants.

        @return None / None.
        """

        timestamps = (self.started_at, self.last_seen_at)
        if any(value.tzinfo is None for value in timestamps) or (
            self.stopped_at is not None and self.stopped_at.tzinfo is None
        ):
            raise ValueError("Resource timestamps must be timezone-aware")
        started_at = self.started_at.astimezone(UTC)
        last_seen_at = self.last_seen_at.astimezone(UTC)
        stopped_at = (
            self.stopped_at.astimezone(UTC) if self.stopped_at is not None else None
        )
        if last_seen_at < started_at:
            raise ValueError("Resource heartbeat cannot precede resource start")
        if stopped_at is not None and stopped_at < started_at:
            raise ValueError("Resource stop cannot precede resource start")
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "last_seen_at", last_seen_at)
        object.__setattr__(self, "stopped_at", stopped_at)


def freeze_json_object(value: object) -> JsonObject:
    """@brief 校验 JSON object 顶层形状 / Validate a top-level JSON-object shape.

    @param value asyncpg 解码值 / Value decoded by asyncpg.
    @return 与外部对象隔离的 JSON object / JSON object isolated from the source value.
    """

    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise TypeError("Expected a JSON object")
    return {key: _json_value(item) for key, item in value.items()}


def _json_value(value: object) -> JsonValue:
    """@brief 深复制并校验 JSON value / Deep-copy and validate a JSON value."""

    if value is None or isinstance(value, str | bool | int | float):
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _json_value(item) for key, item in value.items()}
    raise TypeError("Expected a JSON-compatible value")


__all__ = [
    "ErrorEvent",
    "GenAiStats",
    "HealthPoint",
    "JsonObject",
    "JsonValue",
    "LogEntry",
    "LatencySnapshot",
    "MetricStats",
    "Overview",
    "PipelineStage",
    "RESOURCE_STALE_AFTER",
    "ResourceInstance",
    "ResourceState",
    "RetrievalQueueStats",
    "RetrievalSnapshot",
    "SlowTurn",
    "SpanStats",
    "TimeWindow",
    "TraceDetail",
    "TraceLog",
    "TraceSpan",
    "TraceSummary",
    "TurnLatencyStats",
    "freeze_json_object",
]
