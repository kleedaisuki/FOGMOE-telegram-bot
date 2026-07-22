"""@brief 封闭账户方案策略与 PostgreSQL 事实读取测试 / Tests for closed account-plan policy and PostgreSQL fact reading."""

from __future__ import annotations

import asyncio

import pytest

from fogmoe_bot.domain.accounts.plan import AccountPlan, AccountPlanPolicy
from fogmoe_bot.infrastructure.database import account_plan
from fogmoe_bot.infrastructure.database.account_plan import PostgresAccountPlanResolver


def test_account_plan_policy_prioritizes_administrator_then_combines_paid_facts() -> (
    None
):
    """@brief 管理员优先，订阅或正付费余额均表达 paid / Administrator takes precedence; subscription or positive paid balance expresses paid."""

    policy = AccountPlanPolicy(administrator_id=7)

    assert (
        policy.resolve(
            user_id=7,
            has_active_subscription=False,
            has_paid_token_balance=False,
        )
        is AccountPlan.ADMIN
    )
    assert (
        policy.resolve(
            user_id=8,
            has_active_subscription=True,
            has_paid_token_balance=False,
        )
        is AccountPlan.PAID
    )
    assert (
        policy.resolve(
            user_id=8,
            has_active_subscription=False,
            has_paid_token_balance=True,
        )
        is AccountPlan.PAID
    )
    assert (
        policy.resolve(
            user_id=8,
            has_active_subscription=False,
            has_paid_token_balance=False,
        )
        is AccountPlan.FREE
    )


def test_account_plan_policy_rejects_ambiguous_runtime_values() -> None:
    """@brief 方案策略拒绝 bool-as-int 与非布尔付费事实 / The plan policy rejects bool-as-int and non-Boolean paid facts."""

    with pytest.raises(TypeError, match="administrator_id"):
        AccountPlanPolicy(administrator_id=True)  # type: ignore[arg-type]
    policy = AccountPlanPolicy(administrator_id=7)
    with pytest.raises(TypeError, match="has_active_subscription"):
        policy.resolve(
            user_id=8,
            has_active_subscription=1,  # type: ignore[arg-type]
            has_paid_token_balance=False,
        )
    with pytest.raises(TypeError, match="has_paid_token_balance"):
        policy.resolve(
            user_id=8,
            has_active_subscription=False,
            has_paid_token_balance=1,  # type: ignore[arg-type]
        )


def test_postgres_account_plan_resolver_uses_billing_and_bank_in_caller_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief PostgreSQL resolver 在调用方事务读取半开订阅周期与 paid wallet / The PostgreSQL resolver reads the half-open subscription period and paid wallet in the caller transaction.

    @param monkeypatch pytest 替换器 / Pytest replacement helper.
    """

    async def scenario() -> None:
        """@brief 执行 paid 方案读取 / Execute paid-plan reading.

        @return None / None.
        """

        connection = object()
        calls: list[tuple[str, tuple[object, ...], object]] = []

        async def fake_fetch_one(
            sql: str,
            params: tuple[object, ...],
            *,
            connection: object,
        ) -> tuple[bool, bool]:
            """@brief 记录 SQL 并返回订阅与钱包事实 / Record SQL and return subscription and wallet facts.

            @param sql 查询文本 / Query text.
            @param params 查询参数 / Query parameters.
            @param connection 调用方事务 / Caller transaction.
            @return 无订阅但有正付费余额 / No subscription and a positive paid balance.
            """

            calls.append((sql, params, connection))
            return (False, True)

        monkeypatch.setattr(account_plan.db, "fetch_one", fake_fetch_one)
        resolver = PostgresAccountPlanResolver(AccountPlanPolicy(administrator_id=1))

        assert await resolver.resolve(42, connection=connection) is AccountPlan.PAID  # type: ignore[arg-type]
        sql, params, used_connection = calls[0]
        assert params == (42, 42)
        assert used_connection is connection
        assert "status = 'active'" in sql
        assert "period_starts_at <= CURRENT_TIMESTAMP" in sql
        assert "CURRENT_TIMESTAMP < period_ends_at" in sql
        assert "FROM bank.account_balances" in sql
        assert "balance > 0" in sql

    asyncio.run(scenario())
