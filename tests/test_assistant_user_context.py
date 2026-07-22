"""@brief Assistant 用户上下文 PostgreSQL 读模型测试 / Assistant user-context PostgreSQL read-model tests."""

from __future__ import annotations

import asyncio
import inspect
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from fogmoe_bot.domain.accounts.plan import AccountPlan
from fogmoe_bot.infrastructure.database import assistant_user_context
from fogmoe_bot.infrastructure.database.assistant_user_context import (
    AssistantUserSnapshot,
)
from fogmoe_bot.infrastructure.database.scheduled_assistant_profile import (
    PostgresScheduledAssistantProfileReader,
)


def test_snapshot_read_maps_authoritative_balances_and_reuses_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 用户快照只映射权威 Bank 余额并复用调用方连接 / The user snapshot maps authoritative Bank balances and reuses the caller connection.

    @param monkeypatch pytest patch helper / pytest patch helper.
    @return None / None.
    """

    expected_connection = object()
    calls: list[tuple[str, object, object]] = []

    async def fake_fetch_one(
        sql: str,
        params: object,
        *,
        connection: object,
    ) -> tuple[object, ...]:
        """@brief 返回完整用户投影并记录数据库边界 / Return a complete user projection and record the database boundary."""

        calls.append((sql, params, connection))
        return (42, 2, 11, 7, "  compiler researcher  ", "  Klee  ")

    monkeypatch.setattr(assistant_user_context.db, "fetch_one", fake_fetch_one)

    snapshot = asyncio.run(
        assistant_user_context.fetch_assistant_user_snapshot(
            42,
            connection=expected_connection,  # type: ignore[arg-type]
        )
    )

    assert snapshot is not None
    assert snapshot.user_id == 42
    assert snapshot.permission == 2
    assert snapshot.coins == 11
    assert snapshot.coins_paid == 7
    assert snapshot.total_coins == 18
    assert snapshot.info == "  compiler researcher  "
    assert snapshot.name == "  Klee  "
    assert len(calls) == 1
    sql, params, connection = calls[0]
    assert connection is expected_connection
    assert params == (42,)
    assert "bank.account_balances AS free_balance" in sql
    assert "bank.account_balances AS paid_balance" in sql
    assert "identity.users.coins" not in sql
    assert "FOR UPDATE" not in sql


def test_identity_lock_requires_and_reuses_transaction_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 身份锁读取以必需连接表达事务所有权 / The identity locking read expresses transaction ownership with a required connection.

    @param monkeypatch pytest patch helper / pytest patch helper.
    @return None / None.
    """

    expected_connection = object()
    calls: list[tuple[str, object]] = []

    async def fake_fetch_one(
        sql: str,
        params: object,
        *,
        connection: object,
    ) -> tuple[object, ...]:
        """@brief 返回锁定身份行 / Return the locked identity row."""

        assert params == (42,)
        calls.append((sql, connection))
        return (42, 1, "info")

    monkeypatch.setattr(assistant_user_context.db, "fetch_one", fake_fetch_one)

    identity = asyncio.run(
        assistant_user_context.lock_assistant_identity_context_in_transaction(
            42,
            connection=expected_connection,  # type: ignore[arg-type]
        )
    )

    assert identity is not None and identity.info == "info"
    assert [connection for _, connection in calls] == [expected_connection]
    assert calls[0][0].endswith("FOR UPDATE")
    assert (
        "for_update"
        not in inspect.signature(
            assistant_user_context.fetch_assistant_user_snapshot
        ).parameters
    )
    assert (
        "for_update"
        not in inspect.signature(
            assistant_user_context.fetch_assistant_identity_context
        ).parameters
    )
    assert (
        inspect.signature(
            assistant_user_context.lock_assistant_identity_context_in_transaction
        )
        .parameters["connection"]
        .default
        is inspect.Parameter.empty
    )


def test_identity_read_excludes_balance_and_lock_clauses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 普通身份读取不接触余额且不获取写锁 / A plain identity read touches no balance and takes no write lock.

    @param monkeypatch pytest patch helper / pytest patch helper.
    @return None / None.
    """

    expected_connection = object()
    captured: list[tuple[str, object, object]] = []

    async def fake_fetch_one(
        sql: str,
        params: object,
        *,
        connection: object,
    ) -> tuple[object, ...]:
        """@brief 记录普通身份查询 / Record the plain identity query."""

        captured.append((sql, params, connection))
        return (42, 0, None)

    monkeypatch.setattr(assistant_user_context.db, "fetch_one", fake_fetch_one)

    identity = asyncio.run(
        assistant_user_context.fetch_assistant_identity_context(
            42,
            connection=expected_connection,  # type: ignore[arg-type]
        )
    )

    assert identity is not None
    assert identity.permission == 0
    assert identity.info == ""
    assert captured == [
        (
            "SELECT id, permission, info FROM identity.users WHERE id = %s",
            (42,),
            expected_connection,
        )
    ]


@pytest.mark.parametrize(
    ("row", "expected"),
    [(None, False), ((1,), True)],
    ids=["absent", "present"],
)
def test_diary_existence_query_is_bounded_and_reuses_connection(
    monkeypatch: pytest.MonkeyPatch,
    row: tuple[int] | None,
    expected: bool,
) -> None:
    """@brief 日记存在性查询有界且复用连接 / The diary-existence query is bounded and reuses its connection.

    @param monkeypatch pytest patch helper / pytest patch helper.
    @param row 数据库替身行 / Database-double row.
    @param expected 期望存在性 / Expected existence flag.
    @return None / None.
    """

    expected_connection = object()
    captured: list[tuple[str, object, object]] = []

    async def fake_fetch_one(
        sql: str,
        params: object,
        *,
        connection: object,
    ) -> tuple[int] | None:
        """@brief 返回可控存在性行 / Return the controlled existence row."""

        captured.append((sql, params, connection))
        return row

    monkeypatch.setattr(assistant_user_context.db, "fetch_one", fake_fetch_one)

    result = asyncio.run(
        assistant_user_context.assistant_diary_exists(
            42,
            connection=expected_connection,  # type: ignore[arg-type]
        )
    )

    assert result is expected
    assert len(captured) == 1
    sql, params, connection = captured[0]
    assert params == (42,)
    assert connection is expected_connection
    assert "content != ''" in sql
    assert sql.endswith("LIMIT 1")


def test_scheduled_profile_read_reuses_one_transaction_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 定时用户读模型的各权威 reader 复用一个短事务 / Every authoritative scheduled-user reader reuses one short transaction.

    @param monkeypatch pytest patch helper / pytest patch helper.
    @return None / None.
    """

    expected_connection = object()
    calls: list[tuple[str, int, object]] = []

    @asynccontextmanager
    async def fake_transaction():
        """@brief 提供唯一事务连接 / Provide the sole transaction connection."""

        calls.append(("transaction-enter", 0, expected_connection))
        try:
            yield expected_connection
        finally:
            calls.append(("transaction-exit", 0, expected_connection))

    async def fake_snapshot(
        user_id: int,
        *,
        connection: object,
    ) -> AssistantUserSnapshot:
        """@brief 返回权威账户快照 / Return the authoritative account snapshot."""

        calls.append(("snapshot", user_id, connection))
        return AssistantUserSnapshot(
            user_id=user_id,
            permission=1,
            coins=13,
            coins_paid=8,
            info="  language semantics  ",
            name="  Klee  ",
        )

    async def fake_execute(
        sql: str,
        *,
        connection: object,
    ) -> int:
        """@brief 记录事务特征必须先于事实查询 / Record transaction characteristics before every fact read."""

        assert sql == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
        calls.append(("transaction-characteristics", 0, connection))
        return 0

    async def fake_profile(
        user_id: int,
        *,
        connection: object,
    ) -> None:
        """@brief 表示当前没有 User Profile / Represent an absent User Profile."""

        calls.append(("profile", user_id, connection))
        return None

    async def fake_diary(
        user_id: int,
        *,
        connection: object,
    ) -> bool:
        """@brief 返回日记存在事实 / Return the diary-existence fact."""

        calls.append(("diary", user_id, connection))
        return True

    async def fake_plan(
        user_id: int,
        *,
        connection: object,
    ) -> AccountPlan:
        """@brief 返回权威账户方案 / Return the authoritative account plan."""

        calls.append(("plan", user_id, connection))
        return AccountPlan.PAID

    monkeypatch.setattr(
        assistant_user_context.db,
        "transaction",
        fake_transaction,
    )
    monkeypatch.setattr(
        assistant_user_context.db,
        "execute",
        fake_execute,
    )
    monkeypatch.setattr(
        assistant_user_context,
        "fetch_assistant_user_snapshot",
        fake_snapshot,
    )
    monkeypatch.setattr(
        assistant_user_context,
        "assistant_diary_exists",
        fake_diary,
    )

    result = asyncio.run(
        PostgresScheduledAssistantProfileReader(
            SimpleNamespace(resolve=fake_plan),  # type: ignore[arg-type]
            profiles=SimpleNamespace(  # type: ignore[arg-type]
                read_profile_in_transaction=fake_profile
            ),
        ).read(42)
    )

    assert result is not None
    assert result.user_id == 42
    assert result.username == "Klee"
    assert result.display_name == "Klee"
    assert result.coins == 21
    assert result.plan is AccountPlan.PAID
    assert result.personal_info == "language semantics"
    assert result.diary_exists
    assert [name for name, _, _ in calls] == [
        "transaction-enter",
        "transaction-characteristics",
        "snapshot",
        "profile",
        "diary",
        "plan",
        "transaction-exit",
    ]
    assert all(connection is expected_connection for _, _, connection in calls)
