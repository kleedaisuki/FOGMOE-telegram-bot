"""@brief Economy bounded context 的真实 PostgreSQL 并发契约 / Real-PostgreSQL concurrency contracts for the economy bounded context."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime
import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from fogmoe_bot.application.economy.staking import OpenStake, StakingService
from fogmoe_bot.application.economy.staking_ports import StakeSession
from fogmoe_bot.domain.economy import StakeAction
from fogmoe_bot.infrastructure.database.economy_staking import PostgresStakeSession
from fogmoe_dbctl.postgres import read_service, service_sqlalchemy_url

PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""@brief 项目根目录 / Project root."""


class _EngineTransactions:
    """@brief 测试引擎驱动的质押事务工厂 / Staking transaction factory backed by the test engine."""

    def __init__(self, engine: AsyncEngine) -> None:
        """@brief 注入真实 PostgreSQL 引擎 / Inject the real PostgreSQL engine.

        @param engine 异步引擎 / Async engine.
        """

        self._engine = engine

    def transaction(self) -> AbstractAsyncContextManager[StakeSession]:
        """@brief 创建独立数据库事务 / Create an independent database transaction.

        @return 质押会话上下文 / Staking-session context.
        """

        return self._transaction()

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[StakeSession]:
        """@brief 将连接绑定到生产 session adapter / Bind a connection to the production session adapter.

        @return 质押会话 / Staking session.
        """

        async with self._engine.begin() as connection:
            await connection.execute(text("SET LOCAL lock_timeout = '2s'"))
            yield PostgresStakeSession(connection)


def _postgres_url() -> str:
    """@brief 读取显式测试 DSN 或本地 automation service / Read an explicit test DSN or local automation service.

    @return SQLAlchemy asyncpg URL / SQLAlchemy asyncpg URL.
    """

    explicit = os.environ.get("FOGMOE_TEST_DATABASE_URL")
    if explicit:
        return explicit
    if os.environ.get("FOGMOE_TEST_POSTGRES") != "1":
        pytest.skip("set FOGMOE_TEST_POSTGRES=1 to run the real PostgreSQL contract")
    config_dir = PROJECT_ROOT / "var/psql"
    if not (config_dir / "pg_service.conf").is_file():
        pytest.skip("local PostgreSQL service configuration is unavailable")
    return service_sqlalchemy_url(read_service(config_dir, "fogmoe_automation"))


def _test_user_id() -> int:
    """@brief 生成不与 Telegram 用户冲突的正 BIGINT / Generate a positive BIGINT disjoint from Telegram users.

    @return 测试用户 ID / Test user ID.
    """

    return 8_000_000_000_000_000_000 + int(uuid4().hex[:12], 16)


def test_real_postgres_concurrent_open_conserves_one_coin_and_rollback_is_clean() -> (
    None
):
    """@brief 并发开仓仅扣一枚，外层回滚不留部分状态 / Concurrent opening charges once and outer rollback leaves no partial state."""

    async def scenario() -> None:
        """@brief 执行一枚金币并发与回滚场景 / Execute one-coin concurrency and rollback scenarios."""

        engine = create_async_engine(_postgres_url(), poolclass=NullPool)
        user_id = _test_user_id()
        now = datetime.now()
        try:
            async with engine.begin() as setup:
                await setup.execute(
                    text(
                        "INSERT INTO identity.users "
                        "(id, tg_uid, provider, name, coins, coins_paid, user_plan) "
                        "VALUES (:id, :id, 'telegram', 'economy-pg', 1, 0, 'free')"
                    ),
                    {"id": user_id},
                )
            service = StakingService(_EngineTransactions(engine), admin_user_id=1)
            first, second = await asyncio.wait_for(
                asyncio.gather(
                    service.open(OpenStake(user_id, 1, now, f"pg-open:a:{user_id}")),
                    service.open(OpenStake(user_id, 1, now, f"pg-open:b:{user_id}")),
                ),
                timeout=3,
            )
            assert {first.action, second.action} == {
                StakeAction.OPENED,
                StakeAction.ALREADY_STAKED,
            }
            async with engine.connect() as probe:
                balance = await probe.scalar(
                    text(
                        "SELECT coins + coins_paid FROM identity.users WHERE id = :id"
                    ),
                    {"id": user_id},
                )
                principal = await probe.scalar(
                    text(
                        "SELECT stake_amount FROM economy.user_stakes WHERE user_id = :id"
                    ),
                    {"id": user_id},
                )
                assert balance == 0
                assert principal == 1

            async with engine.connect() as connection:
                transaction = await connection.begin()
                session = PostgresStakeSession(connection)
                account = await session.lock_account(user_id)
                assert account is not None
                await session.credit_free_coins(user_id, 10)
                await transaction.rollback()
            async with engine.connect() as probe:
                assert (
                    await probe.scalar(
                        text(
                            "SELECT coins + coins_paid FROM identity.users WHERE id = :id"
                        ),
                        {"id": user_id},
                    )
                    == 0
                )
        finally:
            async with engine.begin() as cleanup:
                await cleanup.execute(
                    text("DELETE FROM economy.operation_receipts WHERE user_id = :id"),
                    {"id": user_id},
                )
                await cleanup.execute(
                    text("DELETE FROM economy.user_stakes WHERE user_id = :id"),
                    {"id": user_id},
                )
                await cleanup.execute(
                    text("DELETE FROM identity.users WHERE id = :id"),
                    {"id": user_id},
                )
            await engine.dispose()

    asyncio.run(scenario())
