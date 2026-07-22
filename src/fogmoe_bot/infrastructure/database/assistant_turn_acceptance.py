"""@brief PostgreSQL Assistant 直接 Conversation acceptance UoW / PostgreSQL direct Assistant Conversation-acceptance UoW."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.conversation.assistant_ingress import (
    AssistantAccountContext,
    AssistantTurnAcceptanceResult,
    AssistantTurnAccepted,
    AssistantTurnRequest,
    AssistantUserNotRegistered,
    normalize_assistant_personal_info,
)
from fogmoe_bot.application.conversation.workflow import (
    ConversationWorkflow,
)
from fogmoe_bot.domain.conversation.identity import (
    TurnId,
    TurnSource,
)
from fogmoe_bot.domain.temporal import ensure_utc
from fogmoe_bot.domain.user_profile.models import UserProfileSnapshot
from fogmoe_bot.domain.conversation.errors import IdempotencyConflictError
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database.account_plan import (
    TransactionalAccountPlanResolver,
)
from fogmoe_bot.infrastructure.database.repositories import (
    conversation_repository,
    user_repository,
)
from fogmoe_bot.infrastructure.database.conversation_workflow.turn import (
    PostgresTurnRepository,
)
from fogmoe_bot.infrastructure.database.user_profile.store import (
    PostgresUserProfileStore,
)


class TransactionalProfileReader(Protocol):
    """@brief acceptance adapter 所需的同事务 Profile 读取 / Transaction-bound Profile read required by the acceptance adapter."""

    async def read_profile_in_transaction(
        self,
        user_id: int,
        *,
        connection: AsyncConnection,
    ) -> UserProfileSnapshot | None:
        """@brief 读取一个 committed Profile revision / Read one committed Profile revision."""

        ...


class PostgresAssistantTurnAcceptanceUoW:
    """@brief 以 inbox/identity 行锁串行化无计费 Turn acceptance / Serialize direct no-charge Turn acceptance with inbox/identity row locks."""

    def __init__(
        self,
        workflow_repository: PostgresTurnRepository,
        *,
        plans: TransactionalAccountPlanResolver,
        profiles: TransactionalProfileReader | None = None,
    ) -> None:
        """@brief 注入 connection-bound workflow 与 Profile 读取 / Inject connection-bound workflow and Profile reading.

        @param workflow_repository Conversation workflow adapter / Conversation workflow adapter.
        @param plans 当前事务中的账户方案解析器 / Account-plan resolver in the current transaction.
        @param profiles acceptance transaction 内的 Profile reader / Profile reader inside the acceptance transaction.
        """

        self._workflow_repository = workflow_repository
        """@brief 同事务 acceptance primitive / Same-transaction acceptance primitive."""
        self._plans = plans
        """@brief 实时管理员与订阅方案解析 / Live administrator and subscription plan resolution."""
        self._profiles = profiles or PostgresUserProfileStore()
        """@brief acceptance-pinned Profile reader / acceptance-pinned Profile reader."""

    async def accept(
        self,
        request: AssistantTurnRequest,
        *,
        accepted_at: datetime,
    ) -> AssistantTurnAcceptanceResult:
        """@brief 在单个短事务内校验并直接接受 Turn / Validate and directly accept a Turn in one short transaction.

        @param request 已预检入口请求 / Preflighted ingress request.
        @param accepted_at acceptance 时间 / Acceptance time.
        @return 接受、幂等 replay 或无写入业务拒绝 / Acceptance, idempotent replay, or a no-write business rejection.
        @note 事务内不调用 Telegram、LLM、HTTP、文件下载或 sleep。/
            The transaction performs no Telegram, LLM, HTTP, file-download, or sleep call.
        @note 此入口没有扣费步骤，也不读取或改写任何余额。/ This entry point has no
            charging step and reads or writes no balance.
        @note ``message/activity`` 同时已存在是 replay，异或是必须回滚的数据库不变量冲突。/
            Both existing effects form a replay; an exclusive-or is a database-invariant conflict
            that must roll back.
        """

        timestamp = ensure_utc(accepted_at)
        turn_id = TurnId.for_source(TurnSource.telegram(request.update_id))
        async with db_connection.transaction() as connection:
            await self._lock_and_validate_inbound(request, connection=connection)
            await db_connection.fetch_one(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (str(request.conversation_id),),
                connection=connection,
            )
            existing_state = await self._existing_turn_state(
                request,
                turn_id=turn_id,
                connection=connection,
            )
            if existing_state is not None and existing_state != "received":
                return AssistantTurnAccepted(acceptance=None, replayed=True)

            identity_context = await user_repository.fetch_user_identity_context(
                request.user_id,
                connection=connection,
                for_update=True,
            )
            if identity_context is None:
                return AssistantUserNotRegistered()

            profile = (
                None
                if request.is_group
                else await self._profiles.read_profile_in_transaction(
                    request.user_id,
                    connection=connection,
                )
            )
            account_context = AssistantAccountContext(
                coins=0,
                plan=await self._plans.resolve(
                    request.user_id,
                    connection=connection,
                ),
                permission=identity_context.permission,
                profile=profile,
                personal_info=(
                    ""
                    if request.is_group
                    else normalize_assistant_personal_info(identity_context.info)
                ),
                diary_exists=(
                    False
                    if request.is_group
                    else await conversation_repository.user_diary_exists(
                        request.user_id,
                        connection=connection,
                    )
                ),
            )
            prepared = ConversationWorkflow.prepare(
                request.to_accept_turn(account_context, accepted_at=timestamp)
            )
            acceptance = (
                await self._workflow_repository.create_and_accept_turn_in_transaction(
                    connection,
                    prepared.turn,
                    message=prepared.message,
                    activity=prepared.activity,
                    accepted_at=prepared.accepted_at,
                )
            )
            message_inserted = acceptance.user_message.inserted
            activity_inserted = acceptance.inference_activity.inserted
            if message_inserted != activity_inserted:
                raise IdempotencyConflictError(
                    "Assistant acceptance found a partial durable effect set: "
                    f"user_message_inserted={message_inserted}, "
                    f"inference_activity_inserted={activity_inserted}"
                )
            if not message_inserted:
                return AssistantTurnAccepted(acceptance=None, replayed=True)
            return AssistantTurnAccepted(acceptance=acceptance, replayed=False)

    @staticmethod
    async def _lock_and_validate_inbound(
        request: AssistantTurnRequest,
        *,
        connection: AsyncConnection,
    ) -> None:
        """@brief 锁定 durable Update 作为幂等 mutex / Lock the durable Update as the idempotency mutex.

        @param request 入口请求 / Ingress request.
        @param connection 当前事务 / Current transaction.
        @return None / None.
        """

        row = await db_connection.fetch_one(
            "SELECT conversation_id FROM conversation.inbound_updates "
            "WHERE update_id = %s FOR UPDATE",
            (request.update_id.value,),
            connection=connection,
        )
        if row is None:
            raise IdempotencyConflictError(
                f"Inbound Update {request.update_id.value} does not exist"
            )
        if str(row[0]) != str(request.conversation_id):
            raise IdempotencyConflictError(
                f"Inbound Update {request.update_id.value} changed conversation identity"
            )

    @staticmethod
    async def _existing_turn_state(
        request: AssistantTurnRequest,
        *,
        turn_id: TurnId,
        connection: AsyncConnection,
    ) -> str | None:
        """@brief 锁定并验证可能存在的规范 Turn / Lock and validate an existing canonical Turn.

        @param request 入口请求 / Ingress request.
        @param turn_id 确定性 Turn ID / Deterministic Turn ID.
        @param connection 当前事务 / Current transaction.
        @return 状态；不存在为 None / State, or None when absent.
        """

        row = await db_connection.fetch_one(
            "SELECT turn_id, conversation_id, state FROM conversation.conversation_turns "
            "WHERE source_update_id = %s FOR UPDATE",
            (request.update_id.value,),
            connection=connection,
        )
        if row is None:
            return None
        if str(row[0]) != str(turn_id) or str(row[1]) != str(request.conversation_id):
            raise IdempotencyConflictError(
                f"Update {request.update_id.value} already belongs to another Turn"
            )
        return str(row[2])


__all__ = ["PostgresAssistantTurnAcceptanceUoW"]
