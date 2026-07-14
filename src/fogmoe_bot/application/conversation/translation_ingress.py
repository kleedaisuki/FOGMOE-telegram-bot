"""@brief Durable `/tl` 翻译入口用例 / Durable `/tl` translation-ingress use case.

翻译只创建 Conversation Turn 与无工具 inference activity；它不直接调用 provider、
Telegram 或数据库。翻译输入和输出均标记为不进入 Assistant 长期历史。/
Translation only creates a Conversation Turn and a tool-free inference activity; it directly
calls neither a provider, Telegram, nor a database. Translation input and output are marked so
they never enter long-lived Assistant history.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from fogmoe_bot.application.conversation.assistant_ingress import (
    AssistantTurnAcceptanceResult,
    AssistantTurnAcceptanceUoW,
    AssistantTurnRequest,
    AssistantUserNotRegistered,
)
from fogmoe_bot.application.conversation.telegram_identity import (
    GROUP_CHAT_TYPES,
    TelegramConversationAddress,
)
from fogmoe_bot.application.conversation.standalone_outbound import (
    StandaloneOutboundCapability,
    StandaloneOutboundCommand,
)
from fogmoe_bot.application.runtime import SystemUtcClock, UtcClock
from fogmoe_bot.domain.conversation.payloads import JsonObject
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    UpdateId,
)
from fogmoe_bot.domain.temporal import ensure_utc
from fogmoe_bot.domain.conversation.outbox import SEND_TELEGRAM_MESSAGE
from fogmoe_bot.domain.observability.trace import TraceContext


TRANSLATION_TEXT_LIMIT = 3000
"""@brief 翻译输入字符上限 / Translation-input character limit."""

TRANSLATION_CHAT_TYPES = frozenset({"private", *GROUP_CHAT_TYPES})
"""@brief `/tl` 支持的 Telegram chat 类型 / Telegram chat types supported by `/tl`."""


@dataclass(frozen=True, slots=True)
class TranslationReplyTarget:
    """@brief 翻译 Turn 或反馈的稳定目标 / Stable destination for a translation Turn or feedback.

    @param update_id 来源 Update / Source Update.
    @param conversation_id 长期会话 identity / Long-lived conversation identity.
    @param received_at Listener 接收时间 / Listener receipt time.
    @param chat_id Telegram chat ID / Telegram chat identifier.
    @param chat_type Telegram chat 类型 / Telegram chat type.
    @param message_id `/tl` 消息 ID / `/tl` message identifier.
    @param message_thread_id 可选 topic ID / Optional topic identifier.
    @param delivery_stream_id 有序投递流 / Ordered delivery stream.
    """

    update_id: UpdateId
    conversation_id: ConversationId
    received_at: datetime
    chat_id: int
    message_id: int
    message_thread_id: int | None
    delivery_stream_id: DeliveryStreamId
    trace_context: TraceContext = field(default_factory=TraceContext.new_root)
    chat_type: str = "private"

    def __post_init__(self) -> None:
        """@brief 校验外部身份并规范时间 / Validate external identities and normalize time.

        @return None / None.
        """

        if isinstance(self.chat_id, bool) or self.chat_id == 0:
            raise ValueError("Translation chat_id cannot be zero")
        if not isinstance(self.chat_type, str):
            raise TypeError("Translation chat_type must be a string")
        chat_type = self.chat_type.strip().casefold()
        if chat_type not in TRANSLATION_CHAT_TYPES:
            raise ValueError(f"Unsupported translation chat_type: {chat_type}")
        if isinstance(self.message_id, bool) or self.message_id <= 0:
            raise ValueError("Translation message_id must be positive")
        if self.message_thread_id is not None and (
            isinstance(self.message_thread_id, bool) or self.message_thread_id <= 0
        ):
            raise ValueError("Translation message_thread_id must be positive")
        object.__setattr__(self, "received_at", ensure_utc(self.received_at))
        object.__setattr__(self, "chat_type", chat_type)
        if not isinstance(self.trace_context, TraceContext):
            raise TypeError("Translation target requires a TraceContext")


@dataclass(frozen=True, slots=True)
class TranslationTurnRequest:
    """@brief 已解析的 durable 翻译请求 / Parsed durable translation request.

    @param target 稳定 Turn/投递目标 / Stable Turn and delivery destination.
    @param user_id 已认证发送者 / Authenticated sender.
    @param username 可选 Telegram username / Optional Telegram username.
    @param display_name 显示名 / Display name.
    @param is_group 是否群聊 / Whether the command came from a group.
    @param text 待翻译文本 / Text to translate.
    """

    target: TranslationReplyTarget
    user_id: int
    username: str | None
    display_name: str
    is_group: bool
    text: str

    def __post_init__(self) -> None:
        """@brief 校验翻译命令 identity 与文本 / Validate translation identity and text.

        @return None / None.
        """

        if isinstance(self.user_id, bool) or self.user_id <= 0:
            raise ValueError("Translation user_id must be positive")
        if not isinstance(self.is_group, bool):
            raise TypeError("Translation is_group must be a Boolean")
        expected_is_group = self.target.chat_type in GROUP_CHAT_TYPES
        if self.is_group != expected_is_group:
            raise ValueError("Translation is_group must match target chat_type")
        expected_conversation = TelegramConversationAddress(
            chat_type=self.target.chat_type,
            chat_id=self.target.chat_id,
            user_id=self.user_id,
            message_thread_id=self.target.message_thread_id,
        ).conversation_id
        if self.target.conversation_id != expected_conversation:
            raise ValueError(
                "Translation address must match the durable conversation identity"
            )
        display_name = self.display_name.strip()
        if not display_name:
            raise ValueError("Translation display name cannot be blank")
        username = self.username.strip() if self.username is not None else None
        if username == "":
            raise ValueError("Translation username cannot be blank when present")
        text = self.text.strip()
        if not text:
            raise ValueError("Translation text cannot be blank")
        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(self, "username", username)
        object.__setattr__(self, "text", text)

    def to_assistant_request(self) -> AssistantTurnRequest:
        """@brief 转为共享原子 acceptance 命令 / Convert into the shared atomic-acceptance command.

        @return 标记隔离历史的 AssistantTurnRequest / AssistantTurnRequest marked for history isolation.
        @raise ValueError 文本超过产品上限 / Text exceeds the product limit.
        """

        # `/tl` shares the direct Assistant acceptance path.  It creates only
        # Conversation facts and does not carry a token-price field.
        user_content: JsonObject = {
            "schema_version": 1,
            "text": self.text,
            "content_kind": "translation",
            "task_kind": "translation",
            "exclude_from_assistant": True,
            "user": {
                "user_id": self.user_id,
                "username": self.username,
                "display_name": self.display_name,
            },
            "scope": {
                "is_group": self.is_group,
                "group_id": self.target.chat_id if self.is_group else None,
                "message_id": self.target.message_id,
                "message_thread_id": self.target.message_thread_id,
            },
            "source": {
                "update_id": self.target.update_id.value,
                "message_id": self.target.message_id,
            },
        }
        return AssistantTurnRequest(
            update_id=self.target.update_id,
            conversation_id=self.target.conversation_id,
            received_at=self.target.received_at,
            user_id=self.user_id,
            username=self.username,
            display_name=self.display_name,
            chat_id=self.target.chat_id,
            is_group=self.is_group,
            message_id=self.target.message_id,
            message_thread_id=self.target.message_thread_id,
            delivery_stream_id=self.target.delivery_stream_id,
            user_content=user_content,
            task_kind="translation",
            translation_input=self.text,
            trace_context=self.target.trace_context,
        )


class TranslationFeedbackReason(StrEnum):
    """@brief 翻译入口拒绝原因 / Translation-ingress rejection reason."""

    USAGE = "usage"
    TEXT_TOO_LONG = "text_too_long"
    USER_NOT_REGISTERED = "user_not_registered"


@dataclass(frozen=True, slots=True)
class TranslationRejected:
    """@brief 未创建 Turn 的翻译拒绝 / Translation rejection that created no Turn.

    @param reason 稳定拒绝原因 / Stable rejection reason.
    """

    reason: TranslationFeedbackReason


type TranslationIngressResult = AssistantTurnAcceptanceResult | TranslationRejected
"""@brief 翻译接受或拒绝的穷尽结果 / Exhaustive translation acceptance or rejection."""


class TranslationIngressCoordinator:
    """@brief 协调翻译预检、直接 acceptance 与 outbox 反馈 / Coordinate translation preflight, direct acceptance, and outbox feedback."""

    def __init__(
        self,
        *,
        acceptance: AssistantTurnAcceptanceUoW,
        feedback: StandaloneOutboundCapability,
        clock: UtcClock | None = None,
    ) -> None:
        """@brief 注入共享 acceptance、outbox 与时钟 / Inject shared acceptance, outbox, and clock.

        @param acceptance 无计费 Turn 原子 UoW / No-charge turn atomic UoW.
        @param feedback durable standalone outbox / Durable standalone outbox.
        @param clock UTC 时钟 / UTC clock.
        """

        self._acceptance = acceptance
        """@brief 原子 acceptance / Atomic acceptance."""
        self._feedback = feedback
        self._clock = clock or SystemUtcClock()

    async def handle(self, request: TranslationTurnRequest) -> TranslationIngressResult:
        """@brief 接受翻译 Turn 或发布幂等拒绝 / Accept a translation Turn or publish an idempotent rejection.

        @param request 已解析翻译请求 / Parsed translation request.
        @return 接受结果或 typed 拒绝 / Acceptance result or typed rejection.
        """

        if len(request.text) > TRANSLATION_TEXT_LIMIT:
            reason = TranslationFeedbackReason.TEXT_TOO_LONG
            await self.reject(request.target, reason)
            return TranslationRejected(reason)
        assistant_request = request.to_assistant_request()
        result = await self._acceptance.accept(
            assistant_request,
            accepted_at=self._clock.now(),
        )
        if isinstance(result, AssistantUserNotRegistered):
            await self.reject(
                request.target,
                TranslationFeedbackReason.USER_NOT_REGISTERED,
            )
        return result

    async def reject(
        self,
        target: TranslationReplyTarget,
        reason: TranslationFeedbackReason,
    ) -> None:
        """@brief 将翻译拒绝写入幂等 outbox / Write a translation rejection to the idempotent outbox.

        @param target 回复目标 / Reply destination.
        @param reason typed 拒绝原因 / Typed rejection reason.
        @return None / None.
        """

        await self._feedback.enqueue(
            StandaloneOutboundCommand(
                conversation_id=target.conversation_id,
                delivery_stream_id=target.delivery_stream_id,
                kind=SEND_TELEGRAM_MESSAGE,
                payload={
                    "chat_id": target.chat_id,
                    "text": _feedback_text(reason),
                    "message_thread_id": target.message_thread_id,
                    "reply_to_message_id": target.message_id,
                    "disable_web_page_preview": True,
                },
                idempotency_key=(
                    f"update:{target.update_id.value}:translation-feedback:{reason.value}"
                ),
                created_at=self._clock.now(),
            )
        )


def _feedback_text(reason: TranslationFeedbackReason) -> str:
    """@brief 渲染稳定双语翻译反馈 / Render stable bilingual translation feedback.

    @param reason 拒绝原因 / Rejection reason.
    @return 用户可见文本 / User-visible text.
    """

    if reason is TranslationFeedbackReason.USAGE:
        return (
            "使用方法：\n1. 回复一条文本消息并使用 /tl\n"
            "2. 直接使用 /tl <文本>\n\n"
            "Usage:\n1. Reply to a text message with /tl\n"
            "2. Use /tl <text> directly"
        )
    if reason is TranslationFeedbackReason.TEXT_TOO_LONG:
        return (
            "文本太长，无法翻译。请将文本缩短到 3000 字符以内。\n"
            "Text too long for translation. Please keep it within 3000 characters."
        )
    if reason is TranslationFeedbackReason.USER_NOT_REGISTERED:
        return (
            "请先使用 /me 命令注册个人信息后再使用翻译功能。\n"
            "Please register first using /me before using translation."
        )
    raise ValueError(f"Unsupported translation feedback reason: {reason}")


__all__ = [
    "TRANSLATION_TEXT_LIMIT",
    "TranslationFeedbackReason",
    "TranslationIngressCoordinator",
    "TranslationIngressResult",
    "TranslationRejected",
    "TranslationReplyTarget",
    "TranslationTurnRequest",
]
