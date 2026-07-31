"""@brief 免费图片预览领域状态矩阵与分层测试 / Free picture-preview domain matrix and layering tests."""

import ast
from collections.abc import Sequence
from dataclasses import fields
from pathlib import Path

import pytest

from fogmoe_bot.domain.media.picture import (
    PictureCandidate,
    PictureGalleryBatch,
    PicturePermissionRequired,
    PicturePreviewGranted,
    PicturePreviewPolicy,
    PictureRating,
    PictureRegistrationRequired,
    PictureSelection,
    RecentPictureHistory,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""@brief 项目根目录 / Project root."""
SRC_ROOT = PROJECT_ROOT / "src" / "fogmoe_bot"
"""@brief FOGMOE Python 源码根目录 / FOGMOE Python source root."""


def _picture(
    source_id: str,
    *,
    rating: PictureRating = PictureRating.SAFE,
) -> PictureCandidate:
    """@brief 创建最小规范候选 / Create a minimal canonical candidate.

    @param source_id 图库标识 / Gallery identifier.
    @param rating 内容分级 / Content rating.
    @return 测试候选 / Test candidate.
    """

    return PictureCandidate(
        source_id=source_id,
        sample_url=f"https://example.test/{source_id}.jpg",
        file_url=None,
        tags="",
        width=None,
        height=None,
        file_size=None,
        score=None,
        rating=rating,
    )


@pytest.mark.parametrize(
    ("registered", "permission", "rating", "expected_type"),
    (
        (False, None, PictureRating.SAFE, PictureRegistrationRequired),
        (False, None, PictureRating.NSFW, PictureRegistrationRequired),
        (True, None, PictureRating.SAFE, PicturePreviewGranted),
        (True, 1, PictureRating.NSFW, PicturePermissionRequired),
        (True, 2, PictureRating.NSFW, PicturePreviewGranted),
    ),
)
def test_preview_policy_expresses_the_complete_access_matrix(
    registered: bool,
    permission: int | None,
    rating: PictureRating,
    expected_type: type[object],
) -> None:
    """@brief 准入策略穷尽注册、分级与权限矩阵 / Policy exhausts registration, rating, and permission states."""

    decision = PicturePreviewPolicy().decide_access(
        registered=registered,
        permission=permission,
        rating=rating,
    )

    assert isinstance(decision, expected_type)
    if isinstance(decision, PicturePermissionRequired):
        assert decision.required == 2


def test_selection_preserves_order_and_invokes_random_source_once() -> None:
    """@brief 选择仅从未见项中按原顺序调用一次随机源 / Selection invokes randomness once with ordered unseen items."""

    pictures = (_picture("one"), _picture("two"), _picture("three"))
    batch = PictureGalleryBatch.restore(
        rating=PictureRating.SAFE,
        pictures=pictures,
    )
    history = RecentPictureHistory.restore(("one",), record_limit=32)
    pools: list[tuple[PictureCandidate, ...]] = []

    def choose(values: Sequence[PictureCandidate]) -> PictureCandidate:
        """@brief 记录随机源的唯一输入 / Record the random source's sole input."""

        pool = tuple(values)
        pools.append(pool)
        return pool[-1]

    decision = batch.select(recent=history, choose=choose)

    assert pools == [(pictures[1], pictures[2])]
    assert decision.picture == pictures[2]
    assert not decision.reused_recent


def test_selection_reuses_the_full_batch_only_after_unseen_exhaustion() -> None:
    """@brief 未见候选耗尽后精确恢复完整批次 / The full batch is restored exactly after unseen candidates are exhausted."""

    pictures = (_picture("one"), _picture("two"))
    batch = PictureGalleryBatch.restore(
        rating=PictureRating.SAFE,
        pictures=pictures,
    )
    history = RecentPictureHistory.restore(
        ("one", "two"),
        record_limit=32,
    )
    pools: list[tuple[PictureCandidate, ...]] = []

    def choose(values: Sequence[PictureCandidate]) -> PictureCandidate:
        """@brief 记录耗尽后候选批次 / Record the post-exhaustion candidate batch."""

        pool = tuple(values)
        pools.append(pool)
        return pool[0]

    decision = batch.select(recent=history, choose=choose)

    assert pools == [pictures]
    assert decision.picture == pictures[0]
    assert decision.reused_recent


def test_history_preserves_restored_snapshot_and_trims_only_when_recording() -> None:
    """@brief 历史恢复不提前裁剪，新记录才保留最近窗口 / Restore does not pre-trim; recording retains the newest window."""

    history = RecentPictureHistory.restore(
        ("one", "two", "three"),
        record_limit=2,
    )

    assert history.source_ids == ("one", "two", "three")
    assert history.record(_picture("four")).source_ids == ("three", "four")


def test_immutable_picture_collections_use_public_readonly_fields() -> None:
    """@brief 不可变图片集合没有私有字段加转发 getter / Immutable picture collections avoid private fields plus forwarding getters."""

    assert tuple(field.name for field in fields(RecentPictureHistory)) == (
        "source_ids",
        "record_limit",
    )
    assert tuple(field.name for field in fields(PictureGalleryBatch)) == (
        "rating",
        "pictures",
    )
    with pytest.raises(TypeError, match="restore"):
        RecentPictureHistory()
    with pytest.raises(TypeError, match="restore"):
        PictureGalleryBatch()


def test_gallery_restore_rejects_mixed_ratings() -> None:
    """@brief 缓存或上游不能恢复混合分级批次 / Cache or upstream cannot restore a mixed-rating batch."""

    with pytest.raises(ValueError, match="must match its rating"):
        PictureGalleryBatch.restore(
            rating=PictureRating.SAFE,
            pictures=(
                _picture("safe"),
                _picture("nsfw", rating=PictureRating.NSFW),
            ),
        )


def test_public_picture_decisions_reject_forged_invalid_fields() -> None:
    """@brief 公开决定构造器不允许伪造非法状态 / Public decision constructors reject forged illegal states."""

    with pytest.raises(TypeError, match="grant requires a PictureRating"):
        PicturePreviewGranted("safe")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="permission must be an integer"):
        PicturePermissionRequired(True)
    with pytest.raises(ValueError, match="must not be negative"):
        PicturePermissionRequired(-1)
    with pytest.raises(TypeError, match="requires a PictureCandidate"):
        PictureSelection("picture", False)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="must be a bool"):
        PictureSelection(_picture("one"), 1)  # type: ignore[arg-type]


def test_picture_application_orchestrates_without_redefining_domain_policy() -> None:
    """@brief 静态防止准入、历史与选择逻辑退回应用层 / Prevent admission, history, and selection logic from drifting into application."""

    application_path = SRC_ROOT / "application" / "media" / "picture_service.py"
    domain_path = SRC_ROOT / "domain" / "media" / "picture.py"
    application_text = application_path.read_text(encoding="utf-8")
    domain_text = domain_path.read_text(encoding="utf-8")
    application_classes = {
        node.name
        for node in ast.parse(application_text).body
        if isinstance(node, ast.ClassDef)
    }
    domain_classes = {
        node.name
        for node in ast.parse(domain_text).body
        if isinstance(node, ast.ClassDef)
    }

    assert {
        "PicturePreviewPolicy",
        "RecentPictureHistory",
        "PictureGalleryBatch",
        "PictureSelection",
    } <= domain_classes
    assert (
        not {
            "PicturePreviewPolicy",
            "RecentPictureHistory",
            "PictureGalleryBatch",
        }
        & application_classes
    )
    assert ".decide_access(" in application_text
    assert ".select(recent=history, choose=self._choose)" in application_text
    assert ".record(candidate)" in application_text
    assert "candidates or pictures" not in application_text
    assert "fogmoe_bot.application" not in domain_text
    assert "fogmoe_bot.infrastructure" not in domain_text
