"""@brief PostgreSQL 兑换码适配器 / PostgreSQL redemption-code adapter."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, cast

from fogmoe_bot.application.economy.common import EconomyCode
from fogmoe_bot.application.economy.redemption import (
    CreateCodesCommand,
    RedeemCodeCommand,
    RedeemCodeResult,
    RedemptionOperations,
)
from fogmoe_bot.infrastructure.database import connection as db_connection

from .common import _load_result, _lock_account, _save_result


class PostgresRedemptionOperations(RedemptionOperations):
    """@brief 以账户先锁顺序执行兑换码事务 / Execute redemption transactions with account-first locking."""

    def __init__(self, *, admin_user_id: int) -> None:
        """@brief 注入管理员身份 / Inject administrator identity.

        @param admin_user_id 管理员用户 ID / Administrator user ID.
        """

        self._admin_user_id = admin_user_id

    async def redeem(self, command: RedeemCodeCommand) -> RedeemCodeResult:
        """@brief 以 account→code 锁序兑换卡密 / Redeem a code with account→code lock order.

        @param command 兑换命令 / Redemption command.
        @return 兑换结果 / Redemption result.
        """

        async with db_connection.transaction() as connection:
            account = await _lock_account(command.user_id, connection)
            if account is None:
                return RedeemCodeResult(EconomyCode.NOT_REGISTERED)
            replay = await _load_result(command.idempotency_key, connection)
            if replay is not None:
                return _redeem_from_mapping(replay)
            row = await db_connection.fetch_one(
                "SELECT id, amount, is_used, used_by, used_at "
                "FROM economy.redemption_codes WHERE code = %s FOR UPDATE",
                (command.code,),
                connection=connection,
            )
            if row is None:
                result = RedeemCodeResult(EconomyCode.NOT_FOUND)
            elif bool(row[2]):
                result = RedeemCodeResult(
                    EconomyCode.ALREADY_USED,
                    used_by=cast(int | None, row[3]),
                    used_at=cast(datetime | None, row[4]),
                )
            else:
                amount = cast(int, row[1])
                await db_connection.execute(
                    "UPDATE economy.redemption_codes SET is_used = TRUE, "
                    "used_by = %s, used_at = %s WHERE id = %s",
                    (command.user_id, command.redeemed_at, cast(int, row[0])),
                    connection=connection,
                )
                plan = "admin" if command.user_id == self._admin_user_id else "paid"
                await db_connection.execute(
                    "UPDATE identity.users SET coins_paid = coins_paid + %s, "
                    "user_plan = %s WHERE id = %s",
                    (amount, plan, command.user_id),
                    connection=connection,
                )
                result = RedeemCodeResult(
                    EconomyCode.SUCCESS,
                    amount=amount,
                    balance=account.total + amount,
                )
            await _save_result(
                command.idempotency_key,
                "redeem_code",
                command.user_id,
                _redeem_mapping(result),
                connection,
            )
            return result

    async def create_codes(self, command: CreateCodesCommand) -> tuple[str, ...]:
        """@brief 在一个事务创建所有卡密 / Create all codes in one transaction.

        @param command 创建命令 / Creation command.
        @return 已插入卡密 / Inserted codes.
        """

        inserted: list[str] = []
        async with db_connection.transaction() as connection:
            for code in command.codes:
                count = await db_connection.execute(
                    "INSERT INTO economy.redemption_codes (code, amount) "
                    "VALUES (%s, %s) ON CONFLICT (code) DO NOTHING",
                    (code, command.amount),
                    connection=connection,
                )
                if count == 1:
                    inserted.append(code)
        return tuple(inserted)


def _redeem_mapping(result: RedeemCodeResult) -> dict[str, object]:
    """@brief 序列化兑换结果 / Serialize a redemption result.

    @param result 兑换结果 / Redemption result.
    @return JSON mapping / JSON mapping.
    """

    return {
        "code": result.code.value,
        "amount": result.amount,
        "balance": result.balance,
        "used_by": result.used_by,
        "used_at": result.used_at.isoformat() if result.used_at else None,
    }


def _redeem_from_mapping(value: Mapping[str, Any]) -> RedeemCodeResult:
    """@brief 解析兑换回执 / Parse a redemption receipt.

    @param value 回执映射 / Receipt mapping.
    @return 兑换结果 / Redemption result.
    """

    used_at = value.get("used_at")
    return RedeemCodeResult(
        EconomyCode(str(value["code"])),
        amount=int(value.get("amount", 0)),
        balance=int(value.get("balance", 0)),
        used_by=int(value["used_by"]) if value.get("used_by") is not None else None,
        used_at=datetime.fromisoformat(str(used_at)) if used_at else None,
    )
