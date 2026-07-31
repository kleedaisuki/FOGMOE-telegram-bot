"""@brief 免费图片预览领域模型 / Free picture-preview domain model.

此模块拥有内容准入、候选批次、近期历史与选择决定；缓存、随机源和上游
调用仍由应用层编排。/ This module owns content admission, candidate batches,
recent history, and selection decisions; caches, randomness, and upstream calls remain orchestrated
by the application layer.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Self


class PictureRating(StrEnum):
    """@brief 图片内容分级 / Picture content rating."""

    SAFE = "safe"
    NSFW = "nsfw"


@dataclass(frozen=True, slots=True)
class PictureCandidate:
    """@brief 上游图库返回的规范只读候选 / Canonical read-only candidate returned by a gallery.

    @param source_id 图库内稳定标识 / Stable identifier within the gallery.
    @param sample_url 可选预览 URL / Optional preview URL.
    @param file_url 可选原文件 URL / Optional original-file URL.
    @param tags 空格分隔标签 / Space-separated tags.
    @param width 可选像素宽度 / Optional pixel width.
    @param height 可选像素高度 / Optional pixel height.
    @param file_size 可选文件字节数 / Optional file size in bytes.
    @param score 可选图库评分 / Optional gallery score.
    @param rating 内容分级 / Content rating.
    """

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
        """@brief 校验候选身份与可预览性 / Validate candidate identity and previewability.

        @return None / None.
        @raise TypeError 标识或分级类型不正确时抛出 /
            Raised when identifier or rating types are invalid.
        @raise ValueError 标识为空白或无任何可用 URL 时抛出 /
            Raised when the identifier is blank or no usable URL exists.
        """

        if not isinstance(self.source_id, str):
            raise TypeError("picture source_id must be a string")
        if not isinstance(self.rating, PictureRating):
            raise TypeError("picture candidate requires a PictureRating")
        if not self.source_id.strip():
            raise ValueError("source_id must not be blank")
        if not self.sample_url and not self.file_url:
            raise ValueError("picture requires sample_url or file_url")

    @property
    def preview_url(self) -> str:
        """@brief 返回优先预览 URL / Return the preferred preview URL.

        @return sample URL，缺失时返回 file URL / Sample URL, falling back to the file URL.
        """

        return self.sample_url or self.file_url or ""


@dataclass(frozen=True, slots=True)
class PicturePreviewPolicy:
    """@brief 免费预览的准入与资源边界 / Admission and resource bounds for free previews.

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
        @raise TypeError 边界不是严格整数时抛出 /
            Raised when a bound is not a strict integer.
        @raise ValueError 权限为负或容量非正时抛出 /
            Raised when permission is negative or a capacity is not positive.
        """

        values = (self.nsfw_permission, self.gallery_batch_size, self.recent_limit)
        if any(
            isinstance(value, bool) or not isinstance(value, int) for value in values
        ):
            raise TypeError("picture policy bounds must be integers")
        if self.nsfw_permission < 0:
            raise ValueError("nsfw_permission must not be negative")
        if min(self.gallery_batch_size, self.recent_limit) <= 0:
            raise ValueError("picture policy bounds must be positive")

    def decide_access(
        self,
        *,
        registered: bool,
        permission: int | None,
        rating: PictureRating,
    ) -> PicturePreviewAccess:
        """@brief 按既有顺序决定预览准入 / Decide preview access in the established order.

        @param registered 用户是否已注册 / Whether the user is registered.
        @param permission 仅 NSFW 已注册路径需要的权限 / Permission required only for registered NSFW access.
        @param rating 请求内容分级 / Requested content rating.
        @return 允许或显式拒绝原因 / Grant or an explicit denial reason.
        @note 未注册时忽略 permission；SAFE 路径也不读取它。/
            Permission is ignored for unregistered users and is not needed for SAFE requests.
        """

        if not isinstance(registered, bool):
            raise TypeError("picture registration state must be a bool")
        if not isinstance(rating, PictureRating):
            raise TypeError("picture access requires a PictureRating")
        if not registered:
            return PictureRegistrationRequired()
        if rating is PictureRating.SAFE:
            return PicturePreviewGranted(rating)
        if isinstance(permission, bool) or not isinstance(permission, int):
            raise TypeError("NSFW picture access requires an integer permission")
        if permission < self.nsfw_permission:
            return PicturePermissionRequired(self.nsfw_permission)
        return PicturePreviewGranted(rating)


@dataclass(frozen=True, slots=True)
class PicturePreviewGranted:
    """@brief 免费预览已获准入 / Free preview access is granted.

    @param rating 获准的内容分级 / Granted content rating.
    """

    rating: PictureRating

    def __post_init__(self) -> None:
        """@brief 校验获准分级 / Validate the granted rating.

        @return None / None.
        @raise TypeError 分级不是 ``PictureRating`` 时抛出 /
            Raised when the rating is not a ``PictureRating``.
        """

        if not isinstance(self.rating, PictureRating):
            raise TypeError("picture preview grant requires a PictureRating")


@dataclass(frozen=True, slots=True)
class PictureRegistrationRequired:
    """@brief 免费预览需要先注册 / Free preview requires registration."""


@dataclass(frozen=True, slots=True)
class PicturePermissionRequired:
    """@brief NSFW 预览权限不足 / NSFW preview permission is insufficient.

    @param required 所需最低权限 / Minimum required permission.
    """

    required: int

    def __post_init__(self) -> None:
        """@brief 校验所需权限等级 / Validate the required permission level.

        @return None / None.
        @raise TypeError 权限不是严格整数时抛出 /
            Raised when permission is not a strict integer.
        @raise ValueError 权限为负时抛出 / Raised when permission is negative.
        """

        if isinstance(self.required, bool) or not isinstance(self.required, int):
            raise TypeError("required picture permission must be an integer")
        if self.required < 0:
            raise ValueError("required picture permission must not be negative")


type PicturePreviewAccess = (
    PicturePreviewGranted | PictureRegistrationRequired | PicturePermissionRequired
)
"""@brief 免费图片准入的穷尽决定 / Exhaustive free-picture access decision."""


@dataclass(frozen=True, slots=True, init=False)
class RecentPictureHistory:
    """@brief 不可变的近期图片历史 / Immutable recent-picture history.

    恢复时保留完整快照，只在记录新图片时按当前策略裁剪，以保持配置收窄
    时的既有选择语义。/ Restoration retains the complete snapshot and applies the current
    bound only when recording a new picture, preserving established selection behavior when
    configuration narrows.
    """

    _source_ids: tuple[str, ...]
    """@brief 按时间排列的图库标识 / Gallery identifiers ordered by observation time."""
    _record_limit: int
    """@brief 下次记录时应用的裁剪上限 / Trimming bound applied on the next record."""

    @classmethod
    def restore(
        cls,
        source_ids: tuple[str, ...] | None,
        *,
        record_limit: int,
    ) -> Self:
        """@brief 从缓存快照恢复近期历史 / Restore recent history from a cache snapshot.

        @param source_ids 缓存值；缺失时为 None / Cached identifiers, or None when absent.
        @param record_limit 下次记录的容量上限 / Capacity bound for the next record.
        @return 不可变历史 / Immutable history.
        @raise TypeError 快照或标识类型不正确时抛出 /
            Raised when snapshot or identifier types are invalid.
        @raise ValueError 上限非正或标识为空白时抛出 /
            Raised when the bound is not positive or an identifier is blank.
        """

        if isinstance(record_limit, bool) or not isinstance(record_limit, int):
            raise TypeError("recent-picture limit must be an integer")
        if record_limit <= 0:
            raise ValueError("recent-picture limit must be positive")
        values = () if source_ids is None else source_ids
        if not isinstance(values, tuple):
            raise TypeError("recent-picture snapshot must be a tuple")
        if any(not isinstance(value, str) for value in values):
            raise TypeError("recent-picture identifiers must be strings")
        if any(not value.strip() for value in values):
            raise ValueError("recent-picture identifiers must not be blank")
        instance = object.__new__(cls)
        object.__setattr__(instance, "_source_ids", values)
        object.__setattr__(instance, "_record_limit", record_limit)
        return instance

    @property
    def source_ids(self) -> tuple[str, ...]:
        """@brief 返回不可变快照 / Return the immutable snapshot.

        @return 按观测时间排列的标识 / Identifiers ordered by observation time.
        """

        return self._source_ids

    def contains(self, source_id: str) -> bool:
        """@brief 判断图片是否在近期窗口 / Test whether a picture is in the recent window.

        @param source_id 图库标识 / Gallery identifier.
        @return 历史中存在则为 True / True when present in history.
        """

        return source_id in self._source_ids

    def record(self, candidate: PictureCandidate) -> Self:
        """@brief 追加已选图片并裁剪最旧项 / Append a selected picture and trim oldest entries.

        @param candidate 已选图片 / Selected picture.
        @return 新的历史值 / New history value.
        """

        if not isinstance(candidate, PictureCandidate):
            raise TypeError("recent-picture history records PictureCandidate values")
        return self.restore(
            (*self._source_ids, candidate.source_id)[-self._record_limit :],
            record_limit=self._record_limit,
        )


type PictureChoice = Callable[[Sequence[PictureCandidate]], PictureCandidate]
"""@brief 候选图片选择函数 / Picture-candidate choice function."""


@dataclass(frozen=True, slots=True, init=False)
class PictureGalleryBatch:
    """@brief 同一内容分级的非空图库批次 / Non-empty gallery batch for one content rating."""

    _rating: PictureRating
    """@brief 批次内容分级 / Content rating of the batch."""
    _pictures: tuple[PictureCandidate, ...]
    """@brief 保留上游顺序的候选 / Candidates preserving upstream order."""

    @classmethod
    def restore(
        cls,
        *,
        rating: PictureRating,
        pictures: tuple[PictureCandidate, ...],
    ) -> Self:
        """@brief 从缓存或上游批次恢复 / Restore a batch from cache or upstream.

        @param rating 请求分级 / Requested rating.
        @param pictures 不可变候选批次 / Immutable candidate batch.
        @return 已验证批次 / Validated batch.
        @raise TypeError 分级、批次或候选类型不正确时抛出 /
            Raised for invalid rating, batch, or candidate types.
        @raise ValueError 批次为空或包含其他分级时抛出 /
            Raised when the batch is empty or mixes ratings.
        """

        if not isinstance(rating, PictureRating):
            raise TypeError("picture gallery batch requires a PictureRating")
        if not isinstance(pictures, tuple):
            raise TypeError("picture gallery batch must be an immutable tuple")
        if not pictures:
            raise ValueError("picture gallery batch must not be empty")
        if not all(isinstance(picture, PictureCandidate) for picture in pictures):
            raise TypeError("picture gallery batch requires PictureCandidate values")
        if any(picture.rating is not rating for picture in pictures):
            raise ValueError("picture gallery batch candidates must match its rating")
        instance = object.__new__(cls)
        object.__setattr__(instance, "_rating", rating)
        object.__setattr__(instance, "_pictures", pictures)
        return instance

    @property
    def pictures(self) -> tuple[PictureCandidate, ...]:
        """@brief 返回已验证候选 / Return validated candidates.

        @return 保留上游顺序的元组 / Tuple preserving upstream order.
        """

        return self._pictures

    def select(
        self,
        *,
        recent: RecentPictureHistory,
        choose: PictureChoice,
    ) -> PictureSelection:
        """@brief 优先从未近期出现的候选中选择 / Prefer a candidate absent from recent history.

        @param recent 选择时的近期历史快照 / Recent-history snapshot at selection time.
        @param choose 仅调用一次的随机选择策略 / Random choice strategy invoked exactly once.
        @return 已选图片及是否因候选耗尽而复用 / Selected picture and whether exhaustion forced reuse.
        @note 过滤保留上游顺序；若全部在近期历史中，恢复完整批次供选择。/
            Filtering preserves upstream order; when every item is recent, the full batch becomes
            eligible again.
        """

        if not isinstance(recent, RecentPictureHistory):
            raise TypeError("picture selection requires RecentPictureHistory")
        if not callable(choose):
            raise TypeError("picture selection requires a callable choice strategy")
        unseen = tuple(
            picture
            for picture in self._pictures
            if not recent.contains(picture.source_id)
        )
        exhausted = not unseen
        pool = self._pictures if exhausted else unseen
        selected = choose(pool)
        if not isinstance(selected, PictureCandidate) or selected not in pool:
            raise ValueError(
                "picture choice must return a candidate from its input pool"
            )
        return PictureSelection(picture=selected, reused_recent=exhausted)


@dataclass(frozen=True, slots=True)
class PictureSelection:
    """@brief 一次显式图片选择决定 / One explicit picture-selection decision.

    @param picture 已选图片 / Selected picture.
    @param reused_recent 是否因未见候选耗尽而复用 / Whether unseen candidates were exhausted and a recent item was reused.
    """

    picture: PictureCandidate
    reused_recent: bool

    def __post_init__(self) -> None:
        """@brief 校验选择决定字段 / Validate selection-decision fields.

        @return None / None.
        @raise TypeError 图片或复用标记类型不正确时抛出 /
            Raised when picture or reuse-flag types are invalid.
        """

        if not isinstance(self.picture, PictureCandidate):
            raise TypeError("picture selection requires a PictureCandidate")
        if not isinstance(self.reused_recent, bool):
            raise TypeError("picture selection reused_recent must be a bool")
