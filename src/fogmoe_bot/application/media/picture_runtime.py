"""由组合根拥有的图片有界运行状态 / Composition-owned bounded picture runtime state."""

from dataclasses import dataclass, field

from fogmoe_bot.domain.media.identifiers import UserId
from fogmoe_bot.domain.media.picture import PictureCandidate, PictureRating

from .runtime import AsyncBulkhead, BoundedTtlCache


@dataclass(slots=True)
class PictureRuntime:
    """一个进程实例拥有的全部图片缓存与 bulkhead / Picture caches and bulkheads owned by one process instance."""

    picture_batches: BoundedTtlCache[PictureRating, tuple[PictureCandidate, ...]] = (
        field(default_factory=lambda: BoundedTtlCache(capacity=2, ttl_seconds=30 * 60))
    )
    recent_pictures: BoundedTtlCache[UserId, tuple[str, ...]] = field(
        default_factory=lambda: BoundedTtlCache(capacity=4096, ttl_seconds=24 * 60 * 60)
    )
    gallery_bulkhead: AsyncBulkhead = field(
        default_factory=lambda: AsyncBulkhead(capacity=5, queue_timeout_seconds=2)
    )
