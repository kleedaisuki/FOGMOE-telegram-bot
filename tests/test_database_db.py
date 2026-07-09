from contextlib import asynccontextmanager
import asyncio

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
