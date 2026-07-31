"""@brief PostgreSQL 签到聚合映射器 / PostgreSQL mapper for the check-in aggregate."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, time
from typing import Any, Final, cast

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.economy.check_in import (
    CheckInCommand,
    CheckInOperations,
    CheckInResult,
)
from fogmoe_bot.application.economy.common import EconomyCode
from fogmoe_bot.domain.banking.ledger import LedgerAccount, LedgerReason
from fogmoe_bot.domain.banking.money import (
    SystemAccountKind,
    TokenAmount,
    TokenBucket,
)
from fogmoe_bot.domain.economy.check_in import (
    CheckInAlreadyClaimed,
    CheckInGranted,
    CheckInStreak,
)
from fogmoe_bot.infrastructure.database import db
from fogmoe_bot.infrastructure.database.banking import post_bank_transfer

from .common import (
    _load_result,
    _lock_operation_key,
    _registered_user_exists,
    _save_result,
)

_OPERATION_KIND: Final = "check_in"
"""@brief 持久化回执中的签到操作类型 / Check-in operation kind stored in receipts."""


class PostgresCheckInOperations(CheckInOperations):
    """@brief 在一个事务中映射签到聚合、账本奖励与幂等回执 /
    Map the check-in aggregate, ledger reward, and idempotency receipt in one transaction.
    """

    async def check_in(self, command: CheckInCommand) -> CheckInResult:
        """@brief 载入聚合并由领域行为决定持久化转换 / Load the aggregate and let domain behavior decide the transition.

        @param command 已验证签到命令 / Validated check-in command.
        @return 稳定且可回放的签到结果 / Stable replayable check-in result.
        """

        account_id = int(command.account_id)
        async with db.transaction() as connection:
            await _lock_operation_key(command.idempotency_key, connection)
            await _lock_operation_key(
                f"checkin:{account_id}:{command.day.isoformat()}",
                connection,
            )
            if not await _registered_user_exists(account_id, connection):
                return CheckInResult(code=EconomyCode.NOT_REGISTERED)

            replay = await _load_result(command.idempotency_key, connection)
            if replay is not None:
                return _result_from_mapping(replay, replayed=True)

            row = await db.fetch_one(
                "SELECT last_checkin_date, consecutive_days "
                "FROM economy.user_checkin WHERE user_id = %s FOR UPDATE",
                (account_id,),
                connection=connection,
            )
            streak = (
                CheckInStreak(command.account_id)
                if row is None
                else CheckInStreak.restore(
                    account_id=command.account_id,
                    last_claimed_on=cast(date, row[0]),
                    consecutive_days=cast(int, row[1]),
                )
            )
            decision = streak.claim(command.day)
            if isinstance(decision, CheckInAlreadyClaimed):
                result = CheckInResult(
                    code=EconomyCode.ALREADY_CLAIMED,
                    consecutive_days=decision.streak.days,
                )
                await _save_result(
                    command.idempotency_key,
                    _OPERATION_KIND,
                    account_id,
                    _result_mapping(result),
                    connection,
                )
                return result

            await _persist_grant(account_id, command, decision, connection)
            result = CheckInResult(
                code=EconomyCode.SUCCESS,
                consecutive_days=decision.streak.days,
                reward=decision.reward.coins,
            )
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
    command: CheckInCommand,
    decision: CheckInGranted,
    connection: AsyncConnection,
) -> None:
    """@brief 将已接受领域决策映射为状态与账本事实 / Map an accepted domain decision to state and ledger facts.

    @param account_id 数据库账户 ID / Database account ID.
    @param command 当前应用命令 / Current application command.
    @param decision 已接受领域决策 / Accepted domain decision.
    @param connection 当前数据库事务 / Current database transaction.
    @return None / None.
    """

    await db.execute(
        "INSERT INTO economy.user_checkin "
        "(user_id, last_checkin_date, consecutive_days) VALUES (%s, %s, %s) "
        "ON CONFLICT (user_id) DO UPDATE SET "
        "last_checkin_date = EXCLUDED.last_checkin_date, "
        "consecutive_days = EXCLUDED.consecutive_days, "
        "updated_at = CURRENT_TIMESTAMP",
        (account_id, decision.claimed_on, decision.streak.days),
        connection=connection,
    )
    await post_bank_transfer(
        namespace="economy-checkin-reward",
        source_idempotency_key=command.idempotency_key,
        reason=LedgerReason.BANK_ISSUANCE,
        source=LedgerAccount.system(SystemAccountKind.ISSUANCE),
        destination=LedgerAccount.user(account_id, TokenBucket.FREE),
        amount=TokenAmount(decision.reward.coins),
        created_at=datetime.combine(decision.claimed_on, time.min, tzinfo=UTC),
        actor_id=account_id,
        connection=connection,
        metadata={
            "grant_kind": "checkin",
            "consecutive_days": decision.streak.days,
        },
    )


def _result_mapping(result: CheckInResult) -> dict[str, object]:
    """@brief 序列化签到结果 / Serialize a check-in result.

    @param result 签到结果 / Check-in result.
    @return JSON mapping / JSON mapping.
    """

    return {
        "code": result.code.value,
        "consecutive_days": result.consecutive_days,
        "reward": result.reward,
    }


def _result_from_mapping(
    value: Mapping[str, Any],
    *,
    replayed: bool,
) -> CheckInResult:
    """@brief 从已提交回执恢复签到结果 / Restore a check-in result from a committed receipt.

    @param value 回执映射 / Receipt mapping.
    @param replayed 是否标记为回放 / Whether to mark the result as replayed.
    @return 经过结果形状验证的签到结果 / Check-in result validated for a legal shape.
    """

    return CheckInResult(
        code=EconomyCode(str(value["code"])),
        consecutive_days=int(value.get("consecutive_days", 0)),
        reward=int(value.get("reward", 0)),
        replayed=replayed,
    )


__all__ = ["PostgresCheckInOperations"]
