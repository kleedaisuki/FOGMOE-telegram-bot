"""@brief 免费图片预览服务测试 / Tests for the free picture-preview service."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from fogmoe_bot.application.media.picture_runtime import PictureRuntime
from fogmoe_bot.application.media.picture_service import (
    PictureFreeReady,
    PictureNotRegistered,
    PicturePermissionDenied,
    PictureService,
)
from fogmoe_bot.domain.media.identifiers import UserId
from fogmoe_bot.domain.media.picture import PictureCandidate, PictureRating


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
