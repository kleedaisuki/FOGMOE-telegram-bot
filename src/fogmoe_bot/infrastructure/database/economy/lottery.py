"""@brief PostgreSQL 每日抽奖适配器 / PostgreSQL daily-lottery adapter."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast

from fogmoe_bot.application.economy.common import EconomyCode
from fogmoe_bot.application.economy.lottery import (
    LotteryCommand,
    LotteryOperations,
    LotteryResult,
)
from fogmoe_bot.domain.banking.ledger import LedgerAccount, LedgerReason
from fogmoe_bot.domain.banking.money import (
    SystemAccountKind,
    TokenAmount,
    TokenBucket,
)
from fogmoe_bot.infrastructure.database import db
from fogmoe_bot.infrastructure.database.banking import post_bank_transfer

from .common import (
    _load_result,
    _lock_operation_key,
    _registered_user_exists,
    _save_result,
)


class PostgresLotteryOperations(LotteryOperations):
    """@brief 以抽奖状态和银行账本串行化每日领取 / Serialize daily claims through lottery state and the bank ledger."""

    async def claim_lottery(self, command: LotteryCommand) -> LotteryResult:
        """@brief 以抽奖状态和账本原子串行化领取 / Serialize a lottery claim through lottery state and the ledger.

        @param command 抽奖命令 / Lottery command.
        @return 稳定、可回放结果 / Stable replayable result.
        """

        operation_kind = "lottery_claim"
        async with db.transaction() as connection:
            await _lock_operation_key(command.idempotency_key, connection)
            await _lock_operation_key(f"lottery:{command.user_id}", connection)
            if not await _registered_user_exists(command.user_id, connection):
                return LotteryResult(EconomyCode.NOT_REGISTERED)
            replay = await _load_result(
                command.idempotency_key,
                connection,
                expected_kind=operation_kind,
                expected_user_id=command.user_id,
            )
            if replay is not None:
                return _lottery_from_mapping(replay, replayed=True)

            row = await db.fetch_one(
                "SELECT last_lottery_date FROM economy.user_lottery "
                "WHERE user_id = %s FOR UPDATE",
                (command.user_id,),
                connection=connection,
            )
            claimed_at = _as_utc(command.claimed_at)
            last_claimed_at = (
                _as_utc(cast(datetime, row[0]))
                if row is not None and row[0] is not None
                else None
            )
            next_eligible = (
                last_claimed_at + command.cooldown
                if last_claimed_at is not None
                else None
            )
            if next_eligible is not None and claimed_at < next_eligible:
                result = LotteryResult(
                    EconomyCode.ALREADY_CLAIMED,
                    next_eligible_at=next_eligible,
                )
            else:
                await post_bank_transfer(
                    namespace="economy-lottery-reward",
                    source_idempotency_key=command.idempotency_key,
                    reason=LedgerReason.BANK_ISSUANCE,
                    source=LedgerAccount.system(SystemAccountKind.ISSUANCE),
                    destination=LedgerAccount.user(command.user_id, TokenBucket.FREE),
                    amount=TokenAmount(command.prize),
                    created_at=claimed_at,
                    actor_id=command.user_id,
                    connection=connection,
                    metadata={"grant_kind": "daily_lottery"},
                )
                await db.execute(
                    "INSERT INTO economy.user_lottery (user_id, last_lottery_date) "
                    "VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET "
                    "last_lottery_date = EXCLUDED.last_lottery_date",
                    (command.user_id, claimed_at.replace(tzinfo=None)),
                    connection=connection,
                )
                result = LotteryResult(
                    EconomyCode.SUCCESS,
                    prize=command.prize,
                    next_eligible_at=claimed_at + command.cooldown,
                )
            await _save_result(
                command.idempotency_key,
                operation_kind,
                command.user_id,
                _lottery_mapping(result),
                connection,
            )
            return result


def _lottery_mapping(result: LotteryResult) -> dict[str, object]:
    """@brief 序列化抽奖回执 / Serialize a lottery receipt.

    @param result 抽奖结果 / Lottery result.
    @return JSON mapping / JSON mapping.
    """

    return {
        "code": result.code.value,
        "prize": result.prize,
        "next_eligible_at": (
            result.next_eligible_at.isoformat()
            if result.next_eligible_at is not None
            else None
        ),
    }


def _lottery_from_mapping(
    value: Mapping[str, Any],
    *,
    replayed: bool,
) -> LotteryResult:
    """@brief 从回执恢复抽奖结果 / Restore a lottery result from a receipt.

    @param value 回执映射 / Receipt mapping.
    @param replayed 是否标记回放 / Whether to mark the result as replayed.
    @return 抽奖结果 / Lottery result.
    """

    raw_next = value.get("next_eligible_at")
    next_eligible = (
        datetime.fromisoformat(str(raw_next)) if raw_next is not None else None
    )
    return LotteryResult(
        code=EconomyCode(str(value["code"])),
        prize=int(value.get("prize", 0)),
        next_eligible_at=next_eligible,
        replayed=replayed,
    )


def _as_utc(value: datetime) -> datetime:
    """@brief 将数据库或命令时间规范为 aware UTC / Normalize a database or command timestamp to aware UTC.

    @param value naive UTC 或 aware 时间 / Naive UTC or aware timestamp.
    @return aware UTC / Aware UTC.
    """

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = ["PostgresLotteryOperations"]
