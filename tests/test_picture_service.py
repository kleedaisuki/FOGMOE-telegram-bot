"""@brief 免费图片预览服务测试 / Tests for the free picture-preview service."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import cast

from fogmoe_bot.application.media.picture_runtime import PictureRuntime
from fogmoe_bot.application.media.picture_service import (
    PictureFreeReady,
    PictureNotRegistered,
    PicturePermissionDenied,
    PictureService,
)
from fogmoe_bot.domain.media.identifiers import UserId
from fogmoe_bot.domain.media.picture import PictureCandidate, PictureRating
from fogmoe_bot.domain.media.picture import PicturePreviewPolicy


@dataclass(frozen=True, slots=True)
class _Profile:
    """@brief 最小媒体准入资料 / Minimal media-admission profile.

    @param registered 是否已注册 / Whether registered.
    @param permission 权限等级 / Permission level.
    """

    registered: bool = True
    permission: int = 2


class _Accounts:
    """@brief 返回固定资料的测试端口 / Test port returning fixed profiles."""

    def __init__(self, profile: _Profile) -> None:
        """@brief 保存测试资料 / Retain the test profile.

        @param profile 待返回资料 / Profile to return.
        @return None / None.
        """

        self._profile = profile

    async def profile(self, user_id: UserId) -> _Profile:
        """@brief 返回资料而不暴露余额 / Return a profile without exposing a balance.

        @param user_id 请求用户 / Requesting user.
        @return 固定资料 / Fixed profile.
        """

        del user_id
        return self._profile


class _Pictures:
    """@brief 固定图库读取端口 / Fixed gallery read port."""

    async def fetch(
        self,
        rating: PictureRating,
        *,
        limit: int,
    ) -> tuple[PictureCandidate, ...]:
        """@brief 返回两个候选以测试近期去重 / Return two candidates to exercise recent-item exclusion.

        @param rating 内容分级 / Content rating.
        @param limit 请求上限 / Requested limit.
        @return 固定候选 / Fixed candidates.
        """

        del limit
        return (
            PictureCandidate(
                source_id="one",
                sample_url="https://example.test/one.jpg",
                file_url=None,
                tags="cat safe",
                width=1024,
                height=768,
                file_size=1234,
                score=9,
                rating=rating,
            ),
            PictureCandidate(
                source_id="two",
                sample_url="https://example.test/two.jpg",
                file_url=None,
                tags="fox safe",
                width=1280,
                height=720,
                file_size=4567,
                score=8,
                rating=rating,
            ),
        )


def _service(profile: _Profile, *, choose_index: int = 0) -> PictureService:
    """@brief 构造确定性的免费图片服务 / Build a deterministic free-picture service.

    @param profile 测试准入资料 / Test admission profile.
    @param choose_index 优先选择的候选下标 / Preferred candidate index.
    @return 免费图片服务 / Free picture service.
    """

    return PictureService(
        accounts=_Accounts(profile),
        source=_Pictures(),
        runtime=PictureRuntime(),
        choose=lambda values: values[choose_index],
    )


def test_free_picture_uses_no_balance_field_and_avoids_recent_duplicates() -> None:
    """@brief 免费预览只依赖准入资料并避免近期重复 / Free previews use only admission data and avoid recent duplicates.

    @return None / None.
    """

    async def scenario() -> None:
        """@brief 连续请求两次免费图片 / Request two free pictures in succession.

        @return None / None.
        """

        service = _service(_Profile())
        first = await service.request_free_picture(
            user_id=UserId(1),
            rating=PictureRating.SAFE,
        )
        second = await service.request_free_picture(
            user_id=UserId(1),
            rating=PictureRating.SAFE,
        )

        assert isinstance(first, PictureFreeReady)
        assert isinstance(second, PictureFreeReady)
        assert first.picture.source_id == "one"
        assert second.picture.source_id == "two"

    asyncio.run(scenario())


def test_free_picture_enforces_registration_and_nsfw_permission() -> None:
    """@brief 免费预览仍执行注册与 NSFW 准入 / Free previews still enforce registration and NSFW admission.

    @return None / None.
    """

    async def scenario() -> None:
        """@brief 覆盖两个准入失败分支 / Cover both admission-failure branches.

        @return None / None.
        """

        unregistered = await _service(_Profile(registered=False)).request_free_picture(
            user_id=UserId(1),
            rating=PictureRating.SAFE,
        )
        denied = await _service(_Profile(permission=1)).request_free_picture(
            user_id=UserId(1),
            rating=PictureRating.NSFW,
        )

        assert isinstance(unregistered, PictureNotRegistered)
        assert isinstance(denied, PicturePermissionDenied)
        assert denied.required == 2

    asyncio.run(scenario())


class _TrackedProfile:
    """@brief 记录字段读取的准入快照 / Admission snapshot recording field reads."""

    def __init__(
        self,
        log: list[str],
        *,
        registered: bool,
        permission: int,
    ) -> None:
        """@brief 保留显式快照 / Retain the explicit snapshot."""

        self._log = log
        self._registered = registered
        self._permission = permission

    @property
    def registered(self) -> bool:
        """@brief 记录并返回注册状态 / Record and return registration state."""

        self._log.append("profile.registered")
        return self._registered

    @property
    def permission(self) -> int:
        """@brief 记录并返回权限 / Record and return permission."""

        self._log.append("profile.permission")
        return self._permission


class _TrackedAccounts:
    """@brief 记录 profile 调用的账户端口 / Account port recording profile calls."""

    def __init__(self, log: list[str], profile: _TrackedProfile) -> None:
        """@brief 保留调用日志与快照 / Retain call log and snapshot."""

        self._log = log
        self._profile = profile

    async def profile(self, user_id: UserId) -> _TrackedProfile:
        """@brief 记录后返回快照 / Record and return the snapshot."""

        del user_id
        self._log.append("accounts.profile")
        return self._profile


class _TrackedCache[K, V]:
    """@brief 按顺序返回快照的缓存 fake / Cache fake returning snapshots in order."""

    def __init__(
        self,
        log: list[str],
        name: str,
        reads: Sequence[V | None],
    ) -> None:
        """@brief 保留缓存读取序列 / Retain cache-read sequence."""

        self._log = log
        self._name = name
        self._reads = list(reads)
        self.writes: list[tuple[K, V]] = []

    async def get(self, key: K) -> V | None:
        """@brief 返回下一个缓存快照 / Return the next cache snapshot."""

        del key
        self._log.append(f"{self._name}.get")
        if not self._reads:
            raise AssertionError(f"unexpected {self._name}.get")
        return self._reads.pop(0)

    async def put(self, key: K, value: V) -> None:
        """@brief 记录缓存写入 / Record a cache write."""

        self._log.append(f"{self._name}.put")
        self.writes.append((key, value))


class _TrackedBulkhead:
    """@brief 立即执行操作的 bulkhead fake / Bulkhead fake executing an operation immediately."""

    def __init__(self, log: list[str]) -> None:
        """@brief 保留调用日志 / Retain the call log."""

        self._log = log

    async def run[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        """@brief 记录后执行操作 / Record and execute the operation."""

        self._log.append("bulkhead.run")
        return await operation()


class _TrackedPictures:
    """@brief 记录图库读取的上游 fake / Upstream fake recording gallery reads."""

    def __init__(
        self,
        log: list[str],
        pictures: tuple[PictureCandidate, ...],
    ) -> None:
        """@brief 保留固定批次 / Retain the fixed batch."""

        self._log = log
        self._pictures = pictures

    async def fetch(
        self,
        rating: PictureRating,
        *,
        limit: int,
    ) -> tuple[PictureCandidate, ...]:
        """@brief 记录精确请求后返回批次 / Record the exact request and return the batch."""

        assert rating is PictureRating.SAFE
        assert limit == 200
        self._log.append("source.fetch")
        return self._pictures


class _TrackedRuntime:
    """@brief 组合可观测缓存与 bulkhead / Compose observable caches and bulkhead."""

    def __init__(
        self,
        *,
        picture_batches: object,
        recent_pictures: object,
        gallery_bulkhead: object,
    ) -> None:
        """@brief 保留运行时组件 / Retain runtime components."""

        self.picture_batches = picture_batches
        self.recent_pictures = recent_pictures
        self.gallery_bulkhead = gallery_bulkhead


def test_free_picture_preserves_port_cache_and_random_call_order() -> None:
    """@brief 成功路径精确保留端口、两次历史读取与随机源顺序 / Success preserves ports, two history reads, and random order."""

    async def scenario() -> None:
        """@brief 执行一次 cache miss 预览 / Execute one cache-miss preview."""

        log: list[str] = []
        pictures = await _Pictures().fetch(PictureRating.SAFE, limit=200)
        batch_cache = _TrackedCache[PictureRating, tuple[PictureCandidate, ...]](
            log,
            "batch",
            (None,),
        )
        recent_cache = _TrackedCache[UserId, tuple[str, ...]](
            log,
            "recent",
            (("one",), ("one", "external")),
        )
        runtime = _TrackedRuntime(
            picture_batches=batch_cache,
            recent_pictures=recent_cache,
            gallery_bulkhead=_TrackedBulkhead(log),
        )

        def choose(values: Sequence[PictureCandidate]) -> PictureCandidate:
            """@brief 记录并选择唯一未见候选 / Record and choose the sole unseen candidate."""

            log.append("choose")
            assert tuple(item.source_id for item in values) == ("two",)
            return values[0]

        result = await PictureService(
            accounts=_TrackedAccounts(
                log,
                _TrackedProfile(log, registered=True, permission=99),
            ),
            source=_TrackedPictures(log, pictures),
            runtime=cast(PictureRuntime, runtime),
            policy=PicturePreviewPolicy(recent_limit=2),
            choose=choose,
        ).request_free_picture(user_id=UserId(7), rating=PictureRating.SAFE)

        assert isinstance(result, PictureFreeReady)
        assert result.picture.source_id == "two"
        assert log == [
            "accounts.profile",
            "profile.registered",
            "batch.get",
            "bulkhead.run",
            "source.fetch",
            "batch.put",
            "recent.get",
            "choose",
            "recent.get",
            "recent.put",
        ]
        assert batch_cache.writes == [(PictureRating.SAFE, pictures)]
        assert recent_cache.writes == [(UserId(7), ("external", "two"))]

    asyncio.run(scenario())


def test_admission_failures_short_circuit_before_runtime_and_source() -> None:
    """@brief 未注册与 NSFW 权限不足在任何运行时能力前短路 / Registration and NSFW denial short-circuit before runtime capabilities."""

    class UnexpectedSource:
        """@brief 被调用即失败的上游 / Upstream that fails if called."""

        async def fetch(
            self,
            rating: PictureRating,
            *,
            limit: int,
        ) -> tuple[PictureCandidate, ...]:
            """@brief 拒绝准入失败后的上游访问 / Reject upstream access after admission failure."""

            raise AssertionError(f"unexpected source call: {rating=} {limit=}")

    class UnexpectedRuntime:
        """@brief 任何属性访问都失败的运行时 / Runtime failing on any attribute access."""

        def __getattr__(self, name: str) -> object:
            """@brief 拒绝准入失败后的缓存访问 / Reject cache access after admission failure."""

            raise AssertionError(f"unexpected runtime access: {name}")

    async def scenario() -> None:
        """@brief 执行两种短路路径 / Execute both short-circuit paths."""

        unregistered_log: list[str] = []
        unregistered = await PictureService(
            accounts=_TrackedAccounts(
                unregistered_log,
                _TrackedProfile(
                    unregistered_log,
                    registered=False,
                    permission=999,
                ),
            ),
            source=UnexpectedSource(),
            runtime=cast(PictureRuntime, UnexpectedRuntime()),
        ).request_free_picture(user_id=UserId(7), rating=PictureRating.NSFW)

        denied_log: list[str] = []
        denied = await PictureService(
            accounts=_TrackedAccounts(
                denied_log,
                _TrackedProfile(denied_log, registered=True, permission=1),
            ),
            source=UnexpectedSource(),
            runtime=cast(PictureRuntime, UnexpectedRuntime()),
        ).request_free_picture(user_id=UserId(7), rating=PictureRating.NSFW)

        assert isinstance(unregistered, PictureNotRegistered)
        assert unregistered_log == ["accounts.profile", "profile.registered"]
        assert isinstance(denied, PicturePermissionDenied)
        assert denied_log == [
            "accounts.profile",
            "profile.registered",
            "profile.permission",
        ]

    asyncio.run(scenario())
