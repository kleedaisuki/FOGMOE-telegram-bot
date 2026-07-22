"""@brief PostgreSQL User Profile 管理命令 UoW / PostgreSQL UoW for User Profile management commands."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.user_profile import (
    ClearUserProfile,
    RequestUserProfileRegeneration,
    UserProfileManagementResult,
)
from fogmoe_bot.domain.conversation.outbox import OutboundEnqueueResult
from fogmoe_bot.infrastructure.database import db
from fogmoe_bot.infrastructure.database.command_source import (
    validate_telegram_command_source,
)
from fogmoe_bot.infrastructure.database.conversation_workflow.outbox import (
    PostgresOutboxRepository,
    StandaloneOutboxWriter,
)

from .locking import lock_user_profile


class PostgresUserProfileManagementUoW:
    """@brief 原子清除 Profile 或请求 Dreaming，并写确认 / Atomically clear a Profile or request Dreaming and write confirmation."""

    def __init__(self, outbox: StandaloneOutboxWriter | None = None) -> None:
        """@brief 注入 connection-bound outbox primitive / Inject the connection-bound outbox primitive.

        @param outbox 可选 outbox 替身 / Optional outbox substitute.
        """

        self._outbox = outbox or PostgresOutboxRepository()

    async def clear(
        self,
        command: ClearUserProfile,
    ) -> UserProfileManagementResult:
        """@brief 清除 Profile、旧证据与活动 Dream / Clear the Profile, old evidence, and active Dreams.

        @param command 已校验清除命令 / Validated clearing command.
        @return 规范幂等结果 / Canonical idempotent result.
        """

        async with db.transaction() as connection:
            confirmation = await self._begin(
                command,
                operation="Profile reset",
                connection=connection,
            )
            if not confirmation.inserted:
                return UserProfileManagementResult(False, confirmation)

            await lock_user_profile(connection, command.user_id)
            exists, forgotten_through = await self._ensure_profile_row(
                command.user_id,
                now=command.requested_at,
                connection=connection,
            )
            if exists:
                if (
                    forgotten_through is not None
                    and forgotten_through > command.requested_at
                ):
                    return UserProfileManagementResult(True, confirmation)
                await db.execute(
                    "UPDATE user_profile.profiles SET current_revision = NULL, "
                    "observed_through_event_id = 0, next_eligible_at = NULL, "
                    "forgotten_through = GREATEST(COALESCE(forgotten_through, %s), %s), "
                    "updated_at = GREATEST(updated_at, %s) WHERE user_id = %s",
                    (
                        command.requested_at,
                        command.requested_at,
                        command.requested_at,
                        command.user_id,
                    ),
                    connection=connection,
                )
                await db.execute(
                    "DELETE FROM user_profile.dreams WHERE user_id = %s",
                    (command.user_id,),
                    connection=connection,
                )
                await db.execute(
                    "DELETE FROM user_profile.profile_revisions WHERE user_id = %s",
                    (command.user_id,),
                    connection=connection,
                )
                await db.execute(
                    "DELETE FROM user_profile.evidence_events "
                    "WHERE owner_user_id = %s AND occurred_at <= %s",
                    (command.user_id, command.requested_at),
                    connection=connection,
                )
            return UserProfileManagementResult(True, confirmation)

    async def request_regeneration(
        self,
        command: RequestUserProfileRegeneration,
    ) -> UserProfileManagementResult:
        """@brief 立即标记 Profile eligible 并释放 final-failed job / Mark the Profile eligible now and release final-failed jobs.

        @param command 已校验更新请求 / Validated refresh request.
        @return 规范幂等结果 / Canonical idempotent result.
        """

        async with db.transaction() as connection:
            confirmation = await self._begin(
                command,
                operation="Profile regeneration",
                connection=connection,
            )
            if not confirmation.inserted:
                return UserProfileManagementResult(False, confirmation)

            await lock_user_profile(connection, command.user_id)
            exists, forgotten_through = await self._ensure_profile_row(
                command.user_id,
                now=command.requested_at,
                connection=connection,
            )
            if exists:
                if (
                    forgotten_through is not None
                    and forgotten_through > command.requested_at
                ):
                    return UserProfileManagementResult(True, confirmation)
                await db.execute(
                    "DELETE FROM user_profile.dreams "
                    "WHERE user_id = %s AND status = 'failed_final'",
                    (command.user_id,),
                    connection=connection,
                )
                await db.execute(
                    "UPDATE user_profile.profiles SET next_eligible_at = "
                    "LEAST(COALESCE(next_eligible_at, %s), %s), "
                    "updated_at = GREATEST(updated_at, %s) WHERE user_id = %s",
                    (
                        command.requested_at,
                        command.requested_at,
                        command.requested_at,
                        command.user_id,
                    ),
                    connection=connection,
                )
            return UserProfileManagementResult(True, confirmation)

    async def _begin(
        self,
        command: ClearUserProfile | RequestUserProfileRegeneration,
        *,
        operation: str,
        connection: AsyncConnection,
    ) -> OutboundEnqueueResult:
        """@brief 验证来源并用 outbox 建立幂等回执 / Validate the source and establish an idempotency receipt with the outbox.

        @param command Profile 管理命令 / Profile-management command.
        @param operation 安全错误标签 / Safe error label.
        @param connection 当前事务 / Current transaction.
        @return outbox 回执 / Outbox receipt.
        """

        await validate_telegram_command_source(
            command.source,
            command.conversation_id,
            operation=operation,
            connection=connection,
        )
        return await self._outbox.enqueue_standalone_outbound_in_transaction(
            connection,
            command.confirmation,
        )

    @staticmethod
    async def _ensure_profile_row(
        user_id: int,
        *,
        now: datetime,
        connection: AsyncConnection,
    ) -> tuple[bool, datetime | None]:
        """@brief 为已注册用户建立空调度行 / Materialize an empty scheduling row for a registered user.

        @param user_id Profile owner / Profile owner.
        @param now 命令时间 / Command time.
        @param connection 当前事务 / Current transaction.
        @return 用户是否存在及当前遗忘边界 / Whether the user exists and the current forgetting boundary.
        """

        await db.execute(
            "INSERT INTO user_profile.profiles "
            "(user_id, current_revision, observed_through_event_id, next_eligible_at, "
            "forgotten_through, created_at, updated_at) "
            "SELECT id, NULL, 0, NULL, NULL, %s, %s FROM identity.users WHERE id = %s "
            "ON CONFLICT (user_id) DO NOTHING",
            (now, now, user_id),
            connection=connection,
        )
        row = await db.fetch_one(
            "SELECT forgotten_through FROM user_profile.profiles "
            "WHERE user_id = %s FOR UPDATE",
            (user_id,),
            connection=connection,
        )
        if row is None:
            return False, None
        boundary = row[0]
        if boundary is not None and not isinstance(boundary, datetime):
            raise TypeError("Profile forgetting boundary must be a datetime")
        return True, boundary


__all__ = ["PostgresUserProfileManagementUoW"]
