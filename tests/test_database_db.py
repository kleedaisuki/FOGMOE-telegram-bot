from contextlib import asynccontextmanager
import asyncio
import logging

from sqlalchemy import create_engine, text

from fogmoe_bot.application.observability.telemetry import Telemetry, TelemetryBuffer
from fogmoe_bot.domain.observability.signals import SpanSignal, SpanStatus
from fogmoe_bot.infrastructure.database import db


class FakeConnection:
    """@brief 测试连接替身 / Test connection fake."""

    def __init__(self):
        """@brief 初始化替身 / Initialize fake."""

        self.calls = []

    async def execute(self, statement, params):
        """@brief 记录执行参数 / Record execution parameters.

        @param statement SQLAlchemy 语句 / SQLAlchemy statement.
        @param params 绑定参数 / Bound parameters.
        @return 哨兵结果 / Sentinel result.
        """

        self.calls.append((statement, params))
        return "ok"


def test_exec_sql_uses_connection_from_context(monkeypatch):
    """@brief exec_sql 使用上下文连接 / exec_sql uses connection from context."""

    fake_connection = FakeConnection()

    @asynccontextmanager
    async def fake_connect():
        yield fake_connection

    monkeypatch.setattr(db, "connect", fake_connect)

    result = asyncio.run(db.exec_sql("SELECT %s", (42,)))

    assert result == "ok"
    assert len(fake_connection.calls) == 1
    statement, params = fake_connection.calls[0]
    assert str(statement) == "SELECT :p0"
    assert params == {"p0": 42}


def test_sql_operation_is_low_cardinality_and_never_returns_statement() -> None:
    """@brief SQL 分类只返回有限 verb / SQL classification returns only bounded verbs."""

    assert db._sql_operation("  SELECT secret FROM users") == "SELECT"
    assert db._sql_operation("TRUNCATE private_table") == "OTHER"
    assert db._sql_operation("") == "UNKNOWN"


def test_sql_target_exposes_only_safe_schema_table_identity() -> None:
    """@brief SQL target 只保留低基数表名 / SQL targets retain only low-cardinality table names."""

    assert db._sql_target("SELECT secret FROM conversation.turns WHERE id = :id") == (
        "conversation.turns"
    )
    assert db._sql_target("UPDATE public.users SET token = :token") == "public.users"
    assert db._sql_target('SELECT * FROM "private users"') is None


def test_database_span_stack_finishes_success_and_error() -> None:
    """@brief 数据库事件栈正确结束成功与异常 span / The database event stack completes successful and failed spans."""

    class Connection:
        """@brief 提供 SQLAlchemy connection.info 形状 / Provide the SQLAlchemy connection.info shape."""

        def __init__(self) -> None:
            """@brief 初始化事件字典 / Initialize event state."""

            self.info: dict[str, object] = {}

    buffer = TelemetryBuffer(4)
    telemetry = Telemetry(buffer)
    connection = Connection()
    success = telemetry.span("postgresql.query")
    success.__enter__()
    connection.info["fogmoe.observability.spans"] = [success]
    db._finish_database_span(connection, None)

    failure = telemetry.span("postgresql.query")
    failure.__enter__()
    spans = connection.info["fogmoe.observability.spans"]
    assert isinstance(spans, list)
    spans.append(failure)
    db._finish_database_span(connection, OSError("unavailable"))

    signals = buffer.drain(4)
    assert [signal.status for signal in signals if isinstance(signal, SpanSignal)] == [
        SpanStatus.OK,
        SpanStatus.ERROR,
    ]
    assert telemetry.current_context is None


def test_database_span_completion_never_breaks_successful_sql_when_telemetry_fails(
    monkeypatch,
    caplog,
) -> None:
    """@brief 数据库埋点失败不能覆盖 SQL 成功 / Database instrumentation failure cannot overwrite SQL success."""

    class Connection:
        """@brief 提供 SQLAlchemy connection.info 形状 / Provide the SQLAlchemy connection.info shape."""

        def __init__(self) -> None:
            """@brief 初始化事件字典 / Initialize event state."""

            self.info: dict[str, object] = {}

    def fail_signal_admission(_: SpanSignal) -> bool:
        """@brief 模拟遥测信号接纳故障 / Simulate telemetry signal-admission failure.

        @param _ 已结束 span / Completed span.
        @return 永不返回 / Never returns.
        """

        raise ValueError("telemetry sink invariant failed")

    telemetry = Telemetry(TelemetryBuffer(4))
    connection = Connection()
    scope = telemetry.span("postgresql.query")
    scope.__enter__()
    connection.info["fogmoe.observability.spans"] = [scope]
    monkeypatch.setattr(telemetry, "_finish_span", fail_signal_admission)

    with caplog.at_level(logging.ERROR, logger=db.__name__):
        db._finish_database_span(connection, None)

    assert telemetry.current_context is None
    assert connection.info["fogmoe.observability.spans"] == []
    assert "Database telemetry span completion failed" in caplog.text


def test_database_span_start_never_breaks_sql_when_telemetry_fails(
    monkeypatch,
    caplog,
) -> None:
    """@brief 数据库 span 启动失败也不能阻止 SQL / Database span-start failure cannot block SQL."""

    class Connection:
        """@brief 提供 SQLAlchemy connection.info 形状 / Provide the SQLAlchemy connection.info shape."""

        def __init__(self) -> None:
            """@brief 初始化事件字典 / Initialize event state."""

            self.info: dict[str, object] = {}

    class FailingTelemetry:
        """@brief span 构造必定失败的遥测替身 / Telemetry double whose span construction always fails."""

        def span(self, *_: object, **__: object) -> object:
            """@brief 模拟埋点构造异常 / Simulate instrumentation-construction failure.

            @return 永不返回 / Never returns.
            """

            raise ValueError("telemetry construction failed")

    connection = Connection()
    monkeypatch.setattr(db, "_TELEMETRY", FailingTelemetry())

    with caplog.at_level(logging.ERROR, logger=db.__name__):
        db._start_database_span(connection, "SELECT 1", False)

    assert connection.info.get("fogmoe.observability.spans") is None
    assert "Database telemetry span start failed" in caplog.text


def test_before_cursor_hook_keeps_business_sql_running_when_telemetry_fails(
    monkeypatch,
) -> None:
    """@brief SQLAlchemy 前置埋点失败时业务 SQL 仍执行 / Business SQL still executes when SQLAlchemy pre-execution telemetry fails."""

    class FailingTelemetry:
        """@brief span 构造必定失败的遥测替身 / Telemetry double whose span construction always fails."""

        def span(self, *_: object, **__: object) -> object:
            """@brief 模拟埋点构造异常 / Simulate instrumentation-construction failure.

            @return 永不返回 / Never returns.
            """

            raise RuntimeError("telemetry startup failure")

    class AsyncEngineShape:
        """@brief 只暴露同步引擎的 AsyncEngine 形状 / AsyncEngine shape exposing only its synchronous engine."""

        def __init__(self, sync_engine: object) -> None:
            """@brief 保存 SQLAlchemy 同步引擎 / Store the SQLAlchemy synchronous engine.

            @param sync_engine SQLAlchemy 同步引擎 / SQLAlchemy synchronous engine.
            """

            self.sync_engine = sync_engine

    engine = create_engine("sqlite://")
    monkeypatch.setattr(db, "_TELEMETRY", FailingTelemetry())
    monkeypatch.setattr(db, "_INSTRUMENTED_ENGINE_ID", None)
    db._instrument_engine(AsyncEngineShape(engine))  # type: ignore[arg-type]
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT 1")).scalar_one() == 1
    finally:
        engine.dispose()
