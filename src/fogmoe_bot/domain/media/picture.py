"""与传输和存储无关的图片领域模型 / Picture-domain models independent of transport and storage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .identifiers import ArtifactId, UserId


class PictureReceiptConflict(RuntimeError):
    """图片请求幂等键被复用为不同语义 / A picture-request idempotency key changed semantics."""


class PictureRating(StrEnum):
    """图片内容分级 / Picture content rating."""

    SAFE = "safe"
    NSFW = "nsfw"


class HdOfferState(StrEnum):
    """高清图片报价状态 / HD-picture offer state."""

    PREVIEW_PENDING = "preview_pending"
    AVAILABLE = "available"
    CHARGED = "charged"
    DELIVERED = "delivered"
    REFUNDED = "refunded"


@dataclass(frozen=True, slots=True)
class PictureCandidate:
    """上游图库返回的规范图片 / Canonical picture returned by an upstream gallery."""

    source_id: str
    sample_url: str | None
    file_url: str | None
    tags: str
    width: int | None
    height: int | None
    file_size: int | None
    score: int | None
    rating: PictureRating

    def __post_init__(self) -> None:
        """校验图片不变量 / Validate picture invariants."""

        if not self.source_id.strip():
            raise ValueError("source_id must not be blank")
        if not self.sample_url and not self.file_url:
            raise ValueError("picture requires sample_url or file_url")

    @property
    def preview_url(self) -> str:
        """返回优先预览 URL / Return the preferred preview URL."""

        return self.sample_url or self.file_url or ""


@dataclass(frozen=True, slots=True)
class HdOffer:
    """可跨重启恢复的高清图片报价 / Restart-resilient HD-picture offer."""

    offer_id: ArtifactId
    picture: PictureCandidate
    requester_id: UserId
    expires_at: datetime
    state: HdOfferState = HdOfferState.PREVIEW_PENDING
    charged_user_id: UserId | None = None
