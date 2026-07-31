"""@brief 持久化音乐搜索领域模型 / Durable music-search domain model.

模块将查询规范化、有界回退、会话续期、平台转换与分页夹取收归到领域；
应用层只按这些显式决策编排端口。/ This module owns query normalization,
bounded fallback, session renewal, platform transitions, and page clamping; the application layer
only orchestrates ports according to these explicit decisions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Self

from fogmoe_bot.domain.temporal import ensure_utc

from .identifiers import UserId

_MUSIC_SEARCH_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
"""@brief callback 可安全携带的不透明搜索标识格式 / Opaque search-ID format safe for callbacks."""


@dataclass(frozen=True, slots=True)
class MusicSearchId:
    """@brief 持久化音乐搜索会话标识 / Durable music-search-session identifier.

    @param value 32 位小写十六进制不透明值 / A 32-character lowercase hexadecimal opaque value.
    """

    value: str
    """@brief 持久化与 callback 共用的规范值 / Canonical value shared by persistence and callbacks."""

    def __post_init__(self) -> None:
        """@brief 验证搜索标识 / Validate the search identifier.

        @return None / None.
        @raise TypeError 值不是字符串时抛出 / Raised when the value is not a string.
        @raise ValueError 值不符合固定 callback 格式时抛出 /
            Raised when the value violates the fixed callback format.
        """

        if not isinstance(self.value, str):
            raise TypeError("music search ID must be a string")
        if _MUSIC_SEARCH_ID_PATTERN.fullmatch(self.value) is None:
            raise ValueError(
                "music search ID must be 32 lowercase hexadecimal characters"
            )

    def __str__(self) -> str:
        """@brief 返回持久化值 / Return the persistence value.

        @return 32 位小写十六进制值 / The 32-character lowercase hexadecimal value.
        """

        return self.value


class MusicPlatform(StrEnum):
    """@brief 上游音乐平台 / Upstream music platform."""

    NETEASE = "wy"
    QQ = "qq"
    KUWO = "kw"
    MIGU = "mg"
    QIANQIAN = "qi"

    @property
    def display_name(self) -> str:
        """@brief 返回用户可见平台名 / Return the user-visible platform name.

        @return 固定中文平台名 / Stable Chinese platform name.
        """

        return {
            MusicPlatform.NETEASE: "网易云音乐",
            MusicPlatform.QQ: "QQ音乐",
            MusicPlatform.KUWO: "酷我音乐",
            MusicPlatform.MIGU: "咪咕音乐",
            MusicPlatform.QIANQIAN: "千千音乐",
        }[self]

    def track_url(self, track_id: str) -> str:
        """@brief 构造官方播放页 / Build the official playback page.

        @param track_id 平台内歌曲标识 / Track identifier within the platform.
        @return 官方播放页 URL / Official playback-page URL.
        """

        templates = {
            MusicPlatform.NETEASE: "https://music.163.com/#/song?id={}",
            MusicPlatform.QQ: "https://y.qq.com/n/ryqq/songDetail/{}",
            MusicPlatform.KUWO: "https://www.kuwo.cn/play_detail/{}",
            MusicPlatform.MIGU: "https://music.migu.cn/v3/music/song/{}",
            MusicPlatform.QIANQIAN: "https://music.91q.com/player?songIds={}",
        }
        return templates[self].format(track_id)


@dataclass(frozen=True, slots=True)
class MusicTrack:
    """@brief 一条规范音乐搜索结果 / One canonical music-search result.

    @param track_id 平台内标识 / Identifier within the platform.
    @param name 歌曲名 / Track name.
    @param artist 歌手名 / Artist name.
    @param album 专辑名 / Album name.
    @param platform 结果所属平台 / Platform owning the result.
    """

    track_id: str
    name: str
    artist: str
    album: str
    platform: MusicPlatform

    def __post_init__(self) -> None:
        """@brief 校验歌曲身份与展示字段 / Validate track identity and display fields.

        @return None / None.
        @raise TypeError 字段类型不符合领域约定时抛出 /
            Raised when field types violate the domain contract.
        @raise ValueError 歌曲标识或名称为空白时抛出 /
            Raised when the track identifier or name is blank.
        """

        if not all(
            isinstance(value, str)
            for value in (self.track_id, self.name, self.artist, self.album)
        ):
            raise TypeError("music track text fields must be strings")
        if not isinstance(self.platform, MusicPlatform):
            raise TypeError("music track requires a MusicPlatform")
        if not self.track_id.strip() or not self.name.strip():
            raise ValueError("track_id and name must not be blank")


@dataclass(frozen=True, slots=True, init=False)
class MusicSearchQuery:
    """@brief 规范化且有界回退的音乐查询 / Normalized music query with bounded fallback.

    该值对象保留已经上线的决策：折叠空白、先截取到字符上限，若主查询
    无结果且包含多个词，再尝试前半部词组。/ The value object preserves the
    established policy: collapse whitespace, truncate to the character bound, then try the first
    half of the words only when a multi-word primary query has no results.
    """

    _text: str
    """@brief 会话持久化的主查询 / Primary query persisted with the session."""

    @classmethod
    def from_input(cls, value: str, *, character_limit: int) -> Self:
        """@brief 从用户输入创建查询 / Create a query from user input.

        @param value 原始用户文本 / Raw user text.
        @param character_limit 规范化后的最大字符数 / Maximum characters after normalization.
        @return 规范查询 / Normalized query.
        @raise TypeError 输入或上限类型不正确时抛出 /
            Raised when the input or limit has an invalid type.
        @raise ValueError 上限非正数时抛出 / Raised when the limit is not positive.
        """

        if not isinstance(value, str):
            raise TypeError("music query input must be a string")
        if isinstance(character_limit, bool) or not isinstance(character_limit, int):
            raise TypeError("music query character limit must be an integer")
        if character_limit <= 0:
            raise ValueError("music query character limit must be positive")
        normalized = " ".join(value.split())[:character_limit]
        instance = object.__new__(cls)
        object.__setattr__(instance, "_text", normalized)
        return instance

    @classmethod
    def restore(cls, value: str) -> Self:
        """@brief 从持久化值恢复查询 / Restore a query from persistence.

        @param value 先前已规范化的会话查询 / Previously normalized session query.
        @return 恢复的查询 / Restored query.
        @raise TypeError 值不是字符串时抛出 / Raised when the value is not a string.
        @raise ValueError 持久化值为空白或不规范时抛出 /
            Raised when the persisted value is blank or non-canonical.
        """

        if not isinstance(value, str):
            raise TypeError("persisted music query must be a string")
        normalized = " ".join(value.split())
        truncated_after_separator = value.endswith(" ") and value[:-1] == normalized
        if not value or (value != normalized and not truncated_after_separator):
            raise ValueError("persisted music query must be non-blank and normalized")
        instance = object.__new__(cls)
        object.__setattr__(instance, "_text", value)
        return instance

    @property
    def text(self) -> str:
        """@brief 返回规范主查询 / Return the canonical primary query.

        @return 用于上游与持久化的文本 / Text used for upstream calls and persistence.
        """

        return self._text

    @property
    def requests_help(self) -> bool:
        """@brief 判断查询是否要求帮助 / Determine whether the query requests help.

        @return 空查询或不区分大小写的 ``help`` 则为 True /
            True for an empty query or case-insensitive ``help``.
        """

        return not self._text or self._text.casefold() == "help"

    @property
    def search_terms(self) -> tuple[str, ...]:
        """@brief 返回按顺序尝试的有界查询 / Return bounded search terms in attempt order.

        @return 主查询，以及可选的前半部词组 / Primary query and optional first-half fallback.
        @note 应用层应在首个非空结果处停止 / The application layer must stop at the first non-empty result.
        """

        words = self._text.split()
        if len(words) <= 1:
            return (self._text,)
        fallback = " ".join(words[: max(1, len(words) // 2)])
        return self._text, fallback


@dataclass(frozen=True, slots=True)
class MusicSearchPolicy:
    """@brief 音乐搜索、分页与会话的显式边界 / Explicit music search, page, and session bounds.

    @param result_limit 单次上游结果上限 / Per-call upstream result limit.
    @param session_ttl 会话存活时间 / Session time to live.
    @param page_size 每页歌曲数 / Tracks per page.
    @param query_characters 查询字符上限 / Query character bound.
    @param default_platform 新会话默认平台 / Default platform for new sessions.
    """

    result_limit: int = 20
    session_ttl: timedelta = timedelta(minutes=30)
    page_size: int = 5
    query_characters: int = 200
    default_platform: MusicPlatform = MusicPlatform.NETEASE

    def __post_init__(self) -> None:
        """@brief 校验搜索容量与时限 / Validate search capacities and duration.

        @return None / None.
        @raise TypeError 边界类型不正确时抛出 / Raised for invalid bound types.
        @raise ValueError 容量或时限非正数时抛出 /
            Raised when a capacity or duration is not positive.
        """

        integer_bounds = (self.result_limit, self.page_size, self.query_characters)
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in integer_bounds
        ):
            raise TypeError("music policy integer bounds must be integers")
        if min(integer_bounds) <= 0:
            raise ValueError("music policy bounds must be positive")
        if not isinstance(self.session_ttl, timedelta):
            raise TypeError("music session TTL must be a timedelta")
        if self.session_ttl <= timedelta(0):
            raise ValueError("music session TTL must be positive")
        if not isinstance(self.default_platform, MusicPlatform):
            raise TypeError("music policy default platform must be a MusicPlatform")

    def query_from(self, value: str) -> MusicSearchQuery:
        """@brief 按策略规范用户查询 / Normalize a user query according to policy.

        @param value 原始用户文本 / Raw user text.
        @return 有界查询值对象 / Bounded query value object.
        """

        return MusicSearchQuery.from_input(value, character_limit=self.query_characters)

    def expires_after(self, now: datetime) -> datetime:
        """@brief 计算新会话版本的过期时刻 / Calculate expiry for a new session version.

        @param now 开启或续期时刻 / Opening or renewal instant.
        @return ``now + session_ttl`` / ``now + session_ttl``.
        @raise TypeError 时刻不是 datetime 时抛出 / Raised when the instant is not a datetime.
        @raise ValueError 时刻缺少时区时抛出 / Raised when the instant is timezone-naive.
        """

        return ensure_utc(now) + self.session_ttl


@dataclass(frozen=True, slots=True, init=False)
class MusicSearchSession:
    """@brief 可跨重启翻页的富音乐搜索聚合 / Rich restart-resilient music-search aggregate.

    聚合自身拥有平台替换、TTL 续期与分页夹取；不对外暴露可变 setter。/
    The aggregate owns platform replacement, TTL renewal, and page clamping and exposes no mutable
    setters.
    """

    _search_id: MusicSearchId
    """@brief 聚合不透明身份 / Opaque aggregate identity."""
    _requester_id: UserId
    """@brief 原始请求用户 / Original requesting user."""
    _query: MusicSearchQuery
    """@brief 会话的规范主查询 / Canonical primary query of the session."""
    _platform: MusicPlatform
    """@brief 当前结果平台 / Platform of the current results."""
    _tracks: tuple[MusicTrack, ...]
    """@brief 当前不可变搜索结果 / Current immutable search results."""
    _expires_at: datetime
    """@brief 当前会话版本的过期时刻 / Expiry instant of the current session version."""

    @classmethod
    def start(
        cls,
        *,
        search_id: MusicSearchId,
        requester_id: UserId,
        query: MusicSearchQuery,
        tracks: tuple[MusicTrack, ...],
        now: datetime,
        policy: MusicSearchPolicy,
    ) -> Self:
        """@brief 开启默认平台搜索会话 / Start a search session on the default platform.

        @param search_id 新会话标识 / New session identifier.
        @param requester_id 原始请求用户 / Original requesting user.
        @param query 规范查询 / Canonical query.
        @param tracks 首次非空搜索结果 / First non-empty search result.
        @param now 会话开启时刻 / Session opening instant.
        @param policy 平台、TTL 与分页策略 / Platform, TTL, and pagination policy.
        @return 新会话聚合 / New session aggregate.
        """

        if not isinstance(policy, MusicSearchPolicy):
            raise TypeError("music session start requires a MusicSearchPolicy")
        if not tracks:
            raise ValueError("music session start requires non-empty tracks")
        return cls._create(
            search_id=search_id,
            requester_id=requester_id,
            query=query,
            platform=policy.default_platform,
            tracks=tracks,
            expires_at=policy.expires_after(now),
        )

    @classmethod
    def restore(
        cls,
        *,
        search_id: MusicSearchId,
        requester_id: UserId,
        query: str,
        platform: MusicPlatform,
        tracks: tuple[MusicTrack, ...],
        expires_at: datetime,
    ) -> Self:
        """@brief 从持久化快照恢复聚合 / Restore the aggregate from a persistence snapshot.

        @param search_id 会话标识 / Session identifier.
        @param requester_id 原始请求用户 / Original requesting user.
        @param query 已规范化的查询 / Previously normalized query.
        @param platform 当前平台 / Current platform.
        @param tracks 当前结果 / Current results.
        @param expires_at 会话过期时刻 / Session expiry instant.
        @return 恢复的聚合 / Restored aggregate.
        """

        return cls._create(
            search_id=search_id,
            requester_id=requester_id,
            query=MusicSearchQuery.restore(query),
            platform=platform,
            tracks=tracks,
            expires_at=expires_at,
        )

    @classmethod
    def _create(
        cls,
        *,
        search_id: MusicSearchId,
        requester_id: UserId,
        query: MusicSearchQuery,
        platform: MusicPlatform,
        tracks: tuple[MusicTrack, ...],
        expires_at: datetime,
    ) -> Self:
        """@brief 经单一不变量门创建聚合 / Create the aggregate through one invariant gate.

        @param search_id 会话标识 / Session identifier.
        @param requester_id 原始请求用户 / Original requesting user.
        @param query 已规范化的主查询 / Canonical primary query.
        @param platform 当前结果平台 / Platform of the current results.
        @param tracks 不可变结果；恢复时可为空 / Immutable results, possibly empty during restoration.
        @param expires_at 带时区的过期时刻 / Timezone-aware expiry instant.
        @return 已验证聚合 / Validated aggregate.
        @raise TypeError 快照字段类型错误时抛出 / Raised for invalid snapshot field types.
        @raise ValueError 查询或时刻不合法时抛出 / Raised for an invalid query or instant.
        """

        if not isinstance(search_id, MusicSearchId):
            raise TypeError("music session requires a MusicSearchId")
        _require_user_id(requester_id)
        if not isinstance(query, MusicSearchQuery) or query.requests_help:
            raise ValueError("music session requires a searchable query")
        if not isinstance(platform, MusicPlatform):
            raise TypeError("music session requires a MusicPlatform")
        if not isinstance(tracks, tuple):
            raise TypeError("music session tracks must be an immutable tuple")
        if not all(isinstance(track, MusicTrack) for track in tracks):
            raise TypeError("music session tracks must be MusicTrack values")
        normalized_expiry = ensure_utc(expires_at)
        if any(track.platform is not platform for track in tracks):
            raise ValueError("music session tracks must match the session platform")
        instance = object.__new__(cls)
        object.__setattr__(instance, "_search_id", search_id)
        object.__setattr__(instance, "_requester_id", requester_id)
        object.__setattr__(instance, "_query", query)
        object.__setattr__(instance, "_platform", platform)
        object.__setattr__(instance, "_tracks", tracks)
        object.__setattr__(instance, "_expires_at", normalized_expiry)
        return instance

    @property
    def search_id(self) -> MusicSearchId:
        """@brief 返回会话标识 / Return the session identifier.

        @return 不透明搜索标识 / Opaque search identifier.
        """

        return self._search_id

    @property
    def requester_id(self) -> UserId:
        """@brief 返回原始请求用户 / Return the original requesting user.

        @return Telegram 用户标识 / Telegram user identifier.
        """

        return self._requester_id

    @property
    def query(self) -> str:
        """@brief 返回会话主查询 / Return the session primary query.

        @return 规范查询文本 / Canonical query text.
        """

        return self._query.text

    @property
    def platform(self) -> MusicPlatform:
        """@brief 返回当前平台 / Return the current platform.

        @return 当前音乐平台 / Current music platform.
        """

        return self._platform

    @property
    def tracks(self) -> tuple[MusicTrack, ...]:
        """@brief 返回当前不可变结果 / Return current immutable results.

        @return 歌曲元组 / Track tuple.
        """

        return self._tracks

    @property
    def expires_at(self) -> datetime:
        """@brief 返回会话过期时刻 / Return the session expiry instant.

        @return 带时区的过期时刻 / Timezone-aware expiry instant.
        """

        return self._expires_at

    def replace_platform_results(
        self,
        *,
        platform: MusicPlatform,
        tracks: tuple[MusicTrack, ...],
        now: datetime,
        policy: MusicSearchPolicy,
    ) -> Self:
        """@brief 替换平台结果并续期会话 / Replace platform results and renew the session.

        @param platform 新平台 / New platform.
        @param tracks 新平台的非空结果 / Non-empty results from the new platform.
        @param now 转换完成时刻 / Transition completion instant.
        @param policy TTL 与分页策略 / TTL and pagination policy.
        @return 同一身份的新会话版本 / New session version with the same identity.
        """

        if not isinstance(policy, MusicSearchPolicy):
            raise TypeError("music platform transition requires a MusicSearchPolicy")
        if not tracks:
            raise ValueError("music platform transition requires non-empty tracks")
        return self._create(
            search_id=self._search_id,
            requester_id=self._requester_id,
            query=self._query,
            platform=platform,
            tracks=tracks,
            expires_at=policy.expires_after(now),
        )

    def page(self, requested_page: int, *, policy: MusicSearchPolicy) -> MusicPage:
        """@brief 切出一个规范页 / Slice one canonical page.

        @param requested_page 外部请求页码 / Externally requested page number.
        @param policy 分页容量策略 / Pagination-capacity policy.
        @return 夹取到有效范围的不可变页 / Immutable page clamped to the valid range.
        """

        if isinstance(requested_page, bool) or not isinstance(requested_page, int):
            raise TypeError("requested music page must be an integer")
        if not isinstance(policy, MusicSearchPolicy):
            raise TypeError("music pagination requires a MusicSearchPolicy")
        total_pages = max(
            1,
            (len(self._tracks) + policy.page_size - 1) // policy.page_size,
        )
        page = min(max(requested_page, 1), total_pages)
        start = (page - 1) * policy.page_size
        return MusicPage._create(
            session=self,
            page=page,
            total_pages=total_pages,
            page_size=policy.page_size,
            tracks=self._tracks[start : start + policy.page_size],
        )


@dataclass(frozen=True, slots=True, init=False)
class MusicPage:
    """@brief 由会话聚合产生的可渲染页 / Renderable page produced by a session aggregate."""

    session: MusicSearchSession
    """@brief 页所属会话版本 / Session version owning the page."""
    page: int
    """@brief 夹取后的一基页码 / Clamped one-based page number."""
    total_pages: int
    """@brief 当前会话的总页数 / Total pages for the current session."""
    page_size: int
    """@brief 生成投影时的每页容量 / Page capacity used to produce the projection."""
    tracks: tuple[MusicTrack, ...]
    """@brief 当前页不可变歌曲结果 / Immutable track results on the current page."""

    @classmethod
    def _create(
        cls,
        *,
        session: MusicSearchSession,
        page: int,
        total_pages: int,
        page_size: int,
        tracks: tuple[MusicTrack, ...],
    ) -> Self:
        """@brief 仅供聚合分页行为创建投影 / Create a projection only for aggregate pagination.

        @param session 页所属会话 / Session owning the page.
        @param page 夹取后的页码 / Clamped page number.
        @param total_pages 总页数 / Total page count.
        @param page_size 每页容量 / Page capacity.
        @param tracks 当前页切片 / Current page slice.
        @return 已验证的音乐页 / Validated music page.
        """

        instance = object.__new__(cls)
        object.__setattr__(instance, "session", session)
        object.__setattr__(instance, "page", page)
        object.__setattr__(instance, "total_pages", total_pages)
        object.__setattr__(instance, "page_size", page_size)
        object.__setattr__(instance, "tracks", tracks)
        return instance


def _require_user_id(value: object) -> None:
    """@brief 验证运行时媒体用户标识 / Validate a media user identifier at runtime.

    @param value 待验证的 ``UserId`` 运行时值 / Runtime value of a ``UserId``.
    @return None / None.
    @raise TypeError 值不是严格整数时抛出 / Raised when the value is not a strict integer.
    @raise ValueError 值不是正数时抛出 / Raised when the value is not positive.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("music session requester ID must be an integer")
    if value <= 0:
        raise ValueError("music session requester ID must be positive")
