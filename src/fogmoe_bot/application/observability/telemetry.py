"""@brief 有界遥测缓冲、追踪与导出生命周期 / Bounded telemetry buffering, tracing, and export lifecycle."""

from __future__ import annotations

import asyncio
import queue
import threading
import time
from collections.abc import Mapping, Sequence
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType, TracebackType
from typing import Literal, Protocol, Self

from fogmoe_bot.domain.observability.signals import (
    Attributes,
    AttributeValue,
    LogSignal,
    MetricKind,
    MetricSignal,
    Severity,
    SpanKind,
    SpanSignal,
    SpanStatus,
    TelemetrySignal,
    freeze_attributes,
)
from fogmoe_bot.domain.observability.trace import TraceContext


_CURRENT_TRACE: ContextVar[TraceContext | None] = ContextVar(
    "fogmoe_trace_context",
    default=None,
)
"""@brief 当前异步调用链 trace context / Current async-call-chain trace context."""

_CURRENT_ATTRIBUTES: ContextVar[Attributes] = ContextVar(
    "fogmoe_telemetry_attributes",
    default=freeze_attributes(),
)
"""@brief 当前调用链的关联属性 / Correlation attributes for the active call chain.

仅用于 span、日志和错误事件之间的关联；metric 必须显式传入低基数属性，避免把
``turn_id`` 等高基数 identity 变成 metric label。
Only correlates spans, logs, and errors. Metrics must receive explicit low-cardinality
attributes so identities such as ``turn_id`` never become metric labels.
"""


def _signal_counts() -> dict[str, int]:
    """@brief 创建完整 signal-kind 计数器 / Create a complete signal-kind counter.

    @return 各类 signal 的零值计数 / Zero-valued count for every signal kind.
    """

    return {"log": 0, "span": 0, "metric": 0}


def _signal_kind(signal: TelemetrySignal) -> str:
    """@brief 映射 signal 到稳定低基数类别 / Map a signal to a stable low-cardinality kind.

    @param signal 已记录或待丢弃的 signal / Recorded or dropped signal.
    @return ``log``、``span`` 或 ``metric`` / ``log``, ``span``, or ``metric``.
    """

    if isinstance(signal, LogSignal):
        return "log"
    if isinstance(signal, SpanSignal):
        return "span"
    if isinstance(signal, MetricSignal):
        return "metric"
    raise TypeError(f"Unsupported telemetry signal: {type(signal).__name__}")


@dataclass(frozen=True, slots=True)
class BufferSnapshot:
    """@brief 遥测缓冲健康快照 / Telemetry-buffer health snapshot.

    @param queued 当前排队信号数 / Currently queued signal count.
    @param capacity 最大信号数 / Maximum signal count.
    @param accepted_total 已接受总数 / Total accepted signals.
    @param dropped_total 已丢弃总数 / Total dropped signals.
    """

    queued: int
    capacity: int
    accepted_total: int
    dropped_total: int
    accepted_by_signal: Attributes
    dropped_by_signal: Attributes


class TelemetryBuffer:
    """@brief 跨 event loop 与 worker thread 的非阻塞有界缓冲 / Non-blocking bounded buffer shared across the event loop and worker threads."""

    def __init__(self, capacity: int) -> None:
        """@brief 创建缓冲 / Create the buffer.

        @param capacity 最大信号数 / Maximum signal count.
        """

        if capacity < 1:
            raise ValueError("Telemetry buffer capacity must be positive")
        self._queue: queue.Queue[TelemetrySignal] = queue.Queue(maxsize=capacity)
        self._capacity = capacity
        self._accepted_total = 0
        self._dropped_total = 0
        self._accepted_by_signal: dict[str, int] = _signal_counts()
        self._dropped_by_signal: dict[str, int] = _signal_counts()
        self._stats_lock = threading.Lock()

    def offer(self, signal: TelemetrySignal) -> bool:
        """@brief 非阻塞接受信号 / Offer a signal without blocking.

        @param signal 遥测信号 / Telemetry signal.
        @return 接受为 True，容量耗尽为 False / True when accepted, False when full.
        """

        try:
            self._queue.put_nowait(signal)
        except queue.Full:
            with self._stats_lock:
                self._dropped_total += 1
                self._dropped_by_signal[_signal_kind(signal)] += 1
            return False
        with self._stats_lock:
            self._accepted_total += 1
            self._accepted_by_signal[_signal_kind(signal)] += 1
        return True

    def drain(self, limit: int) -> tuple[TelemetrySignal, ...]:
        """@brief 无等待排空有界批次 / Drain a bounded batch without waiting.

        @param limit 批次上限 / Batch limit.
        @return 当前可用批次 / Currently available batch.
        """

        values: list[TelemetrySignal] = []
        while len(values) < limit:
            try:
                values.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return tuple(values)

    def snapshot(self) -> BufferSnapshot:
        """@brief 读取一致健康快照 / Read a coherent health snapshot.

        @return 缓冲快照 / Buffer snapshot.
        """

        with self._stats_lock:
            return BufferSnapshot(
                queued=self._queue.qsize(),
                capacity=self._capacity,
                accepted_total=self._accepted_total,
                dropped_total=self._dropped_total,
                accepted_by_signal=MappingProxyType(dict(self._accepted_by_signal)),
                dropped_by_signal=MappingProxyType(dict(self._dropped_by_signal)),
            )


class Telemetry:
    """@brief 应用唯一的信号记录器 / The application's sole signal recorder."""

    def __init__(self, buffer: TelemetryBuffer) -> None:
        """@brief 注入有界缓冲 / Inject the bounded buffer.

        @param buffer 进程共享缓冲 / Process-shared buffer.
        """

        self._buffer = buffer

    @property
    def current_context(self) -> TraceContext | None:
        """@brief 返回当前 trace context / Return the current trace context.

        @return 当前上下文或 None / Current context or None.
        """

        return _CURRENT_TRACE.get()

    @property
    def current_attributes(self) -> Attributes:
        """@brief 返回当前调用链关联属性 / Return active call-chain correlation attributes.

        @return 不可变关联属性 / Immutable correlation attributes.
        """

        return _CURRENT_ATTRIBUTES.get()

    def span(
        self,
        name: str,
        *,
        kind: SpanKind = SpanKind.INTERNAL,
        parent: TraceContext | None = None,
        attributes: Mapping[str, object] | None = None,
    ) -> SpanScope:
        """@brief 创建同步上下文管理的操作 span / Create a synchronously context-managed operation span.

        @param name 稳定低基数操作名 / Stable low-cardinality operation name.
        @param kind 操作边界类型 / Operation-boundary kind.
        @param parent durable 或当前父上下文 / Durable or current parent context.
        @param attributes 初始属性 / Initial attributes.
        @return 未进入的 span scope / Unentered span scope.
        """

        normalized = name.strip()
        if not normalized or len(normalized) > 255:
            raise ValueError("Span name must contain 1..255 characters")
        inherited = parent if parent is not None else self.current_context
        context = (
            inherited.child() if inherited is not None else TraceContext.new_root()
        )
        return SpanScope(
            telemetry=self,
            context=context,
            parent_span_id=(inherited.span_id if inherited is not None else None),
            name=normalized,
            kind=kind,
            attributes=attributes,
        )

    def log(
        self,
        *,
        occurred_at: datetime,
        severity: Severity,
        severity_text: str,
        logger_name: str,
        body: str,
        event_name: str | None = None,
        exception_type: str | None = None,
        exception_message: str | None = None,
        exception_stack: str | None = None,
        attributes: Mapping[str, object] | None = None,
        context: TraceContext | None = None,
    ) -> bool:
        """@brief 记录结构化日志 / Record a structured log.

        @return 缓冲接受为 True / True when accepted by the buffer.
        """

        active = context if context is not None else self.current_context
        observed_at = datetime.now(UTC)
        return self._buffer.offer(
            LogSignal(
                occurred_at=occurred_at.astimezone(UTC),
                observed_at=observed_at,
                severity=severity,
                severity_text=severity_text[:16],
                logger_name=logger_name[:255],
                body=body[:16384],
                event_name=event_name[:255] if event_name else None,
                trace_id=active.trace_id if active else None,
                span_id=active.span_id if active else None,
                exception_type=exception_type[:255] if exception_type else None,
                exception_message=(
                    exception_message[:4096] if exception_message else None
                ),
                exception_stack=exception_stack[:16384] if exception_stack else None,
                attributes=freeze_attributes(
                    {**self.current_attributes, **dict(attributes or {})}
                ),
            )
        )

    def counter(
        self,
        name: str,
        value: float = 1.0,
        *,
        unit: str = "{event}",
        attributes: Mapping[str, object] | None = None,
    ) -> bool:
        """@brief 记录非负 counter delta / Record a non-negative counter delta.

        @return 缓冲接受为 True / True when accepted by the buffer.
        """

        if value < 0:
            raise ValueError("Counter deltas cannot be negative")
        return self._metric(name, MetricKind.COUNTER, value, unit, attributes)

    def gauge(
        self,
        name: str,
        value: float,
        *,
        unit: str = "1",
        attributes: Mapping[str, object] | None = None,
    ) -> bool:
        """@brief 记录 gauge point / Record a gauge point.

        @return 缓冲接受为 True / True when accepted by the buffer.
        """

        return self._metric(name, MetricKind.GAUGE, value, unit, attributes)

    def _metric(
        self,
        name: str,
        kind: MetricKind,
        value: float,
        unit: str,
        attributes: Mapping[str, object] | None,
    ) -> bool:
        """@brief 记录已校验 metric point / Record a validated metric point."""

        normalized = name.strip()
        if not normalized or len(normalized) > 255:
            raise ValueError("Metric name must contain 1..255 characters")
        context = self.current_context
        return self._buffer.offer(
            MetricSignal(
                observed_at=datetime.now(UTC),
                name=normalized,
                kind=kind,
                value=float(value),
                unit=unit[:63],
                trace_id=context.trace_id if context else None,
                attributes=freeze_attributes(attributes),
            )
        )

    def snapshot(self) -> BufferSnapshot:
        """@brief 返回缓冲健康 / Return buffer health.

        @return 健康快照 / Health snapshot.
        """

        return self._buffer.snapshot()

    def _finish_span(self, signal: SpanSignal) -> bool:
        """@brief 接受已结束 span / Accept a completed span."""

        return self._buffer.offer(signal)


class SpanScope:
    """@brief 可变执行期、不可变结束信号的 span scope / Span scope mutable during execution and immutable after completion."""

    def __init__(
        self,
        *,
        telemetry: Telemetry,
        context: TraceContext,
        parent_span_id: object,
        name: str,
        kind: SpanKind,
        attributes: Mapping[str, object] | None,
    ) -> None:
        """@brief 初始化 scope / Initialize the scope."""

        from fogmoe_bot.domain.observability.trace import SpanId

        if parent_span_id is not None and not isinstance(parent_span_id, SpanId):
            raise TypeError("parent_span_id must be a SpanId")
        self._telemetry = telemetry
        self.context = context
        self._parent_span_id: SpanId | None = parent_span_id
        self._name = name
        self._kind = kind
        self._attributes: dict[str, object] = {
            **_CURRENT_ATTRIBUTES.get(),
            **dict(attributes or {}),
        }
        self._status = SpanStatus.OK
        self._status_message: str | None = None
        self._started_at: datetime | None = None
        self._started_ns: int | None = None
        self._token: Token[TraceContext | None] | None = None
        self._attributes_token: Token[Attributes] | None = None

    def __enter__(self) -> Self:
        """@brief 启动 span 并绑定上下文 / Start the span and bind its context.

        @return 当前 scope / This scope.
        """

        if self._token is not None:
            raise RuntimeError("SpanScope cannot be entered twice")
        self._started_at = datetime.now(UTC)
        self._started_ns = time.perf_counter_ns()
        self._token = _CURRENT_TRACE.set(self.context)
        self._attributes_token = _CURRENT_ATTRIBUTES.set(
            freeze_attributes({**_CURRENT_ATTRIBUTES.get(), **self._attributes})
        )
        return self

    def set_attribute(self, key: str, value: AttributeValue) -> None:
        """@brief 设置结束前属性 / Set an attribute before completion.

        @param key 属性名 / Attribute name.
        @param value OTel 兼容属性值 / OTel-compatible value.
        @return None / None.
        """

        if self._token is None or _CURRENT_TRACE.get() != self.context:
            raise RuntimeError("SpanScope must be the active scope before mutation")
        self._attributes[key] = value
        _CURRENT_ATTRIBUTES.set(
            freeze_attributes({**_CURRENT_ATTRIBUTES.get(), key: value})
        )

    def set_status(self, status: SpanStatus, message: str | None = None) -> None:
        """@brief 显式设置 span 状态 / Set span status explicitly.

        @param status 终态 / Terminal status.
        @param message 有界状态说明 / Bounded status detail.
        @return None / None.
        """

        self._status = status
        self._status_message = message[:2000] if message else None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        """@brief 结束并发出 span / Complete and emit the span.

        @return False，异常继续传播 / False so exceptions continue propagating.
        """

        del traceback
        token = self._token
        attributes_token = self._attributes_token
        started_at = self._started_at
        started_ns = self._started_ns
        if (
            token is None
            or attributes_token is None
            or started_at is None
            or started_ns is None
        ):
            raise RuntimeError("SpanScope was not entered")
        ended_ns = time.perf_counter_ns()
        ended_at = datetime.now(UTC)
        if exc is not None:
            self._status = SpanStatus.ERROR
            self._status_message = (str(exc).strip() or exc.__class__.__name__)[:2000]
            self._attributes["error.type"] = (
                exc_type.__name__ if exc_type else "unknown"
            )
        _CURRENT_TRACE.reset(token)
        _CURRENT_ATTRIBUTES.reset(attributes_token)
        self._token = None
        self._attributes_token = None
        self._telemetry._finish_span(
            SpanSignal(
                started_at=started_at,
                ended_at=ended_at,
                duration_ns=max(0, ended_ns - started_ns),
                trace_id=self.context.trace_id,
                span_id=self.context.span_id,
                parent_span_id=self._parent_span_id,
                name=self._name,
                kind=self._kind,
                status=self._status,
                status_message=self._status_message,
                attributes=freeze_attributes(self._attributes),
            )
        )
        return False


class TelemetrySink(Protocol):
    """@brief 批量遥测持久化端口 / Batched telemetry persistence port."""

    async def write(self, signals: Sequence[TelemetrySignal]) -> None:
        """@brief 原子写入一个批次 / Atomically write one batch."""

        ...

    async def close(self) -> None:
        """@brief 关闭持久化资源 / Close persistence resources."""

        ...


class TelemetryRuntime:
    """@brief 以失败隔离和指数退避批量导出信号 / Batch-export signals with failure isolation and exponential backoff."""

    def __init__(
        self,
        *,
        buffer: TelemetryBuffer,
        sink: TelemetrySink,
        batch_size: int,
        flush_interval: float,
        retry_max_delay: float,
        shutdown_flush_timeout: float,
    ) -> None:
        """@brief 创建导出 runtime / Create the export runtime."""

        if (
            batch_size < 1
            or min(flush_interval, retry_max_delay, shutdown_flush_timeout) <= 0
        ):
            raise ValueError("Telemetry runtime bounds must be positive")
        self._buffer = buffer
        self._sink = sink
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._retry_max_delay = retry_max_delay
        self._shutdown_flush_timeout = shutdown_flush_timeout
        self._export_failures = 0
        self._exported_signals = 0

    @property
    def export_failures(self) -> int:
        """@brief 返回导出失败数 / Return export failure count."""

        return self._export_failures

    @property
    def exported_signals(self) -> int:
        """@brief 返回成功导出信号数 / Return successfully exported signal count."""

        return self._exported_signals

    async def run(self, stop_event: asyncio.Event) -> None:
        """@brief 运行至停止并有界排空 / Run until stopped and perform a bounded drain."""

        pending: tuple[TelemetrySignal, ...] = ()
        retry_delay = self._flush_interval
        try:
            while not stop_event.is_set():
                if not pending:
                    try:
                        await asyncio.wait_for(
                            stop_event.wait(),
                            timeout=self._flush_interval,
                        )
                    except TimeoutError:
                        pass
                    if stop_event.is_set():
                        break
                    pending = self._buffer.drain(self._batch_size)
                    if not pending:
                        continue
                try:
                    await self._sink.write(pending)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._export_failures += 1
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=retry_delay)
                    except TimeoutError:
                        retry_delay = min(self._retry_max_delay, retry_delay * 2)
                    continue
                self._exported_signals += len(pending)
                pending = ()
                retry_delay = self._flush_interval
        finally:
            try:
                async with asyncio.timeout(self._shutdown_flush_timeout):
                    final_batch = pending + self._buffer.drain(
                        self._batch_size - len(pending)
                    )
                    while final_batch:
                        await self._sink.write(final_batch)
                        self._exported_signals += len(final_batch)
                        final_batch = self._buffer.drain(self._batch_size)
            except Exception:
                self._export_failures += 1
            try:
                await self._sink.close()
            except Exception:
                self._export_failures += 1


__all__ = [
    "BufferSnapshot",
    "SpanScope",
    "Telemetry",
    "TelemetryBuffer",
    "TelemetryRuntime",
    "TelemetrySink",
]
