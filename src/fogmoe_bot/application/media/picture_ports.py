"""图片预览与高清领取的外部端口 / External ports for picture preview and HD claiming."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
)
from fogmoe_bot.domain.conversation.outbox import OutboundDraft
from fogmoe_bot.domain.media.identifiers import ArtifactId, UserId
from fogmoe_bot.domain.media.picture import HdOffer, PictureCandidate, PictureRating


@dataclass(frozen=True, slots=True)
class PictureDeliveryTarget:
    """图片预览的稳定投递语义 / Stable delivery semantics for a picture preview."""

    conversation_id: ConversationId
    delivery_stream_id: DeliveryStreamId
    chat_id: int
    message_thread_id: int | None
    reply_to_message_id: int
    mention: str

    def __post_init__(self) -> None:
        """校验与持久化 payload 相同的边界 / Validate the persisted payload boundaries."""

        if (
            isinstance(self.chat_id, bool)
            or not isinstance(self.chat_id, int)
            or self.chat_id == 0
        ):
            raise ValueError("Picture delivery chat_id must be a non-zero integer")
        if (
            isinstance(self.reply_to_message_id, bool)
            or not isinstance(self.reply_to_message_id, int)
            or self.reply_to_message_id < 1
        ):
            raise ValueError("Picture delivery reply_to_message_id must be positive")
        if self.message_thread_id is not None and (
            isinstance(self.message_thread_id, bool)
            or not isinstance(self.message_thread_id, int)
            or self.message_thread_id < 1
        ):
            raise ValueError("Picture delivery message_thread_id must be positive")
        if not self.mention.strip() or len(self.mention) > 128:
            raise ValueError("Picture delivery mention must be 1..128 characters")


class PicturePreviewOutboundFactory(Protocol):
    """将图片报价映射为严格出站意图 / Map a picture offer to a strict outbound intent."""

    def create(
        self,
        *,
        target: PictureDeliveryTarget,
        offer: HdOffer,
        preview_cost: int,
        hd_cost: int,
        idempotency_key: str,
        created_at: datetime,
    ) -> OutboundDraft:
        """创建无 Telegram I/O 的可持久化图片意图 / Create a persistable photo intent without Telegram I/O."""

        ...


class PictureSource(Protocol):
    """图库读取端口 / Picture-gallery read port."""

    async def fetch(
        self,
        rating: PictureRating,
        *,
        limit: int,
    ) -> tuple[PictureCandidate, ...]:
        """获取有界图片批次 / Fetch a bounded picture batch."""

        ...


class BinaryFetcher(Protocol):
    """有界远程二进制读取端口 / Bounded remote-binary fetch port."""

    async def fetch(
        self,
        url: str,
        *,
        max_bytes: int,
        timeout_seconds: float,
    ) -> bytes:
        """下载一个有界二进制 / Download one bounded binary object."""

        ...


class PreviewChargeState(Protocol):
    """原子预览扣费结果 / Atomic preview-charge result."""

    @property
    def charged(self) -> bool: ...

    @property
    def balance(self) -> int: ...

    @property
    def offer(self) -> HdOffer | None: ...

    @property
    def replayed(self) -> bool: ...

    @property
    def cost(self) -> int | None: ...


class HdClaimState(Protocol):
    """原子高清领取结果 / Atomic HD-claim result."""

    @property
    def code(self) -> str: ...

    @property
    def offer(self) -> HdOffer | None: ...

    @property
    def balance(self) -> int | None: ...


class PictureRepository(Protocol):
    """图片报价、回执与扣退款的原子持久化端口 / Atomic picture-offer, receipt, charge, and refund port."""

    async def charge_preview_and_store_offer(
        self,
        *,
        offer: HdOffer,
        cost: int,
        now: datetime,
        idempotency_key: str,
        request_fingerprint: str,
        outbound: OutboundDraft,
    ) -> PreviewChargeState: ...

    async def load_picture_receipt(
        self,
        *,
        idempotency_key: str,
        user_id: UserId,
        rating: PictureRating,
        request_fingerprint: str,
    ) -> PreviewChargeState | None: ...

    async def claim_hd(
        self,
        offer_id: ArtifactId,
        *,
        user_id: UserId,
        cost: int,
        now: datetime,
        lease_for: timedelta,
    ) -> HdClaimState: ...

    async def complete_hd(
        self,
        offer_id: ArtifactId,
        *,
        cost: int,
        now: datetime,
    ) -> None: ...

    async def refund_hd(
        self,
        offer_id: ArtifactId,
        *,
        cost: int,
        now: datetime,
    ) -> None: ...
