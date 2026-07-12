"""@brief 不可变分析视图模型 / Immutable analytics view models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TypeAlias
from uuid import UUID


JsonScalar: TypeAlias = str | bool | int | float | None
JsonObject: TypeAlias = dict[str, "JsonValue"]
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | JsonObject


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


@dataclass(frozen=True, slots=True)
class ResourceInstance:
    """@brief 遥测资源实例生命周期 / Telemetry-resource instance lifecycle."""

    resource_id: UUID
    service_name: str
    service_version: str
    environment: str
    instance_id: str
    started_at: datetime
    stopped_at: datetime | None
    attributes: JsonObject

    @property
    def active(self) -> bool:
        """@brief 指示实例是否仍活动 / Indicate whether the instance is still active.

        @return 未停止为 True / True when not stopped.
        """

        return self.stopped_at is None


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
    "ResourceInstance",
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
