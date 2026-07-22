"""@brief PostgreSQL 赠送、榜单与任务适配器 / PostgreSQL gift, leaderboard, and task adapter."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
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
from fogmoe_bot.domain.banking.ledger import LedgerAccount, LedgerReason
from fogmoe_bot.domain.banking.money import (
    SystemAccountKind,
    TokenAmount,
    TokenBucket,
)
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database.banking import (
    lock_bank_account_balances,
    post_bank_transfer,
)

from .common import (
    _load_result,
    _lock_operation_key,
    _registered_user_exists,
    _save_result,
)


class PostgresCommunityOperations(CommunityOperations):
    """@brief 通过银行账本执行社区金币事务 / Execute community-token transactions through the bank ledger."""

    async def give(self, command: GiftCommand) -> GiftResult:
        """@brief 以银行钱包稳定锁序原子赠送金币 / Atomically gift tokens in the bank wallet's stable lock order.

        @param command 赠送命令 / Gift command.
        @return 稳定、可回放结果 / Stable replayable result.
        @note 余额只读取 ``bank.account_balances``。身份表只提供用户名和账户存在性，
            且从不先锁身份行，避免与账本投影的用户行更新形成死锁环。/
            Balances are read only from ``bank.account_balances``.  The identity table supplies
            names and existence only and is never locked first, preventing a deadlock cycle with
            the ledger projection's user-row update.
        """

        operation_kind = "coin_gift"
        async with db_connection.transaction() as connection:
            await _lock_operation_key(command.idempotency_key, connection)
            target = await db_connection.fetch_one(
                "SELECT id, name FROM identity.users WHERE name = %s "
                "ORDER BY id LIMIT 1",
                (command.target_name,),
                connection=connection,
            )
            target_id = cast(int, target[0]) if target is not None else None
            if not await _registered_user_exists(command.sender_id, connection):
                return GiftResult(EconomyCode.NOT_REGISTERED)
            replay = await _load_result(
                command.idempotency_key,
                connection,
                expected_kind=operation_kind,
                expected_user_id=command.sender_id,
            )
            if replay is not None:
                return _gift_from_mapping(command, replay, replayed=True)

            if target_id is None:
                result = GiftResult(
                    EconomyCode.NOT_FOUND,
                    target_name=command.target_name,
                    available=0,
                )
            elif target_id == command.sender_id:
                assert target is not None
                result = GiftResult(
                    EconomyCode.SELF_TRANSFER,
                    target_name=cast(str, target[1]),
                    available=0,
                )
            else:
                assert target is not None
                sender_wallet = LedgerAccount.user(command.sender_id, TokenBucket.FREE)
                """@brief 赠送者唯一可消费的免费钱包 / Sender's sole spendable free wallet."""
                target_wallet = LedgerAccount.user(target_id, TokenBucket.FREE)
                """@brief 收款人的免费钱包 / Recipient's free wallet."""
                balances = await lock_bank_account_balances(
                    (sender_wallet, target_wallet),
                    connection,
                )
                sender_free = balances[sender_wallet]
                """@brief 账本稳定锁下的赠送者可用免费金币 / Sender free balance under the stable ledger lock."""
                counter = await db_connection.fetch_one(
                    "SELECT give_count FROM economy.user_give_daily "
                    "WHERE user_id = %s AND give_date = %s FOR UPDATE",
                    (command.sender_id, command.business_date),
                    connection=connection,
                )
                count = cast(int, counter[0]) if counter is not None else 0
                total_cost = command.amount + command.fee
                if count >= command.daily_limit:
                    result = GiftResult(
                        EconomyCode.DAILY_LIMIT,
                        target_name=cast(str, target[1]),
                        available=sender_free,
                    )
                elif sender_free < total_cost:
                    result = GiftResult(
                        EconomyCode.INSUFFICIENT_COINS,
                        target_name=cast(str, target[1]),
                        amount=command.amount,
                        fee=command.fee,
                        available=sender_free,
                    )
                else:
                    await post_bank_transfer(
                        namespace="economy-gift-transfer",
                        source_idempotency_key=command.idempotency_key,
                        reason=LedgerReason.USER_TRANSFER,
                        source=sender_wallet,
                        destination=target_wallet,
                        amount=TokenAmount(command.amount),
                        created_at=datetime.combine(
                            command.business_date,
                            datetime.min.time(),
                            tzinfo=UTC,
                        ),
                        actor_id=command.sender_id,
                        connection=connection,
                        metadata={"transfer_kind": "gift"},
                    )
                    if command.fee:
                        await post_bank_transfer(
                            namespace="economy-gift-fee",
                            source_idempotency_key=command.idempotency_key,
                            reason=LedgerReason.BANK_BURN,
                            source=sender_wallet,
                            destination=LedgerAccount.system(SystemAccountKind.BURN),
                            amount=TokenAmount(command.fee),
                            created_at=datetime.combine(
                                command.business_date,
                                datetime.min.time(),
                                tzinfo=UTC,
                            ),
                            actor_id=command.sender_id,
                            connection=connection,
                            metadata={"burn_kind": "gift_fee"},
                        )
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
                        target_name=cast(str, target[1]),
                        amount=command.amount,
                        fee=command.fee,
                        available=sender_free - total_cost,
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
            await _lock_operation_key(command.idempotency_key, connection)
            if not await _registered_user_exists(command.requester_id, connection):
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
                "SELECT users.name, COALESCE(SUM(balances.balance), 0) AS total "
                "FROM identity.users AS users "
                "LEFT JOIN bank.accounts AS accounts "
                "ON accounts.account_scope = 'user' AND accounts.owner_id = users.id "
                "LEFT JOIN bank.account_balances AS balances "
                "ON balances.account_key = accounts.account_key "
                "GROUP BY users.id, users.name "
                "ORDER BY total DESC, users.id ASC LIMIT %s",
                (command.limit,),
                connection=connection,
            )
            result = LeaderboardResult(
                EconomyCode.SUCCESS,
                entries=tuple(
                    LeaderboardEntry(
                        name=cast(str, row[0]),
                        coins=int(row[1]),
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
            await _lock_operation_key(command.idempotency_key, connection)
            if not await _registered_user_exists(command.user_id, connection):
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
                await post_bank_transfer(
                    namespace="economy-task-reward",
                    source_idempotency_key=command.idempotency_key,
                    reason=LedgerReason.BANK_ISSUANCE,
                    source=LedgerAccount.system(SystemAccountKind.ISSUANCE),
                    destination=LedgerAccount.user(command.user_id, TokenBucket.FREE),
                    amount=TokenAmount(command.reward),
                    created_at=datetime.now(UTC),
                    actor_id=command.user_id,
                    connection=connection,
                    metadata={
                        "grant_kind": "verified_task",
                        "task_id": command.task_id,
                    },
                )
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
