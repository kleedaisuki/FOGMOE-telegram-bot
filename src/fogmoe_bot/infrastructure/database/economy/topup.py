"""@brief PostgreSQL 充值适配器 / PostgreSQL top-up adapter."""

from datetime import datetime
from typing import cast

from fogmoe_bot.application.economy.common import EconomyCode
from fogmoe_bot.application.economy.topup import (
    ApproveTopUp,
    TopUpAccountStatus,
    TopUpOperations,
    TopUpResult,
)
from fogmoe_bot.infrastructure.database import connection as db_connection

from .common import _load_result, _lock_account, _save_result


class PostgresTopUpOperations(TopUpOperations):
    """@brief 以账户锁执行充值事务 / Execute top-up transactions with account locks."""

    def __init__(self, *, admin_user_id: int) -> None:
        """@brief 注入管理员身份 / Inject administrator identity.

        @param admin_user_id 管理员用户 ID / Administrator user ID.
        """

        self._admin_user_id = admin_user_id

    async def topup_status(self, user_id: int) -> TopUpAccountStatus:
        """@brief 读取充值所需账户字段 / Read account fields needed for top-up.

        @param user_id 用户 ID / User ID.
        @return 充值账户状态 / Top-up account status.
        """

        row = await db_connection.fetch_one(
            "SELECT name, recharge_blocked_until FROM identity.users WHERE id = %s",
            (user_id,),
        )
        if row is None:
            return TopUpAccountStatus(False)
        return TopUpAccountStatus(
            True,
            name=cast(str, row[0]),
            blocked_until=cast(datetime | None, row[1]),
        )

    async def approve_topup(self, command: ApproveTopUp) -> TopUpResult:
        """@brief 以账户锁和回执原子发放付费金币 / Atomically credit paid coins with account lock and receipt.

        @param command 充值命令 / Top-up command.
        @return 充值结果 / Top-up result.
        """

        async with db_connection.transaction() as connection:
            account = await _lock_account(command.user_id, connection)
            if account is None:
                return TopUpResult(EconomyCode.NOT_FOUND)
            name_row = await db_connection.fetch_one(
                "SELECT name FROM identity.users WHERE id = %s",
                (command.user_id,),
                connection=connection,
            )
            name = cast(str, name_row[0]) if name_row is not None else None
            replay = await _load_result(command.idempotency_key, connection)
            if replay is not None:
                return TopUpResult(
                    EconomyCode(str(replay["code"])),
                    name=str(replay.get("name", "")) or None,
                    coins=int(replay.get("coins", 0)),
                )
            plan = "admin" if command.user_id == self._admin_user_id else "paid"
            await db_connection.execute(
                "UPDATE identity.users SET coins_paid = coins_paid + %s, "
                "user_plan = %s WHERE id = %s",
                (command.coins, plan, command.user_id),
                connection=connection,
            )
            result = TopUpResult(EconomyCode.SUCCESS, name=name, coins=command.coins)
            await _save_result(
                command.idempotency_key,
                "approve_topup",
                command.user_id,
                {"code": result.code.value, "name": name, "coins": command.coins},
                connection,
            )
            return result

    async def block_recharge(self, user_id: int, until: datetime) -> TopUpResult:
        """@brief 设置充值禁用截止时间 / Set a recharge block deadline.

        @param user_id 用户 ID / User ID.
        @param until 截止时间 / Deadline.
        @return 处理结果 / Processing result.
        """

        async with db_connection.transaction() as connection:
            if await _lock_account(user_id, connection) is None:
                return TopUpResult(EconomyCode.NOT_FOUND)
            row = await db_connection.fetch_one(
                "SELECT name FROM identity.users WHERE id = %s",
                (user_id,),
                connection=connection,
            )
            await db_connection.execute(
                "UPDATE identity.users SET recharge_blocked_until = %s WHERE id = %s",
                (until, user_id),
                connection=connection,
            )
            return TopUpResult(
                EconomyCode.SUCCESS,
                name=cast(str, row[0]) if row is not None else None,
            )
