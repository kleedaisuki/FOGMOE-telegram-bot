"""@brief 不可变遥测信号 / Immutable telemetry signals."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import IntEnum, StrEnum
import math
from types import MappingProxyType
from typing import TypeAlias
from uuid import UUID

from .trace import SpanId, TraceId


type AttributeScalar = str | bool | int | float
"""@brief OTel 兼容的标量属性 / OTel-compatible scalar attribute."""

type AttributeValue = AttributeScalar | tuple[AttributeScalar, ...]
"""@brief OTel 兼容的不可变属性值 / OTel-compatible immutable attribute value."""

Attributes: TypeAlias = Mapping[str, AttributeValue]
"""@brief 只读遥测属性 / Read-only telemetry attributes."""


def freeze_attributes(values: Mapping[str, object] | None = None) -> Attributes:
    """@brief 校验并冻结扁平属性 / Validate and freeze flat attributes.

    @param values 待校验属性 / Attributes to validate.
    @return 不可变映射 / Immutable mapping.
    """

    frozen: dict[str, AttributeValue] = {}
    for raw_key, raw_value in (values or {}).items():
        key = raw_key.strip()
        if not key or len(key) > 255:
            raise ValueError("Telemetry attribute keys must contain 1..255 characters")
        if isinstance(raw_value, str | bool | int | float):
            value: AttributeValue = raw_value
        elif isinstance(raw_value, Sequence) and not isinstance(raw_value, str | bytes):
            items = tuple(raw_value)
            if not all(isinstance(item, str | bool | int | float) for item in items):
                raise TypeError(
                    "Telemetry attribute sequences must contain scalar values"
                )
            value = items
        else:
            raise TypeError(f"Unsupported telemetry attribute value for {key!r}")
        frozen[key] = value
    return MappingProxyType(frozen)


class Severity(IntEnum):
    """@brief OpenTelemetry 规范化严重度 / OpenTelemetry normalized severity."""

    TRACE = 1
    DEBUG = 5
    INFO = 9
    WARN = 13
    ERROR = 17
    FATAL = 21


class SpanKind(StrEnum):
    """@brief Span 操作边界类型 / Span operation-boundary kind."""

    INTERNAL = "internal"
    SERVER = "server"
    CLIENT = "client"
    PRODUCER = "producer"
    CONSUMER = "consumer"


class SpanStatus(StrEnum):
    """@brief Span 终态 / Span terminal status."""

    UNSET = "unset"
    OK = "ok"
    ERROR = "error"


class MetricKind(StrEnum):
    """@brief 原始 metric point 类型 / Raw metric-point kind."""

    COUNTER = "counter"
    GAUGE = "gauge"


@dataclass(frozen=True, slots=True)
class Resource:
    """@brief 产生信号的进程资源 / Process resource producing signals.

    @param resource_id 本次进程生命周期 ID / Process-lifecycle identifier.
    @param service_name 服务名 / Service name.
    @param service_version 服务版本 / Service version.
    @param deployment_environment 部署环境 / Deployment environment.
    @param service_instance_id 实例 identity / Instance identity.
    @param started_at 进程开始时间 / Process start time.
    @param attributes 额外固定属性 / Additional fixed attributes.
    """

    resource_id: UUID
    service_name: str
    service_version: str
    deployment_environment: str
    service_instance_id: str
    started_at: datetime
    attributes: Attributes

    def __post_init__(self) -> None:
        """@brief 校验资源字段 / Validate resource fields.

        @return None / None.
        """

        for value in (
            self.service_name,
            self.service_version,
            self.deployment_environment,
            self.service_instance_id,
        ):
            if not value.strip():
                raise ValueError("Resource identity fields cannot be blank")
        if self.started_at.tzinfo is None:
            raise ValueError("Resource started_at must be timezone-aware")
        object.__setattr__(self, "started_at", self.started_at.astimezone(UTC))
        object.__setattr__(self, "attributes", freeze_attributes(self.attributes))


@dataclass(frozen=True, slots=True)
class LogSignal:
    """@brief 结构化日志信号 / Structured log signal."""

    occurred_at: datetime
    observed_at: datetime
    severity: Severity
    severity_text: str
    logger_name: str
    body: str
    event_name: str | None
    trace_id: TraceId | None
    span_id: SpanId | None
    exception_type: str | None
    exception_message: str | None
    exception_stack: str | None
    attributes: Attributes

    def __post_init__(self) -> None:
        """@brief 校验时间、identity 与大小边界 / Validate time, identity, and size bounds.

        @return None / None.
        """

        occurred_at = _utc(self.occurred_at, "LogSignal.occurred_at")
        observed_at = _utc(self.observed_at, "LogSignal.observed_at")
        if not self.severity_text or not self.logger_name:
            raise ValueError("Log severity and logger cannot be blank")
        if self.span_id is not None and self.trace_id is None:
            raise ValueError("A log span_id requires a trace_id")
        object.__setattr__(self, "occurred_at", occurred_at)
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "attributes", freeze_attributes(self.attributes))


@dataclass(frozen=True, slots=True)
class SpanSignal:
    """@brief 已结束操作跨度 / Completed operation span."""

    started_at: datetime
    ended_at: datetime
    duration_ns: int
    trace_id: TraceId
    span_id: SpanId
    parent_span_id: SpanId | None
    name: str
    kind: SpanKind
    status: SpanStatus
    status_message: str | None
    attributes: Attributes

    def __post_init__(self) -> None:
        """@brief 校验 span 生命周期和因果 identity / Validate span lifecycle and causal identity.

        @return None / None.
        """

        started_at = _utc(self.started_at, "SpanSignal.started_at")
        ended_at = _utc(self.ended_at, "SpanSignal.ended_at")
        if ended_at < started_at or self.duration_ns < 0:
            raise ValueError("Span duration cannot be negative")
        if not self.name.strip() or len(self.name) > 255:
            raise ValueError("Span name must contain 1..255 characters")
        if self.parent_span_id == self.span_id:
            raise ValueError("A span cannot parent itself")
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "ended_at", ended_at)
        object.__setattr__(self, "attributes", freeze_attributes(self.attributes))


@dataclass(frozen=True, slots=True)
class MetricSignal:
    """@brief 原始 counter 或 gauge point / Raw counter or gauge point."""

    observed_at: datetime
    name: str
    kind: MetricKind
    value: float
    unit: str
    trace_id: TraceId | None
    attributes: Attributes

    def __post_init__(self) -> None:
        """@brief 校验 metric 语义和数值域 / Validate metric semantics and numeric domain.

        @return None / None.
        """

        observed_at = _utc(self.observed_at, "MetricSignal.observed_at")
        if not self.name.strip() or len(self.name) > 255 or not self.unit:
            raise ValueError("Metric name and unit must be nonblank and bounded")
        if not math.isfinite(self.value):
            raise ValueError("Metric values must be finite")
        if self.kind is MetricKind.COUNTER and self.value < 0:
            raise ValueError("Counter points cannot be negative")
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "attributes", freeze_attributes(self.attributes))


def _utc(value: datetime, name: str) -> datetime:
    """@brief 校验并规范 UTC 时间 / Validate and normalize a UTC timestamp.

    @param value 时区感知时间 / Timezone-aware timestamp.
    @param name 错误字段名 / Field name used in errors.
    @return UTC 时间 / UTC timestamp.
    """

    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


type TelemetrySignal = LogSignal | SpanSignal | MetricSignal
"""@brief PostgreSQL exporter 接受的封闭信号联合 / Closed signal union accepted by the PostgreSQL exporter."""


__all__ = [
    "Attributes",
    "AttributeScalar",
    "AttributeValue",
    "LogSignal",
    "MetricKind",
    "MetricSignal",
    "Resource",
    "Severity",
    "SpanKind",
    "SpanSignal",
    "SpanStatus",
    "TelemetrySignal",
    "freeze_attributes",
]
