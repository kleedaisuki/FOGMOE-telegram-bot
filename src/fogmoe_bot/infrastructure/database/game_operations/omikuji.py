"""@brief 御神签 PostgreSQL adapter / PostgreSQL adapter for Omikuji.

御神签的可消费余额唯一来自 Bank 的 Free 钱包。身份表只用于确认用户是否已注册，绝不读取
或写入其中的历史金币投影。
/ Omikuji's spendable balance comes exclusively from the Bank Free wallet.  The identity table is
used only to confirm registration; its legacy token projection is never read or written.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.games.omikuji.models import (
    DrawOmikuji,
    OmikujiCode,
    OmikujiResult,
)
from fogmoe_bot.application.games.ports.omikuji import OmikujiOperations
from fogmoe_bot.domain.banking.ledger import LedgerAccount, LedgerReason
from fogmoe_bot.domain.banking.money import (
    SystemAccountKind,
    TokenAmount,
    TokenBucket,
)
from fogmoe_bot.domain.games import FortuneLevel
from fogmoe_bot.infrastructure.database import db
from fogmoe_bot.infrastructure.database.banking import (
    lock_bank_account_balances,
    post_bank_transfer,
)


class PostgresOmikujiOperations(OmikujiOperations):
    """@brief 御神签每日唯一扣费与回执 adapter / Omikuji adapter for daily-unique charging and receipts."""

    async def draw_omikuji(self, command: DrawOmikuji) -> OmikujiResult:
        """@brief 原子保存每日签并从 Free 钱包只在首次扣费 / Atomically persist the daily fortune and charge the Free wallet only once.

        @param command 抽签命令 / Draw command.
        @return 抽签结果 / Draw result.
        @note Free 钱包与 burn 系统账户在查询余额前按 Bank 的稳定顺序锁定；
            Free wallet and the burn system account are locked in Bank's stable order before
            inspecting the balance.
        """

        async with db.transaction() as connection:
            await _lock_receipt_key(command.idempotency_key, connection)
            replay = await _load_receipt(
                command.idempotency_key,
                "omikuji.draw",
                command.user_id,
                connection,
            )
            if replay is not None:
                return _omikuji_result_from_json(replay, replayed=True)

            if not await _registered_user_exists(command.user_id, connection):
                result = OmikujiResult(OmikujiCode.NOT_REGISTERED)
            else:
                free_wallet = LedgerAccount.user(command.user_id, TokenBucket.FREE)
                burn_wallet = LedgerAccount.system(SystemAccountKind.BURN)
                balances = await lock_bank_account_balances(
                    (free_wallet, burn_wallet), connection
                )
                free_balance = balances[free_wallet]
                row = await db.fetch_one(
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
                        free_balance,
                    )
                elif free_balance < 1:
                    result = OmikujiResult(
                        OmikujiCode.INSUFFICIENT_FREE_TOKENS,
                        balance=free_balance,
                    )
                else:
                    await post_bank_transfer(
                        namespace="omikuji-offering",
                        source_idempotency_key=command.idempotency_key,
                        reason=LedgerReason.BANK_BURN,
                        source=free_wallet,
                        destination=burn_wallet,
                        amount=TokenAmount(1),
                        created_at=datetime.combine(command.day, time.min, tzinfo=UTC),
                        actor_id=command.user_id,
                        connection=connection,
                        metadata={"burn_kind": "omikuji_offering"},
                    )
                    await db.execute(
                        "INSERT INTO game.user_omikuji "
                        "(user_id, fortune_date, fortune) VALUES (%s, %s, %s)",
                        (command.user_id, command.day, command.drawn_fortune.value),
                        connection=connection,
                    )
                    result = OmikujiResult(
                        OmikujiCode.SUCCESS,
                        command.drawn_fortune,
                        True,
                        free_balance - 1,
                    )
            await _save_receipt(
                command.idempotency_key,
                "omikuji.draw",
                command.user_id,
                _omikuji_result_to_json(result),
                connection,
            )
            return result


async def _registered_user_exists(
    user_id: int,
    connection: AsyncConnection,
) -> bool:
    """@brief 仅检查身份是否存在 / Check identity existence only.

    @param user_id 用户标识 / User identity.
    @param connection 当前事务连接 / Current transaction connection.
    @return 已注册时为 True / True when registered.
    @note 查询故意不选择 ``coins`` 或 ``coins_paid``；余额权威在 Bank。
        / This query deliberately selects neither ``coins`` nor ``coins_paid``; Bank is the
        balance authority.
    """

    row = await db.fetch_one(
        "SELECT 1 FROM identity.users WHERE id = %s",
        (user_id,),
        connection=connection,
    )
    return row is not None


async def _lock_receipt_key(
    idempotency_key: str,
    connection: AsyncConnection,
) -> None:
    """@brief 用事务 advisory lock 串行化御神签回执键 / Serialize an Omikuji receipt key with a transaction advisory lock.

    @param idempotency_key 业务幂等键 / Business idempotency key.
    @param connection 当前事务连接 / Current transaction connection.
    @return None / None.
    @raise ValueError 幂等键为空或过长时抛出 / Raised when the key is blank or too long.
    """

    if not idempotency_key.strip() or len(idempotency_key) > 255:
        raise ValueError("Invalid Omikuji idempotency key")
    await db.fetch_one(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"omikuji-receipt:{idempotency_key}",),
        connection=connection,
    )


async def _load_receipt(
    idempotency_key: str,
    operation: str,
    user_id: int,
    connection: AsyncConnection,
) -> dict[str, Any] | None:
    """@brief 读取并验证已提交的御神签回执 / Load and validate a committed Omikuji receipt.

    @param idempotency_key 幂等键 / Idempotency key.
    @param operation 固定操作名 / Fixed operation name.
    @param user_id 业务用户 / Business user.
    @param connection 当前事务连接 / Current transaction connection.
    @return 结果 JSON；不存在时为 None / Result JSON, or None when absent.
    @raise ValueError 同一键改变业务语义时抛出 / Raised when the same key changes semantics.
    """

    row = await db.fetch_one(
        "SELECT operation, user_id, result FROM game.game_receipts "
        "WHERE idempotency_key = %s",
        (idempotency_key,),
        connection=connection,
    )
    if row is None:
        return None
    if str(row[0]) != operation or int(row[1]) != user_id:
        raise ValueError("Omikuji idempotency key changed meaning")
    return _json_object(row[2])


async def _save_receipt(
    idempotency_key: str,
    operation: str,
    user_id: int,
    result: Mapping[str, object],
    connection: AsyncConnection,
) -> None:
    """@brief 在同一短事务保存御神签回执 / Save an Omikuji receipt in the same short transaction.

    @param idempotency_key 幂等键 / Idempotency key.
    @param operation 固定操作名 / Fixed operation name.
    @param user_id 业务用户 / Business user.
    @param result 版本化结果 / Versioned result.
    @param connection 当前事务连接 / Current transaction connection.
    @return None / None.
    """

    await db.execute(
        "INSERT INTO game.game_receipts "
        "(idempotency_key, operation, user_id, result) VALUES (%s, %s, %s, CAST(%s AS JSONB))",
        (idempotency_key, operation, user_id, _encode_json(result)),
        connection=connection,
    )


def _encode_json(value: Mapping[str, object]) -> str:
    """@brief 编码稳定 JSON 回执 / Encode a stable JSON receipt.

    @param value JSON 对象 / JSON object.
    @return 紧凑、稳定文本 / Compact stable text.
    """

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_object(value: object) -> dict[str, Any]:
    """@brief 将驱动 JSONB 值规范为字典 / Normalize a driver JSONB value to a dictionary.

    @param value 驱动返回的 JSONB 值 / Driver-returned JSONB value.
    @return JSON 字典 / JSON dictionary.
    @raise ValueError 值不是对象时抛出 / Raised when the value is not an object.
    """

    decoded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, Mapping):
        raise ValueError("Persisted Omikuji JSON must be an object")
    return {str(key): item for key, item in decoded.items()}


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
    value: Mapping[str, Any],
    *,
    replayed: bool,
) -> OmikujiResult:
    """@brief 解析御神签回执 / Parse an Omikuji receipt.

    @param value 版本化 JSON / Versioned JSON.
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
