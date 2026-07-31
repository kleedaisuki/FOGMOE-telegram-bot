"""@brief 音乐查询领域生命周期与分层边界测试 / Music-search domain lifecycle and layering tests."""

import ast
from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from fogmoe_bot.domain.media.identifiers import UserId
from fogmoe_bot.domain.media.music import (
    MusicPage,
    MusicPlatform,
    MusicSearchId,
    MusicSearchPolicy,
    MusicSearchQuery,
    MusicSearchSession,
    MusicTrack,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""@brief 静态分层回归所用的项目根目录 / Project root used by static layering regressions."""
SRC_ROOT = PROJECT_ROOT / "src" / "fogmoe_bot"
"""@brief FOGMOE Python 源码根目录 / Root of FOGMOE Python sources."""


def _track(index: int, platform: MusicPlatform = MusicPlatform.NETEASE) -> MusicTrack:
    """@brief 创建可辨识的测试歌曲 / Create a distinguishable test track.

    @param index 歌曲序号 / Track ordinal.
    @param platform 歌曲平台 / Track platform.
    @return 规范测试歌曲 / Canonical test track.
    """

    return MusicTrack(
        track_id=str(index),
        name=f"song-{index}",
        artist="artist",
        album="album",
        platform=platform,
    )


def _session(
    *,
    policy: MusicSearchPolicy,
    now: datetime,
    tracks: tuple[MusicTrack, ...],
) -> MusicSearchSession:
    """@brief 通过公开领域行为开启测试会话 / Start a test session through public domain behavior.

    @param policy 会话策略 / Session policy.
    @param now 开启时刻 / Opening instant.
    @param tracks 初始歌曲 / Initial tracks.
    @return 新的不可变会话 / New immutable session.
    """

    return MusicSearchSession.start(
        search_id=MusicSearchId("a" * 32),
        requester_id=UserId(42),
        query=policy.query_from("alpha beta gamma delta"),
        tracks=tracks,
        now=now,
        policy=policy,
    )


def test_query_owns_normalization_help_and_bounded_fallback() -> None:
    """@brief 查询值对象保留既有规范化与回退顺序 / Query owns established normalization and fallback order."""

    policy = MusicSearchPolicy(query_characters=200)
    query = policy.query_from("  Alpha   Beta Gamma Delta  ")

    assert query.text == "Alpha Beta Gamma Delta"
    assert query.search_terms == ("Alpha Beta Gamma Delta", "Alpha Beta")
    assert not query.requests_help
    assert policy.query_from("  HeLp  ").requests_help
    assert policy.query_from(" \n\t ").requests_help


def test_query_preserves_truncation_before_a_word_separator() -> None:
    """@brief 字符上限截在分隔符后时仍可持久恢复 / A query truncated after a separator remains restorable."""

    policy = MusicSearchPolicy(query_characters=3)
    query = policy.query_from("ab cd")
    now = datetime(2026, 1, 1, tzinfo=UTC)

    assert query.text == "ab "
    restored = MusicSearchSession.restore(
        search_id=MusicSearchId("b" * 32),
        requester_id=UserId(7),
        query=query.text,
        platform=MusicPlatform.NETEASE,
        tracks=(_track(1),),
        expires_at=now + timedelta(minutes=30),
    )
    assert restored.query.text == "ab "


def test_session_owns_platform_transition_and_ttl_renewal() -> None:
    """@brief 平台替换生成同身份新版本并续期 / Platform replacement creates a renewed version with the same identity."""

    policy = MusicSearchPolicy(session_ttl=timedelta(minutes=30))
    opened_at = datetime(2026, 1, 1, tzinfo=UTC)
    original = _session(policy=policy, now=opened_at, tracks=(_track(1),))
    switched_at = opened_at + timedelta(minutes=5)
    qq_tracks = (_track(2, MusicPlatform.QQ),)

    switched = original.replace_platform_results(
        platform=MusicPlatform.QQ,
        tracks=qq_tracks,
        now=switched_at,
        policy=policy,
    )

    assert switched is not original
    assert switched.search_id == original.search_id
    assert switched.requester_id == original.requester_id
    assert switched.query == original.query
    assert switched.platform is MusicPlatform.QQ
    assert switched.tracks == qq_tracks
    assert switched.expires_at == switched_at + timedelta(minutes=30)
    assert original.platform is MusicPlatform.NETEASE
    assert original.expires_at == opened_at + timedelta(minutes=30)


def test_immutable_music_values_use_public_readonly_fields() -> None:
    """@brief 不可变音乐值直接暴露强类型只读字段 / Immutable music values expose typed readonly fields directly."""

    assert tuple(field.name for field in fields(MusicSearchQuery)) == ("text",)
    assert tuple(field.name for field in fields(MusicSearchSession)) == (
        "search_id",
        "requester_id",
        "query",
        "platform",
        "tracks",
        "expires_at",
    )
    with pytest.raises(TypeError, match="from_input"):
        MusicSearchQuery()
    with pytest.raises(TypeError, match="start"):
        MusicSearchSession()
    with pytest.raises(TypeError, match="page"):
        MusicPage()


def test_session_clamps_and_slices_pages_as_one_domain_operation() -> None:
    """@brief 分页行为同时夹取页码、计算总页数与切片 / Pagination clamps, counts, and slices atomically."""

    policy = MusicSearchPolicy(page_size=3)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    tracks = tuple(_track(index) for index in range(8))
    session = _session(policy=policy, now=now, tracks=tracks)

    first = session.page(0, policy=policy)
    last = session.page(99, policy=policy)

    assert isinstance(first, MusicPage)
    assert (first.page, first.total_pages, first.page_size) == (1, 3, 3)
    assert first.tracks == tracks[:3]
    assert (last.page, last.total_pages, last.page_size) == (3, 3, 3)
    assert last.tracks == tracks[6:]


def test_session_rejects_unrepresentable_identity_results_and_time() -> None:
    """@brief 类型系统与构造门拒绝非法领域状态 / Types and factories reject illegal domain states."""

    policy = MusicSearchPolicy()
    aware_now = datetime(2026, 1, 1, tzinfo=UTC)

    with pytest.raises(ValueError, match="32 lowercase hexadecimal"):
        MusicSearchId("not-opaque")
    with pytest.raises(FrozenInstanceError):
        MusicSearchId("a" * 32).value = "b" * 32  # type: ignore[misc]
    with pytest.raises(ValueError, match="start requires non-empty tracks"):
        _session(policy=policy, now=aware_now, tracks=())
    with pytest.raises(ValueError, match="requester ID must be positive"):
        MusicSearchSession.start(
            search_id=MusicSearchId("c" * 32),
            requester_id=UserId(-1),
            query=policy.query_from("query"),
            tracks=(_track(1),),
            now=aware_now,
            policy=policy,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        _session(
            policy=policy,
            now=datetime(2026, 1, 1),
            tracks=(_track(1),),
        )


def test_session_normalizes_opening_and_restored_instants_to_utc() -> None:
    """@brief 开启与恢复时刻都规范为 UTC / Opening and restored instants are normalized to UTC."""

    offset = timezone(timedelta(hours=8))
    policy = MusicSearchPolicy(session_ttl=timedelta(minutes=30))
    local_now = datetime(2026, 1, 1, 8, tzinfo=offset)

    opened = _session(policy=policy, now=local_now, tracks=(_track(1),))
    restored = MusicSearchSession.restore(
        search_id=MusicSearchId("e" * 32),
        requester_id=UserId(7),
        query="query",
        platform=MusicPlatform.NETEASE,
        tracks=(_track(1),),
        expires_at=datetime(2026, 1, 1, 9, tzinfo=offset),
    )

    assert opened.expires_at == datetime(2026, 1, 1, 0, 30, tzinfo=UTC)
    assert opened.expires_at.tzinfo is UTC
    assert restored.expires_at == datetime(2026, 1, 1, 1, tzinfo=UTC)
    assert restored.expires_at.tzinfo is UTC


def test_every_session_factory_rejects_tracks_from_another_platform() -> None:
    """@brief 开启、恢复与切换共用平台一致性门 / Start, restore, and switch share one platform-consistency gate."""

    policy = MusicSearchPolicy()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    netease_tracks = (_track(1, MusicPlatform.NETEASE),)
    qq_tracks = (_track(2, MusicPlatform.QQ),)

    with pytest.raises(ValueError, match="tracks must match"):
        MusicSearchSession.start(
            search_id=MusicSearchId("f" * 32),
            requester_id=UserId(7),
            query=policy.query_from("query"),
            tracks=qq_tracks,
            now=now,
            policy=policy,
        )
    with pytest.raises(ValueError, match="tracks must match"):
        MusicSearchSession.restore(
            search_id=MusicSearchId("f" * 32),
            requester_id=UserId(7),
            query="query",
            platform=MusicPlatform.NETEASE,
            tracks=qq_tracks,
            expires_at=now,
        )
    session = _session(policy=policy, now=now, tracks=netease_tracks)
    with pytest.raises(ValueError, match="tracks must match"):
        session.replace_platform_results(
            platform=MusicPlatform.QQ,
            tracks=netease_tracks,
            now=now,
            policy=policy,
        )


def test_restore_preserves_an_existing_empty_persistence_projection() -> None:
    """@brief 恢复路径保留既有损坏行的空页语义 / Restoration preserves the established empty-page semantics of a degraded row."""

    policy = MusicSearchPolicy()
    session = MusicSearchSession.restore(
        search_id=MusicSearchId("d" * 32),
        requester_id=UserId(7),
        query="persisted query",
        platform=MusicPlatform.NETEASE,
        tracks=(),
        expires_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    page = session.page(1, policy=policy)

    assert page.page == 1
    assert page.total_pages == 1
    assert page.tracks == ()


def test_application_orchestrates_domain_music_behavior_without_redefining_it() -> None:
    """@brief 静态防止分页与策略退回应用层 / Statically prevent pagination and policy from drifting back into application."""

    application_path = SRC_ROOT / "application" / "media" / "music_service.py"
    domain_path = SRC_ROOT / "domain" / "media" / "music.py"
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
        "MusicSearchQuery",
        "MusicSearchPolicy",
        "MusicSearchSession",
        "MusicPage",
    } <= domain_classes
    assert (
        not {"MusicSearchPolicy", "MusicSearchSession", "MusicPage"}
        & application_classes
    )
    assert "MusicSearchSession.start(" in application_text
    assert ".replace_platform_results(" in application_text
    assert ".page(" in application_text
    assert '" ".join(query.split())' not in application_text
    assert "fogmoe_bot.application" not in domain_text
    assert "fogmoe_bot.infrastructure" not in domain_text
