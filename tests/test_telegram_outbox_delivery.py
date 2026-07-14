"""@brief Telegram outbox 投递适配器测试 / Telegram outbox-delivery adapter tests."""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

import pytest
from telegram import (
    Bot,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    ReplyParameters,
    Sticker,
    StickerSet,
)
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut

from fogmoe_bot.application.conversation.outbox_worker import (
    AmbiguousDeliveryTimeout,
    DeliveryErrorCategory,
    OutboundPayloadError,
    PermanentDeliveryError,
    RetryableDeliveryError,
)
from fogmoe_bot.domain.conversation.payloads import JsonObject
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    MessageSequence,
    OutboundMessageId,
    TurnId,
)
from fogmoe_bot.domain.conversation.outbox import (
    EDIT_TELEGRAM_MESSAGE,
    SEND_TELEGRAM_ARTIFACT,
    SEND_TELEGRAM_MESSAGE,
    SEND_TELEGRAM_PHOTO,
    SEND_TELEGRAM_STICKER,
    OutboundDraft,
    OutboundKind,
    OutboundMessage,
    OutboundStatus,
)
from fogmoe_bot.domain.media.artifact import ArtifactKind
from fogmoe_bot.infrastructure.media.file_artifact_store import FileArtifactStore
from fogmoe_bot.infrastructure.telegram.outbox_delivery import (
    TelegramOutboxDeliveryAdapter,
    parse_send_message_payload,
    parse_send_photo_payload,
    parse_send_sticker_payload,
)


NOW = datetime(2026, 7, 11, 10, tzinfo=timezone.utc)
"""@brief 测试基准时间 / Test reference time."""


def _message(
    payload: JsonObject,
    *,
    kind: OutboundKind = SEND_TELEGRAM_MESSAGE,
) -> OutboundMessage:
    """@brief 构造 processing outbox 消息 / Build a processing outbox message.

    @param payload Telegram payload / Telegram payload.
    @param kind 出站动作类型 / Outbound action kind.
    @return 测试消息 / Test message.
    """

    return OutboundMessage(
        draft=OutboundDraft(
            message_id=OutboundMessageId.new(),
            conversation_id=ConversationId("assistant-user:7"),
            turn_id=TurnId.new(),
            delivery_stream_id=DeliveryStreamId("telegram:chat:7"),
            kind=kind,
            payload=payload,
            idempotency_key="answer:1",
            created_at=NOW,
        ),
        stream_sequence=MessageSequence(1),
        status=OutboundStatus.PROCESSING,
        version=1,
        attempt_count=1,
        next_attempt_at=None,
        updated_at=NOW + timedelta(seconds=1),
    )


class _TelegramMessage:
    """@brief 最小 Telegram Message 替身 / Minimal Telegram Message double."""

    def __init__(self, message_id: int) -> None:
        """@brief 保存消息 ID / Store the message ID.

        @param message_id Telegram 消息 ID / Telegram message ID.
        """

        self.message_id = message_id


def _sticker(file_id: str, emoji: str) -> Sticker:
    """@brief 构造 Telegram sticker 替身 / Build a Telegram sticker value.

    @param file_id 可观测测试 ID / Observable test ID.
    @param emoji sticker emoji / Sticker emoji.
    @return sticker 对象 / Sticker object.
    """

    return Sticker(
        file_id=file_id,
        file_unique_id=f"unique-{file_id}",
        width=512,
        height=512,
        is_animated=False,
        is_video=False,
        type="regular",
        emoji=emoji,
        set_name="WhiteWind",
    )


class _Bot:
    """@brief 可注入异常的 PTB Bot 替身 / PTB Bot double with injectable errors."""

    def __init__(self, error: Exception | None = None) -> None:
        """@brief 创建 Bot 替身 / Create the Bot double.

        @param error 调用时抛出的异常 / Exception raised by calls.
        """

        self.error = error
        self.send_calls: list[dict[str, object]] = []
        self.edit_calls: list[dict[str, object]] = []
        self.artifact_calls: list[tuple[str, int | str]] = []
        self.sticker_sets: dict[str, StickerSet] = {}
        self.sticker_set_calls: list[str] = []
        self.sticker_calls: list[dict[str, object]] = []
        self.photo_calls: list[dict[str, object]] = []

    async def send_message(
        self,
        *,
        chat_id: int | str,
        text: str,
        parse_mode: str | None,
        disable_notification: bool | None,
        protect_content: bool | None,
        message_thread_id: int | None,
        reply_parameters: ReplyParameters | None,
        link_preview_options: LinkPreviewOptions | None,
    ) -> _TelegramMessage:
        """@brief 记录 send_message / Record send_message.

        @return Telegram 消息替身 / Telegram message double.
        """

        self._raise_if_configured()
        self.send_calls.append(
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_notification": disable_notification,
                "protect_content": protect_content,
                "message_thread_id": message_thread_id,
                "reply_parameters": reply_parameters,
                "link_preview_options": link_preview_options,
            }
        )
        return _TelegramMessage(42)

    async def edit_message_text(
        self,
        *,
        chat_id: int | str,
        message_id: int,
        text: str,
        parse_mode: str | None,
        link_preview_options: LinkPreviewOptions | None,
    ) -> _TelegramMessage:
        """@brief 记录 edit_message_text / Record edit_message_text.

        @return Telegram 消息替身 / Telegram message double.
        """

        self._raise_if_configured()
        self.edit_calls.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": parse_mode,
                "link_preview_options": link_preview_options,
            }
        )
        return _TelegramMessage(message_id)

    async def send_photo(
        self,
        *,
        chat_id: int | str,
        photo: object,
        caption: str | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
        has_spoiler: bool | None = None,
        message_thread_id: int | None = None,
        reply_parameters: ReplyParameters | None = None,
    ) -> _TelegramMessage:
        """@brief 记录 artifact photo / Record an artifact photo."""

        self._raise_if_configured()
        if caption is None:
            self.artifact_calls.append(("image", chat_id))
        else:
            self.photo_calls.append(
                {
                    "chat_id": chat_id,
                    "photo": photo,
                    "caption": caption,
                    "reply_markup": reply_markup,
                    "has_spoiler": has_spoiler,
                    "message_thread_id": message_thread_id,
                    "reply_parameters": reply_parameters,
                }
            )
        assert photo
        return _TelegramMessage(88)

    async def send_voice(
        self,
        *,
        chat_id: int | str,
        voice: object,
        filename: str,
    ) -> _TelegramMessage:
        """@brief 记录 artifact voice / Record an artifact voice."""

        self._raise_if_configured()
        self.artifact_calls.append((filename, chat_id))
        assert voice
        return _TelegramMessage(89)

    async def get_sticker_set(self, name: str) -> StickerSet:
        """@brief 记录 sticker-set lookup / Record a sticker-set lookup.

        @param name sticker pack 名 / Sticker-pack name.
        @return 已配置 sticker set / Configured sticker set.
        """

        self._raise_if_configured()
        self.sticker_set_calls.append(name)
        return self.sticker_sets[name]

    async def send_sticker(
        self,
        *,
        chat_id: int | str,
        sticker: Sticker,
        message_thread_id: int | None,
    ) -> _TelegramMessage:
        """@brief 记录已解析 sticker 投递 / Record resolved-sticker delivery.

        @param chat_id 目标 chat / Target chat.
        @param sticker 从 pack 解析的 sticker / Sticker resolved from the pack.
        @param message_thread_id 可选话题 / Optional topic.
        @return Telegram 消息替身 / Telegram message double.
        """

        self._raise_if_configured()
        self.sticker_calls.append(
            {
                "chat_id": chat_id,
                "sticker": sticker,
                "message_thread_id": message_thread_id,
            }
        )
        return _TelegramMessage(90)

    def _raise_if_configured(self) -> None:
        """@brief 抛出配置异常 / Raise the configured exception.

        @return None / None.
        """

        if self.error is not None:
            raise self.error


def _adapter(bot: _Bot) -> TelegramOutboxDeliveryAdapter:
    """@brief 将测试 Bot 注入强类型 adapter / Inject a test Bot into the typed adapter.

    @param bot Bot 替身 / Bot double.
    @return Telegram adapter / Telegram adapter.
    """

    return TelegramOutboxDeliveryAdapter(cast(Bot, bot))


def test_send_message_returns_external_message_id() -> None:
    """@brief send_message 返回 Telegram message_id / send_message returns Telegram message_id."""

    bot = _Bot()
    receipt = asyncio.run(
        _adapter(bot).deliver(
            _message(
                {
                    "chat_id": -100,
                    "text": "hello",
                    "parse_mode": "HTML",
                    "disable_notification": True,
                    "reply_to_message_id": 5,
                    "disable_web_page_preview": True,
                }
            )
        )
    )

    assert receipt.external_message_id == "42"
    assert bot.send_calls[0]["chat_id"] == -100
    assert bot.send_calls[0]["parse_mode"] == "HTML"
    reply_parameters = bot.send_calls[0]["reply_parameters"]
    preview_options = bot.send_calls[0]["link_preview_options"]
    assert isinstance(reply_parameters, ReplyParameters)
    assert reply_parameters.message_id == 5
    assert isinstance(preview_options, LinkPreviewOptions)
    assert preview_options.is_disabled is True


def test_edit_message_returns_existing_external_id() -> None:
    """@brief edit_message_text 返回被编辑消息 ID / edit_message_text returns the edited message ID."""

    bot = _Bot()
    receipt = asyncio.run(
        _adapter(bot).deliver(
            _message(
                {"chat_id": -100, "message_id": 77, "text": "updated"},
                kind=EDIT_TELEGRAM_MESSAGE,
            )
        )
    )

    assert receipt.external_message_id == "77"
    assert bot.edit_calls[0]["message_id"] == 77


def test_artifact_delivery_claims_and_completes_only_through_outbox(
    tmp_path: Path,
) -> None:
    """@brief durable artifact 只由 outbox claim 投递 / A durable artifact is delivered only through an outbox claim.

    @param tmp_path 临时 artifact root / Temporary artifact root.
    """

    artifacts = FileArtifactStore(tmp_path)
    record = artifacts.store(
        kind=ArtifactKind.IMAGE,
        content=b"not-a-real-image-but-bot-is-a-double",
        filename="result.png",
        mime_type="image/png",
        ttl=timedelta(minutes=5),
        max_bytes=1024,
    )
    bot = _Bot()
    receipt = asyncio.run(
        TelegramOutboxDeliveryAdapter(cast(Bot, bot), artifacts=artifacts).deliver(
            _message(
                {
                    "chat_id": 7,
                    "artifact_id": str(record.artifact_id),
                    "kind": record.kind.value,
                    "filename": record.filename,
                    "mime_type": record.mime_type,
                    "size_bytes": record.size_bytes,
                },
                kind=SEND_TELEGRAM_ARTIFACT,
            )
        )
    )

    assert receipt.external_message_id == "88"
    assert bot.artifact_calls == [("image", 7)]
    assert artifacts.claim(record.artifact_id, expected_kind=ArtifactKind.IMAGE) is None


def test_sticker_delivery_resolves_the_first_exact_emoji_from_the_pack() -> None:
    """@brief sticker outbox 按 pack 顺序确定性选首个 emoji 匹配 / Sticker delivery deterministically selects the first exact emoji match."""

    first = _sticker("first", "😊")
    other = _sticker("other", "😢")
    duplicate = _sticker("duplicate", "😊")
    bot = _Bot()
    bot.sticker_sets["WhiteWind"] = StickerSet(
        "WhiteWind",
        "White Wind",
        (first, other, duplicate),
        "regular",
    )

    receipt = asyncio.run(
        _adapter(bot).deliver(
            _message(
                {
                    "chat_id": -100,
                    "pack_name": "WhiteWind",
                    "emoji": "😊",
                    "message_thread_id": 7,
                },
                kind=SEND_TELEGRAM_STICKER,
            )
        )
    )

    assert receipt.external_message_id == "90"
    assert bot.sticker_set_calls == ["WhiteWind"]
    assert bot.sticker_calls == [
        {"chat_id": -100, "sticker": first, "message_thread_id": 7}
    ]


def test_sticker_payload_rejects_file_id_and_missing_emoji() -> None:
    """@brief sticker payload 拒绝 file_id 注入与不存在 emoji / Sticker payload rejects file-ID injection and missing emoji."""

    with pytest.raises(OutboundPayloadError, match="Unknown outbound fields"):
        parse_send_sticker_payload(
            {
                "chat_id": 7,
                "pack_name": "WhiteWind",
                "emoji": "😊",
                "file_id": "arbitrary",
            }
        )

    bot = _Bot()
    bot.sticker_sets["WhiteWind"] = StickerSet(
        "WhiteWind",
        "White Wind",
        (_sticker("sad", "😢"),),
        "regular",
    )
    with pytest.raises(PermanentDeliveryError) as captured:
        asyncio.run(
            _adapter(bot).deliver(
                _message(
                    {"chat_id": 7, "pack_name": "WhiteWind", "emoji": "😊"},
                    kind=SEND_TELEGRAM_STICKER,
                )
            )
        )
    assert captured.value.category is DeliveryErrorCategory.INVALID_REQUEST


def test_photo_delivery_preserves_reply_thread_spoiler_and_optional_keyboard() -> None:
    """@brief photo outbox 保留回复、topic、spoiler 与通用 callback / Photo outbox preserves reply, topic, spoiler, and a generic callback."""

    bot = _Bot()
    receipt = asyncio.run(
        _adapter(bot).deliver(
            _message(
                {
                    "chat_id": -100,
                    "photo_url": "https://example.test/preview.jpg",
                    "caption": "@klee 的免费预览图片。",
                    "has_spoiler": True,
                    "message_thread_id": 7,
                    "reply_to_message_id": 99,
                    "button_text": "打开相关页面",
                    "button_callback_data": "music_page:example",
                },
                kind=SEND_TELEGRAM_PHOTO,
            )
        )
    )

    assert receipt.external_message_id == "88"
    call = bot.photo_calls[0]
    assert call["photo"] == "https://example.test/preview.jpg"
    assert call["has_spoiler"] is True
    assert call["message_thread_id"] == 7
    assert isinstance(call["reply_parameters"], ReplyParameters)
    assert call["reply_parameters"].message_id == 99
    assert isinstance(call["reply_markup"], InlineKeyboardMarkup)
    assert (
        call["reply_markup"].inline_keyboard[0][0].callback_data == "music_page:example"
    )


def test_photo_payload_rejects_non_https_and_partial_keyboard() -> None:
    """@brief photo parser 拒绝非 HTTPS 与不完整按钮 / Photo parsing rejects non-HTTPS URLs and partial buttons."""

    base = {
        "chat_id": 7,
        "photo_url": "http://example.test/preview.jpg",
        "caption": "caption",
        "has_spoiler": False,
    }
    with pytest.raises(OutboundPayloadError, match="HTTPS"):
        parse_send_photo_payload(base)
    with pytest.raises(OutboundPayloadError, match="appear together"):
        parse_send_photo_payload(
            {
                **base,
                "photo_url": "https://example.test/preview.jpg",
                "button_text": "HD",
            }
        )


def test_retry_after_is_classified_with_provider_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief RetryAfter 保留 provider 延迟 / RetryAfter preserves the provider delay."""

    monkeypatch.setenv("PTB_TIMEDELTA", "1")
    with pytest.raises(RetryableDeliveryError) as captured:
        asyncio.run(
            _adapter(_Bot(RetryAfter(timedelta(seconds=9)))).deliver(
                _message({"chat_id": 7, "text": "hello"})
            )
        )

    assert captured.value.category is DeliveryErrorCategory.RATE_LIMIT
    assert captured.value.retry_after == timedelta(seconds=9)
    assert captured.value.outcome_ambiguous is False


@pytest.mark.parametrize(
    "error, category",
    [
        (Forbidden("blocked"), DeliveryErrorCategory.PERMISSION),
        (BadRequest("bad payload"), DeliveryErrorCategory.INVALID_REQUEST),
    ],
)
def test_permanent_telegram_errors_are_not_retryable(
    error: Exception,
    category: DeliveryErrorCategory,
) -> None:
    """@brief Forbidden/BadRequest 映射永久错误 / Forbidden and BadRequest map to permanent errors.

    @param error PTB 异常 / PTB exception.
    @param category 预期分类 / Expected category.
    """

    with pytest.raises(PermanentDeliveryError) as captured:
        asyncio.run(
            _adapter(_Bot(error)).deliver(_message({"chat_id": 7, "text": "hello"}))
        )

    assert captured.value.category is category


def test_timeout_is_retryable_and_explicitly_ambiguous() -> None:
    """@brief TimedOut 明确表达可能已投递 / TimedOut explicitly records possible delivery."""

    with pytest.raises(AmbiguousDeliveryTimeout) as captured:
        asyncio.run(
            _adapter(_Bot(TimedOut("read timeout"))).deliver(
                _message({"chat_id": 7, "text": "hello"})
            )
        )

    assert captured.value.category is DeliveryErrorCategory.AMBIGUOUS_TIMEOUT
    assert captured.value.outcome_ambiguous is True


def test_network_error_is_retryable_and_conservatively_ambiguous() -> None:
    """@brief NetworkError 在预算内重试且保守标记未知 / NetworkError retries within budget and is conservatively ambiguous."""

    with pytest.raises(RetryableDeliveryError) as captured:
        asyncio.run(
            _adapter(_Bot(NetworkError("connection reset"))).deliver(
                _message({"chat_id": 7, "text": "hello"})
            )
        )

    assert captured.value.category is DeliveryErrorCategory.NETWORK
    assert captured.value.outcome_ambiguous is True


def test_payload_validation_rejects_unknown_and_mistyped_fields() -> None:
    """@brief payload validation 拒绝未知字段和 bool chat_id / Payload validation rejects unknown fields and Boolean chat IDs."""

    with pytest.raises(OutboundPayloadError, match="Unknown outbound fields"):
        parse_send_message_payload({"chat_id": 7, "text": "hello", "arbitrary": True})
    with pytest.raises(OutboundPayloadError, match="chat_id"):
        parse_send_message_payload({"chat_id": True, "text": "hello"})


def test_unknown_outbound_kind_is_a_permanent_payload_error() -> None:
    """@brief 未知 kind 永久失败 / Unknown kinds fail permanently."""

    with pytest.raises(OutboundPayloadError) as captured:
        asyncio.run(
            _adapter(_Bot()).deliver(
                _message(
                    {"chat_id": 7, "text": "hello"},
                    kind=OutboundKind("telegram.unknown"),
                )
            )
        )

    assert captured.value.category is DeliveryErrorCategory.UNSUPPORTED_KIND
