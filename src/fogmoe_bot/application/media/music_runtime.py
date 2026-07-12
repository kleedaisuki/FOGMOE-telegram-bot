"""由组合根拥有的音乐有界运行状态 / Composition-owned bounded music runtime state."""

from dataclasses import dataclass, field

from fogmoe_bot.domain.media.music import MusicPlatform, MusicTrack

from .runtime import AsyncBulkhead, BoundedTtlCache, SlidingWindowLimiter


@dataclass(slots=True)
class MusicRuntime:
    """一个进程实例拥有的音乐缓存、限流与 bulkhead / Music cache, limiter, and bulkhead owned by one process instance."""

    results: BoundedTtlCache[tuple[str, MusicPlatform], tuple[MusicTrack, ...]] = field(
        default_factory=lambda: BoundedTtlCache(capacity=512, ttl_seconds=5 * 60)
    )
    rate_limit: SlidingWindowLimiter = field(
        default_factory=lambda: SlidingWindowLimiter(
            capacity=4096,
            max_requests=5,
            window_seconds=10,
            cooldown_seconds=15,
        )
    )
    upstream_bulkhead: AsyncBulkhead = field(
        default_factory=lambda: AsyncBulkhead(capacity=8, queue_timeout_seconds=2)
    )
