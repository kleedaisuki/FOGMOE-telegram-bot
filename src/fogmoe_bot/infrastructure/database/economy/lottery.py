"""@brief PostgreSQL 每日抽奖聚合映射器 / PostgreSQL mapper for the daily-lottery aggregate."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final, assert_never, cast

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.economy.common import EconomyCode
from fogmoe_bot.application.economy.lottery import (
    ClaimLotteryCommand,
    LotteryAlreadyClaimedResult,
    LotteryClaimTransaction,
    LotteryGrantedResult,
    LotteryNotRegisteredResult,
    LotteryResult,
)
from fogmoe_bot.domain.banking.ledger import LedgerAccount, LedgerReason
from fogmoe_bot.domain.banking.money import (
    SystemAccountKind,
    TokenAmount,
    TokenBucket,
)
from fogmoe_bot.domain.economy.lottery import (
    DailyLottery,
    LotteryAlreadyClaimed,
    LotteryClaimInstant,
    LotteryGranted,
    LotteryPrize,
)
from fogmoe_bot.infrastructure.database import db
from fogmoe_bot.infrastructure.database.banking import post_bank_transfer

from .common import (
    _load_result,
    _lock_operation_key,
    _registered_user_exists,
    _save_result,
)

_OPERATION_KIND: Final = "lottery_claim"
"""@brief receipt 中稳定的每日抽奖操作类型 / Stable daily-lottery operation kind in receipts."""


class PostgresLotteryClaimTransaction(LotteryClaimTransaction):
    """@brief 在一个事务中映射抽奖聚合、账本发放与幂等回执 /
    Map the daily-lottery aggregate, ledger grant, and idempotency receipt in one transaction.
    """

    async def claim_lottery(self, command: ClaimLotteryCommand) -> LotteryResult:
        """@brief 载入聚合并仅映射领域决策 / Load the aggregate and only map its domain decision.

        @param command 已验证领取命令 / Validated claim command.
        @return 稳定且可重放的用例结果 / Stable replayable use-case result.
        """

        account_id = int(command.account_id)
        async with db.transaction() as connection:
            await _lock_operation_key(command.idempotency_key, connection)
            await _lock_operation_key(f"lottery:{account_id}", connection)
            if not await _registered_user_exists(account_id, connection):
                return LotteryNotRegisteredResult()

            replay = await _load_result(
                command.idempotency_key,
                connection,
                expected_kind=_OPERATION_KIND,
                expected_user_id=account_id,
            )
            if replay is not None:
                return _result_from_mapping(replay, replayed=True)

            row = await db.fetch_one(
                "SELECT last_lottery_date FROM economy.user_lottery "
                "WHERE user_id = %s FOR UPDATE",
                (account_id,),
                connection=connection,
            )
            lottery = (
                DailyLottery(command.account_id)
                if row is None or row[0] is None
                else DailyLottery.restore(
                    account_id=command.account_id,
                    last_claimed_at=LotteryClaimInstant(cast(datetime, row[0])),
                )
            )
            decision = lottery.claim(
                claimed_at=command.claimed_at,
                proposed_prize=command.proposed_prize,
            )
            match decision:
                case LotteryAlreadyClaimed():
                    result: LotteryResult = LotteryAlreadyClaimedResult(
                        next_eligible_at=decision.next_eligible_at,
                    )
                case LotteryGranted():
                    await _persist_grant(account_id, command, decision, connection)
                    result = LotteryGrantedResult(
                        prize=decision.prize,
                        next_eligible_at=decision.next_eligible_at,
                    )
                case unreachable:
                    assert_never(unreachable)

            await _save_result(
                command.idempotency_key,
                _OPERATION_KIND,
                account_id,
                _result_mapping(result),
                connection,
            )
            return result


async def _persist_grant(
    account_id: int,
    command: ClaimLotteryCommand,
    decision: LotteryGranted,
    connection: AsyncConnection,
) -> None:
    """@brief 将授予决策映射为账本与聚合快照 / Map a grant decision to the ledger and aggregate snapshot.

    @param account_id 数据库账户 ID / Database account identifier.
    @param command 当前应用命令 / Current application command.
    @param decision 已接受领域决策 / Accepted domain decision.
    @param connection 当前数据库事务 / Current database transaction.
    @return None / None.
    """

    await post_bank_transfer(
        namespace="economy-lottery-reward",
        source_idempotency_key=command.idempotency_key,
        reason=LedgerReason.BANK_ISSUANCE,
        source=LedgerAccount.system(SystemAccountKind.ISSUANCE),
        destination=LedgerAccount.user(account_id, TokenBucket.FREE),
        amount=TokenAmount(int(decision.prize)),
        created_at=decision.claimed_at.value,
        actor_id=account_id,
        connection=connection,
        metadata={"grant_kind": "daily_lottery"},
    )
    await db.execute(
        "INSERT INTO economy.user_lottery (user_id, last_lottery_date) "
        "VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET "
        "last_lottery_date = EXCLUDED.last_lottery_date",
        (account_id, decision.claimed_at.value.replace(tzinfo=None)),
        connection=connection,
    )


def _result_mapping(result: LotteryResult) -> dict[str, object]:
    """@brief 按既有 JSON 形状序列化抽奖回执 / Serialize a lottery receipt using its established JSON shape.

    @param result 抽奖用例结果 / Lottery use-case result.
    @return 与既有 schema 兼容的 JSON mapping / JSON mapping compatible with the established schema.
    """

    match result:
        case LotteryGrantedResult():
            return {
                "code": EconomyCode.SUCCESS.value,
                "prize": int(result.prize),
                "next_eligible_at": result.next_eligible_at.value.isoformat(),
            }
        case LotteryAlreadyClaimedResult():
            return {
                "code": EconomyCode.ALREADY_CLAIMED.value,
                "prize": 0,
                "next_eligible_at": result.next_eligible_at.value.isoformat(),
            }
        case LotteryNotRegisteredResult():
            return {
                "code": EconomyCode.NOT_REGISTERED.value,
                "prize": 0,
                "next_eligible_at": None,
            }
        case unreachable:
            assert_never(unreachable)


def _result_from_mapping(
    value: Mapping[str, Any],
    *,
    replayed: bool,
) -> LotteryResult:
    """@brief 从既有 JSON 形状恢复抽奖结果 / Restore a lottery result from its established JSON shape.

    @param value 回执 mapping / Receipt mapping.
    @param replayed 是否标记为回放 / Whether to mark the result as replayed.
    @return 经过类型不变量验证的用例结果 / Use-case result validated by type invariants.
    """

    code = EconomyCode(str(value["code"]))
    raw_next = value.get("next_eligible_at")
    if code is EconomyCode.SUCCESS:
        if raw_next is None:
            raise ValueError("Successful lottery receipt requires next eligibility")
        return LotteryGrantedResult(
            prize=LotteryPrize(int(value["prize"])),
            next_eligible_at=LotteryClaimInstant(datetime.fromisoformat(str(raw_next))),
            replayed=replayed,
        )
    if code is EconomyCode.ALREADY_CLAIMED:
        if raw_next is None:
            raise ValueError("Repeated lottery receipt requires next eligibility")
        return LotteryAlreadyClaimedResult(
            next_eligible_at=LotteryClaimInstant(datetime.fromisoformat(str(raw_next))),
            replayed=replayed,
        )
    if code is EconomyCode.NOT_REGISTERED:
        return LotteryNotRegisteredResult(replayed=replayed)
    raise ValueError("Unsupported lottery receipt code")


__all__ = ["PostgresLotteryClaimTransaction"]
