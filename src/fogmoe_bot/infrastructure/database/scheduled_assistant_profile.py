"""@brief PostgreSQL 定时 Assistant 用户快照适配器 / PostgreSQL scheduled-Assistant user snapshot adapter."""

from __future__ import annotations

from fogmoe_bot.application.assistant.inference_command import (
    DurableAssistantUser,
    DurableUserProfile,
)
from fogmoe_bot.application.conversation.assistant_ingress import (
    normalize_assistant_personal_info,
)
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database.account_plan import (
    TransactionalAccountPlanResolver,
)
from fogmoe_bot.infrastructure.database.repositories import (
    conversation_repository,
    user_repository,
)
from fogmoe_bot.infrastructure.database.user_profile.store import (
    PostgresUserProfileStore,
)


class PostgresScheduledAssistantProfileReader:
    """@brief 在一个只读快照中装配定时回合用户上下文 / Assemble scheduled-turn user context in one read snapshot."""

    def __init__(
        self,
        plans: TransactionalAccountPlanResolver,
        profiles: PostgresUserProfileStore | None = None,
    ) -> None:
        """@brief 注入方案与 User Profile reader / Inject plan and User Profile readers.

        @param plans 当前事务中的账户方案解析器 / Account-plan resolver in the current transaction.
        @param profiles PostgreSQL Profile store / PostgreSQL Profile store.
        """

        self._plans = plans
        """@brief 实时管理员、付费余额与订阅方案解析 / Live administrator, paid-balance, and subscription plan resolution."""
        self._profiles = profiles or PostgresUserProfileStore()

    async def read(self, user_id: int) -> DurableAssistantUser | None:
        """@brief 读取并规范化用户快照 / Read and normalize a user snapshot.

        @param user_id Telegram 用户 ID / Telegram user identifier.
        @return 严格用户快照；账户不存在时为 None / Strict user snapshot, or None when absent.
        """

        async with db_connection.transaction() as connection:
            account = await user_repository.fetch_user_account(
                user_id,
                connection=connection,
            )
            if account is None:
                return None
            profile = await self._profiles.read_profile_in_transaction(
                user_id, connection=connection
            )
            diary_exists = await conversation_repository.user_diary_exists(
                user_id,
                connection=connection,
            )
            plan = await self._plans.resolve(user_id, connection=connection)

        display_name = account.name.strip()[:256] or f"user-{user_id}"
        username_candidate = account.name.strip()
        username = username_candidate if 1 <= len(username_candidate) <= 64 else None
        return DurableAssistantUser(
            user_id=user_id,
            username=username,
            display_name=display_name,
            coins=account.total_coins,
            plan=plan,
            permission=account.permission,
            profile=(
                DurableUserProfile.from_snapshot(profile)
                if profile is not None
                else None
            ),
            personal_info=normalize_assistant_personal_info(account.info),
            diary_exists=diary_exists,
        )


__all__ = ["PostgresScheduledAssistantProfileReader"]
