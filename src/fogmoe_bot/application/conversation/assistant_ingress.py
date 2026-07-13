"""@brief Durable Assistant 入口用例与端口 / Durable Assistant ingress use case and ports."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from enum import StrEnum
from typing import Protocol

from fogmoe_bot.application.assistant.inference_command import (
    ASSISTANT_INFERENCE_SCHEMA_VERSION,
    AssistantTaskKind,
    DurableAssistantInferenceCommand,
    DurableAssistantScope,
    DurableAssistantUser,
    DurableUserProfile,
)
from fogmoe_bot.domain.user_profile.models import UserProfileSnapshot
from fogmoe_bot.application.conversation.workflow import AcceptConversationTurn
from fogmoe_bot.application.conversation.standalone_outbound import (
    StandaloneOutboundCapability,
    StandaloneOutboundCommand,
)
from fogmoe_bot.application.runtime import SystemUtcClock, UtcClock
from fogmoe_bot.domain.conversation.payloads import JsonObject
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    TurnId,
    TurnSource,
    UpdateId,
)
from fogmoe_bot.domain.temporal import ensure_utc
from fogmoe_bot.domain.conversation.outbox import SEND_TELEGRAM_MESSAGE
from fogmoe_bot.domain.conversation.workflow_results import TurnAcceptanceResult
from fogmoe_bot.domain.observability.trace import TraceContext


ASSISTANT_TEXT_LIMIT = 4096
"""@brief Assistant 文本输入上限 / Assistant text-input limit."""

ASSISTANT_MEDIA_LIMIT_BYTES = 8 * 1024 * 1024
"""@brief Assistant 单媒体下载上限 / Per-media Assistant download limit."""

_POOL_RATE = Decimal("0.2")
"""@brief 对话费用进入质押池的比例 / Share of conversation charges added to the staking pool."""

_POOL_QUANT = Decimal("0.01")
"""@brief 质押池金额精度 / Staking-pool amount precision."""


@dataclass(frozen=True, slots=True)
class AssistantAccountContext:
    """@brief 扣费后用于 durable inference 的账户上下文 / Post-charge account context for durable inference.

    @param coins 扣费后总余额 / Total balance after charging.
    @param plan 用户计划 / User plan.
    @param permission 权限等级 / Permission level.
    @param profile 当前 committed User Profile / Current committed User Profile.
    @param personal_info 规范化个人信息 / Normalized personal information.
    @param diary_exists 是否存在日记 / Whether a diary exists.
    """

    coins: int
    plan: str
    permission: int
    profile: UserProfileSnapshot | None
    personal_info: str
    diary_exists: bool

    def __post_init__(self) -> None:
        """@brief 校验账户上下文 / Validate account context.

        @return None / None.
        """

        if isinstance(self.coins, bool) or self.coins < 0:
            raise ValueError("Assistant account coins cannot be negative")
        if not self.plan.strip():
            raise ValueError("Assistant account plan cannot be blank")
        if isinstance(self.permission, bool) or not isinstance(self.permission, int):
            raise TypeError("Assistant account permission must be an integer")
        if not isinstance(self.diary_exists, bool):
            raise TypeError("Assistant diary_exists must be a Boolean")


@dataclass(frozen=True, slots=True)
class AssistantTurnRequest:
    """@brief 已通过 Telegram 解析与预检的 Assistant 回合请求 / Assistant turn request after Telegram parsing and preflight.

    @param update_id 来源 Update / Source Update.
    @param conversation_id 长期会话 / Long-lived conversation.
    @param received_at Listener 接收时间 / Listener receipt time.
    @param user_id Telegram 用户 ID / Telegram user ID.
    @param username 可选用户名 / Optional username.
    @param display_name 展示名 / Display name.
    @param chat_id Telegram chat ID / Telegram chat ID.
    @param is_group 是否群聊 / Whether this is a group chat.
    @param message_id 来源消息 ID / Source message ID.
    @param message_thread_id 可选话题 ID / Optional topic ID.
    @param delivery_stream_id 有序投递流 / Ordered delivery stream.
    @param user_content 规范化用户消息 / Normalized user message.
    @param coin_cost 本回合费用 / Charge for this turn.
    @param task_kind 推理任务种类 / Inference task kind.
    @param translation_input 翻译活动的隔离输入 / Isolated translation input.
    """

    update_id: UpdateId
    conversation_id: ConversationId
    received_at: datetime
    user_id: int
    username: str | None
    display_name: str
    chat_id: int
    is_group: bool
    message_id: int
    message_thread_id: int | None
    delivery_stream_id: DeliveryStreamId
    user_content: JsonObject
    coin_cost: int
    trace_context: TraceContext = field(default_factory=TraceContext.new_root)
    task_kind: AssistantTaskKind = "assistant"
    translation_input: str | None = None

    def __post_init__(self) -> None:
        """@brief 校验请求身份、费用与 JSON / Validate request identity, charge, and JSON.

        @return None / None.
        """

        if isinstance(self.user_id, bool) or self.user_id <= 0:
            raise ValueError("Assistant user_id must be positive")
        if isinstance(self.chat_id, bool) or self.chat_id == 0:
            raise ValueError("Assistant chat_id cannot be zero")
        if isinstance(self.message_id, bool) or self.message_id <= 0:
            raise ValueError("Assistant message_id must be positive")
        if self.message_thread_id is not None and (
            isinstance(self.message_thread_id, bool) or self.message_thread_id <= 0
        ):
            raise ValueError("Assistant message_thread_id must be positive")
        if not self.display_name.strip():
            raise ValueError("Assistant display name cannot be blank")
        if self.username is not None and not self.username.strip():
            raise ValueError("Assistant username cannot be blank when present")
        if isinstance(self.coin_cost, bool) or not 0 <= self.coin_cost <= 5:
            raise ValueError("Assistant coin cost must be between zero and five")
        text = self.user_content.get("text")
        if not isinstance(text, str) or not text:
            raise ValueError("Assistant user content requires non-empty text")
        excluded = self.user_content.get("exclude_from_assistant")
        if self.task_kind == "translation":
            if (
                self.translation_input is None
                or not self.translation_input.strip()
                or len(self.translation_input) > 3000
            ):
                raise ValueError(
                    "Translation tasks require a 1-3000 character translation_input"
                )
            if excluded is not True:
                raise ValueError(
                    "Translation user content must be excluded from Assistant history"
                )
        elif self.translation_input is not None:
            raise ValueError("translation_input is only valid for translation tasks")
        object.__setattr__(self, "received_at", ensure_utc(self.received_at))
        object.__setattr__(self, "display_name", self.display_name.strip())
        if self.username is not None:
            object.__setattr__(self, "username", self.username.strip())
        object.__setattr__(self, "user_content", dict(self.user_content))
        if not isinstance(self.trace_context, TraceContext):
            raise TypeError("Assistant request requires a TraceContext")

    def to_accept_turn(
        self,
        account: AssistantAccountContext,
        *,
        accepted_at: datetime,
    ) -> AcceptConversationTurn:
        """@brief 以扣费后账户快照构造严格 v1 Conversation acceptance / Build a strict-v1 Conversation acceptance from the post-charge account snapshot.

        @param account 扣费后上下文 / Post-charge account context.
        @param accepted_at 应用接受时间 / Application acceptance time.
        @return 可交给 ConversationWorkflow.prepare 的命令 / Command for ConversationWorkflow.prepare.
        @raise ValueError 群聊上下文携带私人状态 / A group context carries private state.
        """

        if self.is_group and (
            account.profile is not None
            or bool(account.personal_info)
            or account.diary_exists
        ):
            raise ValueError(
                "Group Assistant acceptance cannot freeze private User Profile, "
                "personal_info, or diary state"
            )

        source = TurnSource.telegram(self.update_id)
        turn_id = TurnId.for_source(source)
        inference_request = DurableAssistantInferenceCommand(
            schema_version=ASSISTANT_INFERENCE_SCHEMA_VERSION,
            task_kind=self.task_kind,
            translation_input=self.translation_input,
            conversation_id=str(self.conversation_id),
            turn_id=str(turn_id),
            delivery_stream_id=str(self.delivery_stream_id),
            chat_id=self.chat_id,
            reply_to_message_id=self.message_id,
            message_thread_id=self.message_thread_id,
            user=DurableAssistantUser(
                user_id=self.user_id,
                username=self.username,
                display_name=self.display_name,
                coins=account.coins,
                plan=account.plan,
                permission=account.permission,
                profile=(
                    DurableUserProfile.from_snapshot(account.profile)
                    if account.profile is not None
                    else None
                ),
                personal_info=account.personal_info,
                diary_exists=account.diary_exists,
            ),
            scope=DurableAssistantScope(
                is_group=self.is_group,
                group_id=self.chat_id if self.is_group else None,
                message_id=self.message_id,
                message_thread_id=self.message_thread_id,
            ),
            disable_notification=False,
            protect_content=False,
            disable_web_page_preview=False,
        ).to_json()
        return AcceptConversationTurn(
            source=source,
            conversation_id=self.conversation_id,
            user_content=self.user_content,
            inference_request=inference_request,
            received_at=self.received_at,
            accepted_at=ensure_utc(accepted_at),
            trace_context=self.trace_context,
        )


@dataclass(frozen=True, slots=True)
class AssistantTurnAccepted:
    """@brief 回合已接受或幂等重放 / Turn accepted or replayed idempotently.

    @param acceptance 新提交回执；纯 replay 时为 None / New acceptance receipt, or None for a pure replay.
    @param replayed 是否已有同一 Update 的回合 / Whether the Update already owned a turn.
    """

    acceptance: TurnAcceptanceResult | None
    replayed: bool

    def __post_init__(self) -> None:
        """@brief 校验 replay 回执组合 / Validate replay receipt combinations.

        @return None / None.
        """

        if self.replayed == (self.acceptance is not None):
            raise ValueError(
                "Exactly one of replayed or acceptance must describe success"
            )


@dataclass(frozen=True, slots=True)
class AssistantUserNotRegistered:
    """@brief 用户尚未注册，未产生任何写入 / User is not registered and no writes were made."""


@dataclass(frozen=True, slots=True)
class AssistantInsufficientCoins:
    """@brief 余额不足，未创建 Turn / Insufficient balance; no Turn was created.

    @param available 当前可用余额 / Available balance.
    @param required 本回合费用 / Required charge.
    """

    available: int
    required: int

    def __post_init__(self) -> None:
        """@brief 校验余额拒绝 / Validate the balance rejection.

        @return None / None.
        """

        if self.available < 0 or self.required <= self.available:
            raise ValueError("Insufficient-coins result requires available < required")


type AssistantTurnAcceptanceResult = (
    AssistantTurnAccepted | AssistantUserNotRegistered | AssistantInsufficientCoins
)
"""@brief 扣费与 acceptance 的穷尽结果 / Exhaustive charge-and-accept result."""


class AssistantTurnAcceptanceUoW(Protocol):
    """@brief 账户、奖池与 Conversation acceptance 的原子 UoW / Atomic account, pool, and Conversation-acceptance unit of work."""

    async def accept(
        self,
        request: AssistantTurnRequest,
        *,
        accepted_at: datetime,
    ) -> AssistantTurnAcceptanceResult:
        """@brief 在单个短事务内扣费并接受回合 / Charge and accept in one short transaction.

        @param request 已预检请求 / Preflighted request.
        @param accepted_at 接受时间 / Acceptance time.
        @return 接受或业务拒绝 / Acceptance or business rejection.
        @note 实现必须先锁 durable Update 与账户，且任何拒绝/异常都不得留下 Turn。/
            Implementations must lock the durable Update and account first; rejection or failure must not leave a Turn.
        """

        ...


class AssistantFeedbackReason(StrEnum):
    """@brief Assistant 入口拒绝原因 / Assistant-ingress rejection reason."""

    TEXT_TOO_LONG = "text_too_long"
    MEDIA_TOO_LARGE = "media_too_large"
    USER_NOT_REGISTERED = "user_not_registered"
    INSUFFICIENT_COINS = "insufficient_coins"


class AssistantIngressCoordinator:
    """@brief 协调原子 acceptance 与幂等拒绝反馈 / Coordinate atomic acceptance and idempotent rejection feedback."""

    def __init__(
        self,
        *,
        acceptance: AssistantTurnAcceptanceUoW,
        feedback: StandaloneOutboundCapability,
        clock: UtcClock | None = None,
    ) -> None:
        """@brief 注入 UoW、反馈能力与时钟 / Inject the UoW, feedback capability, and clock.

        @param acceptance 原子扣费与回合 acceptance / Atomic charge-and-turn acceptance.
        @param feedback standalone outbox 能力 / Standalone-outbox capability.
        @param clock UTC 时钟 / UTC clock.
        """

        self._acceptance = acceptance
        """@brief 原子 acceptance UoW / Atomic acceptance UoW."""
        self._feedback = feedback
        self._clock = clock or SystemUtcClock()

    async def handle(
        self,
        request: AssistantTurnRequest,
    ) -> AssistantTurnAcceptanceResult:
        """@brief 接受请求或发布业务拒绝反馈 / Accept a request or publish business-rejection feedback.

        @param request 已预检请求 / Preflighted request.
        @return acceptance UoW 结果 / Acceptance-UoW result.
        """

        result = await self._acceptance.accept(
            request,
            accepted_at=self._clock.now(),
        )
        if isinstance(result, AssistantUserNotRegistered):
            await self.reject(request, AssistantFeedbackReason.USER_NOT_REGISTERED)
        elif isinstance(result, AssistantInsufficientCoins):
            await self.reject(request, AssistantFeedbackReason.INSUFFICIENT_COINS)
        return result

    async def reject(
        self,
        request: AssistantTurnRequest,
        reason: AssistantFeedbackReason,
    ) -> None:
        """@brief 将预检或业务拒绝写入幂等 outbox / Write a preflight or business rejection to the idempotent outbox.

        @param request 可定位反馈目标的请求 / Request locating the feedback target.
        @param reason 拒绝原因 / Rejection reason.
        @return None / None.
        """

        text = _feedback_text(reason, required=request.coin_cost)
        payload: JsonObject = {
            "chat_id": request.chat_id,
            "text": text,
            "message_thread_id": request.message_thread_id,
            "reply_to_message_id": request.message_id,
        }
        await self._feedback.enqueue(
            StandaloneOutboundCommand(
                conversation_id=request.conversation_id,
                delivery_stream_id=request.delivery_stream_id,
                kind=SEND_TELEGRAM_MESSAGE,
                payload=payload,
                idempotency_key=(
                    f"update:{request.update_id.value}:assistant-feedback:{reason.value}"
                ),
                created_at=self._clock.now(),
            )
        )


def assistant_text_cost(text: str) -> int:
    """@brief 按旧产品边界计算文本费用 / Calculate text cost using legacy product boundaries.

    @param text 非空消息文本 / Non-empty message text.
    @return 1 至 5 枚硬币 / Between one and five coins.
    @raises ValueError 文本为空或超过上限 / Text is empty or exceeds the limit.
    """

    if not text:
        raise ValueError("Assistant text cannot be empty")
    length = len(text)
    if length > ASSISTANT_TEXT_LIMIT:
        raise ValueError(
            f"Assistant text cannot exceed {ASSISTANT_TEXT_LIMIT} characters"
        )
    if length > 2000:
        return 5
    if length > 1000:
        return 4
    if length > 500:
        return 3
    if length > 100:
        return 2
    return 1


def assistant_pool_contribution(coin_cost: int) -> Decimal:
    """@brief 计算对话费用进入质押池的金额 / Calculate the staking-pool contribution from a conversation charge.

    @param coin_cost 正整数费用 / Positive integer charge.
    @return 向下取整到分的 20% / Twenty percent rounded down to cents.
    """

    if isinstance(coin_cost, bool) or coin_cost <= 0:
        raise ValueError("Assistant coin cost must be positive")
    return (Decimal(coin_cost) * _POOL_RATE).quantize(
        _POOL_QUANT,
        rounding=ROUND_DOWN,
    )


def normalize_assistant_personal_info(value: str | None) -> str:
    """@brief 规范化 durable 个人信息 / Normalize durable personal information.

    @param value 原始个人信息 / Raw personal information.
    @return 最多 500 字符 / Up to 500 characters.
    """

    return (value or "").strip()[:500]


def _feedback_text(reason: AssistantFeedbackReason, *, required: int) -> str:
    """@brief 渲染兼容旧产品的拒绝文本 / Render product-compatible rejection text.

    @param reason 拒绝原因 / Rejection reason.
    @param required 本回合费用 / Required charge.
    @return 双语拒绝文本 / Bilingual rejection text.
    """

    if reason is AssistantFeedbackReason.TEXT_TOO_LONG:
        return (
            "消息过长，无法处理。请缩短消息长度！\n"
            "The message is too long to process. Please shorten the message."
        )
    if reason is AssistantFeedbackReason.MEDIA_TOO_LARGE:
        return (
            "图片太大啦，请压缩后再发送。\n"
            "The image is too large. Please compress it and try again."
        )
    if reason is AssistantFeedbackReason.USER_NOT_REGISTERED:
        return (
            "请先使用 /me 命令注册个人信息后再聊天。\n"
            "Please register first using the /me command before chatting."
        )
    return (
        f"您的硬币不足，无法与雾萌娘连接，需要{required}个硬币。试试通过 /lottery 抽奖吧！\n"
        f"You don't have enough coins (need {required}), I don't want to talk to you. "
        "Try using /lottery to get some coins!"
    )


__all__ = [
    "ASSISTANT_INFERENCE_SCHEMA_VERSION",
    "ASSISTANT_MEDIA_LIMIT_BYTES",
    "ASSISTANT_TEXT_LIMIT",
    "AssistantAccountContext",
    "AssistantFeedbackReason",
    "AssistantIngressCoordinator",
    "AssistantInsufficientCoins",
    "AssistantTurnAcceptanceResult",
    "AssistantTurnAcceptanceUoW",
    "AssistantTurnAccepted",
    "AssistantTurnRequest",
    "AssistantUserNotRegistered",
    "assistant_pool_contribution",
    "assistant_text_cost",
    "normalize_assistant_personal_info",
]
