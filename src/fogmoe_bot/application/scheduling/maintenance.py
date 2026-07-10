"""@brief 系统维护任务的 scheduling 适配器 / Scheduling adapters for system maintenance tasks."""

from __future__ import annotations

import logging
from datetime import timedelta

from fogmoe_bot.application.economy import shop
from fogmoe_bot.application.games import sicbo
from fogmoe_bot.application.media import music, pic
from fogmoe_bot.application.assistant.conversation_context_cache import CONVERSATION_CONTEXT_CACHE
from fogmoe_bot.domain.scheduling import JobKind, MaintenanceTask


logger = logging.getLogger(__name__)


class ShopRecordCleanup:
    """@brief 清理抽奖消息记录 / Clean expired shop lottery-message records."""

    task = MaintenanceTask(
        kind=JobKind("maintenance.shop-record-cleanup"),
        interval=timedelta(hours=1),
        initial_delay=timedelta(seconds=30),
    )

    async def handle(self) -> None:
        """@brief 执行消息记录清理 / Execute message-record cleanup.

        @return None / None.
        """

        await shop.cleanup_message_records()


class SicboGameCleanup:
    """@brief 清理过期骰宝局 / Clean expired Sic Bo game sessions."""

    task = MaintenanceTask(
        kind=JobKind("maintenance.sicbo-game-cleanup"),
        interval=timedelta(minutes=5),
        initial_delay=timedelta(seconds=120),
    )

    async def handle(self) -> None:
        """@brief 执行骰宝局清理 / Execute Sic Bo session cleanup.

        @return None / None.
        """

        await sicbo.cleanup_expired_games(None)


class ImageCacheRefresh:
    """@brief 刷新图片缓存 / Refresh image caches."""

    task = MaintenanceTask(
        kind=JobKind("maintenance.image-cache-refresh"),
        interval=timedelta(minutes=30),
        initial_delay=timedelta(seconds=60),
    )

    async def handle(self) -> None:
        """@brief 执行图片缓存刷新 / Execute image-cache refresh.

        @return None / None.
        """

        await pic.refresh_cache_job(None)


class ImageRequestCleanup:
    """@brief 清理图片请求状态 / Clean expired image-request state."""

    task = MaintenanceTask(
        kind=JobKind("maintenance.image-request-cleanup"),
        interval=timedelta(hours=1),
        initial_delay=timedelta(minutes=30),
    )

    async def handle(self) -> None:
        """@brief 执行图片请求清理 / Execute image-request cleanup.

        @return None / None.
        """

        await pic.clean_expired_requests(None)


class MusicRequestCleanup:
    """@brief 清理音乐请求状态 / Clean expired music-request state."""

    task = MaintenanceTask(
        kind=JobKind("maintenance.music-request-cleanup"),
        interval=timedelta(minutes=5),
        initial_delay=timedelta(seconds=90),
    )

    async def handle(self) -> None:
        """@brief 执行音乐请求清理 / Execute music-request cleanup.

        @return None / None.
        """

        await music.clean_expired_requests_job(None)


class ConversationContextCacheCleanup:
    """@brief 清理过期会话工作集 / Clean expired conversation working sets."""

    task = MaintenanceTask(
        kind=JobKind("maintenance.conversation-context-cache-cleanup"),
        interval=timedelta(minutes=5),
        initial_delay=timedelta(minutes=5),
    )

    async def handle(self) -> None:
        """@brief 回收已过期的 ContextState / Reclaim expired ContextState values.

        @return None / None.
        """

        removed = CONVERSATION_CONTEXT_CACHE.purge_expired()
        if removed:
            logger.info("Purged expired conversation contexts: count=%s", removed)


def maintenance_handlers() -> tuple[
    ShopRecordCleanup,
    SicboGameCleanup,
    ImageCacheRefresh,
    ImageRequestCleanup,
    MusicRequestCleanup,
    ConversationContextCacheCleanup,
]:
    """@brief 返回进程级维护任务处理器 / Return process-level maintenance task handlers.

    @return 可由 scheduling runtime 执行的处理器 / Handlers executable by the scheduling runtime.
    """

    return (
        ShopRecordCleanup(),
        SicboGameCleanup(),
        ImageCacheRefresh(),
        ImageRequestCleanup(),
        MusicRequestCleanup(),
        ConversationContextCacheCleanup(),
    )
