"""图片预览、报价与高清领取用例 / Picture preview, offer, and HD-claim use cases."""

from __future__ import annotations

import hashlib
import json
import random
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fogmoe_bot.domain.media.identifiers import ArtifactId, UserId
from fogmoe_bot.domain.media.picture import (
    HdOffer,
    PictureCandidate,
    PictureRating,
)

from .account import MediaAccountProfiles
from .picture_ports import (
    BinaryFetcher,
    PictureDeliveryTarget,
    PicturePreviewOutboundFactory,
    PictureRepository,
    PictureSource,
)
from .picture_runtime import PictureRuntime


PICTURE_SERVICE_DATA_KEY = "media.picture.service"


@dataclass(frozen=True, slots=True)
class PicturePolicy:
    """图片预览与高清领取的显式资源边界 / Explicit resource bounds for picture preview and HD claims."""

    preview_cost: int = 5
    hd_cost: int = 10
    nsfw_permission: int = 2
    gallery_batch_size: int = 200
    recent_limit: int = 32
    offer_ttl: timedelta = timedelta(minutes=30)
    hd_claim_lease: timedelta = timedelta(minutes=2)
    hd_max_bytes: int = 20 * 1024 * 1024
    hd_timeout_seconds: float = 30

    def __post_init__(self) -> None:
        """校验图片容量、费用与时限 / Validate picture capacities, costs, and durations."""

        numeric = (
            self.preview_cost,
            self.hd_cost,
            self.nsfw_permission,
            self.gallery_batch_size,
            self.recent_limit,
            self.hd_max_bytes,
        )
        if min(numeric) <= 0 or self.hd_timeout_seconds <= 0:
            raise ValueError("picture policy bounds must be positive")
        if self.offer_ttl <= timedelta(0) or self.hd_claim_lease <= timedelta(0):
            raise ValueError("picture durations must be positive")


@dataclass(frozen=True, slots=True)
class PictureHelp:
    """展示图片帮助 / Show picture help."""

    first_use: bool


@dataclass(frozen=True, slots=True)
class PictureNotRegistered:
    """图片请求用户未注册 / Picture requester is not registered."""


@dataclass(frozen=True, slots=True)
class PicturePermissionDenied:
    """NSFW 权限不足 / Insufficient NSFW permission."""

    required: int


@dataclass(frozen=True, slots=True)
class PictureInsufficientCoins:
    """图片请求金币不足 / Insufficient coins for a picture request."""

    required: int
    balance: int


@dataclass(frozen=True, slots=True)
class PictureUnavailable:
    """图片上游暂不可用 / Picture upstream is temporarily unavailable."""


@dataclass(frozen=True, slots=True)
class PictureReady:
    """已扣费且可发送的图片预览 / Charged picture preview ready for delivery."""

    offer: HdOffer
    cost: int
    replayed: bool = False


type PictureRequestResult = (
    PictureHelp
    | PictureNotRegistered
    | PicturePermissionDenied
    | PictureInsufficientCoins
    | PictureUnavailable
    | PictureReady
)


@dataclass(frozen=True, slots=True)
class HdUnavailable:
    """高清报价不可领取 / HD offer cannot be claimed."""

    code: str
    balance: int | None = None


@dataclass(frozen=True, slots=True)
class HdDeliveryReady:
    """已扣费且等待 Telegram 投递的高清结果 / Charged HD result awaiting Telegram delivery."""

    offer: HdOffer
    content: bytes | None
    filename: str
    fallback_url: str


type HdRequestResult = HdUnavailable | HdDeliveryReady
type PictureChoice = Callable[[Sequence[PictureCandidate]], PictureCandidate]
type IdFactory = Callable[[], str]
type UtcNow = Callable[[], datetime]


def _utc_now() -> datetime:
    """读取系统 UTC 时间 / Read system UTC time."""

    return datetime.now(UTC)


class PictureService:
    """协调图片报价持久语义与有界外部 I/O / Coordinate durable picture offers and bounded external I/O."""

    def __init__(
        self,
        *,
        accounts: MediaAccountProfiles,
        repository: PictureRepository,
        source: PictureSource,
        binary_fetcher: BinaryFetcher,
        runtime: PictureRuntime,
        preview_outbound: PicturePreviewOutboundFactory,
        policy: PicturePolicy = PicturePolicy(),
        choose: PictureChoice = random.choice,
        id_factory: IdFactory = lambda: uuid.uuid4().hex,
        now: UtcNow = _utc_now,
    ) -> None:
        self._accounts = accounts
        self._repository = repository
        self._source = source
        self._binary_fetcher = binary_fetcher
        self._runtime = runtime
        self._preview_outbound = preview_outbound
        self._policy = policy
        self._choose = choose
        self._id_factory = id_factory
        self._now = now

    @property
    def policy(self) -> PicturePolicy:
        return self._policy

    async def request_picture(
        self,
        *,
        user_id: UserId,
        rating: PictureRating,
        idempotency_key: str,
        target: PictureDeliveryTarget,
        explicit_help: bool = False,
    ) -> PictureRequestResult:
        """处理图片请求直到可投递预览 / Handle a picture request through preview readiness."""

        fingerprint = _request_fingerprint(
            user_id=user_id,
            rating=rating,
            target=target,
            explicit_help=explicit_help,
        )
        profile = await self._accounts.profile(user_id)
        replay = await self._repository.load_picture_receipt(
            idempotency_key=idempotency_key,
            user_id=user_id,
            rating=rating,
            request_fingerprint=fingerprint,
        )
        if replay is not None:
            if not replay.charged or replay.offer is None:
                raise RuntimeError("Committed picture receipt has no canonical offer")
            return PictureReady(
                offer=replay.offer,
                cost=replay.cost or self._policy.preview_cost,
                replayed=True,
            )
        seen_help = await self._runtime.help_seen.get(user_id)
        if explicit_help or seen_help is None:
            await self._runtime.help_seen.put(user_id, True)
            return PictureHelp(first_use=not explicit_help)
        if not profile.registered:
            return PictureNotRegistered()
        if (
            rating is PictureRating.NSFW
            and profile.permission < self._policy.nsfw_permission
        ):
            return PicturePermissionDenied(self._policy.nsfw_permission)
        if profile.coins < self._policy.preview_cost:
            return PictureInsufficientCoins(self._policy.preview_cost, profile.coins)

        candidate = await self._select_picture(user_id, rating)
        if candidate is None:
            return PictureUnavailable()
        now = self._now()
        offer = HdOffer(
            offer_id=ArtifactId(self._id_factory()),
            picture=candidate,
            requester_id=user_id,
            expires_at=now + self._policy.offer_ttl,
        )
        outbound = self._preview_outbound.create(
            target=target,
            offer=offer,
            preview_cost=self._policy.preview_cost,
            hd_cost=self._policy.hd_cost,
            idempotency_key=idempotency_key,
            created_at=now,
        )
        charged = await self._repository.charge_preview_and_store_offer(
            offer=offer,
            cost=self._policy.preview_cost,
            now=now,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            outbound=outbound,
        )
        if not charged.charged:
            return PictureInsufficientCoins(self._policy.preview_cost, charged.balance)
        canonical_offer = charged.offer
        if canonical_offer is None:
            raise RuntimeError("Successful picture charge has no canonical offer")
        recent = await self._runtime.recent_pictures.get(user_id) or ()
        updated = (*recent, canonical_offer.picture.source_id)[
            -self._policy.recent_limit :
        ]
        await self._runtime.recent_pictures.put(user_id, updated)
        return PictureReady(
            offer=canonical_offer,
            cost=self._policy.preview_cost,
            replayed=charged.replayed,
        )

    async def request_hd(
        self,
        *,
        offer_id: ArtifactId,
        user_id: UserId,
    ) -> HdRequestResult:
        """原子领取并准备高清媒体 / Atomically claim and prepare HD media."""

        claim = await self._repository.claim_hd(
            offer_id,
            user_id=user_id,
            cost=self._policy.hd_cost,
            now=self._now(),
            lease_for=self._policy.hd_claim_lease,
        )
        if claim.code != "claimed" or claim.offer is None:
            return HdUnavailable(claim.code, claim.balance)
        offer = claim.offer
        file_url = offer.picture.file_url
        if not file_url:
            await self._repository.refund_hd(
                offer_id,
                cost=self._policy.hd_cost,
                now=self._now(),
            )
            return HdUnavailable("missing")
        try:
            content = await self._runtime.download_bulkhead.run(
                lambda: self._binary_fetcher.fetch(
                    file_url,
                    max_bytes=self._policy.hd_max_bytes,
                    timeout_seconds=self._policy.hd_timeout_seconds,
                )
            )
        except Exception:
            content = None
        return HdDeliveryReady(
            offer=offer,
            content=content,
            filename=_filename_from_url(file_url, fallback=f"picture-{offer_id}.jpg"),
            fallback_url=file_url,
        )

    async def complete_hd(self, offer_id: ArtifactId) -> None:
        """确认高清投递 / Confirm HD delivery."""

        await self._repository.complete_hd(
            offer_id,
            cost=self._policy.hd_cost,
            now=self._now(),
        )

    async def refund_hd(self, offer_id: ArtifactId) -> None:
        """永久失败后幂等退款 / Idempotently refund after permanent failure."""

        await self._repository.refund_hd(
            offer_id,
            cost=self._policy.hd_cost,
            now=self._now(),
        )

    async def refresh_cache(self) -> None:
        """刷新两种图库 cache / Refresh both gallery caches."""

        for rating in PictureRating:
            try:
                pictures = await self._runtime.gallery_bulkhead.run(
                    lambda: self._source.fetch(
                        rating,
                        limit=self._policy.gallery_batch_size,
                    )
                )
            except Exception:
                continue
            if pictures:
                await self._runtime.picture_batches.put(rating, pictures)

    async def _select_picture(
        self,
        user_id: UserId,
        rating: PictureRating,
    ) -> PictureCandidate | None:
        """选择未近期展示图片 / Select a not-recently-shown picture."""

        pictures = await self._runtime.picture_batches.get(rating)
        if not pictures:
            try:
                pictures = await self._runtime.gallery_bulkhead.run(
                    lambda: self._source.fetch(
                        rating,
                        limit=self._policy.gallery_batch_size,
                    )
                )
            except Exception:
                return None
            if pictures:
                await self._runtime.picture_batches.put(rating, pictures)
        if not pictures:
            return None
        recent = set(await self._runtime.recent_pictures.get(user_id) or ())
        candidates = tuple(item for item in pictures if item.source_id not in recent)
        return self._choose(candidates or pictures)


def _request_fingerprint(
    *,
    user_id: UserId,
    rating: PictureRating,
    target: PictureDeliveryTarget,
    explicit_help: bool,
) -> str:
    """对影响图片副作用的请求语义取指纹 / Fingerprint picture-effect request semantics."""

    payload = {
        "user_id": int(user_id),
        "rating": rating.value,
        "conversation_id": str(target.conversation_id),
        "delivery_stream_id": str(target.delivery_stream_id),
        "chat_id": target.chat_id,
        "message_thread_id": target.message_thread_id,
        "reply_to_message_id": target.reply_to_message_id,
        "mention": target.mention,
        "explicit_help": explicit_help,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _filename_from_url(url: str, *, fallback: str) -> str:
    """从 URL 提取安全文件名 / Extract a safe filename from a URL."""

    tail = url.split("?", 1)[0].rsplit("/", 1)[-1].strip()
    if not tail or tail in {".", ".."}:
        return fallback
    return tail.replace("/", "_").replace("\\", "_")[:200]
