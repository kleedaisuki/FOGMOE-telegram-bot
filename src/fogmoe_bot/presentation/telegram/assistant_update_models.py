"""Typed Telegram Assistant messages after durable-payload validation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from fogmoe_bot.application.conversation.assistant_ingress import (
    ASSISTANT_MEDIA_LIMIT_BYTES,
    ASSISTANT_TEXT_LIMIT,
    AssistantTurnRequest,
    assistant_text_cost,
)
from fogmoe_bot.application.conversation.inbox_worker import PermanentIngressError
from fogmoe_bot.domain.conversation.identity import ConversationId
from fogmoe_bot.domain.conversation.inbox import InboundUpdate
from fogmoe_bot.domain.conversation.payloads import JsonObject

from .delivery import delivery_stream_for_chat


GROUP_CHAT_TYPES = frozenset({"group", "supergroup"})
"""Telegram chat types that use Assistant group-trigger semantics."""

SUPPORTED_ASSISTANT_CHAT_TYPES = frozenset({"private", *GROUP_CHAT_TYPES})
"""Telegram chat types accepted by Assistant ingress."""


class TelegramAssistantContentKind(StrEnum):
    """@brief Telegram Assistant 输入内容种类 / Telegram Assistant input-content kind."""

    TEXT = "text"
    PHOTO = "photo"
    STICKER = "sticker"


class MalformedTelegramAssistantUpdate(PermanentIngressError):
    """@brief 候选 Assistant Update 的持久化 JSON 非法 / Persisted JSON for an Assistant candidate is invalid."""


@dataclass(frozen=True, slots=True)
class TelegramMediaReference:
    """@brief 未下载的 Telegram 媒体引用 / Telegram media reference that has not been downloaded.

    @param kind 媒体种类 / Media kind.
    @param file_id Telegram file ID / Telegram file ID.
    @param file_unique_id 稳定文件 identity / Stable file identity.
    @param file_size 可选声明字节数 / Optional declared byte size.
    @param width 宽度 / Width.
    @param height 高度 / Height.
    @param mime_type 可选 MIME 类型 / Optional MIME type.
    @param emoji Sticker emoji / Sticker emoji.
    """

    kind: TelegramAssistantContentKind
    file_id: str
    file_unique_id: str
    file_size: int | None
    width: int
    height: int
    mime_type: str | None
    emoji: str | None = None

    @property
    def declared_too_large(self) -> bool:
        """@brief 判断 Telegram 声明大小是否超限 / Check whether Telegram's declared size exceeds the limit.

        @return 明确超过 8 MiB 时为 True / True when explicitly above 8 MiB.
        """

        return (
            self.file_size is not None and self.file_size > ASSISTANT_MEDIA_LIMIT_BYTES
        )

    def to_json(self) -> JsonObject:
        """@brief 构造供后续媒体 adapter 使用的严格 JSON / Build strict JSON for a later media adapter.

        @return 媒体引用 / Media reference.
        """

        return {
            "kind": self.kind.value,
            "file_id": self.file_id,
            "file_unique_id": self.file_unique_id,
            "file_size": self.file_size,
            "width": self.width,
            "height": self.height,
            "mime_type": self.mime_type,
            "emoji": self.emoji,
            "max_download_bytes": ASSISTANT_MEDIA_LIMIT_BYTES,
        }


@dataclass(frozen=True, slots=True)
class TelegramReplyMetadata:
    """@brief 被引用消息的 provider-neutral 元数据 / Provider-neutral metadata for a replied-to message.

    @param message_id 被引用消息 ID / Replied-to message ID.
    @param user_id 可选作者 ID / Optional author ID.
    @param username 可选作者用户名 / Optional author username.
    @param kind 被引用内容种类 / Replied content kind.
    @param text 文本或 caption / Text or caption.
    @param emoji 可选 sticker emoji / Optional sticker emoji.
    """

    message_id: int
    user_id: int | None
    username: str | None
    kind: str
    text: str | None
    emoji: str | None

    def to_json(self) -> JsonObject:
        """@brief 转为持久化 JSON / Convert to persistable JSON.

        @return reply metadata / Reply metadata.
        """

        return {
            "message_id": self.message_id,
            "user_id": self.user_id,
            "username": self.username,
            "kind": self.kind,
            "text": self.text,
            "emoji": self.emoji,
        }


@dataclass(frozen=True, slots=True)
class ParsedTelegramAssistantMessage:
    """@brief 从 durable payload 严格解析的消息 / Message strictly parsed from a durable payload."""

    update_id: int
    edited: bool
    message_id: int
    message_date: int
    edit_date: int | None
    message_thread_id: int | None
    chat_id: int
    chat_type: str
    chat_title: str | None
    user_id: int
    is_bot: bool
    username: str | None
    display_name: str
    content_kind: TelegramAssistantContentKind
    text: str
    command: str | None
    command_target: str | None
    media: TelegramMediaReference | None
    reply: TelegramReplyMetadata | None

    def matches(self, *, bot_user_id: int, bot_username: str) -> bool:
        """@brief 应用互斥 command 与群触发规则 / Apply exclusive command and group-trigger rules.

        @param bot_user_id Bot 用户 ID / Bot user ID.
        @param bot_username Bot 用户名 / Bot username.
        @return 应进入 Assistant route 时为 True / True when this message belongs to the Assistant route.
        """

        if self.is_bot or self.chat_type not in SUPPORTED_ASSISTANT_CHAT_TYPES:
            return False
        if self.command is not None:
            return self.command == "fogmoebot" and (
                self.command_target is None
                or self.command_target.casefold() == bot_username.casefold()
            )
        if self.chat_type not in GROUP_CHAT_TYPES:
            return True
        if self.reply is not None and self.reply.user_id == bot_user_id:
            return True
        text = (
            self.text if self.content_kind is TelegramAssistantContentKind.TEXT else ""
        )
        folded = text.casefold()
        return (
            "/fogmoebot" in text
            or "@FogMoeBot" in text
            or "雾萌" in text
            or "fog moe" in folded
            or "萌娘" in text
            or "fogmoe" in folded
        )

    def to_request(self, inbound: InboundUpdate) -> AssistantTurnRequest:
        """@brief 构造应用层 AssistantTurnRequest / Build an application-layer AssistantTurnRequest.

        @param inbound durable Update / Durable Update.
        @return 预检请求 / Preflighted request.
        """

        expected_conversation = ConversationId(f"assistant-user:{self.user_id}")
        if inbound.conversation_id != expected_conversation:
            raise MalformedTelegramAssistantUpdate(
                "Inbound conversation identity does not match Telegram sender"
            )
        scope: JsonObject = {
            "is_group": self.chat_type in GROUP_CHAT_TYPES,
            "group_id": (self.chat_id if self.chat_type in GROUP_CHAT_TYPES else None),
            "message_id": self.message_id,
            "message_thread_id": self.message_thread_id,
        }
        chat: JsonObject = {
            "chat_id": self.chat_id,
            "type": self.chat_type,
            "title": self.chat_title,
        }
        user: JsonObject = {
            "user_id": self.user_id,
            "username": self.username,
            "display_name": self.display_name,
        }
        source: JsonObject = {
            "update_id": self.update_id,
            "message_id": self.message_id,
            "date": self.message_date,
            "edited": self.edited,
            "edit_date": self.edit_date,
        }
        user_content: JsonObject = {
            "text": self.text,
            "content_kind": self.content_kind.value,
            "chat": chat,
            "user": user,
            "scope": scope,
            "reply": self.reply.to_json() if self.reply is not None else None,
            "source": source,
            "media": self.media.to_json() if self.media is not None else None,
        }
        coin_cost = (
            5
            if self.media is not None or len(self.text) > ASSISTANT_TEXT_LIMIT
            else assistant_text_cost(self.text)
        )
        is_group = self.chat_type in GROUP_CHAT_TYPES
        return AssistantTurnRequest(
            update_id=inbound.update_id,
            conversation_id=inbound.conversation_id,
            received_at=inbound.received_at,
            user_id=self.user_id,
            username=self.username,
            display_name=self.display_name,
            chat_id=self.chat_id,
            is_group=is_group,
            message_id=self.message_id,
            message_thread_id=self.message_thread_id,
            delivery_stream_id=delivery_stream_for_chat(
                self.chat_id,
                self.message_thread_id,
            ),
            user_content=user_content,
            coin_cost=coin_cost,
        )
