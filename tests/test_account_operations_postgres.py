"""@brief Account operations 的真实 PostgreSQL 契约 / Real-PostgreSQL contracts for account operations."""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest

from fogmoe_bot.application.accounts.operations import (
    PersonalInfoCommand,
    RegisterAccount,
)
from fogmoe_bot.domain.accounts.plan import AccountPlanPolicy
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database import db
from fogmoe_bot.infrastructure.database.account_operations import (
    PostgresAccountOperations,
)
from fogmoe_bot.infrastructure.database.account_plan import PostgresAccountPlanResolver
from postgres_test_support import configure_bot_database


def _postgres_url() -> str:
    """@brief 读取显式测试 DSN / Read an explicit test DSN.

    @return async SQLAlchemy URL / Async SQLAlchemy URL.
    """

    explicit = os.environ.get("FOGMOE_TEST_DATABASE_URL")
    if explicit:
        return explicit
    pytest.skip("set FOGMOE_TEST_DATABASE_URL to run the real PostgreSQL contract")


def _test_user_id() -> int:
    """@brief 生成正 BIGINT ID / Generate a positive BIGINT ID.

    @return test user ID / Test user ID.
    """

    return 6_000_000_000_000_000_000 + int(uuid4().hex[:12], 16)


def test_real_postgres_registration_and_personal_info_have_stable_receipts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 注册并发只发一次奖励，后续事实变化不改变旧命令快照 / Concurrent registration grants one bonus and later facts do not change old command snapshots.

    @param monkeypatch 临时绑定测试 DSN / Temporarily bind the test DSN.
    """

    async def scenario() -> None:
        """@brief 执行注册、冲突与资料回放 / Execute registration, conflict, and info replay.

        @return None / None.
        """

        await db.dispose_current_engine()
        configure_bot_database(_postgres_url())
        user_id = _test_user_id()
        suffix = uuid4().hex
        register_key = f"pg-account:register:{suffix}"
        info_key = f"pg-account:info:{suffix}"
        operations = PostgresAccountOperations(
            PostgresAccountPlanResolver(AccountPlanPolicy(administrator_id=1))
        )
        command = RegisterAccount(
            user_id=user_id,
            username=f"account_{suffix}",
            initial_coins=20,
            idempotency_key=register_key,
        )
        try:
            first, second = await asyncio.gather(
                operations.register(command),
                operations.register(command),
            )
            assert first.profile.total_coins == 20
            assert second.profile == first.profile
            assert {first.replayed, second.replayed} == {False, True}

            async with db_connection.transaction() as connection:
                await db_connection.execute(
                    "UPDATE identity.users SET permission = permission + 5 WHERE id = %s",
                    (user_id,),
                    connection=connection,
                )
            old_snapshot = await operations.register(command)
            assert old_snapshot.replayed
            assert old_snapshot.profile.total_coins == 20

            changed = RegisterAccount(
                user_id=user_id,
                username=f"changed_{suffix}",
                initial_coins=20,
                idempotency_key=register_key,
            )
            with pytest.raises(ValueError, match="changed semantics"):
                await operations.register(changed)

            info = PersonalInfoCommand(
                user_id=user_id,
                new_info="first value",
                idempotency_key=info_key,
            )
            committed = await operations.personal_info(info)
            assert committed.previous_info == ""
            assert committed.current_info == "first value"
            async with db_connection.transaction() as connection:
                await db_connection.execute(
                    "UPDATE identity.users SET info = 'later value' WHERE id = %s",
                    (user_id,),
                    connection=connection,
                )
            replay = await operations.personal_info(info)
            assert replay.replayed
            assert replay.current_info == "first value"

            async with db_connection.connect() as connection:
                row = await db_connection.fetch_one(
                    "SELECT name, permission, info FROM identity.users WHERE id = %s",
                    (user_id,),
                    connection=connection,
                )
                assert row is not None
                assert row[0] == command.username
                assert row[1] == 5
                assert row[2] == "later value"
                bank_balance = await db_connection.fetch_one(
                    "SELECT balance FROM bank.account_balances WHERE account_key = %s",
                    (f"user:{user_id}:free",),
                    connection=connection,
                )
                assert bank_balance is not None and bank_balance[0] == 20
        finally:
            async with db_connection.transaction() as connection:
                await db_connection.execute(
                    "DELETE FROM identity.operation_receipts WHERE user_id = %s",
                    (user_id,),
                    connection=connection,
                )
                await db_connection.execute(
                    "DELETE FROM identity.users WHERE id = %s",
                    (user_id,),
                    connection=connection,
                )
            await db.dispose_current_engine()

    asyncio.run(scenario())
