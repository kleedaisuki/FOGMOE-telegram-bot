"""@brief 会话仓储回归测试 / Conversation repository regression tests."""

import asyncio

from fogmoe_bot.infrastructure.database.repositories import conversation_repository


class _TransactionContext:
    """@brief 可控事务上下文 / Controllable transaction context."""

    def __init__(self, connection: object) -> None:
        """@brief 保存事务连接 / Store transaction connection.

        @param connection 模拟的数据库连接 / Fake database connection.
        """
        self.connection = connection

    async def __aenter__(self) -> object:
        """@brief 返回事务连接 / Return the transaction connection.

        @return 模拟的数据库连接 / Fake database connection.
        """
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        """@brief 结束模拟事务 / Finish the fake transaction.

        @param exc_type 异常类型 / Exception type.
        @param exc 异常对象 / Exception object.
        @param traceback 异常回溯 / Exception traceback.
        @return None / None.
        """
        return None


def test_archive_and_clear_chat_prunes_records_via_database_module(monkeypatch):
    """@brief 裁剪函数属于数据库模块而非连接 / Pruning belongs to database module, not connection."""
    transaction_connection = object()
    calls: list[tuple[str, object]] = []

    async def fake_fetch(conversation_id: int, *, connection: object):
        calls.append(("fetch", connection))
        assert conversation_id == 456
        return [{"role": "user", "content": "hello"}]

    async def fake_insert(user_id: int, snapshot_text: str, *, connection: object):
        calls.append(("insert", connection))
        assert user_id == 123
        assert "hello" in snapshot_text

    async def fake_prune(user_id: int, *, connection: object):
        calls.append(("prune", connection))
        assert user_id == 123
        return [{"record_id": 1}]

    async def fake_delete(conversation_id: int, *, connection: object):
        calls.append(("delete", connection))
        assert conversation_id == 456

    monkeypatch.setattr(
        conversation_repository.db_connection,
        "transaction",
        lambda: _TransactionContext(transaction_connection),
    )
    monkeypatch.setattr(conversation_repository, "fetch_chat_messages_raw", fake_fetch)
    monkeypatch.setattr(conversation_repository, "insert_permanent_snapshot", fake_insert)
    monkeypatch.setattr(
        conversation_repository.db_connection,
        "prune_permanent_records",
        fake_prune,
    )
    monkeypatch.setattr(conversation_repository, "delete_chat_record", fake_delete)

    snapshot_created, archived_records = asyncio.run(
        conversation_repository.archive_and_clear_chat(123, 456)
    )

    assert snapshot_created is True
    assert archived_records == [{"record_id": 1}]
    assert calls == [
        ("fetch", transaction_connection),
        ("insert", transaction_connection),
        ("prune", transaction_connection),
        ("delete", transaction_connection),
    ]
