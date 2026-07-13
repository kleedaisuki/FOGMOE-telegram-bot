"""@brief FogMoe 稳定遥测语义约定 / Stable FogMoe telemetry semantic conventions.

该模块只表达业务可观测性 vocabulary，不依赖 logging、数据库或任意 exporter。
This module owns business-observability vocabulary only; it depends on no logger,
database, or exporter.
"""

from __future__ import annotations

from enum import StrEnum


class Outcome(StrEnum):
    """@brief 低基数操作终态 / Low-cardinality operation outcome."""

    SUCCESS = "success"
    FAILURE = "failure"
    TIMEOUT = "timeout"
    RETRY = "retry"
    REJECTED = "rejected"
    DROPPED = "dropped"


class MetricName(StrEnum):
    """@brief 核心业务 metric 名称 / Core business metric names.

    仅包含可全量聚合的低基数指标。高基数 identity 应置于 logs/spans attributes。
    Contains only metrics safe for complete low-cardinality aggregation. High-cardinality
    identities belong in log/span attributes.
    """

    INBOX_OUTCOMES = "fogmoe.inbox.outcomes"
    INFERENCE_OUTCOMES = "fogmoe.inference.outcomes"
    OUTBOX_OUTCOMES = "fogmoe.outbox.outcomes"
    LLM_OUTCOMES = "fogmoe.llm.outcomes"
    TOOL_OUTCOMES = "fogmoe.tool.outcomes"
    RETRIEVAL_OUTCOMES = "fogmoe.retrieval.outcomes"
    DEPENDENCY_OUTCOMES = "fogmoe.dependency.outcomes"
    LEASE_RECOVERIES = "fogmoe.pipeline.lease.recoveries"


class EventName(StrEnum):
    """@brief 核心结构化事件名称 / Core structured event names."""

    INBOX_LEASE_RECOVERED = "inbox.lease.recovered"
    INFERENCE_LEASE_RECOVERED = "inference.lease.recovered"
    OUTBOX_LEASE_RECOVERED = "outbox.lease.recovered"
    INBOX_PROCESS_FAILED = "inbox.process.failed"
    INFERENCE_ATTEMPT_FAILED = "inference.attempt.failed"
    OUTBOX_DELIVERY_FAILED = "outbox.delivery.failed"


__all__ = ["EventName", "MetricName", "Outcome"]
