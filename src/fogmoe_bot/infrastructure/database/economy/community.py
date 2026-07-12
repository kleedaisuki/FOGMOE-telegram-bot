"""@brief PostgreSQL 赠送、榜单与任务适配器 / PostgreSQL gift, leaderboard, and task adapter."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

from fogmoe_bot.application.economy.common import EconomyCode
from fogmoe_bot.application.economy.community import (
    CommunityOperations,
    GiftCommand,
    GiftResult,
    LeaderboardCommand,
    LeaderboardEntry,
    LeaderboardResult,
    TaskClaimCommand,
    TaskClaimResult,
)
from fogmoe_bot.domain.economy import AccountBalance
from fogmoe_bot.infrastructure.database import connection as db_connection

from .common import (
    _credit_free,
    _load_result,
    _lock_account,
    _plan_after_spend,
    _save_result,
)


class PostgresCommunityOperations(CommunityOperations):
    """@brief 执行经济社区事务 / Execute economy community transactions."""

    def __init__(self, *, admin_user_id: int) -> None:
        """@brief 注入管理员身份 / Inject administrator identity.

        @param admin_user_id 管理员用户 ID / Administrator user ID.
        """

        self._admin_user_id = admin_user_id

    async def give(self, command: GiftCommand) -> GiftResult:
        """@brief 以排序账户锁原子赠送金币 / Atomically gift coins with sorted account locks.

        @param command 赠送命令 / Gift command.
        @return 稳定、可回放结果 / Stable replayable result.
        @note 锁顺序固定为 account ID 升序，随后才锁每日计数行。/ Accounts are locked in
            ascending ID order before the daily-counter row.
        """

        operation_kind = "coin_gift"
        async with db_connection.transaction() as connection:
            target = await db_connection.fetch_one(
                "SELECT id, name FROM identity.users WHERE name = %s "
                "ORDER BY id LIMIT 1",
                (command.target_name,),
                connection=connection,
            )
            target_id = cast(int, target[0]) if target is not None else None
            account_ids = (
                (command.sender_id,)
                if target_id is None
                else tuple(sorted({command.sender_id, target_id}))
            )
            locked_rows = await db_connection.fetch_all(
                "SELECT id, coins, coins_paid, user_plan, name "
                "FROM identity.users WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
                (list(account_ids),),
                connection=connection,
            )
            locked = {cast(int, row[0]): row for row in locked_rows}
            sender_row = locked.get(command.sender_id)
            if sender_row is None:
                return GiftResult(EconomyCode.NOT_REGISTERED)
            replay = await _load_result(
                command.idempotency_key,
                connection,
                expected_kind=operation_kind,
                expected_user_id=command.sender_id,
            )
            if replay is not None:
                return _gift_from_mapping(command, replay, replayed=True)

            sender = AccountBalance(
                command.sender_id,
                cast(int, sender_row[1]),
                cast(int, sender_row[2]),
                cast(str, sender_row[3]),
            )
            if target_id is None:
                result = GiftResult(
                    EconomyCode.NOT_FOUND,
                    target_name=command.target_name,
                    available=sender.total,
                )
            elif target_id == command.sender_id:
                result = GiftResult(
                    EconomyCode.SELF_TRANSFER,
                    target_name=cast(str, sender_row[4]),
                    available=sender.total,
                )
            else:
                target_row = locked.get(target_id)
                if target_row is None:
                    raise RuntimeError("Gift target disappeared while acquiring locks")
                counter = await db_connection.fetch_one(
                    "SELECT give_count FROM economy.user_give_daily "
                    "WHERE user_id = %s AND give_date = %s FOR UPDATE",
                    (command.sender_id, command.business_date),
                    connection=connection,
                )
                count = cast(int, counter[0]) if counter is not None else 0
                total_cost = command.amount + command.fee
                charged = sender.spend(total_cost)
                if count >= command.daily_limit:
                    result = GiftResult(
                        EconomyCode.DAILY_LIMIT,
                        target_name=cast(str, target_row[4]),
                        available=sender.total,
                    )
                elif charged is None:
                    result = GiftResult(
                        EconomyCode.INSUFFICIENT_COINS,
                        target_name=cast(str, target_row[4]),
                        amount=command.amount,
                        fee=command.fee,
                        available=sender.total,
                    )
                else:
                    await db_connection.execute(
                        "UPDATE identity.users SET coins = %s, coins_paid = %s, "
                        "user_plan = %s WHERE id = %s",
                        (
                            charged.free,
                            charged.paid,
                            _plan_after_spend(
                                command.sender_id,
                                charged.paid,
                                self._admin_user_id,
                            ),
                            command.sender_id,
                        ),
                        connection=connection,
                    )
                    await _credit_free(target_id, command.amount, connection)
                    await db_connection.execute(
                        "INSERT INTO economy.user_give_daily "
                        "(user_id, give_date, give_count) VALUES (%s, %s, 1) "
                        "ON CONFLICT (user_id, give_date) DO UPDATE SET "
                        "give_count = economy.user_give_daily.give_count + 1",
                        (command.sender_id, command.business_date),
                        connection=connection,
                    )
                    result = GiftResult(
                        EconomyCode.SUCCESS,
                        target_name=cast(str, target_row[4]),
                        amount=command.amount,
                        fee=command.fee,
                        available=sender.total,
                    )
            await _save_result(
                command.idempotency_key,
                operation_kind,
                command.sender_id,
                _gift_mapping(command, result),
                connection,
            )
            return result

    async def leaderboard(self, command: LeaderboardCommand) -> LeaderboardResult:
        """@brief 读取并冻结排行榜快照 / Read and freeze a leaderboard snapshot.

        @param command 排行榜命令 / Leaderboard command.
        @return 可重放快照 / Replayable snapshot.
        """

        operation_kind = "coin_leaderboard"
        async with db_connection.transaction() as connection:
            if await _lock_account(command.requester_id, connection) is None:
                return LeaderboardResult(EconomyCode.NOT_REGISTERED)
            replay = await _load_result(
                command.idempotency_key,
                connection,
                expected_kind=operation_kind,
                expected_user_id=command.requester_id,
            )
            if replay is not None:
                return _leaderboard_from_mapping(command, replay, replayed=True)
            rows = await db_connection.fetch_all(
                "SELECT name, coins + coins_paid AS total FROM identity.users "
                "ORDER BY total DESC, id ASC LIMIT %s",
                (command.limit,),
                connection=connection,
            )
            result = LeaderboardResult(
                EconomyCode.SUCCESS,
                entries=tuple(
                    LeaderboardEntry(
                        name=cast(str, row[0]),
                        coins=cast(int, row[1]),
                    )
                    for row in rows
                ),
            )
            await _save_result(
                command.idempotency_key,
                operation_kind,
                command.requester_id,
                _leaderboard_mapping(command, result),
                connection,
            )
            return result

    async def claim_task(self, command: TaskClaimCommand) -> TaskClaimResult:
        """@brief 在一个事务中记录任务与发奖 / Record task completion and grant reward in one transaction.

        @param command 任务命令 / Task command.
        @return 领取结果 / Claim result.
        """

        async with db_connection.transaction() as connection:
            if await _lock_account(command.user_id, connection) is None:
                return TaskClaimResult(EconomyCode.NOT_REGISTERED)
            replay = await _load_result(command.idempotency_key, connection)
            if replay is not None:
                return TaskClaimResult(
                    EconomyCode(str(replay["code"])),
                    reward=int(replay.get("reward", 0)),
                )
            inserted = await db_connection.execute(
                "INSERT INTO economy.user_task (user_id, task_id) VALUES (%s, %s) "
                "ON CONFLICT (user_id, task_id) DO NOTHING",
                (command.user_id, command.task_id),
                connection=connection,
            )
            result = TaskClaimResult(EconomyCode.ALREADY_CLAIMED)
            if inserted == 1:
                await _credit_free(command.user_id, command.reward, connection)
                result = TaskClaimResult(EconomyCode.SUCCESS, command.reward)
            await _save_result(
                command.idempotency_key,
                "task_claim",
                command.user_id,
                {"code": result.code.value, "reward": result.reward},
                connection,
            )
            return result


def _gift_mapping(
    command: GiftCommand,
    result: GiftResult,
) -> dict[str, object]:
    """@brief 序列化赠送回执及命令语义 / Serialize a gift receipt and its command semantics.

    @param command 原命令 / Original command.
    @param result 赠送结果 / Gift result.
    @return JSON mapping / JSON mapping.
    """

    return {
        "code": result.code.value,
        "target_name": result.target_name,
        "requested_target": command.target_name,
        "amount": result.amount,
        "requested_amount": command.amount,
        "fee": result.fee,
        "requested_fee": command.fee,
        "available": result.available,
    }


def _gift_from_mapping(
    command: GiftCommand,
    value: Mapping[str, Any],
    *,
    replayed: bool,
) -> GiftResult:
    """@brief 校验命令语义并恢复赠送回执 / Validate command semantics and restore a gift receipt.

    @param command 本次命令 / Current command.
    @param value 回执映射 / Receipt mapping.
    @param replayed 是否标记回放 / Whether to mark replay.
    @return 赠送结果 / Gift result.
    @raise ValueError 同一幂等键改变目标、金额或手续费 / The same key changes target, amount, or fee.
    """

    if (
        str(value.get("requested_target", "")) != command.target_name
        or int(value.get("requested_amount", -1)) != command.amount
        or int(value.get("requested_fee", -1)) != command.fee
    ):
        raise ValueError("Coin-gift idempotency key changed command semantics")
    raw_name = value.get("target_name")
    return GiftResult(
        code=EconomyCode(str(value["code"])),
        target_name=str(raw_name) if raw_name is not None else None,
        amount=int(value.get("amount", 0)),
        fee=int(value.get("fee", 0)),
        available=int(value.get("available", 0)),
        replayed=replayed,
    )


def _leaderboard_mapping(
    command: LeaderboardCommand,
    result: LeaderboardResult,
) -> dict[str, object]:
    """@brief 序列化排行榜快照 / Serialize a leaderboard snapshot.

    @param command 原命令 / Original command.
    @param result 快照结果 / Snapshot result.
    @return JSON mapping / JSON mapping.
    """

    return {
        "code": result.code.value,
        "limit": command.limit,
        "entries": [
            {"name": entry.name, "coins": entry.coins} for entry in result.entries
        ],
    }


def _leaderboard_from_mapping(
    command: LeaderboardCommand,
    value: Mapping[str, Any],
    *,
    replayed: bool,
) -> LeaderboardResult:
    """@brief 校验 limit 并恢复排行榜快照 / Validate the limit and restore a leaderboard snapshot.

    @param command 当前命令 / Current command.
    @param value 回执映射 / Receipt mapping.
    @param replayed 是否回放 / Whether replayed.
    @return 排行榜结果 / Leaderboard result.
    """

    if int(value.get("limit", -1)) != command.limit:
        raise ValueError("Leaderboard idempotency key changed command semantics")
    raw_entries = value.get("entries")
    if not isinstance(raw_entries, Sequence):
        raise ValueError("Invalid leaderboard receipt entries")
    entries: list[LeaderboardEntry] = []
    """@brief 已校验 receipt entries / Validated receipt entries."""
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, Mapping):
            raise ValueError("Invalid leaderboard receipt entry")
        entries.append(
            LeaderboardEntry(
                name=str(raw_entry["name"]),
                coins=int(raw_entry["coins"]),
            )
        )
    return LeaderboardResult(
        EconomyCode(str(value["code"])),
        entries=tuple(entries),
        replayed=replayed,
    )
