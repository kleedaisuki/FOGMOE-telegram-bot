"""@brief PostgreSQL 签到与抽奖适配器 / PostgreSQL check-in and lottery adapter."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

from fogmoe_bot.application.economy.common import EconomyCode
from fogmoe_bot.application.economy.rewards import (
    CheckInCommand,
    CheckInResult,
    LotteryCommand,
    LotteryResult,
    RewardOperations,
    calculate_checkin_reward,
)
from fogmoe_bot.infrastructure.database import connection as db_connection

from .common import _credit_free, _load_result, _lock_account, _save_result


class PostgresRewardOperations(RewardOperations):
    """@brief 以账户锁串行化签到与抽奖奖励 / Serialize check-in and lottery rewards with account locks."""

    async def check_in(self, command: CheckInCommand) -> CheckInResult:
        """@brief 以账户行串行化签到与奖励 / Serialize check-in and reward on the account row.

        @param command 签到命令 / Check-in command.
        @return 签到结果 / Check-in result.
        """

        async with db_connection.transaction() as connection:
            account = await _lock_account(command.user_id, connection)
            if account is None:
                return CheckInResult(EconomyCode.NOT_REGISTERED)
            replay = await _load_result(command.idempotency_key, connection)
            if replay is not None:
                return CheckInResult(
                    EconomyCode(str(replay["code"])),
                    consecutive_days=int(replay.get("consecutive_days", 0)),
                    reward=int(replay.get("reward", 0)),
                    replayed=True,
                )
            row = await db_connection.fetch_one(
                "SELECT last_checkin_date, consecutive_days "
                "FROM economy.user_checkin WHERE user_id = %s FOR UPDATE",
                (command.user_id,),
                connection=connection,
            )
            if row is not None and cast(date, row[0]) == command.day:
                result = CheckInResult(
                    EconomyCode.ALREADY_CLAIMED,
                    consecutive_days=cast(int, row[1]),
                )
                await _save_result(
                    command.idempotency_key,
                    "check_in",
                    command.user_id,
                    _checkin_mapping(result),
                    connection,
                )
                return result
            consecutive = 1
            if row is not None and cast(date, row[0]) == command.day - timedelta(
                days=1
            ):
                consecutive = cast(int, row[1]) + 1
            reward = calculate_checkin_reward(consecutive)
            await db_connection.execute(
                "INSERT INTO economy.user_checkin "
                "(user_id, last_checkin_date, consecutive_days) VALUES (%s, %s, %s) "
                "ON CONFLICT (user_id) DO UPDATE SET "
                "last_checkin_date = EXCLUDED.last_checkin_date, "
                "consecutive_days = EXCLUDED.consecutive_days, "
                "updated_at = CURRENT_TIMESTAMP",
                (command.user_id, command.day, consecutive),
                connection=connection,
            )
            await _credit_free(command.user_id, reward, connection)
            result = CheckInResult(
                EconomyCode.SUCCESS,
                consecutive_days=consecutive,
                reward=reward,
            )
            await _save_result(
                command.idempotency_key,
                "check_in",
                command.user_id,
                _checkin_mapping(result),
                connection,
            )
            return result

    async def claim_lottery(self, command: LotteryCommand) -> LotteryResult:
        """@brief 以账户行串行化抽奖与奖励 / Serialize a lottery claim and reward on the account row.

        @param command 抽奖命令 / Lottery command.
        @return 稳定、可回放结果 / Stable replayable result.
        """

        operation_kind = "lottery_claim"
        async with db_connection.transaction() as connection:
            if await _lock_account(command.user_id, connection) is None:
                return LotteryResult(EconomyCode.NOT_REGISTERED)
            replay = await _load_result(
                command.idempotency_key,
                connection,
                expected_kind=operation_kind,
                expected_user_id=command.user_id,
            )
            if replay is not None:
                return _lottery_from_mapping(replay, replayed=True)

            row = await db_connection.fetch_one(
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
                await _credit_free(command.user_id, command.prize, connection)
                await db_connection.execute(
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


def _checkin_mapping(result: CheckInResult) -> dict[str, object]:
    """@brief 序列化签到结果 / Serialize a check-in result.

    @param result 签到结果 / Check-in result.
    @return JSON mapping / JSON mapping.
    """

    return {
        "code": result.code.value,
        "consecutive_days": result.consecutive_days,
        "reward": result.reward,
    }


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
