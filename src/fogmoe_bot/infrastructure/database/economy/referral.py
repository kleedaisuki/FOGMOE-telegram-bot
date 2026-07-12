"""@brief PostgreSQL 推荐关系适配器 / PostgreSQL referral adapter."""

from collections.abc import Sequence
from datetime import datetime
from typing import cast

from fogmoe_bot.application.economy.common import EconomyCode
from fogmoe_bot.application.economy.referral import (
    InvitedUser,
    ReferralCommand,
    ReferralOperations,
    ReferralResult,
    ReferralSummary,
)
from fogmoe_bot.infrastructure.database import connection as db_connection

from .common import _credit_free, _load_result, _save_result


class PostgresReferralOperations(ReferralOperations):
    """@brief 按稳定用户锁序绑定和读取推荐关系 / Bind and read referrals with stable user lock order."""

    async def bind_referral(self, command: ReferralCommand) -> ReferralResult:
        """@brief 以用户 ID 升序锁定双方并绑定推荐 / Lock both users by ascending ID and bind a referral.

        @param command 推荐命令 / Referral command.
        @return 绑定结果 / Binding result.
        """

        async with db_connection.transaction() as connection:
            referrer = await db_connection.fetch_one(
                "SELECT name FROM identity.users WHERE id = %s",
                (command.referrer_id,),
                connection=connection,
            )
            if referrer is None:
                return ReferralResult(EconomyCode.REFERRER_NOT_FOUND)
            created = await db_connection.execute(
                "INSERT INTO identity.users (id, tg_uid, provider, name, coins) "
                "VALUES (%s, %s, 'telegram', %s, 0) ON CONFLICT (id) DO NOTHING",
                (
                    command.invited_user_id,
                    command.invited_user_id,
                    command.invited_name,
                ),
                connection=connection,
            )
            await db_connection.fetch_all(
                "SELECT id FROM identity.users WHERE id IN (%s, %s) "
                "ORDER BY id FOR UPDATE",
                (command.invited_user_id, command.referrer_id),
                connection=connection,
            )
            replay = await _load_result(command.idempotency_key, connection)
            if replay is not None:
                return ReferralResult(
                    EconomyCode(str(replay["code"])),
                    new_user=bool(replay.get("new_user", False)),
                    referrer_name=str(replay.get("referrer_name", "")) or None,
                )
            existing = await db_connection.fetch_one(
                "SELECT referrer_id FROM economy.user_invitations "
                "WHERE invited_user_id = %s",
                (command.invited_user_id,),
                connection=connection,
            )
            if existing is not None:
                result = ReferralResult(
                    EconomyCode.ALREADY_BOUND,
                    referrer_name=cast(str, referrer[0]),
                )
            else:
                await db_connection.execute(
                    "INSERT INTO economy.user_invitations "
                    "(invited_user_id, referrer_id, invitation_time, reward_claimed) "
                    "VALUES (%s, %s, CURRENT_TIMESTAMP, TRUE)",
                    (command.invited_user_id, command.referrer_id),
                    connection=connection,
                )
                invited_reward = command.invitation_reward + (
                    command.new_user_bonus if created == 1 else 0
                )
                await _credit_free(command.invited_user_id, invited_reward, connection)
                await _credit_free(
                    command.referrer_id,
                    command.invitation_reward,
                    connection,
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
