"""Strictly parse persisted Telegram Assistant update payloads without PTB."""

from __future__ import annotations

from fogmoe_bot.domain.conversation.inbox import InboundUpdate
from fogmoe_bot.domain.conversation.payloads import JsonObject, JsonValue

from .assistant_update_models import (
    MalformedTelegramAssistantUpdate,
    ParsedTelegramAssistantMessage,
    TelegramAssistantContentKind,
    TelegramMediaReference,
    TelegramReplyMetadata,
)


_SERVICE_MESSAGE_KEYS = frozenset(
    {
        "new_chat_members",
        "left_chat_member",
        "new_chat_title",
        "new_chat_photo",
        "delete_chat_photo",
        "group_chat_created",
        "supergroup_chat_created",
        "channel_chat_created",
        "migrate_to_chat_id",
        "migrate_from_chat_id",
        "pinned_message",
        "forum_topic_created",
        "forum_topic_closed",
        "forum_topic_reopened",
        "video_chat_started",
        "video_chat_ended",
    }
)
"""Membership and service events excluded from Assistant ingress."""


def parse_telegram_assistant_update(
    update: InboundUpdate,
) -> ParsedTelegramAssistantMessage:
    """@brief 严格解析 PTB ``to_json`` payload 而不依赖 SDK / Strictly parse a PTB ``to_json`` payload without the SDK.

    @param update durable Update / Durable Update.
    @return 类型化消息 / Typed message.
    @raises MalformedTelegramAssistantUpdate 候选 payload 字段非法 / Candidate payload fields are invalid.
    """

    payload_update_id = _required_int(update.payload, "update_id", minimum=0)
    if payload_update_id != update.update_id.value:
        raise _malformed("payload update_id does not match durable identity")
    message, edited = _message_object(update.payload)
    if _has_service_event(message):
        raise _malformed("service or membership messages are not Assistant input")

    message_id = _required_int(message, "message_id", minimum=1)
    message_date = _required_int(message, "date", minimum=0)
    edit_date = _optional_int(message, "edit_date", minimum=0)
    message_thread_id = _optional_int(message, "message_thread_id", minimum=1)

    chat = _required_object(message, "chat")
    chat_id = _required_nonzero_int(chat, "id")
    chat_type = _required_string(chat, "type")
    chat_title = _optional_string(chat, "title")

    sender = _required_object(message, "from")
    user_id = _required_int(sender, "id", minimum=1)
    is_bot = _required_bool(sender, "is_bot")
    username = _optional_string(sender, "username")
    first_name = _required_string(sender, "first_name")
    last_name = _optional_string(sender, "last_name")
    display_name = " ".join(
        part for part in (first_name.strip(), (last_name or "").strip()) if part
    )
    if not display_name:
        raise _malformed("Telegram sender display name cannot be blank")

    content_kind, text, media = _parse_content(message)
    command, command_target = _parse_command(message, text, content_kind)
    reply = _parse_reply(message.get("reply_to_message"))
    return ParsedTelegramAssistantMessage(
        update_id=payload_update_id,
        edited=edited,
        message_id=message_id,
        message_date=message_date,
        edit_date=edit_date,
        message_thread_id=message_thread_id,
        chat_id=chat_id,
        chat_type=chat_type,
        chat_title=chat_title,
        user_id=user_id,
        is_bot=is_bot,
        username=username,
        display_name=display_name,
        content_kind=content_kind,
        text=text,
        command=command,
        command_target=command_target,
        media=media,
        reply=reply,
    )


def looks_like_assistant_candidate(payload: JsonObject) -> bool:
    """@brief 识别应隔离而非静默忽略的畸形候选 / Identify malformed candidates that should be quarantined rather than ignored.

    @param payload Update payload / Update payload.
    @return 顶层消息声明 Assistant 内容且非服务事件时为 True /
        True when a top-level message declares Assistant content and is not a service event.
    """

    values = [payload.get("message"), payload.get("edited_message")]
    messages = [value for value in values if isinstance(value, dict)]
    return any(
        any(key in message for key in ("text", "photo", "sticker"))
        and not _has_service_event(message)
        for message in messages
    )


def _message_object(payload: JsonObject) -> tuple[JsonObject, bool]:
    """@brief 选择唯一 message/edited_message / Select exactly one message or edited_message.

    @param payload Update payload / Update payload.
    @return 消息对象与 edited 标记 / Message object and edited flag.
    """

    message = payload.get("message")
    edited = payload.get("edited_message")
    if (message is None) == (edited is None):
        raise _malformed(
            "Assistant Update requires exactly one message or edited_message"
        )
    selected = edited if edited is not None else message
    if not isinstance(selected, dict):
        raise _malformed("Telegram message must be an object")
    return selected, edited is not None


def _parse_content(
    message: JsonObject,
) -> tuple[TelegramAssistantContentKind, str, TelegramMediaReference | None]:
    """@brief 解析唯一 text/photo/sticker 内容 / Parse exactly one text, photo, or sticker content.

    @param message 消息对象 / Message object.
    @return 内容种类、文本与媒体 / Content kind, text, and media.
    """

    present = tuple(key for key in ("text", "photo", "sticker") if key in message)
    if len(present) != 1:
        raise _malformed(
            "Assistant message requires exactly one text, photo, or sticker"
        )
    key = present[0]
    if key == "text":
        return (
            TelegramAssistantContentKind.TEXT,
            _required_string(message, "text"),
            None,
        )
    caption = _optional_string(message, "caption") or ""
    if key == "photo":
        media = _parse_photo(message["photo"])
        return TelegramAssistantContentKind.PHOTO, caption or "[photo]", media
    media = _parse_sticker(message["sticker"])
    return TelegramAssistantContentKind.STICKER, caption or "[sticker]", media


def _parse_photo(value: JsonValue) -> TelegramMediaReference:
    """@brief 解析最高分辨率 PhotoSize / Parse the highest-resolution PhotoSize.

    @param value photo 数组 / Photo array.
    @return 未下载媒体引用 / Undownloaded media reference.
    """

    if not isinstance(value, list) or not value:
        raise _malformed("Telegram photo must be a non-empty array")
    sizes: list[JsonObject] = []
    for item in value:
        if not isinstance(item, dict):
            raise _malformed("Every Telegram PhotoSize must be an object")
        sizes.append(item)
    selected = sizes[-1]
    return TelegramMediaReference(
        kind=TelegramAssistantContentKind.PHOTO,
        file_id=_required_string(selected, "file_id"),
        file_unique_id=_required_string(selected, "file_unique_id"),
        file_size=_optional_int(selected, "file_size", minimum=1),
        width=_required_int(selected, "width", minimum=1),
        height=_required_int(selected, "height", minimum=1),
        mime_type="image/jpeg",
    )


def _parse_sticker(value: JsonValue) -> TelegramMediaReference:
    """@brief 解析 Sticker 引用 / Parse a Sticker reference.

    @param value sticker object / Sticker object.
    @return 未下载媒体引用 / Undownloaded media reference.
    """

    if not isinstance(value, dict):
        raise _malformed("Telegram sticker must be an object")
    animated = _required_bool(value, "is_animated")
    video = _required_bool(value, "is_video")
    return TelegramMediaReference(
        kind=TelegramAssistantContentKind.STICKER,
        file_id=_required_string(value, "file_id"),
        file_unique_id=_required_string(value, "file_unique_id"),
        file_size=_optional_int(value, "file_size", minimum=1),
        width=_required_int(value, "width", minimum=1),
        height=_required_int(value, "height", minimum=1),
        mime_type=None if animated or video else "image/webp",
        emoji=_optional_string(value, "emoji"),
    )


def _parse_command(
    message: JsonObject,
    text: str,
    content_kind: TelegramAssistantContentKind,
) -> tuple[str | None, str | None]:
    """@brief 解析首个 BOT_COMMAND entity / Parse the leading BOT_COMMAND entity.

    @param message 消息对象 / Message object.
    @param text 消息文本 / Message text.
    @param content_kind 内容种类 / Content kind.
    @return command 名与可选目标 username / Command name and optional target username.
    """

    entities = message.get("entities")
    if entities is None or content_kind is not TelegramAssistantContentKind.TEXT:
        return None, None
    if not isinstance(entities, list):
        raise _malformed("Telegram entities must be an array")
    command_token: str | None = None
    text_units = _utf16_length(text)
    for entity in entities:
        if not isinstance(entity, dict):
            raise _malformed("Telegram message entity must be an object")
        entity_type = _required_string(entity, "type")
        offset = _required_int(entity, "offset", minimum=0)
        length = _required_int(entity, "length", minimum=1)
        if offset + length > text_units:
            raise _malformed("Telegram message entity exceeds text length")
        if entity_type == "bot_command" and offset == 0 and command_token is None:
            command_token = _utf16_slice(text, offset=offset, length=length)
    if command_token is None:
        return None, None
    if not command_token.startswith("/"):
        raise _malformed("BOT_COMMAND entity must begin with slash")
    raw = command_token[1:]
    command, separator, target = raw.partition("@")
    if not command:
        raise _malformed("Telegram bot command cannot be empty")
    return command.casefold(), target if separator else None


def _utf16_length(value: str) -> int:
    """@brief 返回 Telegram entity 使用的 UTF-16 code-unit 长度 / Return the UTF-16 code-unit length used by Telegram entities.

    @param value Unicode 文本 / Unicode text.
    @return UTF-16 code-unit 数 / UTF-16 code-unit count.
    """

    return len(value.encode("utf-16-le")) // 2


def _utf16_slice(value: str, *, offset: int, length: int) -> str:
    """@brief 按 Telegram UTF-16 entity 边界切片 / Slice at Telegram UTF-16 entity boundaries.

    @param value Unicode 文本 / Unicode text.
    @param offset 起始 code-unit 偏移 / Starting code-unit offset.
    @param length code-unit 长度 / Code-unit length.
    @return 解码后的 entity 文本 / Decoded entity text.
    @raises MalformedTelegramAssistantUpdate entity 切断 surrogate pair 时抛出 / Raised when an entity splits a surrogate pair.
    """

    encoded = value.encode("utf-16-le")
    start = offset * 2
    end = (offset + length) * 2
    try:
        return encoded[start:end].decode("utf-16-le")
    except UnicodeDecodeError as exc:
        raise _malformed(
            "Telegram message entity splits a UTF-16 surrogate pair"
        ) from exc


def _parse_reply(value: JsonValue | None) -> TelegramReplyMetadata | None:
    """@brief 解析有界 reply metadata / Parse bounded reply metadata.

    @param value reply_to_message / reply_to_message.
    @return reply metadata 或 None / Reply metadata or None.
    """

    if value is None:
        return None
    if not isinstance(value, dict):
        raise _malformed("reply_to_message must be an object")
    author = value.get("from")
    user_id: int | None = None
    username: str | None = None
    if author is not None:
        if not isinstance(author, dict):
            raise _malformed("reply author must be an object")
        user_id = _required_int(author, "id", minimum=1)
        username = _optional_string(author, "username")
    kind = "other"
    text: str | None = None
    emoji: str | None = None
    if "text" in value:
        kind = "text"
        text = _required_string(value, "text")
    elif "photo" in value:
        kind = "photo"
        text = _optional_string(value, "caption")
    elif "sticker" in value:
        kind = "sticker"
        sticker = value["sticker"]
        if not isinstance(sticker, dict):
            raise _malformed("reply sticker must be an object")
        emoji = _optional_string(sticker, "emoji")
    return TelegramReplyMetadata(
        message_id=_required_int(value, "message_id", minimum=1),
        user_id=user_id,
        username=username,
        kind=kind,
        text=text,
        emoji=emoji,
    )


def _has_service_event(message: JsonObject) -> bool:
    """@brief 忽略 PTB 序列化的 False 默认值并识别真实服务事件 / Ignore PTB's serialized False defaults and detect real service events.

    @param message Telegram message object / Telegram message object.
    @return 任一服务字段具有真实值时为 True / True when a service field carries a real value.
    """

    return any(
        (value := message.get(key)) is not None and value is not False
        for key in _SERVICE_MESSAGE_KEYS
    )


def _required_object(value: JsonObject, key: str) -> JsonObject:
    """@brief 读取必需 JSON object / Read a required JSON object.

    @param value 父 object / Parent object.
    @param key 字段名 / Field name.
    @return 子 object / Child object.
    """

    item = value.get(key)
    if not isinstance(item, dict):
        raise _malformed(f"{key} must be an object")
    return item


def _required_string(value: JsonObject, key: str) -> str:
    """@brief 读取必需非空字符串 / Read a required non-empty string.

    @param value 父 object / Parent object.
    @param key 字段名 / Field name.
    @return 字符串 / String.
    """

    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise _malformed(f"{key} must be a non-empty string")
    return item


def _optional_string(value: JsonObject, key: str) -> str | None:
    """@brief 读取可选字符串 / Read an optional string.

    @param value 父 object / Parent object.
    @param key 字段名 / Field name.
    @return 字符串或 None / String or None.
    """

    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str):
        raise _malformed(f"{key} must be a string or null")
    return item


def _required_bool(value: JsonObject, key: str) -> bool:
    """@brief 读取必需 Boolean / Read a required Boolean.

    @param value 父 object / Parent object.
    @param key 字段名 / Field name.
    @return Boolean / Boolean.
    """

    item = value.get(key)
    if not isinstance(item, bool):
        raise _malformed(f"{key} must be a Boolean")
    return item


def _required_nonzero_int(value: JsonObject, key: str) -> int:
    """@brief 读取必需非零整数 / Read a required non-zero integer.

    @param value 父 object / Parent object.
    @param key 字段名 / Field name.
    @return 非零整数 / Non-zero integer.
    """

    result = _required_int(value, key)
    if result == 0:
        raise _malformed(f"{key} cannot be zero")
    return result


def _required_int(
    value: JsonObject,
    key: str,
    *,
    minimum: int | None = None,
) -> int:
    """@brief 读取严格整数 / Read a strict integer.

    @param value 父 object / Parent object.
    @param key 字段名 / Field name.
    @param minimum 可选下界 / Optional lower bound.
    @return 整数 / Integer.
    """

    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int):
        raise _malformed(f"{key} must be an integer")
    if minimum is not None and item < minimum:
        raise _malformed(f"{key} cannot be below {minimum}")
    return item


def _optional_int(
    value: JsonObject,
    key: str,
    *,
    minimum: int | None = None,
) -> int | None:
    """@brief 读取可选严格整数 / Read an optional strict integer.

    @param value 父 object / Parent object.
    @param key 字段名 / Field name.
    @param minimum 可选下界 / Optional lower bound.
    @return 整数或 None / Integer or None.
    """

    if value.get(key) is None:
        return None
    return _required_int(value, key, minimum=minimum)


def _malformed(message: str) -> MalformedTelegramAssistantUpdate:
    """@brief 构造永久入口错误 / Build a permanent ingress error.

    @param message 错误详情 / Error detail.
    @return 畸形 payload 错误 / Malformed-payload error.
    """

    return MalformedTelegramAssistantUpdate(message)
