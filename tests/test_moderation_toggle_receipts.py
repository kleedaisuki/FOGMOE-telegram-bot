"""@brief 垃圾过滤 source-receipt 测试 / Spam-control source-receipt tests."""

from __future__ import annotations

import asyncio
from types import TracebackType

import pytest

from fogmoe_bot.domain.moderation.models import (
    ChatId,
    ModerationCommandReceiptConflict,
    ModerationToggleResult,
)
from fogmoe_bot.infrastructure.database import db
from fogmoe_bot.infrastructure.database.moderation.group import (
    PostgresModerationGroupRepository,
)


class _Transaction:
    """@brief 记录同一事务连接 / Record one transaction connection."""

    def __init__(self) -> None:
        """@brief 创建连接标记 / Create a connection marker.

        @return None / None.
        """

        self.connection = object()
        self.exit_exception: type[BaseException] | None = None

    async def __aenter__(self) -> object:
        """@brief 进入事务 / Enter the transaction.

        @return 连接标记 / Connection marker.
        """

        return self.connection

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        """@brief 记录退出异常 / Record the exit exception.

        @param exc_type 异常类型 / Exception type.
        @param exc 异常 / Exception.
        @param traceback 回溯 / Traceback.
        @return False，传播异常 / False to propagate errors.
        """

        del exc, traceback
        self.exit_exception = exc_type
        return False


def test_spam_toggle_mutation_and_receipt_commit_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 同一 Update 只反转一次并返回首次 enabled / One Update reverses once and returns its first enabled value."""

    transaction = _Transaction()
    calls: list[tuple[str, str, object]] = []
    receipt_reads = 0

    async def fetch_one(
        sql: str,
        params: object = None,
        *,
        connection: object,
    ) -> tuple[object, ...] | None:
        """@brief 模拟锁、回执和空聚合 / Simulate locks, receipts, and an empty aggregate.

        @param sql SQL 文本 / SQL text.
        @param params SQL 参数 / SQL parameters.
        @param connection 当前事务 / Current transaction.
        @return 模拟行 / Simulated row.
        """

        del params
        nonlocal receipt_reads
        calls.append(("fetch", sql, connection))
        if "pg_advisory_xact_lock" in sql:
            return (None,)
        if "FROM moderation.toggle_command_receipts" in sql:
            receipt_reads += 1
            if receipt_reads == 1:
                return None
            return ("spam_control", -1001, 42, {}, True)
        if "moderation.group_spam_control" in sql:
            return (
                False,
                False,
                False,
                True,
                "override_global",
                "fail_closed",
                0,
                [],
                [],
            )
        raise AssertionError(f"unexpected SQL: {sql}")

    async def execute(
        sql: str,
        params: object = None,
        *,
        connection: object,
    ) -> int:
        """@brief 模拟 OCC insert 与 receipt insert / Simulate the OCC insert and receipt insert.

        @param sql SQL 文本 / SQL text.
        @param params SQL 参数 / SQL parameters.
        @param connection 当前事务 / Current transaction.
        @return 影响行数 / Affected rows.
        """

        del params
        calls.append(("execute", sql, connection))
        if sql.startswith("UPDATE moderation.group_spam_control"):
            return 0
        return 1

    monkeypatch.setattr(
        db,
        "transaction",
        lambda: transaction,
    )
    monkeypatch.setattr(db, "fetch_one", fetch_one)
    monkeypatch.setattr(db, "execute", execute)
    repository = PostgresModerationGroupRepository()
    key = "telegram-update:88:moderation.spam-toggle"

    async def scenario() -> tuple[ModerationToggleResult, ModerationToggleResult]:
        """@brief 执行首次命令与回放 / Execute the first command and replay.

        @return 两次结果 / Both results.
        """

        first = await repository.toggle_group(
            ChatId(-1001), actor_id=42, idempotency_key=key
        )
        replay = await repository.toggle_group(
            ChatId(-1001), actor_id=42, idempotency_key=key
        )
        with pytest.raises(ModerationCommandReceiptConflict):
            await repository.toggle_group(
                ChatId(-1001), actor_id=43, idempotency_key=key
            )
        return first, replay

    first, replay = asyncio.run(scenario())
    assert first.enabled is True and not first.replayed
    assert replay.enabled is True and replay.replayed
    writes = [sql for kind, sql, _connection in calls if kind == "execute"]
    assert (
        sum("INSERT INTO moderation.group_spam_control" in sql for sql in writes) == 1
    )
    assert (
        sum("INSERT INTO moderation.toggle_command_receipts" in sql for sql in writes)
        == 1
    )
    assert all(
        connection is transaction.connection for _kind, _sql, connection in calls
    )
    assert transaction.exit_exception is ModerationCommandReceiptConflict
