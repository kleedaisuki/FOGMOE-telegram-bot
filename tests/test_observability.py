"""@brief Typed observability 核心行为测试 / Typed-observability core behavior tests."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

import pytest

from fogmoe_bot.application.observability.telemetry import (
    Telemetry,
    TelemetryBuffer,
    TelemetryRuntime,
)
from fogmoe_bot.domain.observability.signals import (
    LogSignal,
    SpanSignal,
    SpanStatus,
    TelemetrySignal,
)
from fogmoe_bot.domain.observability.trace import TraceContext, TraceId
from fogmoe_bot.infrastructure.observability.logging import (
    ContextQueueHandler,
    TelemetryLogHandler,
)


def test_traceparent_round_trip_and_child_preserve_trace_identity() -> None:
    """@brief W3C carrier 往返且子 span 保持 trace / W3C carrier round-trips and child spans preserve the trace."""

    root = TraceContext.new_root()
    parsed = TraceContext.parse(root.to_traceparent())
    child = parsed.child()

    assert parsed == root
    assert len(root.to_traceparent()) == 55
    assert child.trace_id == root.trace_id
    assert child.span_id != root.span_id


@pytest.mark.parametrize(
    "value",
    (
        "00-00000000000000000000000000000000-1111111111111111-01",
        "00-11111111111111111111111111111111-0000000000000000-01",
        "ff-11111111111111111111111111111111-1111111111111111-01",
    ),
)
def test_traceparent_rejects_invalid_or_unsupported_identity(value: str) -> None:
    """@brief 零 identity 与未知版本被拒绝 / Zero identities and unknown versions are rejected."""

    with pytest.raises(ValueError):
        TraceContext.parse(value)


def test_nested_spans_emit_parented_immutable_signals_and_restore_context() -> None:
    """@brief 嵌套 span 发出父子信号并恢复 ContextVar / Nested spans emit parented signals and restore the ContextVar."""

    buffer = TelemetryBuffer(16)
    telemetry = Telemetry(buffer)
    with telemetry.span("root") as root:
        with telemetry.span("child") as child:
            assert telemetry.current_context == child.context
        assert telemetry.current_context == root.context
    assert telemetry.current_context is None

    signals = buffer.drain(16)
    spans = [signal for signal in signals if isinstance(signal, SpanSignal)]
    assert [span.name for span in spans] == ["child", "root"]
    assert spans[0].parent_span_id == root.context.span_id
    assert spans[0].trace_id == spans[1].trace_id
    assert all(span.duration_ns >= 0 for span in spans)


def test_nested_spans_inherit_business_correlation_attributes() -> None:
    """@brief 子 span 继承父业务关联属性 / Child spans inherit parent business-correlation attributes."""

    buffer = TelemetryBuffer(8)
    telemetry = Telemetry(buffer)
    with telemetry.span("turn", attributes={"fogmoe.turn.id": "turn-1"}):
        with telemetry.span("dependency"):
            pass

    spans = [signal for signal in buffer.drain(8) if isinstance(signal, SpanSignal)]
    dependency = next(span for span in spans if span.name == "dependency")
    assert dependency.attributes["fogmoe.turn.id"] == "turn-1"


def test_span_records_caught_failure_when_caller_sets_typed_status() -> None:
    """@brief 被业务捕获的异常仍可显式标记 span / A business-caught exception can still mark the span explicitly."""

    buffer = TelemetryBuffer(4)
    telemetry = Telemetry(buffer)
    with telemetry.span("operation") as span:
        span.set_status(SpanStatus.ERROR, "timeout")
        span.set_attribute("error.type", "TimeoutError")

    signal = buffer.drain(1)[0]
    assert isinstance(signal, SpanSignal)
    assert signal.status is SpanStatus.ERROR
    assert signal.attributes["error.type"] == "TimeoutError"


def test_log_handler_redacts_credentials_and_correlates_with_current_span() -> None:
    """@brief 结构日志脱敏并继承 trace / Structured logs redact credentials and inherit the trace."""

    buffer = TelemetryBuffer(8)
    telemetry = Telemetry(buffer)
    handler = TelemetryLogHandler(telemetry)
    record = logging.LogRecord(
        "fogmoe.test",
        logging.ERROR,
        __file__,
        1,
        "request token=super-secret",
        (),
        None,
    )
    with telemetry.span("request") as span:
        handler.emit(record)

    signals = buffer.drain(8)
    log = next(signal for signal in signals if isinstance(signal, LogSignal))
    assert log.body == "request token=[REDACTED]"
    assert log.trace_id == span.context.trace_id
    assert log.span_id == span.context.span_id


def test_log_inherits_turn_correlation_attributes_from_active_span() -> None:
    """@brief 日志继承 active span 的业务关联属性 / Logs inherit active-span business correlation attributes."""

    buffer = TelemetryBuffer(8)
    telemetry = Telemetry(buffer)
    handler = TelemetryLogHandler(telemetry)
    with telemetry.span("turn", attributes={"fogmoe.turn.id": "turn-1"}):
        handler.emit(
            logging.LogRecord(
                "fogmoe.test",
                logging.INFO,
                __file__,
                1,
                "turn progress",
                (),
                None,
            )
        )

    log = next(signal for signal in buffer.drain(8) if isinstance(signal, LogSignal))
    assert log.attributes["fogmoe.turn.id"] == "turn-1"


def test_queue_handler_captures_producer_context_before_cross_thread_delivery() -> None:
    """@brief producer context 在跨线程前冻结 / Producer context is frozen before cross-thread delivery."""

    import queue

    buffer = TelemetryBuffer(8)
    telemetry = Telemetry(buffer)
    records: queue.Queue[logging.LogRecord] = queue.Queue(maxsize=1)
    handler = ContextQueueHandler(records, telemetry)
    with telemetry.span("producer") as span:
        handler.emit(
            logging.LogRecord(
                "fogmoe.test",
                logging.INFO,
                __file__,
                1,
                "hello",
                (),
                None,
            )
        )
    prepared = records.get_nowait()
    assert prepared.fogmoe_trace_context == span.context


def test_queue_handler_captures_producer_correlation_before_cross_thread_delivery() -> (
    None
):
    """@brief 队列日志在生产线程冻结 Turn 关联 / Queue logs freeze Turn correlation in producer thread."""

    import queue

    buffer = TelemetryBuffer(8)
    telemetry = Telemetry(buffer)
    records: queue.Queue[logging.LogRecord] = queue.Queue(maxsize=1)
    handler = ContextQueueHandler(records, telemetry)
    with telemetry.span("turn", attributes={"fogmoe.turn.id": "turn-1"}):
        handler.emit(
            logging.LogRecord(
                "fogmoe.test",
                logging.INFO,
                __file__,
                1,
                "hello",
                (),
                None,
            )
        )
    prepared = records.get_nowait()
    assert prepared.fogmoe_telemetry_attributes["fogmoe.turn.id"] == "turn-1"


class _FlakySink:
    """@brief 首次失败后成功的 sink / Sink succeeding after its first failure."""

    def __init__(self) -> None:
        """@brief 初始化调用记录 / Initialize call records."""

        self.calls = 0
        self.persisted: list[TelemetrySignal] = []
        self.closed = False

    async def write(self, signals: Sequence[TelemetrySignal]) -> None:
        """@brief 首次抛错，后续保存 / Fail once and then persist."""

        self.calls += 1
        if self.calls == 1:
            raise OSError("database unavailable")
        self.persisted.extend(signals)

    async def close(self) -> None:
        """@brief 记录关闭 / Record closure."""

        self.closed = True


def test_export_runtime_retries_failed_atomic_batch_without_blocking_producer() -> None:
    """@brief exporter 重试相同批次且 producer 仅入队 / Exporter retries the same batch while producers only enqueue."""

    async def scenario() -> None:
        """@brief 驱动失败恢复 / Drive failure recovery."""

        buffer = TelemetryBuffer(8)
        telemetry = Telemetry(buffer)
        sink = _FlakySink()
        runtime = TelemetryRuntime(
            buffer=buffer,
            sink=sink,
            batch_size=4,
            flush_interval=0.001,
            retry_max_delay=0.01,
            shutdown_flush_timeout=0.1,
        )
        assert telemetry.gauge("fogmoe.test", 1.0)
        stop = asyncio.Event()
        task = asyncio.create_task(runtime.run(stop))
        async with asyncio.timeout(1):
            while not sink.persisted:
                await asyncio.sleep(0.001)
        stop.set()
        await task

        assert sink.calls >= 2
        assert len(sink.persisted) == 1
        assert runtime.export_failures == 1
        assert sink.closed is True

    asyncio.run(scenario())


def test_export_runtime_drains_every_shutdown_batch_within_deadline() -> None:
    """@brief 停机排空不局限于单个批次 / Shutdown draining is not limited to one batch."""

    async def scenario() -> None:
        """@brief 在已停止状态启动并验证完整排空 / Start stopped and verify a complete drain."""

        buffer = TelemetryBuffer(16)
        telemetry = Telemetry(buffer)
        sink = _FlakySink()
        sink.calls = 1
        runtime = TelemetryRuntime(
            buffer=buffer,
            sink=sink,
            batch_size=2,
            flush_interval=0.01,
            retry_max_delay=0.1,
            shutdown_flush_timeout=0.2,
        )
        for ordinal in range(5):
            assert telemetry.gauge("fogmoe.shutdown", ordinal)
        stop = asyncio.Event()
        stop.set()

        await runtime.run(stop)

        assert len(sink.persisted) == 5
        assert sink.calls == 4
        assert runtime.exported_signals == 5
        assert sink.closed is True

    asyncio.run(scenario())


def test_bounded_buffer_counts_drops_without_blocking() -> None:
    """@brief 满缓冲立即丢弃并可观测 / A full buffer drops immediately and observably."""

    buffer = TelemetryBuffer(1)
    telemetry = Telemetry(buffer)
    assert telemetry.gauge("fogmoe.first", 1)
    assert not telemetry.gauge("fogmoe.second", 2)
    assert telemetry.snapshot().dropped_total == 1
    assert telemetry.snapshot().accepted_by_signal == {"log": 0, "span": 0, "metric": 1}
    assert telemetry.snapshot().dropped_by_signal == {"log": 0, "span": 0, "metric": 1}


def test_trace_id_rejects_zero_bytes() -> None:
    """@brief trace identity 不允许全零 / A trace identity cannot be all zeroes."""

    with pytest.raises(ValueError):
        TraceId(bytes(16))
