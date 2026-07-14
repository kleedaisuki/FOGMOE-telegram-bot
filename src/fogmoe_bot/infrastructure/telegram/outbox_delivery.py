"""@brief Telegram transactional-outbox 投递适配器 / Telegram transactional-outbox delivery adapter.

适配器严格解析持久化 kind/payload，再调用 python-telegram-bot。Telegram Bot API
没有通用 idempotency key，因此成功回执后可记录 external message ID，但网络模糊
超时后的重试仍可能重复投递；这里不声称 exactly-once。
/ The adapter strictly parses persisted kinds and payloads before calling
python-telegram-bot. The Telegram Bot API has no general idempotency key, so an
external message ID can be recorded after a receipt, but retrying an ambiguous
network timeout can still duplicate delivery; exactly-once is not claimed.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlsplit

from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    ReplyParameters,
    Sticker,
)
from telegram.error import (
    BadRequest,
    Forbidden,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
)

from fogmoe_bot.application.conversation.outbox_worker import (
    AmbiguousDeliveryTimeout,
    DeliveryError,
    DeliveryErrorCategory,
    DeliveryReceipt,
    OutboundPayloadError,
    PermanentDeliveryError,
    RetryableDeliveryError,
)
from fogmoe_bot.domain.conversation.payloads import JsonObject
from fogmoe_bot.domain.conversation.outbox import (
    EDIT_TELEGRAM_MESSAGE,
    SEND_TELEGRAM_ARTIFACT,
    SEND_TELEGRAM_MESSAGE,
    SEND_TELEGRAM_PHOTO,
    SEND_TELEGRAM_STICKER,
    OutboundMessage,
)
from fogmoe_bot.domain.media.artifact import ArtifactKind
from fogmoe_bot.domain.media.identifiers import ArtifactId
from fogmoe_bot.infrastructure.media.file_artifact_store import FileArtifactStore


_SEND_KEYS = frozenset(
    {
        "chat_id",
        "text",
        "parse_mode",
        "disable_notification",
        "protect_content",
        "message_thread_id",
        "reply_to_message_id",
        "disable_web_page_preview",
    }
)
"""@brief send_message 允许的持久化字段 / Persisted fields allowed for send_message."""

_EDIT_KEYS = frozenset(
    {
        "chat_id",
        "message_id",
        "text",
        "parse_mode",
        "disable_web_page_preview",
    }
)
"""@brief edit_message_text 允许的持久化字段 / Persisted fields allowed for edit_message_text."""

_PARSE_MODES = frozenset({"HTML", "Markdown", "MarkdownV2"})
"""@brief 支持的 Telegram parse_mode 值 / Supported Telegram parse_mode values."""

_MAX_TEXT_LENGTH = 4096
"""@brief Telegram 文本消息字符上限 / Telegram text-message character limit."""

_ARTIFACT_KEYS = frozenset(
    {"chat_id", "artifact_id", "kind", "filename", "mime_type", "size_bytes"}
)
"""@brief artifact outbox 允许字段 / Allowed artifact-outbox fields."""

_STICKER_KEYS = frozenset({"chat_id", "pack_name", "emoji", "message_thread_id"})
"""@brief sticker outbox 允许字段 / Allowed sticker-outbox fields."""

_PHOTO_KEYS = frozenset(
    {
        "chat_id",
        "photo_url",
        "caption",
        "has_spoiler",
        "message_thread_id",
        "reply_to_message_id",
        "button_text",
        "button_callback_data",
    }
)
"""@brief photo outbox 允许的持久化字段 / Persisted fields allowed for photo delivery."""

_STICKER_PACK_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
"""@brief Telegram sticker pack 安全名称语法 / Safe Telegram sticker-pack name grammar."""

_MAX_STICKER_PACK_LENGTH = 64
"""@brief sticker pack 名称上限 / Sticker-pack name limit."""

_MAX_STICKER_EMOJI_LENGTH = 32
"""@brief sticker emoji 序列字符上限 / Sticker-emoji sequence character limit."""


@dataclass(frozen=True, slots=True)
class SendMessagePayload:
    """@brief 已校验的 Telegram send_message 载荷 / Validated Telegram send_message payload.

    @param chat_id 目标 chat ID 或频道用户名 / Target chat ID or channel username.
    @param text 消息文本 / Message text.
    @param parse_mode 可选解析模式 / Optional parse mode.
    @param disable_notification 是否静默发送 / Whether to send silently.
    @param protect_content 是否保护内容 / Whether to protect content.
    @param message_thread_id 可选话题 ID / Optional topic ID.
    @param reply_to_message_id 可选回复消息 ID / Optional replied-to message ID.
    @param disable_web_page_preview 是否禁用链接预览 / Whether to disable link previews.
    """

    chat_id: int | str
    text: str
    parse_mode: str | None
    disable_notification: bool | None
    protect_content: bool | None
    message_thread_id: int | None
    reply_to_message_id: int | None
    disable_web_page_preview: bool | None


@dataclass(frozen=True, slots=True)
class EditMessagePayload:
    """@brief 已校验的 Telegram edit_message_text 载荷 / Validated Telegram edit_message_text payload.

    @param chat_id 目标 chat ID 或频道用户名 / Target chat ID or channel username.
    @param message_id 待编辑消息 ID / Message ID to edit.
    @param text 新消息文本 / New message text.
    @param parse_mode 可选解析模式 / Optional parse mode.
    @param disable_web_page_preview 是否禁用链接预览 / Whether to disable link previews.
    """

    chat_id: int | str
    message_id: int
    text: str
    parse_mode: str | None
    disable_web_page_preview: bool | None


@dataclass(frozen=True, slots=True)
class SendArtifactPayload:
    """@brief 已校验 durable artifact payload / Validated durable-artifact payload.

    @param chat_id Telegram chat ID / Telegram chat ID.
    @param artifact_id artifact ID / Artifact ID.
    @param kind artifact kind / Artifact kind.
    @param filename 用户文件名 / User-facing filename.
    @param mime_type MIME type / MIME type.
    @param size_bytes 预期字节数 / Expected byte size.
    """

    chat_id: int | str
    artifact_id: ArtifactId
    kind: ArtifactKind
    filename: str
    mime_type: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class SendStickerPayload:
    """@brief 已校验的 Telegram sticker 语义载荷 / Validated Telegram sticker-intent payload.

    @param chat_id 目标 chat ID / Target chat identifier.
    @param pack_name 受限贴纸包名 / Constrained sticker-pack name.
    @param emoji 贴纸包内精确 emoji / Exact emoji within the pack.
    @param message_thread_id 可选话题 ID / Optional topic identifier.
    """

    chat_id: int | str
    pack_name: str
    emoji: str
    message_thread_id: int | None


@dataclass(frozen=True, slots=True)
class SendPhotoPayload:
    """@brief 已校验的 Telegram remote-photo 载荷 / Validated Telegram remote-photo payload."""

    chat_id: int | str
    photo_url: str
    caption: str
    has_spoiler: bool
    message_thread_id: int | None
    reply_to_message_id: int | None
    button_text: str | None
    button_callback_data: str | None


class TelegramOutboxDeliveryAdapter:
    """@brief 将类型化 outbox 消息投递到 Telegram / Deliver typed outbox messages to Telegram."""

    def __init__(
        self,
        bot: Bot,
        *,
        artifacts: FileArtifactStore | None = None,
    ) -> None:
        """@brief 创建 Telegram 投递适配器 / Create a Telegram delivery adapter.

        @param bot 已初始化的 PTB Bot / Initialized PTB Bot.
        @param artifacts 可选 durable artifact store / Optional durable artifact store.
        """

        self._bot = bot
        self._artifacts = artifacts

    async def deliver(self, message: OutboundMessage) -> DeliveryReceipt:
        """@brief 解析并投递一条 outbox 消息 / Parse and deliver one outbox message.

        @param message 已领取 outbox 消息 / Claimed outbox message.
        @return Telegram 外部消息 ID / Telegram external message ID.
        @raise OutboundPayloadError kind 或 payload 非法 / Invalid kind or payload.
        @raise RetryableDeliveryError Telegram 暂时不可用 / Telegram is temporarily unavailable.
        @raise PermanentDeliveryError Telegram 永久拒绝请求 / Telegram permanently rejected the request.
        """

        try:
            if message.kind == SEND_TELEGRAM_MESSAGE:
                return await self._deliver_message(message.payload)
            if message.kind == EDIT_TELEGRAM_MESSAGE:
                return await self._deliver_edit(message.payload)
            if message.kind == SEND_TELEGRAM_STICKER:
                return await self._deliver_sticker(message.payload)
            if message.kind == SEND_TELEGRAM_PHOTO:
                return await self._deliver_photo(message.payload)
            if message.kind == SEND_TELEGRAM_ARTIFACT:
                return await self._deliver_artifact(message.payload)
            raise OutboundPayloadError(
                f"Unsupported outbound kind: {message.kind.value}",
                category=DeliveryErrorCategory.UNSUPPORTED_KIND,
            )
        except DeliveryError:
            raise
        except RetryAfter as error:
            raise RetryableDeliveryError(
                str(error),
                category=DeliveryErrorCategory.RATE_LIMIT,
                retry_after=_retry_after_delay(error),
            ) from error
        except TimedOut as error:
            raise AmbiguousDeliveryTimeout(str(error)) from error
        except Forbidden as error:
            raise PermanentDeliveryError(
                str(error),
                category=DeliveryErrorCategory.PERMISSION,
            ) from error
        except BadRequest as error:
            raise PermanentDeliveryError(
                str(error),
                category=DeliveryErrorCategory.INVALID_REQUEST,
            ) from error
        except NetworkError as error:
            raise RetryableDeliveryError(
                str(error),
                category=DeliveryErrorCategory.NETWORK,
                outcome_ambiguous=True,
            ) from error
        except TelegramError as error:
            raise PermanentDeliveryError(
                str(error),
                category=DeliveryErrorCategory.PROVIDER,
            ) from error

    async def _deliver_message(self, payload: JsonObject) -> DeliveryReceipt:
        """@brief 投递已校验文本消息 / Deliver a validated text message."""

        parsed = parse_send_message_payload(payload)
        sent = await self._bot.send_message(
            chat_id=parsed.chat_id,
            text=parsed.text,
            parse_mode=parsed.parse_mode,
            disable_notification=parsed.disable_notification,
            protect_content=parsed.protect_content,
            message_thread_id=parsed.message_thread_id,
            reply_parameters=(
                ReplyParameters(message_id=parsed.reply_to_message_id)
                if parsed.reply_to_message_id is not None
                else None
            ),
            link_preview_options=_link_preview_options(parsed.disable_web_page_preview),
        )
        return DeliveryReceipt(str(sent.message_id))

    async def _deliver_edit(self, payload: JsonObject) -> DeliveryReceipt:
        """@brief 投递已校验消息编辑 / Deliver a validated message edit."""

        parsed = parse_edit_message_payload(payload)
        edited = await self._bot.edit_message_text(
            chat_id=parsed.chat_id,
            message_id=parsed.message_id,
            text=parsed.text,
            parse_mode=parsed.parse_mode,
            link_preview_options=_link_preview_options(parsed.disable_web_page_preview),
        )
        if edited is True:
            return DeliveryReceipt(str(parsed.message_id))
        if edited is False:
            raise PermanentDeliveryError(
                "Telegram returned False for edit_message_text",
                category=DeliveryErrorCategory.PROVIDER,
            )
        return DeliveryReceipt(str(edited.message_id))

    async def _deliver_sticker(self, payload: JsonObject) -> DeliveryReceipt:
        """@brief 解析 pack/emoji 并投递贴纸 / Resolve pack/emoji and deliver a sticker."""

        parsed = parse_send_sticker_payload(payload)
        sticker_set = await self._bot.get_sticker_set(parsed.pack_name)
        sticker = _first_matching_sticker(sticker_set.stickers, emoji=parsed.emoji)
        if sticker is None:
            raise PermanentDeliveryError(
                "Sticker pack does not contain the requested emoji",
                category=DeliveryErrorCategory.INVALID_REQUEST,
            )
        sent = await self._bot.send_sticker(
            chat_id=parsed.chat_id,
            sticker=sticker,
            message_thread_id=parsed.message_thread_id,
        )
        return DeliveryReceipt(str(sent.message_id))

    async def _deliver_photo(self, payload: JsonObject) -> DeliveryReceipt:
        """@brief 投递已校验远程图片 / Deliver a validated remote photo."""

        parsed = parse_send_photo_payload(payload)
        reply_markup = None
        if parsed.button_text is not None:
            reply_markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            parsed.button_text,
                            callback_data=parsed.button_callback_data,
                        )
                    ]
                ]
            )
        sent = await self._bot.send_photo(
            chat_id=parsed.chat_id,
            photo=parsed.photo_url,
            caption=parsed.caption,
            reply_markup=reply_markup,
            has_spoiler=parsed.has_spoiler,
            message_thread_id=parsed.message_thread_id,
            reply_parameters=(
                ReplyParameters(message_id=parsed.reply_to_message_id)
                if parsed.reply_to_message_id is not None
                else None
            ),
        )
        return DeliveryReceipt(str(sent.message_id))

    async def _deliver_artifact(self, payload: JsonObject) -> DeliveryReceipt:
        """@brief 领取、投递并确认 durable artifact / Claim, deliver, and acknowledge a durable artifact."""

        parsed = parse_send_artifact_payload(payload)
        artifacts = self._artifacts
        if artifacts is None:
            raise OutboundPayloadError(
                "Artifact delivery is not configured",
                category=DeliveryErrorCategory.UNSUPPORTED_KIND,
            )
        claim = artifacts.claim(parsed.artifact_id, expected_kind=parsed.kind)
        if claim is None:
            raise OutboundPayloadError(
                "Artifact is missing, expired, or already claimed",
                category=DeliveryErrorCategory.INVALID_PAYLOAD,
            )
        try:
            if (
                claim.record.filename != parsed.filename
                or claim.record.mime_type != parsed.mime_type
                or claim.record.size_bytes != parsed.size_bytes
            ):
                raise OutboundPayloadError(
                    "Artifact manifest does not match outbox payload",
                    category=DeliveryErrorCategory.INVALID_PAYLOAD,
                )
            with claim.path.open("rb") as handle:
                if parsed.kind is ArtifactKind.IMAGE:
                    sent = await self._bot.send_photo(
                        chat_id=parsed.chat_id,
                        photo=handle,
                    )
                else:
                    sent = await self._bot.send_voice(
                        chat_id=parsed.chat_id,
                        voice=handle,
                        filename=parsed.filename,
                    )
        except BaseException:
            artifacts.release(claim)
            raise
        artifacts.complete(claim)
        return DeliveryReceipt(str(sent.message_id))


def parse_send_message_payload(payload: JsonObject) -> SendMessagePayload:
    """@brief 严格解析 send_message 载荷 / Strictly parse a send_message payload.

    @param payload 持久化 JSON 对象 / Persisted JSON object.
    @return 类型化载荷 / Typed payload.
    @raise OutboundPayloadError 缺少字段、字段多余或类型非法 / Missing, extra, or invalid fields.
    """

    _validate_keys(payload, allowed=_SEND_KEYS, required=frozenset({"chat_id", "text"}))
    return SendMessagePayload(
        chat_id=_chat_id(payload),
        text=_text(payload),
        parse_mode=_parse_mode(payload),
        disable_notification=_optional_bool(payload, "disable_notification"),
        protect_content=_optional_bool(payload, "protect_content"),
        message_thread_id=_optional_positive_int(payload, "message_thread_id"),
        reply_to_message_id=_optional_positive_int(payload, "reply_to_message_id"),
        disable_web_page_preview=_optional_bool(
            payload,
            "disable_web_page_preview",
        ),
    )


def parse_edit_message_payload(payload: JsonObject) -> EditMessagePayload:
    """@brief 严格解析 edit_message_text 载荷 / Strictly parse an edit_message_text payload.

    @param payload 持久化 JSON 对象 / Persisted JSON object.
    @return 类型化载荷 / Typed payload.
    @raise OutboundPayloadError 缺少字段、字段多余或类型非法 / Missing, extra, or invalid fields.
    """

    _validate_keys(
        payload,
        allowed=_EDIT_KEYS,
        required=frozenset({"chat_id", "message_id", "text"}),
    )
    message_id = _required_positive_int(payload, "message_id")
    return EditMessagePayload(
        chat_id=_chat_id(payload),
        message_id=message_id,
        text=_text(payload),
        parse_mode=_parse_mode(payload),
        disable_web_page_preview=_optional_bool(
            payload,
            "disable_web_page_preview",
        ),
    )


def parse_send_artifact_payload(payload: JsonObject) -> SendArtifactPayload:
    """@brief 严格解析 artifact payload / Strictly parse an artifact payload.

    @param payload 持久化 payload / Persisted payload.
    @return 类型化 payload / Typed payload.
    """

    _validate_keys(payload, allowed=_ARTIFACT_KEYS, required=_ARTIFACT_KEYS)
    artifact_id = payload["artifact_id"]
    kind = payload["kind"]
    filename = payload["filename"]
    mime_type = payload["mime_type"]
    size_bytes = payload["size_bytes"]
    if not isinstance(artifact_id, str) or not artifact_id:
        raise _payload_error("artifact_id must be a non-empty string")
    if not isinstance(kind, str):
        raise _payload_error("kind must be a string")
    try:
        artifact_kind = ArtifactKind(kind)
    except ValueError as error:
        raise _payload_error("kind must be image or audio") from error
    if not isinstance(filename, str) or not filename or len(filename) > 255:
        raise _payload_error("filename must be 1..255 characters")
    if not isinstance(mime_type, str) or not mime_type or len(mime_type) > 255:
        raise _payload_error("mime_type must be 1..255 characters")
    if (
        isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes < 1
    ):
        raise _payload_error("size_bytes must be a positive integer")
    return SendArtifactPayload(
        chat_id=_chat_id(payload),
        artifact_id=ArtifactId(artifact_id),
        kind=artifact_kind,
        filename=filename,
        mime_type=mime_type,
        size_bytes=size_bytes,
    )


def parse_send_sticker_payload(payload: JsonObject) -> SendStickerPayload:
    """@brief 严格解析 pack/emoji sticker 载荷 / Strictly parse a pack-and-emoji sticker payload.

    @param payload 持久化 payload / Persisted payload.
    @return 类型化 sticker 意图 / Typed sticker intent.
    @raise OutboundPayloadError 字段集、pack 或 emoji 非法 / Invalid keys, pack, or emoji.
    """

    _validate_keys(
        payload,
        allowed=_STICKER_KEYS,
        required=frozenset({"chat_id", "pack_name", "emoji"}),
    )
    pack_name = payload["pack_name"]
    emoji = payload["emoji"]
    if (
        not isinstance(pack_name, str)
        or not 1 <= len(pack_name) <= _MAX_STICKER_PACK_LENGTH
        or _STICKER_PACK_PATTERN.fullmatch(pack_name) is None
    ):
        raise _payload_error(
            "pack_name must start with an ASCII letter and contain only letters, digits, or underscores"
        )
    if (
        not isinstance(emoji, str)
        or not 1 <= len(emoji) <= _MAX_STICKER_EMOJI_LENGTH
        or any(character.isspace() for character in emoji)
    ):
        raise _payload_error(
            "emoji must be a non-empty bounded sequence without whitespace"
        )
    return SendStickerPayload(
        chat_id=_chat_id(payload),
        pack_name=pack_name,
        emoji=emoji,
        message_thread_id=_optional_positive_int(payload, "message_thread_id"),
    )


def parse_send_photo_payload(payload: JsonObject) -> SendPhotoPayload:
    """@brief 严格解析 remote-photo 载荷 / Strictly parse a remote-photo payload."""

    _validate_keys(
        payload,
        allowed=_PHOTO_KEYS,
        required=frozenset({"chat_id", "photo_url", "caption", "has_spoiler"}),
    )
    photo_url = payload["photo_url"]
    if not isinstance(photo_url, str) or len(photo_url) > 2048:
        raise _payload_error("photo_url must be a bounded HTTPS URL")
    try:
        parsed_url = urlsplit(photo_url)
        hostname = parsed_url.hostname
    except ValueError as error:
        raise _payload_error("photo_url must be a bounded HTTPS URL") from error
    if (
        parsed_url.scheme != "https"
        or not hostname
        or parsed_url.username is not None
        or parsed_url.password is not None
    ):
        raise _payload_error("photo_url must be a bounded HTTPS URL")
    caption = payload["caption"]
    if not isinstance(caption, str) or not 1 <= len(caption) <= 1024:
        raise _payload_error("caption must be 1..1024 characters")
    has_spoiler = payload["has_spoiler"]
    if not isinstance(has_spoiler, bool):
        raise _payload_error("has_spoiler must be a Boolean")
    button_text = payload.get("button_text")
    callback_data = payload.get("button_callback_data")
    if (button_text is None) != (callback_data is None):
        raise _payload_error(
            "button_text and button_callback_data must appear together"
        )
    if button_text is not None and (
        not isinstance(button_text, str) or not 1 <= len(button_text) <= 128
    ):
        raise _payload_error("button_text must be 1..128 characters")
    if callback_data is not None and (
        not isinstance(callback_data, str)
        or not 1 <= len(callback_data.encode("utf-8")) <= 64
    ):
        raise _payload_error("button_callback_data must be 1..64 UTF-8 bytes")
    return SendPhotoPayload(
        chat_id=_chat_id(payload),
        photo_url=photo_url,
        caption=caption,
        has_spoiler=has_spoiler,
        message_thread_id=_optional_positive_int(payload, "message_thread_id"),
        reply_to_message_id=_optional_positive_int(payload, "reply_to_message_id"),
        button_text=button_text,
        button_callback_data=callback_data,
    )


def _first_matching_sticker(
    stickers: Sequence[Sticker],
    *,
    emoji: str,
) -> Sticker | None:
    """@brief 按 sticker-set 规范顺序选择第一个 emoji 匹配 / Select the first emoji match in canonical sticker-set order.

    @param stickers Telegram sticker-set 顺序 / Telegram sticker-set order.
    @param emoji 精确目标 emoji / Exact target emoji.
    @return 第一个匹配或 None / First match or None.
    """

    return next((sticker for sticker in stickers if sticker.emoji == emoji), None)


def _validate_keys(
    payload: JsonObject,
    *,
    allowed: frozenset[str],
    required: frozenset[str],
) -> None:
    """@brief 校验 payload 字段集合 / Validate the payload key set.

    @param payload 持久化 JSON 对象 / Persisted JSON object.
    @param allowed 允许字段 / Allowed fields.
    @param required 必需字段 / Required fields.
    @return None / None.
    @raise OutboundPayloadError 字段集合非法 / Invalid key set.
    """

    keys = frozenset(payload)
    missing = sorted(required - keys)
    unknown = sorted(keys - allowed)
    if missing:
        raise _payload_error(f"Missing required outbound fields: {', '.join(missing)}")
    if unknown:
        raise _payload_error(f"Unknown outbound fields: {', '.join(unknown)}")


def _chat_id(payload: JsonObject) -> int | str:
    """@brief 读取 Telegram chat ID / Read a Telegram chat ID.

    @param payload 持久化 payload / Persisted payload.
    @return 数字 ID 或频道用户名 / Numeric ID or channel username.
    @raise OutboundPayloadError chat_id 非法 / Invalid chat_id.
    """

    value = payload["chat_id"]
    if isinstance(value, bool):
        raise _payload_error("chat_id must be an integer or non-empty string")
    if isinstance(value, int):
        if value == 0:
            raise _payload_error("chat_id cannot be zero")
        return value
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise _payload_error("chat_id must be an integer or non-empty string")


def _text(payload: JsonObject) -> str:
    """@brief 读取并校验 Telegram 文本 / Read and validate Telegram text.

    @param payload 持久化 payload / Persisted payload.
    @return 非空文本 / Non-empty text.
    @raise OutboundPayloadError 文本非法 / Invalid text.
    """

    value = payload["text"]
    if not isinstance(value, str) or not value:
        raise _payload_error("text must be a non-empty string")
    if len(value) > _MAX_TEXT_LENGTH:
        raise _payload_error(f"text cannot exceed {_MAX_TEXT_LENGTH} characters")
    return value


def _parse_mode(payload: JsonObject) -> str | None:
    """@brief 读取可选 parse_mode / Read an optional parse_mode.

    @param payload 持久化 payload / Persisted payload.
    @return parse_mode 或 None / Parse mode or None.
    @raise OutboundPayloadError parse_mode 不受支持 / Unsupported parse_mode.
    """

    value = payload.get("parse_mode")
    if value is None:
        return None
    if isinstance(value, str) and value in _PARSE_MODES:
        return value
    raise _payload_error("parse_mode must be one of HTML, Markdown, or MarkdownV2")


def _required_positive_int(payload: JsonObject, key: str) -> int:
    """@brief 读取必需正整数 / Read a required positive integer.

    @param payload 持久化 payload / Persisted payload.
    @param key 字段名 / Field name.
    @return 正整数 / Positive integer.
    @raise OutboundPayloadError 字段非法 / Invalid field.
    """

    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _payload_error(f"{key} must be a positive integer")
    return value


def _optional_positive_int(payload: JsonObject, key: str) -> int | None:
    """@brief 读取可选正整数 / Read an optional positive integer.

    @param payload 持久化 payload / Persisted payload.
    @param key 字段名 / Field name.
    @return 正整数或 None / Positive integer or None.
    @raise OutboundPayloadError 字段非法 / Invalid field.
    """

    if payload.get(key) is None:
        return None
    return _required_positive_int(payload, key)


def _optional_bool(payload: JsonObject, key: str) -> bool | None:
    """@brief 读取可选布尔值 / Read an optional Boolean.

    @param payload 持久化 payload / Persisted payload.
    @param key 字段名 / Field name.
    @return 布尔值或 None / Boolean or None.
    @raise OutboundPayloadError 字段非法 / Invalid field.
    """

    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    raise _payload_error(f"{key} must be a Boolean")


def _payload_error(message: str) -> OutboundPayloadError:
    """@brief 创建统一 payload 错误 / Create a normalized payload error.

    @param message 错误详情 / Error detail.
    @return 永久 payload 错误 / Permanent payload error.
    """

    return OutboundPayloadError(
        message,
        category=DeliveryErrorCategory.INVALID_PAYLOAD,
    )


def _link_preview_options(
    disabled: bool | None,
) -> LinkPreviewOptions | None:
    """@brief 转换业务预览标志为现代 PTB 类型 / Convert the business preview flag to the modern PTB type.

    @param disabled 是否禁用链接预览 / Whether link previews are disabled.
    @return PTB LinkPreviewOptions 或 None / PTB LinkPreviewOptions or None.
    """

    return LinkPreviewOptions(is_disabled=disabled) if disabled is not None else None


def _retry_after_delay(error: RetryAfter) -> timedelta:
    """@brief 归一化 PTB RetryAfter 延迟 / Normalize a PTB RetryAfter delay.

    @param error PTB 限流异常 / PTB rate-limit exception.
    @return 正 timedelta / Positive timedelta.
    """

    value = error.retry_after
    delay = value if isinstance(value, timedelta) else timedelta(seconds=value)
    return max(delay, timedelta(milliseconds=1))


__all__ = [
    "EditMessagePayload",
    "SendArtifactPayload",
    "SendMessagePayload",
    "SendPhotoPayload",
    "SendStickerPayload",
    "TelegramOutboxDeliveryAdapter",
    "parse_edit_message_payload",
    "parse_send_artifact_payload",
    "parse_send_message_payload",
    "parse_send_photo_payload",
    "parse_send_sticker_payload",
]
