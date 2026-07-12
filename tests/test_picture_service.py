"""图片应用服务的语义与持久 callback 测试 / Semantic and durable-callback tests for the picture service."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from fogmoe_bot.application.media.picture_ports import PictureDeliveryTarget
from fogmoe_bot.application.media.picture_runtime import PictureRuntime
from fogmoe_bot.application.media.picture_service import (
    HdDeliveryReady,
    PicturePolicy,
    PictureHelp,
    PictureReady,
    PictureService,
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
    OutboundMessageId,
)
from fogmoe_bot.domain.conversation.outbox import (
    SEND_TELEGRAM_PHOTO,
    OutboundDraft,
)


@dataclass(frozen=True)
class Profile:
    """@brief 测试账户快照 / Test account snapshot."""

    registered: bool = True
    permission: int = 2
    coins: int = 100


@dataclass(frozen=True)
class Charge:
    """@brief 测试扣费结果 / Test charge result."""

    charged: bool
    balance: int
    offer: HdOffer | None = None
    replayed: bool = False
    cost: int | None = None


@dataclass(frozen=True)
class Claim:
    """@brief 测试高清领取结果 / Test HD-claim result."""

    code: str
    offer: HdOffer | None
    balance: int | None = None


class Repository:
    """@brief 保存 callback 的内存测试仓储 / In-memory test repository retaining callbacks."""

    def __init__(self) -> None:
        self.offers: dict[ArtifactId, HdOffer] = {}
        self.hd_completed = False
        self.receipts: dict[str, tuple[UserId, PictureRating, str, Charge]] = {}
        self.charge_calls = 0

    async def profile(self, user_id):
        return Profile()

    async def load_picture_receipt(
        self, *, idempotency_key, user_id, rating, request_fingerprint
    ):
        receipt = self.receipts.get(idempotency_key)
        if receipt is None:
            return None
        owner, stored_rating, fingerprint, result = receipt
        if (owner, stored_rating, fingerprint) != (
            user_id,
            rating,
            request_fingerprint,
        ):
            raise RuntimeError("test receipt conflict")
        return Charge(
            result.charged,
            result.balance,
            result.offer,
            replayed=True,
            cost=result.cost,
        )

    async def charge_preview_and_store_offer(
        self,
        *,
        offer,
        cost,
        now,
        idempotency_key,
        request_fingerprint,
        outbound,
    ):
        self.charge_calls += 1
        replay = await self.load_picture_receipt(
            idempotency_key=idempotency_key,
            user_id=offer.requester_id,
            rating=offer.picture.rating,
            request_fingerprint=request_fingerprint,
        )
        if replay is not None:
            return replay
        self.offers[offer.offer_id] = offer
        result = Charge(True, 100 - cost, offer=offer, cost=cost)
        self.receipts[idempotency_key] = (
            offer.requester_id,
            offer.picture.rating,
            request_fingerprint,
            result,
        )
        return result

    async def claim_hd(self, offer_id, *, user_id, cost, now, lease_for):
        offer = self.offers.get(offer_id)
        if offer is None:
            return Claim("missing", None)
        claimed = HdOffer(
            offer.offer_id,
            offer.picture,
            offer.requester_id,
            offer.expires_at,
            HdOfferState.CHARGED,
            user_id,
        )
        self.offers[offer_id] = claimed
        return Claim("claimed", claimed)

    async def complete_hd(self, offer_id, *, cost, now):
        self.hd_completed = True

    async def refund_hd(self, offer_id, *, cost, now):
        return None


class Pictures:
    """@brief 固定图库 / Fixed gallery."""

    async def fetch(self, rating, *, limit):
        return (
            PictureCandidate(
                source_id="42",
                sample_url="https://example.test/sample.jpg",
                file_url="https://example.test/full.jpg",
                tags="cat safe",
                width=1024,
                height=768,
                file_size=1234,
                score=9,
                rating=rating,
            ),
        )


class Fetcher:
    """@brief 固定二进制下载 / Fixed binary fetcher."""

    async def fetch(self, url, *, max_bytes, timeout_seconds):
        return b"full-image"


class PreviewOutbound:
    """@brief 构造确定性图片 outbox 的测试替身 / Deterministic photo-outbox test double."""

    def create(
        self,
        *,
        target,
        offer,
        preview_cost,
        hd_cost,
        idempotency_key,
        created_at,
    ):
        outbound_key = f"{idempotency_key}:photo"
        return OutboundDraft(
            message_id=OutboundMessageId.for_conversation(
                target.conversation_id,
                outbound_key,
            ),
            conversation_id=target.conversation_id,
            turn_id=None,
            delivery_stream_id=target.delivery_stream_id,
            kind=SEND_TELEGRAM_PHOTO,
            payload={"chat_id": target.chat_id, "photo_url": offer.picture.preview_url},
            idempotency_key=outbound_key,
            created_at=created_at,
        )


def _target() -> PictureDeliveryTarget:
    """@brief 创建稳定图片投递目标 / Create a stable picture-delivery target."""

    return PictureDeliveryTarget(
        conversation_id=ConversationId("assistant-user:1"),
        delivery_stream_id=DeliveryStreamId("telegram:primary:chat:10:thread:0"),
        chat_id=10,
        message_thread_id=None,
        reply_to_message_id=20,
        mention="@klee",
    )


def _service(repository: Repository) -> PictureService:
    """创建确定性图片服务 / Create a deterministic picture service."""

    ids = iter(("a" * 32, "b" * 32, "c" * 32))
    return PictureService(
        accounts=repository,
        repository=repository,
        source=Pictures(),
        binary_fetcher=Fetcher(),
        runtime=PictureRuntime(),
        preview_outbound=PreviewOutbound(),
        policy=PicturePolicy(),
        choose=lambda values: values[0],
        id_factory=lambda: next(ids),
        now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_picture_first_use_help_then_atomic_offer_and_hd() -> None:
    """@brief 保持首次帮助、预览扣费与高清领取语义 / Preserve first-help, preview-charge, and HD-claim semantics."""

    async def scenario() -> None:
        repository = Repository()
        service = _service(repository)
        first = await service.request_picture(
            user_id=UserId(1),
            rating=PictureRating.SAFE,
            idempotency_key="update:1:pic",
            target=_target(),
        )
        assert isinstance(first, PictureHelp)
        ready = await service.request_picture(
            user_id=UserId(1),
            rating=PictureRating.SAFE,
            idempotency_key="update:2:pic",
            target=_target(),
        )
        assert isinstance(ready, PictureReady)
        assert ready.offer.state is HdOfferState.PREVIEW_PENDING
        hd = await service.request_hd(
            offer_id=ready.offer.offer_id,
            user_id=UserId(2),
        )
        assert isinstance(hd, HdDeliveryReady)
        assert hd.content == b"full-image"
        assert hd.offer.charged_user_id == UserId(2)
        await service.complete_hd(ready.offer.offer_id)
        assert repository.hd_completed

    asyncio.run(scenario())


def test_picture_request_replays_the_first_canonical_offer_without_a_second_charge() -> (
    None
):
    """@brief 同一 source key 重放首次报价且不再扣费 / The same source key replays the first offer without charging twice."""

    async def scenario() -> None:
        repository = Repository()
        service = _service(repository)
        await service.request_picture(
            user_id=UserId(1),
            rating=PictureRating.SAFE,
            idempotency_key="update:help:pic",
            target=_target(),
        )
        first = await service.request_picture(
            user_id=UserId(1),
            rating=PictureRating.SAFE,
            idempotency_key="update:canonical:pic",
            target=_target(),
        )
        replay = await _service(repository).request_picture(
            user_id=UserId(1),
            rating=PictureRating.SAFE,
            idempotency_key="update:canonical:pic",
            target=_target(),
        )

        assert isinstance(first, PictureReady)
        assert isinstance(replay, PictureReady)
        assert replay.offer == first.offer
        assert replay.replayed
        assert repository.charge_calls == 1
        assert len(repository.offers) == 1

    asyncio.run(scenario())
