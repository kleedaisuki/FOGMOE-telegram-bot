import asyncio
from pathlib import Path

from fogmoe_bot.application.accounts import service as process_user
from fogmoe_bot.infrastructure.database.repositories.user_repository import UserAccount
from fogmoe_bot.application.telegram.features.economy import shop


def _run(coro):
    return asyncio.run(coro)


def _account(user_id=42, *, coins=0, coins_paid=0, permission=0):
    return UserAccount(
        user_id=user_id,
        permission=permission,
        coins=coins,
        coins_paid=coins_paid,
        permanent_records_limit=100,
        info="",
    )


def test_spend_user_coins_uses_free_balance_before_paid_balance(monkeypatch):
    connection = object()
    writes = []
    lock_reads = []

    async def fake_fetch_user_account(user_id, *, connection=None, for_update=False):
        lock_reads.append(for_update)
        return _account(user_id, coins=50, coins_paid=30)

    async def fake_set_coin_balances_and_plan(
        user_id,
        coins,
        coins_paid,
        user_plan,
        *,
        connection=None,
    ):
        writes.append((user_id, coins, coins_paid, user_plan))

    monkeypatch.setattr(
        process_user.user_repository,
        "fetch_user_account",
        fake_fetch_user_account,
    )
    monkeypatch.setattr(
        process_user.user_repository,
        "set_coin_balances_and_plan",
        fake_set_coin_balances_and_plan,
    )

    spent = _run(process_user.spend_user_coins(42, 60, connection=connection))

    assert spent is True
    assert lock_reads == [True]
    assert writes == [(42, 0, 20, process_user.USER_PLAN_PAID)]


def test_spend_user_coins_does_not_write_when_balance_is_insufficient(monkeypatch):
    writes = []

    async def fake_fetch_user_account(user_id, *, connection=None, for_update=False):
        return _account(user_id, coins=2, coins_paid=3)

    async def fake_set_coin_balances_and_plan(*args, **kwargs):
        writes.append((args, kwargs))

    monkeypatch.setattr(
        process_user.user_repository,
        "fetch_user_account",
        fake_fetch_user_account,
    )
    monkeypatch.setattr(
        process_user.user_repository,
        "set_coin_balances_and_plan",
        fake_set_coin_balances_and_plan,
    )

    spent = _run(process_user.spend_user_coins(42, 6, connection=object()))

    assert spent is False
    assert writes == []


def test_shop_handler_keeps_sql_out_of_telegram_callback():
    source = Path(shop.__file__).read_text(encoding="utf-8")

    assert "db_connection.fetch_one" not in source
    assert "db_connection.fetch_all" not in source
    assert "db_connection.execute" not in source
    assert "exec_driver_sql" not in source
