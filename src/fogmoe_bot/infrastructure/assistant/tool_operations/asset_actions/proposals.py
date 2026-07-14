"""@brief Assistant 工具到资产确认提议的适配器 / Adapter from Assistant tools to asset-confirmation proposals."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Callable, Mapping
from uuid import UUID, uuid5

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.asset_actions.callbacks import AssetActionCallbackData
from fogmoe_bot.application.asset_actions.models import ProposeAssetAction
from fogmoe_bot.application.asset_actions.ports import AssetActionConfirmationStore
from fogmoe_bot.application.assistant.tool_runtime import ToolEffectRequest
from fogmoe_bot.domain.asset_actions.confirmation import (
    AssetActionDecision,
    AssetActionKind,
)
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    OutboundMessageId,
)
from fogmoe_bot.domain.conversation.outbox import (
    OutboundDraft,
    SEND_TELEGRAM_ASSET_CONFIRMATION,
)
from fogmoe_bot.domain.conversation.payloads import JsonObject, JsonValue
from fogmoe_bot.domain.observability.trace import TraceContext
from fogmoe_bot.infrastructure.database.conversation_workflow.outbox import (
    StandaloneOutboxWriter,
)


type UtcNow = Callable[[], datetime]
"""@brief 可注入 UTC 时钟 / Injectable UTC clock."""


_CONFIRMATION_NAMESPACE = UUID("536af6b3-184a-4c46-b70d-19b31e5c5645")
"""@brief Agent 来源键派生 confirmation UUIDv5 的命名空间 / UUIDv5 namespace deriving confirmations from Agent source keys."""

_TOOL_KINDS: Mapping[str, AssetActionKind] = MappingProxyType(
    {
        "bank_review_token_request": AssetActionKind.BANK_REVIEW_TOKEN_REQUEST,
        "bank_issue_tokens": AssetActionKind.BANK_ISSUE_TOKENS,
        "bank_fund_activity_pot": AssetActionKind.BANK_FUND_ACTIVITY_POT,
    }
)
"""@brief Agent 工具到封闭资产动作类别的映射 / Mapping from Agent tools to closed asset-action kinds."""

_ADMIN_ACTIONS = frozenset(
    {
        AssetActionKind.BANK_REVIEW_TOKEN_REQUEST,
        AssetActionKind.BANK_ISSUE_TOKENS,
        AssetActionKind.BANK_FUND_ACTIVITY_POT,
    }
)
"""@brief 仅银行管理员可提议的动作类别 / Action kinds only the bank administrator may propose."""


def _utc_now() -> datetime:
    """@brief 返回系统 UTC 时刻 / Return the system UTC time.

    @return aware UTC 时间 / Aware UTC time.
    """

    return datetime.now(UTC)


class AssistantAssetActionProposalOperation:
    """@brief 让 Agent 创建待确认资产动作，而非直接改资产 / Let the Agent create pending asset actions rather than modify assets directly."""

    def __init__(
        self,
        *,
        store: AssetActionConfirmationStore,
        administrator_id: int,
        confirmation_ttl: timedelta = timedelta(minutes=15),
        now: UtcNow = _utc_now,
    ) -> None:
        """@brief 注入持久化端口、管理员身份与过期策略 / Inject persistence port, administrator identity, and expiration policy.

        @param store 确认聚合持久化端口 / Confirmation aggregate persistence port.
        @param administrator_id 唯一银行管理员 Telegram ID / Sole bank-administrator Telegram ID.
        @param confirmation_ttl owner 可确认的时长 / Duration for which the owner may confirm.
        @param now 可替换 UTC 时钟 / Replaceable UTC clock.
        @return None / None.
        """

        if administrator_id <= 0:
            raise ValueError("Asset-action administrator_id must be positive")
        if confirmation_ttl <= timedelta():
            raise ValueError("Asset-action confirmation_ttl must be positive")
        self._store = store
        self._administrator_id = administrator_id
        self._confirmation_ttl = confirmation_ttl
        self._now = now

    async def execute(
        self,
        request: ToolEffectRequest,
        *,
        connection: AsyncConnection | None,
    ) -> JsonObject:
        """@brief 创建或重放一项 owner 绑定的资产确认 / Create or replay an owner-bound asset confirmation.

        @param request catalog 已校验的 Agent 工具请求 / Catalog-validated Agent tool request.
        @param connection Agent receipt 的活动短事务 / Active short transaction of the Agent receipt.
        @return 供模型说明下一步的 JSON 结果 / JSON result for the model to explain the next step.
        """

        kind = _TOOL_KINDS.get(request.tool_name)
        if kind is None:
            raise ValueError("Asset-action proposal operation received an unknown tool")
        if connection is None:
            raise ValueError("Asset-action proposals require an atomic transaction")
        if not request.mutating or request.effect_kind != f"asset.propose.{kind.value}":
            raise ValueError("Asset-action proposal effect classification is invalid")
        context = request.context
        if context.is_group:
            return {
                "status": "rejected",
                "reason": "private_chat_required",
                "message": "资产相关操作只能在与 Bot 的私聊中确认。",
            }
        if (
            isinstance(context.chat_id, bool)
            or not isinstance(context.chat_id, int)
            or context.chat_id <= 0
            or context.chat_id != context.user_id
        ):
            return {
                "status": "rejected",
                "reason": "private_chat_identity_invalid",
                "message": "无法验证当前私聊身份，因此没有创建资产操作。",
            }
        if kind in _ADMIN_ACTIONS and context.user_id != self._administrator_id:
            return {
                "status": "rejected",
                "reason": "bank_administrator_required",
                "message": "只有银行管理员可以提议此资产操作。",
            }
        source_key = _source_key(request)
        confirmation_id = uuid5(_CONFIRMATION_NAMESPACE, source_key)
        created_at = self._now()
        command = ProposeAssetAction(
            confirmation_id=confirmation_id,
            source_key=source_key,
            kind=kind,
            owner_user_id=context.user_id,
            chat_id=context.chat_id,
            conversation_id=str(context.conversation_id),
            delivery_stream_id=str(context.delivery_stream_id),
            arguments=dict(request.arguments),
            created_at=created_at,
            expires_at=created_at + self._confirmation_ttl,
        )
        confirmation = await self._store.propose_in_transaction(
            command,
            connection=connection,
        )
        return {
            "status": "confirmation_required",
            "confirmation_id": str(confirmation.confirmation_id),
            "action": confirmation.kind.value,
            "expires_at": confirmation.expires_at.isoformat(),
            "summary": _summary(confirmation.kind, confirmation.arguments),
        }

    async def finalize(
        self,
        request: ToolEffectRequest,
        result: JsonValue,
        *,
        connection: AsyncConnection,
        outbox: StandaloneOutboxWriter,
    ) -> None:
        """@brief 将确认卡片与 Agent receipt 在同一事务写入 outbox / Write confirmation card and Agent receipt in the same transaction.

        @param request 已完成 proposal 的工具请求 / Tool request whose proposal completed.
        @param result proposal 操作返回的 JSON / JSON returned by the proposal operation.
        @param connection receipt finalize 的活动事务 / Active receipt-finalization transaction.
        @param outbox 同事务 standalone outbox writer / Same-transaction standalone outbox writer.
        @return None / None.
        """

        if not isinstance(result, Mapping):
            raise TypeError("Asset-action proposal result must be an object")
        if result.get("status") != "confirmation_required":
            return
        confirmation_id = _confirmation_id(result)
        summary = _result_text(result, "summary")
        expires_at = _result_text(result, "expires_at")
        context = request.context
        if (
            context.is_group
            or isinstance(context.chat_id, bool)
            or not isinstance(context.chat_id, int)
            or context.chat_id <= 0
            or context.chat_id != context.user_id
        ):
            raise ValueError("Asset-action confirmation lost its private-chat context")
        conversation_id = ConversationId(str(context.conversation_id))
        idempotency_key = f"asset-confirmation:{confirmation_id}:prompt"
        await outbox.enqueue_standalone_outbound_in_transaction(
            connection,
            OutboundDraft(
                message_id=OutboundMessageId.for_conversation(
                    conversation_id,
                    idempotency_key,
                ),
                conversation_id=conversation_id,
                turn_id=None,
                delivery_stream_id=DeliveryStreamId(str(context.delivery_stream_id)),
                kind=SEND_TELEGRAM_ASSET_CONFIRMATION,
                payload={
                    "chat_id": context.chat_id,
                    "text": _prompt_text(summary, expires_at),
                    "approve_callback_data": AssetActionCallbackData(
                        confirmation_id=confirmation_id,
                        decision=AssetActionDecision.APPROVE,
                    ).encode(),
                    "cancel_callback_data": AssetActionCallbackData(
                        confirmation_id=confirmation_id,
                        decision=AssetActionDecision.CANCEL,
                    ).encode(),
                },
                idempotency_key=idempotency_key,
                created_at=self._now(),
                trace_context=TraceContext.new_root(),
            ),
        )


def _source_key(request: ToolEffectRequest) -> str:
    """@brief 从 durable 工具调用构造稳定来源键 / Build a stable source key from a durable tool invocation.

    @param request 已校验工具请求 / Validated tool request.
    @return 长度受限的来源键 / Length-bounded source key.
    """

    value = (
        f"asset-action:{request.context.turn_id}:{request.invocation_id}:"
        f"{request.effect_kind}"
    )
    if len(value) > 255:
        raise ValueError("Asset-action proposal source key exceeds storage limit")
    return value


def _summary(kind: AssetActionKind, arguments: JsonObject) -> str:
    """@brief 从已校验参数生成可核对摘要 / Generate a reviewable summary from validated arguments.

    @param kind 封闭动作类别 / Closed action kind.
    @param arguments 目录已经验证的参数 / Catalog-validated arguments.
    @return 不含模型自由文本的用户可核对摘要 / User-reviewable summary without model free text.
    """

    match kind:
        case AssetActionKind.BANK_REVIEW_TOKEN_REQUEST:
            decision = _required_argument_text(arguments, "decision")
            request_id = _required_argument_text(arguments, "request_id")
            note = arguments.get("note")
            rendered_note = f"；说明：{note}" if isinstance(note, str) and note else ""
            return f"审核申请 {request_id}：{decision}{rendered_note}"
        case AssetActionKind.BANK_ISSUE_TOKENS:
            return (
                f"向用户 {_required_argument_int(arguments, 'recipient_id')} 发行 "
                f"{_required_argument_int(arguments, 'amount')} 枚免费金币；用途："
                f"{_required_argument_text(arguments, 'purpose')}"
            )
        case AssetActionKind.BANK_FUND_ACTIVITY_POT:
            return (
                f"向活动奖池注资 {_required_argument_int(arguments, 'amount')} 枚免费金币；"
                f"用途：{_required_argument_text(arguments, 'purpose')}"
            )
    raise AssertionError("Unhandled asset-action kind")


def _prompt_text(summary: str, expires_at: str) -> str:
    """@brief 渲染确认卡片正文 / Render confirmation-card body.

    @param summary 用户核对摘要 / User-reviewable summary.
    @param expires_at ISO8601 到期时刻 / ISO8601 expiration time.
    @return 用户可见确认文本 / User-visible confirmation text.
    """

    return (
        "雾萌娘准备执行一项账户/资产操作，请亲自核对：\n"
        f"{summary}\n\n"
        f"此确认将在 {expires_at} 失效。点击【确认执行】后才会实际变更；"
        "点击【取消】不会改动任何资产。"
    )


def _confirmation_id(result: Mapping[str, object]) -> UUID:
    """@brief 从 proposal 结果读取 confirmation UUID / Read a confirmation UUID from proposal result.

    @param result proposal 返回对象 / Proposal return object.
    @return confirmation UUID / Confirmation UUID.
    """

    raw = result.get("confirmation_id")
    if not isinstance(raw, str):
        raise ValueError("Asset-action proposal result lacks confirmation_id")
    try:
        return UUID(raw)
    except ValueError as error:
        raise ValueError("Asset-action proposal confirmation_id is invalid") from error


def _result_text(result: Mapping[str, object], key: str) -> str:
    """@brief 从 proposal 结果读取非空文本 / Read non-empty text from proposal result.

    @param result proposal 返回对象 / Proposal return object.
    @param key 必需文本键 / Required text key.
    @return 文本 / Text.
    """

    value = result.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Asset-action proposal result lacks {key}")
    return value


def _required_argument_text(arguments: JsonObject, key: str) -> str:
    """@brief 从已验证参数读取文本 / Read text from validated arguments.

    @param arguments 已验证参数 / Validated arguments.
    @param key 文本键 / Text key.
    @return 非空文本 / Non-empty text.
    """

    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Asset-action argument {key!r} must be non-empty text")
    return value.strip()


def _required_argument_int(arguments: JsonObject, key: str) -> int:
    """@brief 从已验证参数读取正整数 / Read a positive integer from validated arguments.

    @param arguments 已验证参数 / Validated arguments.
    @param key 整数键 / Integer key.
    @return 正整数 / Positive integer.
    """

    value = arguments.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"Asset-action argument {key!r} must be a positive integer")
    return value


__all__ = ["AssistantAssetActionProposalOperation"]
