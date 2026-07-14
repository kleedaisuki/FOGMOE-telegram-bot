"""@brief 免费图片预览用例 / Free picture-preview use case.

此模块不定义报价、扣费、高清领取或出站回执。这样 `/pic` 的能力边界本身就无法触及
余额写入，而不只是依赖 Telegram handler 的偶然调用方式。
/ This module defines no offers, charges, HD claims, or outbound receipts.  The capability
boundary itself therefore cannot reach a balance write; it does not merely rely on an accidental
Telegram-handler call path.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from fogmoe_bot.domain.media.identifiers import UserId
from fogmoe_bot.domain.media.picture import PictureCandidate, PictureRating

from .account import MediaAccountProfiles
from .picture_runtime import PictureRuntime
from .picture_source import PictureSource


PICTURE_SERVICE_DATA_KEY = "media.picture.service"
"""@brief 图片服务 capability 键 / Picture-service capability key."""


@dataclass(frozen=True, slots=True)
class PicturePolicy:
    """@brief 免费预览的显式资源边界 / Explicit bounds for free previews.

    @param nsfw_permission 查看 NSFW 所需权限等级 / Permission level required for NSFW.
    @param gallery_batch_size 单次图库读取上限 / Maximum candidates per gallery read.
    @param recent_limit 每位用户的近期去重窗口 / Per-user recent-item exclusion window.
    """

    nsfw_permission: int = 2
    gallery_batch_size: int = 200
    recent_limit: int = 32

    def __post_init__(self) -> None:
        """@brief 校验免费预览容量边界 / Validate free-preview capacity bounds.

        @return None / None.
        """

        if self.nsfw_permission < 0:
            raise ValueError("nsfw_permission must not be negative")
        if min(self.gallery_batch_size, self.recent_limit) <= 0:
            raise ValueError("picture policy bounds must be positive")


@dataclass(frozen=True, slots=True)
class PictureNotRegistered:
    """@brief 图片请求用户未注册 / Picture requester is not registered."""


@dataclass(frozen=True, slots=True)
class PicturePermissionDenied:
    """@brief NSFW 权限不足 / Insufficient NSFW permission.

    @param required 所需权限等级 / Required permission level.
    """

    required: int


@dataclass(frozen=True, slots=True)
class PictureUnavailable:
    """@brief 图片上游暂不可用 / Picture upstream is temporarily unavailable."""


@dataclass(frozen=True, slots=True)
class PictureFreeReady:
    """@brief 可直接投递的免费图片预览 / A free picture preview ready for direct delivery.

    @param picture 已选中的图片 / Selected picture.
    """

    picture: PictureCandidate


type PictureFreeRequestResult = (
    PictureFreeReady
    | PictureNotRegistered
    | PicturePermissionDenied
    | PictureUnavailable
)
"""@brief 免费图片请求的穷尽结果 / Exhaustive result of a free-picture request."""

type PictureChoice = Callable[[Sequence[PictureCandidate]], PictureCandidate]
"""@brief 图片候选选择函数 / Picture-candidate selection function."""


class PictureService:
    """@brief 协调免费图片准入、去重与有界上游读取 / Coordinate free admission, deduplication, and bounded upstream reads."""

    def __init__(
        self,
        *,
        accounts: MediaAccountProfiles,
        source: PictureSource,
        runtime: PictureRuntime,
        policy: PicturePolicy = PicturePolicy(),
        choose: PictureChoice = random.choice,
    ) -> None:
        """@brief 注入免费预览所需的最小依赖 / Inject the minimum dependencies for free previews.

        @param accounts 只读媒体准入资料端口 / Read-only media-admission profile port.
        @param source 有界图库读取端口 / Bounded gallery read port.
        @param runtime 组合根拥有的缓存与 bulkhead / Composition-owned caches and bulkhead.
        @param policy 免费预览资源边界 / Free-preview resource bounds.
        @param choose 候选选择策略 / Candidate selection strategy.
        @return None / None.
        """

        self._accounts = accounts
        self._source = source
        self._runtime = runtime
        self._policy = policy
        self._choose = choose

    @property
    def policy(self) -> PicturePolicy:
        """@brief 返回不可变免费预览策略 / Return the immutable free-preview policy.

        @return 当前策略 / Current policy.
        """

        return self._policy

    async def request_free_picture(
        self,
        *,
        user_id: UserId,
        rating: PictureRating,
    ) -> PictureFreeRequestResult:
        """@brief 选择免费随机预览且绝不触及货币适配器 / Select a free random preview without touching a money adapter.

        @param user_id 请求用户 / Requesting user.
        @param rating 安全或 NSFW 分级 / Safe or NSFW rating.
        @return 可投递图片或准入/上游错误 / Deliverable picture or an admission/upstream error.
        @note 此路径不读取或写入报价、receipt、金币余额或银行账本。/
            This path reads or writes neither offers, receipts, token balances, nor the bank ledger.
        """

        profile = await self._accounts.profile(user_id)
        if not profile.registered:
            return PictureNotRegistered()
        if (
            rating is PictureRating.NSFW
            and profile.permission < self._policy.nsfw_permission
        ):
            return PicturePermissionDenied(self._policy.nsfw_permission)
        candidate = await self._select_picture(user_id, rating)
        if candidate is None:
            return PictureUnavailable()
        recent = await self._runtime.recent_pictures.get(user_id) or ()
        updated = (*recent, candidate.source_id)[-self._policy.recent_limit :]
        await self._runtime.recent_pictures.put(user_id, updated)
        return PictureFreeReady(candidate)

    async def refresh_cache(self) -> None:
        """@brief 刷新两种内容分级的图库缓存 / Refresh gallery caches for both content ratings.

        @return None / None.
        @note 单个上游失败不影响另一分级或已有缓存 / One upstream failure does not affect the other rating or cached items.
        """

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
        """@brief 选择未在近期窗口出现的图片 / Select a picture outside the recent window.

        @param user_id 请求用户 / Requesting user.
        @param rating 内容分级 / Content rating.
        @return 一个图片候选；无可用上游时为 None / A candidate, or None when the upstream is unavailable.
        """

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
