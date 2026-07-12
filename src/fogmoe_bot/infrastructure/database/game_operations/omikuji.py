"""@brief 御神签 PostgreSQL adapter / PostgreSQL adapter for Omikuji."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


from fogmoe_bot.application.games.omikuji.models import (
    DrawOmikuji,
    OmikujiCode,
    OmikujiResult,
)
from fogmoe_bot.application.games.ports.omikuji import OmikujiOperations
from fogmoe_bot.domain.games import (
    FortuneLevel,
)
from fogmoe_bot.infrastructure.database import connection as db_connection

from .common import (
    _AccountOperations,
    _load_receipt,
    _lock_account,
    _lock_receipt_key,
    _save_receipt,
)


class PostgresOmikujiOperations(_AccountOperations, OmikujiOperations):
    """@brief 御神签每日唯一扣费与回执 adapter / Omikuji adapter for daily-unique charging and receipts."""

    def __init__(self, *, admin_user_id: int) -> None:
        """@brief 注入管理员身份 / Inject the administrator identity.

        @param admin_user_id 管理员用户 ID / Administrator user ID.
        """

        super().__init__(admin_user_id=admin_user_id)

    async def draw_omikuji(self, command: DrawOmikuji) -> OmikujiResult:
        """@brief 原子保存每日签并只在首次扣费 / Atomically persist the daily fortune and charge only once.

        @param command 抽签命令 / Draw command.
        @return 抽签结果 / Draw result.
        """

        async with db_connection.transaction() as connection:
            await _lock_receipt_key(command.idempotency_key, connection)
            replay = await _load_receipt(
                command.idempotency_key, "omikuji.draw", command.user_id, connection
            )
            if replay is not None:
                return _omikuji_result_from_json(replay, replayed=True)
            account = await _lock_account(command.user_id, connection)
            if account is None:
                result = OmikujiResult(OmikujiCode.NOT_REGISTERED)
            else:
                row = await db_connection.fetch_one(
                    "SELECT fortune FROM game.user_omikuji "
                    "WHERE user_id = %s AND fortune_date = %s",
                    (command.user_id, command.day),
                    connection=connection,
                )
                if row is not None:
                    result = OmikujiResult(
                        OmikujiCode.ALREADY_DRAWN,
                        FortuneLevel(str(row[0])),
                        False,
                        account.total,
                    )
                elif not await self._spend_account(account, 1, connection):
                    result = OmikujiResult(
                        OmikujiCode.INSUFFICIENT_COINS, balance=account.total
                    )
                else:
                    await db_connection.execute(
                        "INSERT INTO game.user_omikuji "
                        "(user_id, fortune_date, fortune) VALUES (%s, %s, %s)",
                        (command.user_id, command.day, command.drawn_fortune.value),
                        connection=connection,
                    )
                    result = OmikujiResult(
                        OmikujiCode.SUCCESS,
                        command.drawn_fortune,
                        True,
                        account.total - 1,
                    )
            await _save_receipt(
                command.idempotency_key,
                "omikuji.draw",
                command.user_id,
                _omikuji_result_to_json(result),
                connection,
            )
            return result


def _omikuji_result_to_json(result: OmikujiResult) -> dict[str, object]:
    """@brief 序列化御神签回执 / Serialize an Omikuji receipt.

    @param result 御神签结果 / Omikuji result.
    @return 版本化 JSON / Versioned JSON.
    """

    return {
        "schema": 1,
        "code": result.code.value,
        "fortune": result.fortune.value if result.fortune is not None else None,
        "charged": result.charged,
        "balance": result.balance,
    }


def _omikuji_result_from_json(
    value: Mapping[str, Any], *, replayed: bool
) -> OmikujiResult:
    """@brief 解析御神签回执 / Parse an Omikuji receipt.

    @param value 回执 JSON / Receipt JSON.
    @param replayed 是否回放 / Whether replayed.
    @return 御神签结果 / Omikuji result.
    """

    return OmikujiResult(
        OmikujiCode(str(value["code"])),
        FortuneLevel(str(value["fortune"]))
        if value.get("fortune") is not None
        else None,
        bool(value.get("charged", False)),
        int(value["balance"]) if value.get("balance") is not None else None,
        replayed,
    )


__all__ = ["PostgresOmikujiOperations"]
