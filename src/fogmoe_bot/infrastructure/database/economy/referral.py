"""@brief PostgreSQL 推荐关系适配器 / PostgreSQL referral adapter."""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast

from fogmoe_bot.application.economy.common import EconomyCode
from fogmoe_bot.application.economy.referral import (
    InvitedUser,
    ReferralCommand,
    ReferralOperations,
    ReferralResult,
    ReferralSummary,
)
from fogmoe_bot.domain.banking.ledger import LedgerAccount, LedgerReason
from fogmoe_bot.domain.banking.money import (
    SystemAccountKind,
    TokenAmount,
    TokenBucket,
)
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database.banking import post_bank_transfer

from .common import _load_result, _lock_operation_key, _save_result


class PostgresReferralOperations(ReferralOperations):
    """@brief 以唯一关系和银行账本绑定推荐 / Bind referrals through the unique relationship and bank ledger."""

    async def bind_referral(self, command: ReferralCommand) -> ReferralResult:
        """@brief 以唯一邀请关系原子绑定并发奖 / Atomically bind a referral through its unique invitation relationship.

        @param command 推荐命令 / Referral command.
        @return 绑定结果 / Binding result.
        @note 不在银行 posting 前锁 ``identity.users``。邀请主键是竞争仲裁点，成功插入
            的事务才可发奖；这维持账本账户优先的全局锁序。/
            This method never locks ``identity.users`` before a bank posting.  The invitation
            primary key arbitrates races and only the transaction that inserts it can award
            tokens, preserving the global bank-account-first lock order.
        """

        async with db_connection.transaction() as connection:
            await _lock_operation_key(command.idempotency_key, connection)
            referrer = await db_connection.fetch_one(
                "SELECT name FROM identity.users WHERE id = %s",
                (command.referrer_id,),
                connection=connection,
            )
            if referrer is None:
                return ReferralResult(EconomyCode.REFERRER_NOT_FOUND)
            created = await db_connection.execute(
                "INSERT INTO identity.users (id, tg_uid, provider, name) "
                "VALUES (%s, %s, 'telegram', %s) ON CONFLICT (id) DO NOTHING",
                (
                    command.invited_user_id,
                    command.invited_user_id,
                    command.invited_name,
                ),
                connection=connection,
            )
            replay = await _load_result(command.idempotency_key, connection)
            if replay is not None:
                return ReferralResult(
                    EconomyCode(str(replay["code"])),
                    new_user=bool(replay.get("new_user", False)),
                    referrer_name=str(replay.get("referrer_name", "")) or None,
                )
            inserted_invitation = await db_connection.fetch_one(
                "INSERT INTO economy.user_invitations "
                "(invited_user_id, referrer_id, invitation_time, reward_claimed) "
                "VALUES (%s, %s, CURRENT_TIMESTAMP, TRUE) "
                "ON CONFLICT (invited_user_id) DO NOTHING "
                "RETURNING referrer_id",
                (command.invited_user_id, command.referrer_id),
                connection=connection,
            )
            if inserted_invitation is None:
                result = ReferralResult(
                    EconomyCode.ALREADY_BOUND,
                    referrer_name=cast(str, referrer[0]),
                )
            else:
                invited_reward = command.invitation_reward + (
                    command.new_user_bonus if created == 1 else 0
                )
                awarded_at = datetime.now(UTC)
                await post_bank_transfer(
                    namespace="economy-referral-invited",
                    source_idempotency_key=command.idempotency_key,
                    reason=LedgerReason.BANK_ISSUANCE,
                    source=LedgerAccount.system(SystemAccountKind.ISSUANCE),
                    destination=LedgerAccount.user(
                        command.invited_user_id,
                        TokenBucket.FREE,
                    ),
                    amount=TokenAmount(invited_reward),
                    created_at=awarded_at,
                    actor_id=command.invited_user_id,
                    connection=connection,
                    metadata={
                        "grant_kind": "referral_invited",
                        "new_user": created == 1,
                    },
                )
                await post_bank_transfer(
                    namespace="economy-referral-referrer",
                    source_idempotency_key=command.idempotency_key,
                    reason=LedgerReason.BANK_ISSUANCE,
                    source=LedgerAccount.system(SystemAccountKind.ISSUANCE),
                    destination=LedgerAccount.user(
                        command.referrer_id,
                        TokenBucket.FREE,
                    ),
                    amount=TokenAmount(command.invitation_reward),
                    created_at=awarded_at,
                    actor_id=command.invited_user_id,
                    connection=connection,
                    metadata={"grant_kind": "referral_referrer"},
                )
                result = ReferralResult(
                    EconomyCode.SUCCESS,
                    new_user=created == 1,
                    referrer_name=cast(str, referrer[0]),
                )
            await _save_result(
                command.idempotency_key,
                "bind_referral",
                command.invited_user_id,
                {
                    "code": result.code.value,
                    "new_user": result.new_user,
                    "referrer_name": result.referrer_name,
                },
                connection,
            )
            return result

    async def referral_summary(self, user_id: int) -> ReferralSummary:
        """@brief 读取邀请人与最近邀请 / Read referrer and recent invited users.

        @param user_id 用户 ID / User ID.
        @return 推荐概览 / Referral summary.
        """

        referrer = await db_connection.fetch_one(
            "SELECT i.referrer_id, u.name FROM economy.user_invitations i "
            "JOIN identity.users u ON u.id = i.referrer_id "
            "WHERE i.invited_user_id = %s",
            (user_id,),
        )
        count_row = await db_connection.fetch_one(
            "SELECT COUNT(*) FROM economy.user_invitations WHERE referrer_id = %s",
            (user_id,),
        )
        rows = await db_connection.fetch_all(
            "SELECT i.invited_user_id, u.name, i.invitation_time "
            "FROM economy.user_invitations i "
            "JOIN identity.users u ON u.id = i.invited_user_id "
            "WHERE i.referrer_id = %s ORDER BY i.invitation_time DESC LIMIT 10",
            (user_id,),
        )
        invited = tuple(
            InvitedUser(cast(int, row[0]), cast(str, row[1]), cast(datetime, row[2]))
            for row in cast(Sequence[Sequence[object]], rows)
        )
        return ReferralSummary(
            referrer_id=cast(int, referrer[0]) if referrer is not None else None,
            referrer_name=cast(str, referrer[1]) if referrer is not None else None,
            invited=invited,
            total=cast(int, count_row[0]) if count_row is not None else 0,
        )
