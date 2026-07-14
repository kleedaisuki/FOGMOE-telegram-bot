"""@brief PostgreSQL 资产动作确认与 fenced 执行适配器 / PostgreSQL asset-action confirmation and fenced-execution adapter."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.asset_actions.models import (
    AssetActionDecisionCode,
    AssetActionDecisionCommand,
    AssetActionDecisionResult,
    AssetActionExecutionClaim,
    ProposeAssetAction,
)
from fogmoe_bot.application.asset_actions.ports import AssetActionConfirmationStore
from fogmoe_bot.domain.asset_actions.confirmation import (
    AssetActionConfirmation,
    AssetActionDecision,
    AssetActionKind,
    AssetActionStatus,
)
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    OutboundMessageId,
)
from fogmoe_bot.domain.conversation.outbox import (
    OutboundDraft,
    SEND_TELEGRAM_MESSAGE,
)
from fogmoe_bot.domain.conversation.payloads import JsonObject
from fogmoe_bot.domain.observability.trace import TraceContext
from fogmoe_bot.domain.temporal import ensure_utc
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database.conversation_workflow.outbox import (
    PostgresOutboxRepository,
    StandaloneOutboxWriter,
)


class PostgresAssetActionConfirmationStore(AssetActionConfirmationStore):
    """@brief 以行锁、租约 fencing 和事务 outbox 实现确认状态机 / Implement confirmation state machine with row locks, lease fencing, and transactional outbox."""

    def __init__(self, *, outbox: StandaloneOutboxWriter | None = None) -> None:
        """@brief 注入同事务 standalone outbox / Inject the same-transaction standalone outbox.

        @param outbox 可替换的 outbox writer / Replaceable outbox writer.
        @return None / None.
        """

        self._outbox = outbox or PostgresOutboxRepository()

    async def propose_in_transaction(
        self,
        command: ProposeAssetAction,
        *,
        connection: AsyncConnection,
    ) -> AssetActionConfirmation:
        """@brief 在 Agent receipt 事务内创建或规范重放提议 / Create or canonically replay a proposal inside the Agent-receipt transaction.

        @param command 类型化确认提议 / Typed confirmation proposal.
        @param connection 调用方拥有的短事务 / Caller-owned short transaction.
        @return 规范确认聚合 / Canonical confirmation aggregate.
        @raise ValueError 同一 source_key 改变语义时抛出 / Raised when one source key changes semantics.
        """

        await db_connection.execute(
            "INSERT INTO assistant.asset_action_confirmations "
            "(confirmation_id, source_key, action_kind, owner_user_id, chat_id, "
            "conversation_id, delivery_stream_id, arguments, status, created_at, "
            "expires_at, updated_at) VALUES "
            "(CAST(%s AS UUID), %s, %s, %s, %s, %s, %s, CAST(%s AS JSONB), "
            "'pending', %s, %s, %s) "
            "ON CONFLICT (source_key) DO NOTHING",
            (
                str(command.confirmation_id),
                command.source_key,
                command.kind.value,
                command.owner_user_id,
                command.chat_id,
                command.conversation_id,
                command.delivery_stream_id,
                _encode_json(command.arguments),
                command.created_at,
                command.expires_at,
                command.created_at,
            ),
            connection=connection,
        )
        confirmation = await _load_by_source_key(
            command.source_key,
            connection=connection,
            for_update=True,
        )
        if confirmation is None:
            raise RuntimeError("Asset-action proposal insert returned no confirmation")
        _validate_replayed_proposal(confirmation, command)
        return confirmation

    async def begin_decision(
        self,
        command: AssetActionDecisionCommand,
        *,
        lease_for: timedelta,
    ) -> AssetActionExecutionClaim | AssetActionDecisionResult:
        """@brief 原子校验 callback，并仅在批准时领取租约 / Atomically validate a callback and claim a lease only for approval.

        @param command owner 的类型化 callback 选择 / Typed owner callback choice.
        @param lease_for 执行租约时长 / Execution lease duration.
        @return 领取成功的执行权或终态/处理中结果 / Claimed execution authority or terminal/processing result.
        """

        if lease_for <= timedelta():
            raise ValueError("Asset-action decision lease must be positive")
        now = ensure_utc(command.decided_at)
        async with db_connection.transaction() as connection:
            confirmation = await _load_by_id(
                command.confirmation_id,
                connection=connection,
                for_update=True,
            )
            if confirmation is None:
                return AssetActionDecisionResult(AssetActionDecisionCode.NOT_FOUND)
            if (
                confirmation.owner_user_id != command.actor_user_id
                or confirmation.chat_id != command.chat_id
            ):
                return AssetActionDecisionResult(
                    AssetActionDecisionCode.FORBIDDEN,
                    confirmation=confirmation,
                )
            if confirmation.status is AssetActionStatus.EXECUTED:
                if confirmation.result is None:
                    raise RuntimeError("Executed asset action lost its result")
                return AssetActionDecisionResult(
                    AssetActionDecisionCode.EXECUTED,
                    confirmation=confirmation,
                    result=confirmation.result,
                    replayed=True,
                )
            if confirmation.status is AssetActionStatus.CANCELLED:
                await self._enqueue_terminal_outcome(
                    confirmation,
                    text=_cancelled_outcome_text(confirmation),
                    created_at=now,
                    connection=connection,
                )
                return AssetActionDecisionResult(
                    AssetActionDecisionCode.CANCELLED,
                    confirmation=confirmation,
                    replayed=True,
                )
            if confirmation.status is AssetActionStatus.EXPIRED:
                await self._enqueue_terminal_outcome(
                    confirmation,
                    text=_expired_outcome_text(confirmation),
                    created_at=now,
                    connection=connection,
                )
                return AssetActionDecisionResult(
                    AssetActionDecisionCode.EXPIRED,
                    confirmation=confirmation,
                    replayed=True,
                )
            if confirmation.status is AssetActionStatus.EXECUTING:
                lease_expires_at = confirmation.execution_lease_expires_at
                if lease_expires_at is None:
                    raise RuntimeError("Executing asset action lost its lease expiry")
                if command.decision is AssetActionDecision.CANCEL or lease_expires_at > now:
                    return AssetActionDecisionResult(
                        AssetActionDecisionCode.PROCESSING,
                        confirmation=confirmation,
                    )
                return await _reclaim_expired_execution(
                    confirmation,
                    now=now,
                    lease_for=lease_for,
                    connection=connection,
                )
            if confirmation.status is not AssetActionStatus.PENDING:
                raise AssertionError("Unhandled asset-action confirmation state")
            if confirmation.is_expired_at(now):
                expired = await _mark_expired(
                    confirmation,
                    now=now,
                    connection=connection,
                )
                await self._enqueue_terminal_outcome(
                    expired,
                    text=_expired_outcome_text(expired),
                    created_at=now,
                    connection=connection,
                )
                return AssetActionDecisionResult(
                    AssetActionDecisionCode.EXPIRED,
                    confirmation=expired,
                )
            if command.decision is AssetActionDecision.CANCEL:
                cancelled = await _mark_cancelled(
                    confirmation,
                    command=command,
                    now=now,
                    connection=connection,
                )
                await self._enqueue_terminal_outcome(
                    cancelled,
                    text=_cancelled_outcome_text(cancelled),
                    created_at=now,
                    connection=connection,
                )
                return AssetActionDecisionResult(
                    AssetActionDecisionCode.CANCELLED,
                    confirmation=cancelled,
                )
            return await _claim_execution(
                confirmation,
                command=command,
                now=now,
                lease_for=lease_for,
                connection=connection,
            )

    async def finalize_execution(
        self,
        claim: AssetActionExecutionClaim,
        *,
        result: JsonObject,
        completed_at: datetime,
        outcome_text: str,
    ) -> AssetActionConfirmation:
        """@brief 以 token fencing 原子写入执行结果和通知 / Atomically write execution result and notification using token fencing.

        @param claim 当前 fenced 执行 claim / Current fenced execution claim.
        @param result 已执行的 JSON 结果 / Executed JSON result.
        @param completed_at 完成时刻 / Completion time.
        @param outcome_text 用户可见终态文本 / User-visible terminal text.
        @return 已终结的规范确认 / Canonical terminal confirmation.
        @raise RuntimeError claim 已被其他 worker 取代时抛出 / Raised when another worker replaced the claim.
        """

        now = ensure_utc(completed_at)
        if not outcome_text.strip() or len(outcome_text) > 4096:
            raise ValueError("Asset-action outcome text must contain 1-4096 characters")
        async with db_connection.transaction() as connection:
            current = await _load_by_id(
                claim.confirmation.confirmation_id,
                connection=connection,
                for_update=True,
            )
            if current is None:
                raise RuntimeError("Asset-action confirmation disappeared during execution")
            if (
                current.status is not AssetActionStatus.EXECUTING
                or current.execution_token != claim.token
            ):
                raise RuntimeError("Asset-action execution claim is stale")
            await self._outbox.enqueue_standalone_outbound_in_transaction(
                connection,
                _outcome_outbound(current, text=outcome_text, created_at=now),
            )
            row = await db_connection.fetch_one(
                "UPDATE assistant.asset_action_confirmations SET "
                "status = 'executed', result = CAST(%s AS JSONB), executed_at = %s, "
                "execution_token = NULL, execution_lease_expires_at = NULL, "
                "updated_at = %s, version = version + 1 "
                "WHERE confirmation_id = CAST(%s AS UUID) "
                "AND status = 'executing' AND execution_token = CAST(%s AS UUID) "
                "RETURNING " + _SELECT_COLUMNS,
                (
                    _encode_json(result),
                    now,
                    now,
                    str(current.confirmation_id),
                    str(claim.token),
                ),
                connection=connection,
            )
            if row is None:
                raise RuntimeError("Asset-action execution finalization lost its fence")
            return _confirmation_from_row(row)

    async def _enqueue_terminal_outcome(
        self,
        confirmation: AssetActionConfirmation,
        *,
        text: str,
        created_at: datetime,
        connection: AsyncConnection,
    ) -> None:
        """@brief 在确认状态事务内重申终态通知 / Reassert a terminal notification in the confirmation-state transaction.

        @param confirmation 已取消或过期的规范确认 / Canonical cancelled or expired confirmation.
        @param text 用户可见终态文本 / User-visible terminal text.
        @param created_at 终态观察时刻 / Terminal observation time.
        @param connection 调用方拥有的事务连接 / Caller-owned transaction connection.
        @return None / None.
        @note 使用确认 ID 派生的确定性 outbox key；重放不会生成第二条通知。/
            A deterministic confirmation-ID-derived outbox key prevents duplicate notifications on replay.
        """

        await self._outbox.enqueue_standalone_outbound_in_transaction(
            connection,
            _outcome_outbound(confirmation, text=text, created_at=created_at),
        )

    async def claim_expired_executions(
        self,
        *,
        now: datetime,
        lease_for: timedelta,
        limit: int,
    ) -> tuple[AssetActionExecutionClaim, ...]:
        """@brief 小批量领取已过期的执行租约 / Claim expired execution leases in a small batch.

        @param now 可信 UTC 当前时刻 / Trusted current UTC time.
        @param lease_for 新 fencing 租约时长 / New fencing-lease duration.
        @param limit 本次短事务的最大领取数 / Maximum claims in this short transaction.
        @return 只包含原本 ``executing`` 且租约过期的 claims /
            Claims that were already ``executing`` with expired leases only.
        @raise ValueError 租约或批量边界非法时抛出 / Raised for invalid lease or batch bounds.
        @note 领取事务只更新 token/lease，不调用银行；业务执行严格发生在事务外。/
            The claim transaction updates only token/lease and never calls the bank; business
            execution occurs strictly outside the transaction.
        """

        if lease_for <= timedelta():
            raise ValueError("Asset-action recovery lease must be positive")
        if isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("Asset-action recovery limit must be between 1 and 100")
        recovered_at = ensure_utc(now)
        async with db_connection.transaction() as connection:
            rows = await db_connection.fetch_all(
                f"SELECT {_SELECT_COLUMNS} FROM assistant.asset_action_confirmations "
                "WHERE status = 'executing' "
                "AND execution_lease_expires_at <= %s "
                "ORDER BY execution_lease_expires_at, confirmation_id "
                "LIMIT %s FOR UPDATE SKIP LOCKED",
                (recovered_at, limit),
                connection=connection,
            )
            claims: list[AssetActionExecutionClaim] = []
            for row in rows:
                confirmation = _confirmation_from_row(row)
                claims.append(
                    await _reclaim_expired_execution(
                        confirmation,
                        now=recovered_at,
                        lease_for=lease_for,
                        connection=connection,
                    )
                )
            return tuple(claims)


_SELECT_COLUMNS = (
    "confirmation_id, source_key, action_kind, owner_user_id, chat_id, "
    "conversation_id, delivery_stream_id, arguments, status, created_at, expires_at, "
    "updated_at, execution_token, execution_lease_expires_at, execution_attempts, "
    "result, executed_at, version"
)
"""@brief 确认聚合标准 SELECT 列 / Canonical SELECT columns for the confirmation aggregate."""


async def _load_by_source_key(
    source_key: str,
    *,
    connection: AsyncConnection,
    for_update: bool,
) -> AssetActionConfirmation | None:
    """@brief 按幂等来源键读取确认 / Load a confirmation by its idempotency source key.

    @param source_key Agent 工具调用来源键 / Agent tool-invocation source key.
    @param connection 当前事务连接 / Current transaction connection.
    @param for_update 是否行锁 / Whether to take a row lock.
    @return 确认或 None / Confirmation or None.
    """

    lock = " FOR UPDATE" if for_update else ""
    row = await db_connection.fetch_one(
        f"SELECT {_SELECT_COLUMNS} FROM assistant.asset_action_confirmations "
        f"WHERE source_key = %s{lock}",
        (source_key,),
        connection=connection,
    )
    return _confirmation_from_row(row) if row is not None else None


async def _load_by_id(
    confirmation_id: UUID,
    *,
    connection: AsyncConnection,
    for_update: bool,
) -> AssetActionConfirmation | None:
    """@brief 按主键读取确认 / Load a confirmation by its primary key.

    @param confirmation_id 确认 UUID / Confirmation UUID.
    @param connection 当前事务连接 / Current transaction connection.
    @param for_update 是否行锁 / Whether to take a row lock.
    @return 确认或 None / Confirmation or None.
    """

    lock = " FOR UPDATE" if for_update else ""
    row = await db_connection.fetch_one(
        f"SELECT {_SELECT_COLUMNS} FROM assistant.asset_action_confirmations "
        f"WHERE confirmation_id = CAST(%s AS UUID){lock}",
        (str(confirmation_id),),
        connection=connection,
    )
    return _confirmation_from_row(row) if row is not None else None


async def _mark_expired(
    confirmation: AssetActionConfirmation,
    *,
    now: datetime,
    connection: AsyncConnection,
) -> AssetActionConfirmation:
    """@brief 将 pending 确认终结为 expired / Terminally mark a pending confirmation expired.

    @param confirmation 已锁定的 pending 聚合 / Locked pending aggregate.
    @param now 可信当前时刻 / Trusted current time.
    @param connection 当前事务连接 / Current transaction connection.
    @return 规范 expired 聚合 / Canonical expired aggregate.
    """

    row = await db_connection.fetch_one(
        "UPDATE assistant.asset_action_confirmations SET "
        "status = 'expired', updated_at = %s, version = version + 1 "
        "WHERE confirmation_id = CAST(%s AS UUID) AND status = 'pending' "
        "RETURNING " + _SELECT_COLUMNS,
        (now, str(confirmation.confirmation_id)),
        connection=connection,
    )
    if row is None:
        raise RuntimeError("Asset-action expiry lost its pending state")
    return _confirmation_from_row(row)


async def _mark_cancelled(
    confirmation: AssetActionConfirmation,
    *,
    command: AssetActionDecisionCommand,
    now: datetime,
    connection: AsyncConnection,
) -> AssetActionConfirmation:
    """@brief 将 owner 取消记录为终态 / Persist an owner cancellation as a terminal state.

    @param confirmation 已锁定 pending 聚合 / Locked pending aggregate.
    @param command 认证后的取消 callback / Authenticated cancellation callback.
    @param now 可信当前时刻 / Trusted current time.
    @param connection 当前事务连接 / Current transaction connection.
    @return 规范 cancelled 聚合 / Canonical cancelled aggregate.
    """

    row = await db_connection.fetch_one(
        "UPDATE assistant.asset_action_confirmations SET "
        "status = 'cancelled', decision_update_id = %s, decision_by_user_id = %s, "
        "decision = 'cancel', decided_at = %s, updated_at = %s, version = version + 1 "
        "WHERE confirmation_id = CAST(%s AS UUID) AND status = 'pending' "
        "RETURNING " + _SELECT_COLUMNS,
        (
            command.update_id,
            command.actor_user_id,
            now,
            now,
            str(confirmation.confirmation_id),
        ),
        connection=connection,
    )
    if row is None:
        raise RuntimeError("Asset-action cancellation lost its pending state")
    return _confirmation_from_row(row)


async def _claim_execution(
    confirmation: AssetActionConfirmation,
    *,
    command: AssetActionDecisionCommand,
    now: datetime,
    lease_for: timedelta,
    connection: AsyncConnection,
) -> AssetActionExecutionClaim:
    """@brief 把 pending 确认转换为 fenced executing / Transition a pending confirmation into fenced executing.

    @param confirmation 已锁定 pending 聚合 / Locked pending aggregate.
    @param command 认证后的批准 callback / Authenticated approval callback.
    @param now 可信当前时刻 / Trusted current time.
    @param lease_for 执行租约时长 / Execution lease duration.
    @param connection 当前事务连接 / Current transaction connection.
    @return 新 fencing token 的执行 claim / Execution claim with a new fencing token.
    """

    token = uuid4()
    row = await db_connection.fetch_one(
        "UPDATE assistant.asset_action_confirmations SET "
        "status = 'executing', decision_update_id = %s, decision_by_user_id = %s, "
        "decision = 'approve', decided_at = %s, execution_token = CAST(%s AS UUID), "
        "execution_lease_expires_at = %s, execution_attempts = execution_attempts + 1, "
        "updated_at = %s, version = version + 1 "
        "WHERE confirmation_id = CAST(%s AS UUID) AND status = 'pending' "
        "RETURNING " + _SELECT_COLUMNS,
        (
            command.update_id,
            command.actor_user_id,
            now,
            str(token),
            now + lease_for,
            now,
            str(confirmation.confirmation_id),
        ),
        connection=connection,
    )
    if row is None:
        raise RuntimeError("Asset-action execution claim lost its pending state")
    return AssetActionExecutionClaim(_confirmation_from_row(row), token)


async def _reclaim_expired_execution(
    confirmation: AssetActionConfirmation,
    *,
    now: datetime,
    lease_for: timedelta,
    connection: AsyncConnection,
) -> AssetActionExecutionClaim:
    """@brief 回收过期执行租约 / Recover an expired execution lease.

    @param confirmation 已锁定且租约过期的聚合 / Locked aggregate with an expired lease.
    @param now 可信当前时刻 / Trusted current time.
    @param lease_for 新执行租约时长 / New execution lease duration.
    @param connection 当前事务连接 / Current transaction connection.
    @return 新 fencing token 的恢复 claim / Recovery claim with a new fencing token.
    """

    token = uuid4()
    row = await db_connection.fetch_one(
        "UPDATE assistant.asset_action_confirmations SET "
        "execution_token = CAST(%s AS UUID), execution_lease_expires_at = %s, "
        "execution_attempts = execution_attempts + 1, updated_at = %s, "
        "version = version + 1 "
        "WHERE confirmation_id = CAST(%s AS UUID) AND status = 'executing' "
        "AND execution_lease_expires_at <= %s "
        "RETURNING " + _SELECT_COLUMNS,
        (
            str(token),
            now + lease_for,
            now,
            str(confirmation.confirmation_id),
            now,
        ),
        connection=connection,
    )
    if row is None:
        raise RuntimeError("Asset-action execution lease recovery lost its fence")
    return AssetActionExecutionClaim(_confirmation_from_row(row), token)


def _confirmation_from_row(row: object) -> AssetActionConfirmation:
    """@brief 将数据库行映射为确认聚合 / Map a database row to a confirmation aggregate.

    @param row 按 ``_SELECT_COLUMNS`` 排列的行 / Row ordered as ``_SELECT_COLUMNS``.
    @return 规范确认聚合 / Canonical confirmation aggregate.
    """

    values = _row_values(row, 18)
    return AssetActionConfirmation(
        confirmation_id=_uuid(values[0]),
        source_key=_text(values[1]),
        kind=AssetActionKind(_text(values[2])),
        owner_user_id=_int(values[3]),
        chat_id=_int(values[4]),
        conversation_id=_text(values[5]),
        delivery_stream_id=_text(values[6]),
        arguments=_json_object(values[7]),
        status=AssetActionStatus(_text(values[8])),
        created_at=_datetime(values[9]),
        expires_at=_datetime(values[10]),
        updated_at=_datetime(values[11]),
        execution_token=_optional_uuid(values[12]),
        execution_lease_expires_at=_optional_datetime(values[13]),
        execution_attempts=_int(values[14]),
        result=_optional_json_object(values[15]),
        executed_at=_optional_datetime(values[16]),
        version=_int(values[17]),
    )


def _validate_replayed_proposal(
    existing: AssetActionConfirmation,
    command: ProposeAssetAction,
) -> None:
    """@brief 拒绝同一 Agent 来源键的语义漂移 / Reject semantic drift for one Agent source key.

    @param existing 数据库中的规范确认 / Canonical confirmation from storage.
    @param command 当前待写提议 / Current proposal request.
    @return None / None.
    @raise ValueError 确认 ID、owner、动作或参数不一致时抛出 / Raised when confirmation ID, owner, action, or arguments disagree.
    """

    if (
        existing.confirmation_id != command.confirmation_id
        or existing.kind is not command.kind
        or existing.owner_user_id != command.owner_user_id
        or existing.chat_id != command.chat_id
        or existing.conversation_id != command.conversation_id
        or existing.delivery_stream_id != command.delivery_stream_id
        or existing.arguments != command.arguments
    ):
        raise ValueError("Asset-action proposal source key changed semantics")


def _outcome_outbound(
    confirmation: AssetActionConfirmation,
    *,
    text: str,
    created_at: datetime,
) -> OutboundDraft:
    """@brief 为执行终态构造确定性 standalone outbox / Build deterministic standalone outbox for an execution terminal state.

    @param confirmation 已执行确认 / Executed confirmation.
    @param text 用户可见终态文本 / User-visible terminal text.
    @param created_at 出站创建时刻 / Outbound creation time.
    @return 同确认 ID 幂等的 outbox 草稿 / Outbox draft idempotent by confirmation ID.
    """

    idempotency_key = f"asset-confirmation:{confirmation.confirmation_id}:outcome"
    conversation_id = ConversationId(confirmation.conversation_id)
    return OutboundDraft(
        message_id=OutboundMessageId.for_conversation(conversation_id, idempotency_key),
        conversation_id=conversation_id,
        turn_id=None,
        delivery_stream_id=DeliveryStreamId(confirmation.delivery_stream_id),
        kind=SEND_TELEGRAM_MESSAGE,
        payload={
            "chat_id": confirmation.chat_id,
            "text": text,
            "disable_web_page_preview": True,
        },
        idempotency_key=idempotency_key,
        created_at=created_at,
        trace_context=TraceContext.new_root(),
    )


def _cancelled_outcome_text(confirmation: AssetActionConfirmation) -> str:
    """@brief 生成已取消确认的耐久通知文本 / Build a durable notification for a cancelled confirmation.

    @param confirmation 已取消的规范确认 / Canonical cancelled confirmation.
    @return 用户可见终态文本 / User-visible terminal text.
    """

    return f"已取消待确认的资产操作：{confirmation.kind.value}。未改动任何资产。"


def _expired_outcome_text(confirmation: AssetActionConfirmation) -> str:
    """@brief 生成已过期确认的耐久通知文本 / Build a durable notification for an expired confirmation.

    @param confirmation 已过期的规范确认 / Canonical expired confirmation.
    @return 用户可见终态文本 / User-visible terminal text.
    """

    return f"资产确认已过期：{confirmation.kind.value}。未改动任何资产。"


def _encode_json(value: JsonObject) -> str:
    """@brief 编码 compact JSON / Encode compact JSON.

    @param value JSON 对象 / JSON object.
    @return 数据库存储文本 / Database storage text.
    """

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _row_values(row: object, expected: int) -> Sequence[object]:
    """@brief 严格读取数据库行列数 / Strictly read a database row's column count.

    @param row 数据库返回行 / Database-returned row.
    @param expected 预期列数 / Expected column count.
    @return 固定顺序列 / Fixed-order columns.
    """

    if not isinstance(row, Sequence) or isinstance(row, str | bytes):
        raise TypeError("Asset-action database row must be a sequence")
    if len(row) != expected:
        raise ValueError(
            f"Asset-action database row has {len(row)} columns, expected {expected}"
        )
    return row


def _text(value: object) -> str:
    """@brief 验证非空文本数据库值 / Validate a non-empty text database value.

    @param value 原始数据库值 / Raw database value.
    @return 文本 / Text.
    """

    if not isinstance(value, str) or not value:
        raise ValueError("Asset-action database text value is invalid")
    return value


def _int(value: object) -> int:
    """@brief 验证非布尔整数数据库值 / Validate a non-Boolean integer database value.

    @param value 原始数据库值 / Raw database value.
    @return 整数 / Integer.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("Asset-action database integer value is invalid")
    return value


def _uuid(value: object) -> UUID:
    """@brief 验证 UUID 数据库值 / Validate a UUID database value.

    @param value 原始数据库值 / Raw database value.
    @return UUID / UUID.
    """

    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        return UUID(value)
    raise TypeError("Asset-action database UUID value is invalid")


def _optional_uuid(value: object) -> UUID | None:
    """@brief 验证可选 UUID 数据库值 / Validate an optional UUID database value.

    @param value 原始数据库值 / Raw database value.
    @return UUID 或 None / UUID or None.
    """

    return None if value is None else _uuid(value)


def _datetime(value: object) -> datetime:
    """@brief 验证 aware 时间数据库值 / Validate an aware datetime database value.

    @param value 原始数据库值 / Raw database value.
    @return UTC-aware 时间 / UTC-aware time.
    """

    if not isinstance(value, datetime):
        raise TypeError("Asset-action database datetime value is invalid")
    return ensure_utc(value)


def _optional_datetime(value: object) -> datetime | None:
    """@brief 验证可选 aware 时间数据库值 / Validate an optional aware datetime database value.

    @param value 原始数据库值 / Raw database value.
    @return UTC-aware 时间或 None / UTC-aware time or None.
    """

    return None if value is None else _datetime(value)


def _json_object(value: object) -> JsonObject:
    """@brief 解码严格 JSON 对象数据库值 / Decode a strict JSON-object database value.

    @param value 原始 JSONB 或文本 / Raw JSONB or text.
    @return 独立 JSON 对象 / Independent JSON object.
    """

    decoded: object = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, Mapping):
        raise TypeError("Asset-action database JSON value must be an object")
    return cast(JsonObject, dict(decoded))


def _optional_json_object(value: object) -> JsonObject | None:
    """@brief 解码可选严格 JSON 对象数据库值 / Decode an optional strict JSON-object database value.

    @param value 原始 JSONB、文本或 None / Raw JSONB, text, or None.
    @return JSON 对象或 None / JSON object or None.
    """

    return None if value is None else _json_object(value)


__all__ = ["PostgresAssetActionConfirmationStore"]
