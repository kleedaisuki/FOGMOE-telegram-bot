"""Telegram 图片 adapter 的外部副作用故障窗口测试 / External-side-effect failure-window tests for Telegram picture adapters."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fogmoe_bot.application.media.picture_ports import PictureDeliveryTarget
from fogmoe_bot.application.media.picture_service import (
    HdDeliveryReady,
    PicturePolicy,
    PictureReady,
)
from fogmoe_bot.domain.media.identifiers import ArtifactId, UserId
from fogmoe_bot.domain.media.picture import (
    HdOffer,
    HdOfferState,
    PictureCandidate,
    PictureRating,
)
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
)
from fogmoe_bot.infrastructure.telegram.outbox_delivery import (
    parse_send_photo_payload,
)
from fogmoe_bot.presentation.telegram.media_handlers import picture as picture_handlers


class _PictureServiceFake:
    """@brief 可注入故障的媒体 service fake / Media-service fake with injectable failures."""

    def __init__(self, offer: HdOffer) -> None:
        """@brief 创建 fake / Create the fake.

        @param offer 测试报价 / Test offer.
        """

        self.policy = PicturePolicy()
        """@brief handler 可见策略 / Handler-visible policy."""
        self.offer = offer
        """@brief 返回给 handler 的报价 / Offer returned to the handler."""
        self.complete_hd = AsyncMock()
        """@brief 高清确认 mock / HD-confirmation mock."""
        self.refund_hd = AsyncMock()
        """@brief 高清退款 mock / HD-refund mock."""
        self.request_picture_kwargs: dict[str, object] = {}
        """@brief 图片请求输入 / Picture-request inputs."""

    async def request_picture(self, **kwargs: object) -> PictureReady:
        """@brief 返回已扣费预览 / Return a charged preview.

        @param kwargs 被 adapter 传入的参数 / Arguments passed by the adapter.
        @return 可发送预览 / Deliverable preview.
        """

        self.request_picture_kwargs = dict(kwargs)
        return PictureReady(self.offer, self.policy.preview_cost)

    async def request_hd(self, **kwargs: object) -> HdDeliveryReady:
        """@brief 返回已扣费高清内容 / Return charged HD content.

        @param kwargs 被 adapter 传入的参数 / Arguments passed by the adapter.
        @return 可发送高清内容 / Deliverable HD content.
        """

        charged = HdOffer(
            offer_id=self.offer.offer_id,
            picture=self.offer.picture,
            requester_id=self.offer.requester_id,
            expires_at=self.offer.expires_at,
            state=HdOfferState.CHARGED,
            charged_user_id=UserId(42),
        )
        return HdDeliveryReady(
            offer=charged,
            content=b"full-image",
            filename="full.jpg",
            fallback_url="https://example.test/full.jpg",
        )


def _offer() -> HdOffer:
    """@brief 构造 handler 测试报价 / Build a handler-test offer.

    @return 测试报价 / Test offer.
    """

    from datetime import UTC, datetime, timedelta

    return HdOffer(
        offer_id=ArtifactId("a" * 32),
        picture=PictureCandidate(
            source_id="handler-picture",
            sample_url="https://example.test/sample.jpg",
            file_url="https://example.test/full.jpg",
            tags="safe",
            width=100,
            height=100,
            file_size=1000,
            score=1,
            rating=PictureRating.SAFE,
        ),
        requester_id=UserId(42),
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )


def _picture_adapter(service: _PictureServiceFake) -> tuple[object, object, object]:
    """@brief 构造图片命令 adapter fakes / Build picture-command adapter fakes.

    @param service 媒体 service fake / Media-service fake.
    @return update、context 与 source message / Update, context, and source message.
    """

    message = SimpleNamespace(
        chat_id=100,
        message_id=200,
        message_thread_id=7,
        reply_text=AsyncMock(),
    )
    update = SimpleNamespace(
        update_id=321,
        effective_user=SimpleNamespace(id=42, username="klee"),
        effective_message=message,
    )
    context = SimpleNamespace(
        args=(),
        bot=SimpleNamespace(send_photo=AsyncMock()),
    )
    return update, context, message


def _hd_adapter(service: _PictureServiceFake) -> tuple[object, object, object]:
    """@brief 构造高清 callback adapter fakes / Build HD-callback adapter fakes.

    @param service 媒体 service fake / Media-service fake.
    @return update、context 与 callback query / Update, context, and callback query.
    """

    query = SimpleNamespace(
        data=f"pic_hd_{service.offer.offer_id}",
        message=SimpleNamespace(message_id=200),
        answer=AsyncMock(),
        edit_message_caption=AsyncMock(),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42, username="klee"),
        effective_chat=SimpleNamespace(id=100),
        callback_query=query,
    )
    context = SimpleNamespace(
        bot=SimpleNamespace(send_document=AsyncMock(), send_message=AsyncMock()),
    )
    return update, context, query


def test_picture_success_is_committed_to_outbox_without_direct_telegram_send(
    monkeypatch,
) -> None:
    """@brief 图片成功路径仅提交 outbox 且携带 source key / Picture success only commits an outbox with its source key."""

    async def scenario() -> None:
        """@brief 执行确认失败分支 / Exercise the confirmation-failure branch.

        @return None / None.
        """

        service = _PictureServiceFake(_offer())
        monkeypatch.setattr(picture_handlers, "_service", lambda context: service)
        update, context, message = _picture_adapter(service)

        await picture_handlers.pic_command(update, context)

        context.bot.send_photo.assert_not_awaited()
        message.reply_text.assert_not_awaited()
        assert (
            service.request_picture_kwargs["idempotency_key"]
            == "telegram-update:321:media.pic"
        )
        target = service.request_picture_kwargs["target"]
        assert target.chat_id == 100
        assert target.message_thread_id == 7
        assert target.reply_to_message_id == 200

    asyncio.run(scenario())


def test_picture_outbound_factory_persists_canonical_photo_semantics() -> None:
    """@brief 图片 factory 持久化有序流、回复与高清 callback / The picture factory persists stream, reply, and HD callback semantics."""

    offer = _offer()
    target = PictureDeliveryTarget(
        conversation_id=ConversationId("assistant-user:42"),
        delivery_stream_id=DeliveryStreamId("telegram:primary:chat:100:thread:7"),
        chat_id=100,
        message_thread_id=7,
        reply_to_message_id=200,
        mention="@klee",
    )
    draft = picture_handlers.TelegramPicturePreviewOutboundFactory().create(
        target=target,
        offer=offer,
        preview_cost=5,
        hd_cost=10,
        idempotency_key="telegram-update:321:media.pic",
        created_at=offer.expires_at,
    )
    payload = parse_send_photo_payload(draft.payload)

    assert draft.turn_id is None
    assert draft.delivery_stream_id == target.delivery_stream_id
    assert payload.photo_url == offer.picture.preview_url
    assert payload.message_thread_id == 7
    assert payload.reply_to_message_id == 200
    assert payload.button_callback_data == f"pic_hd_{offer.offer_id}"


def test_delivered_hd_is_not_refunded_or_hidden_when_confirmation_fails(
    monkeypatch,
) -> None:
    """@brief 高清已发送但 confirm 失败时保留 callback 并禁止误退款 / A delivered HD item with failed confirmation keeps its callback and is not refunded."""

    async def scenario() -> None:
        """@brief 执行高清确认失败分支 / Exercise the HD-confirmation failure branch.

        @return None / None.
        """

        service = _PictureServiceFake(_offer())
        service.complete_hd.side_effect = RuntimeError("database unavailable")
        monkeypatch.setattr(picture_handlers, "_service", lambda context: service)
        update, context, query = _hd_adapter(service)

        await picture_handlers.hd_pic_callback(update, context)

        context.bot.send_document.assert_awaited_once()
        service.complete_hd.assert_awaited_once()
        service.refund_hd.assert_not_awaited()
        query.edit_message_caption.assert_not_awaited()

    asyncio.run(scenario())


def test_hd_callback_is_hidden_only_after_delivery_and_confirmation(
    monkeypatch,
) -> None:
    """@brief 高清发送与 confirm 都成功后才移除 callback / Remove the HD callback only after delivery and confirmation both succeed."""

    async def scenario() -> None:
        """@brief 执行高清成功分支 / Exercise the successful HD branch.

        @return None / None.
        """

        service = _PictureServiceFake(_offer())
        monkeypatch.setattr(picture_handlers, "_service", lambda context: service)
        update, context, query = _hd_adapter(service)

        await picture_handlers.hd_pic_callback(update, context)

        context.bot.send_document.assert_awaited_once()
        service.complete_hd.assert_awaited_once()
        service.refund_hd.assert_not_awaited()
        query.edit_message_caption.assert_awaited_once()

    asyncio.run(scenario())
