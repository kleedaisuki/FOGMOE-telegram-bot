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
    AssistantTurnSteered,
    AssistantUserNotRegistered,
    normalize_assistant_personal_info,
)
from fogmoe_bot.application.conversation.workflow import (
    ConversationWorkflow,
)
from fogmoe_bot.domain.conversation.errors import IdempotencyConflictError
from fogmoe_bot.domain.conversation.identity import (
    ConversationMessageId,
    TurnId,
    TurnRevision,
    TurnSource,
)
from fogmoe_bot.domain.conversation.message import MessageDraft, MessageRole
from fogmoe_bot.domain.conversation.steering import STEER_INPUT_KIND, TurnSteer
from fogmoe_bot.domain.temporal import ensure_utc
from fogmoe_bot.domain.user_profile.models import UserProfileSnapshot
from fogmoe_bot.infrastructure.database import assistant_user_context, db
from fogmoe_bot.infrastructure.database.account_plan import (
    TransactionalAccountPlanResolver,
)
from fogmoe_bot.infrastructure.database.conversation_workflow.turn import (
    PostgresTurnRepository,
)
from fogmoe_bot.infrastructure.database.conversation_workflow.turn_uow import (
    _append_message,
    _map_message,
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
        """@brief 实时管理员、付费余额与订阅方案解析 / Live administrator, paid-balance, and subscription plan resolution."""
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
        async with db.transaction() as connection:
            await self._lock_and_validate_inbound(request, connection=connection)
            await db.fetch_one(
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
            replayed_steer = await self._existing_steer(
                request,
                connection=connection,
            )
            if replayed_steer is not None:
                return AssistantTurnSteered(steer=replayed_steer, replayed=True)

            identity_context = await assistant_user_context.lock_assistant_identity_context_in_transaction(
                request.user_id,
                connection=connection,
            )
            if identity_context is None:
                return AssistantUserNotRegistered()

            steered = await self._try_accept_steer(
                request,
                accepted_at=timestamp,
                connection=connection,
            )
            if steered is not None:
                return AssistantTurnSteered(steer=steered, replayed=False)

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
                    else await assistant_user_context.assistant_diary_exists(
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
    async def _existing_steer(
        request: AssistantTurnRequest,
        *,
        connection: AsyncConnection,
    ) -> TurnSteer | None:
        """@brief 读取同一 Update 已提交的 canonical steer / Load a canonical steer already committed for the same Update.

        @param request 已锁定 inbox 的入口请求 / Ingress request whose inbox row is locked.
        @param connection 当前事务 / Current transaction.
        @return 已提交 steer；不存在为 None / Committed steer, or None when absent.
        @raise IdempotencyConflictError 同一 Update 指向另一会话或畸形 revision 时抛出 /
            Raised when the same Update points at another conversation or a malformed revision.
        """

        row = await db.fetch_one(
            "SELECT message_id, conversation_id, sequence, turn_id, source_update_id, "
            "role, content, idempotency_key, created_at "
            "FROM conversation.conversation_messages "
            "WHERE source_update_id = %s AND content ->> 'input_kind' = %s "
            "FOR UPDATE",
            (request.update_id.value, STEER_INPUT_KIND),
            connection=connection,
        )
        if row is None:
            return None
        message = _map_message(row)
        draft = message.draft
        if (
            draft.conversation_id != request.conversation_id
            or draft.turn_id is None
            or draft.source_update_id != request.update_id
        ):
            raise IdempotencyConflictError(
                f"Update {request.update_id.value} already belongs to another steer"
            )
        revision_value = draft.content.get("input_revision")
        if (
            isinstance(revision_value, bool)
            or not isinstance(revision_value, int)
            or revision_value < 1
        ):
            raise IdempotencyConflictError(
                f"Steer Update {request.update_id.value} has an invalid revision"
            )
        return TurnSteer(
            turn_id=draft.turn_id,
            conversation_id=draft.conversation_id,
            source_update_id=request.update_id,
            revision=TurnRevision(revision_value),
            message=message,
            accepted_at=draft.created_at,
        )

    @staticmethod
    async def _try_accept_steer(
        request: AssistantTurnRequest,
        *,
        accepted_at: datetime,
        connection: AsyncConnection,
    ) -> TurnSteer | None:
        """@brief 将普通文本原子追加为 active generation 的 steer / Atomically append ordinary text as a steer of the active generation.

        @param request 已验证且身份存在的 Assistant 请求 / Validated Assistant request for an existing identity.
        @param accepted_at durable 接受时刻 / Durable acceptance time.
        @param connection 当前事务 / Current transaction.
        @return 新 steer；当前没有可修订 generation 时为 None /
            New steer, or None when no generation can be revised.
        @note 附件与 translation 具有独立副作用语义，不合并进正在执行的 generation。/
            Attachments and translation carry independent effect semantics and are not merged into
            a running generation.
        """

        if request.task_kind != "assistant" or request.current_turn_upload is not None:
            return None
        row = await db.fetch_one(
            "SELECT activity.activity_id, activity.turn_id, activity.input_revision, "
            "activity.status "
            "FROM conversation.inference_activities AS activity "
            "JOIN conversation.conversation_turns AS turn "
            "ON turn.turn_id = activity.turn_id "
            "WHERE activity.conversation_id = %s "
            "AND activity.status IN ('processing', 'steer_pending') "
            "AND turn.state = 'waiting_inference' "
            "AND activity.request ->> 'task_kind' = 'assistant' "
            "AND jsonb_typeof(activity.request #> '{user,user_id}') = 'number' "
            "AND activity.request #>> '{user,user_id}' = %s "
            "AND COALESCE(jsonb_typeof("
            "activity.request -> 'current_turn_upload') = 'null', TRUE) "
            "ORDER BY activity.created_at ASC, activity.activity_id ASC "
            "LIMIT 1 FOR UPDATE OF activity, turn",
            (str(request.conversation_id), str(request.user_id)),
            connection=connection,
        )
        if row is None:
            return None
        turn_id = TurnId.parse(row[1])
        current_revision_value = row[2]
        if (
            isinstance(current_revision_value, bool)
            or not isinstance(current_revision_value, int)
        ):
            raise IdempotencyConflictError(
                f"Active Turn {turn_id} has an invalid input revision"
            )
        current_revision = TurnRevision(current_revision_value)
        revision = current_revision.next()
        content = dict(request.user_content)
        content["input_kind"] = STEER_INPUT_KIND
        content["input_revision"] = int(revision)
        semantic_key = f"steer.update.{request.update_id.value}"
        message_result = await _append_message(
            MessageDraft(
                message_id=ConversationMessageId.for_turn(turn_id, semantic_key),
                conversation_id=request.conversation_id,
                turn_id=turn_id,
                source_update_id=request.update_id,
                role=MessageRole.USER,
                content=content,
                idempotency_key=f"turn:{turn_id}:{semantic_key}",
                created_at=accepted_at,
            ),
            connection=connection,
        )
        if not message_result.inserted:
            raise IdempotencyConflictError(
                f"Steer Update {request.update_id.value} replay was not detected before mutation"
            )
        rowcount = await db.execute(
            "UPDATE conversation.inference_activities "
            "SET status = 'steer_pending', input_revision = %s, "
            "version = version + 1, next_attempt_at = %s, "
            "claim_token = NULL, lease_expires_at = NULL, completion_token = NULL, "
            "last_error = NULL, retry_budget_used = 0, updated_at = %s "
            "WHERE activity_id = CAST(%s AS UUID) "
            "AND status IN ('processing', 'steer_pending') "
            "AND input_revision = %s",
            (
                int(revision),
                accepted_at,
                accepted_at,
                str(row[0]),
                int(current_revision),
            ),
            connection=connection,
        )
        if rowcount != 1:
            raise IdempotencyConflictError(
                f"Active Turn {turn_id} changed while accepting revision {revision.value}"
            )
        return TurnSteer(
            turn_id=turn_id,
            conversation_id=request.conversation_id,
            source_update_id=request.update_id,
            revision=revision,
            message=message_result.message,
            accepted_at=accepted_at,
        )

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

        row = await db.fetch_one(
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

        row = await db.fetch_one(
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
