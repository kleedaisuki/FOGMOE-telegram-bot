from contextlib import asynccontextmanager
import asyncio

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
