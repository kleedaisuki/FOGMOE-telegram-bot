"""Typed Telegram Assistant messages after durable-payload validation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from fogmoe_bot.application.conversation.assistant_ingress import (
    ASSISTANT_MEDIA_LIMIT_BYTES,
    AssistantTurnRequest,
)
from fogmoe_bot.application.assistant.current_turn_upload import (
    CurrentTurnUploadKind,
    CurrentTurnUploadReference,
    workspace_attachment_file_path,
)
from fogmoe_bot.application.conversation.inbox_worker import PermanentIngressError
from fogmoe_bot.application.conversation.telegram_identity import (
    GROUP_CHAT_TYPES,
    TelegramConversationAddress,
)
from fogmoe_bot.domain.assistant.messages import text_message
from fogmoe_bot.domain.context import ChatMessageContext, render_chat_message
from fogmoe_bot.domain.conversation.identity import TurnId, TurnSource
from fogmoe_bot.domain.conversation.inbox import InboundUpdate
from fogmoe_bot.domain.conversation.message import MessageRole
from fogmoe_bot.domain.conversation.payloads import JsonObject
from fogmoe_bot.domain.workspace.attachment import pending_workspace_attachment_marker

from .delivery import delivery_stream_for_chat

SUPPORTED_ASSISTANT_CHAT_TYPES = frozenset({"private", *GROUP_CHAT_TYPES})
"""Telegram chat types accepted by Assistant ingress."""


class TelegramAssistantContentKind(StrEnum):
    """@brief Telegram Assistant 输入内容种类 / Telegram Assistant input-content kind."""

    TEXT = "text"
    PHOTO = "photo"
    STICKER = "sticker"
    DOCUMENT = "document"
    VOICE = "voice"
    AUDIO = "audio"
    VIDEO = "video"
    ANIMATION = "animation"
    VIDEO_NOTE = "video_note"


class MalformedTelegramAssistantUpdate(PermanentIngressError):
    """@brief 候选 Assistant Update 的持久化 JSON 非法 / Persisted JSON for an Assistant candidate is invalid."""


@dataclass(frozen=True, slots=True)
class TelegramMediaReference:
    """@brief 未下载的 Telegram 媒体引用 / Telegram media reference that has not been downloaded.

    @param kind 媒体种类 / Media kind.
    @param file_id Telegram file ID / Telegram file ID.
    @param file_unique_id 稳定文件 identity / Stable file identity.
    @param file_size 可选声明字节数 / Optional declared byte size.
    @param width 可选宽度 / Optional width.
    @param height 可选高度 / Optional height.
    @param mime_type 可选 MIME 类型 / Optional MIME type.
    @param file_name 可选 Telegram 文件名 / Optional Telegram filename.
    @param emoji Sticker emoji / Sticker emoji.

    @note 这是 durable 引用而非已下载文件：不含内容 bytes、下载 URL、host path 或
        workspace path。Telegram 的 Document、Audio、Video 与 Animation 可省略
        ``file_name`` 与 ``mime_type``，因此两者以 ``None`` 原样表达。/ This is a durable reference rather than downloaded
        data: it contains no content bytes, download URL, host path, or workspace path.
        Telegram Documents, Audio, Video, and Animation may omit ``file_name`` and
        ``mime_type``, so both are represented as ``None`` when absent.
    """

    kind: TelegramAssistantContentKind
    file_id: str
    file_unique_id: str
    file_size: int | None
    width: int | None
    height: int | None
    mime_type: str | None
    file_name: str | None = None
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

        payload: JsonObject = {
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
        # 保持既有 photo/sticker/voice/video-note 的持久 JSON 形状；可能带文件名的媒体始终
        # 带显式 filename 字段，即使 Telegram 省略它也以 None 表达。 Preserve the existing
        # persisted JSON shape for photos/stickers/voice/video-notes; media that may carry a
        # filename always carry an explicit field, using None when Telegram omitted it.
        if self.kind in {
            TelegramAssistantContentKind.DOCUMENT,
            TelegramAssistantContentKind.AUDIO,
            TelegramAssistantContentKind.VIDEO,
            TelegramAssistantContentKind.ANIMATION,
        }:
            payload["file_name"] = self.file_name
        return payload


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
        # Media captions participate only in presentation-layer routing.  Once accepted, an
        # attachment request overwrites both durable model-facing text fields with its Workspace
        # placeholder in ``to_request``; a caption therefore cannot become an indirect model
        # input merely because it contained a group mention.  媒体 caption 只参与 presentation
        # 层路由；一旦被接受，``to_request`` 会用 Workspace 占位符覆盖两处 durable 模型文本，
        # 所以其中的群提及不会变成间接模型输入。
        routing_text = self.text
        folded = routing_text.casefold()
        return (
            "/fogmoebot" in routing_text
            or f"@{bot_username.removeprefix('@').casefold()}" in folded
            or "雾萌" in routing_text
            or "fog moe" in folded
            or "萌娘" in routing_text
            or "fogmoe" in folded
        )

    def to_request(self, inbound: InboundUpdate) -> AssistantTurnRequest:
        """@brief 构造应用层 AssistantTurnRequest / Build an application-layer AssistantTurnRequest.

        @param inbound durable Update / Durable Update.
        @return 预检请求 / Preflighted request.
        """

        expected_conversation = TelegramConversationAddress(
            chat_type=self.chat_type,
            chat_id=self.chat_id,
            user_id=self.user_id,
            message_thread_id=self.message_thread_id,
        ).conversation_id
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
        is_group = self.chat_type in GROUP_CHAT_TYPES
        current_turn_upload: CurrentTurnUploadReference | None = None
        if self.media is not None:
            media = self.media
            if media.kind is not self.content_kind:
                raise MalformedTelegramAssistantUpdate(
                    "Telegram attachment content requires a matching media reference"
                )
            try:
                current_turn_upload = CurrentTurnUploadReference(
                    kind=CurrentTurnUploadKind(self.content_kind.value),
                    file_id=media.file_id,
                    file_unique_id=media.file_unique_id,
                    source_update_id=inbound.update_id.value,
                    source_message_id=self.message_id,
                    declared_byte_size=media.file_size,
                    original_file_name=media.file_name,
                    mime_type=media.mime_type,
                )
            except (TypeError, ValueError) as error:
                raise MalformedTelegramAssistantUpdate(
                    "Telegram attachment cannot form a current-turn upload reference"
                ) from error
        if current_turn_upload is not None:
            model_text = (
                '<workspace_file path="'
                + workspace_attachment_file_path(
                    turn_id=TurnId.for_source(TurnSource.telegram(inbound.update_id)),
                    reference=current_turn_upload,
                )
                + '" />'
            )
            # 附件 caption 不是另一条模型输入：持久 envelope 的普通 text 和 canonical
            # model_message 都只能是同一个 Workspace 路径占位符。这样 Working Memory、
            # Profile Dreaming 等以后读取 ``content.text`` 的派生流程也不会重新把 caption
            # 回灌给模型。/ An attachment caption is not a second model input: both ordinary
            # durable-envelope text and canonical model_message may contain only the same
            # Workspace-path placeholder. Thus later derivations reading ``content.text``, such as
            # Working Memory and Profile Dreaming, cannot feed the caption back into a model.
            user_content["text"] = model_text
            # 路径文本本身不证明文件已存在：只有 receipt store 在 native ``add_file`` 成功后
            # 原子地把该 marker 切换为 imported，它才可进入任一模型派生面。/ Path-looking
            # text itself does not prove a file exists: it may enter any model-derived surface
            # only after the receipt store atomically changes this marker to imported following
            # native ``add_file`` success.
            user_content["workspace_attachment"] = pending_workspace_attachment_marker()
        else:
            model_text = self.text
            if self.chat_type in GROUP_CHAT_TYPES:
                model_text = render_chat_message(
                    ChatMessageContext(
                        chat_type=self.chat_type,
                        chat_title=self.chat_title,
                        timestamp=datetime.fromtimestamp(
                            self.message_date,
                            tz=UTC,
                        ).isoformat(),
                        user_name=self.display_name,
                        username=self.username,
                        user_id=self.user_id,
                        message_text=self.text,
                        message_id=self.message_id,
                        message_thread_id=self.message_thread_id,
                    )
                )
        user_content["model_message"] = text_message(
            MessageRole.USER,
            model_text,
        ).to_json()
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
            trace_context=inbound.trace_context,
            current_turn_upload=current_turn_upload,
        )
