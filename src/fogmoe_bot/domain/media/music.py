"""持久化音乐搜索领域模型 / Durable music-search domain models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import NewType

from .identifiers import UserId

MusicSearchId = NewType("MusicSearchId", str)
"""持久化音乐搜索会话标识 / Durable music-search-session identifier."""


class MusicPlatform(StrEnum):
    """上游音乐平台 / Upstream music platform."""

    NETEASE = "wy"
    QQ = "qq"
    KUWO = "kw"
    MIGU = "mg"
    QIANQIAN = "qi"

    @property
    def display_name(self) -> str:
        """返回用户可见平台名 / Return the user-visible platform name."""

        return {
            MusicPlatform.NETEASE: "网易云音乐",
            MusicPlatform.QQ: "QQ音乐",
            MusicPlatform.KUWO: "酷我音乐",
            MusicPlatform.MIGU: "咪咕音乐",
            MusicPlatform.QIANQIAN: "千千音乐",
        }[self]

    def track_url(self, track_id: str) -> str:
        """构造官方播放页 / Build the official playback page."""

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
    """一条规范音乐搜索结果 / One canonical music-search result."""

    track_id: str
    name: str
    artist: str
    album: str
    platform: MusicPlatform

    def __post_init__(self) -> None:
        """校验歌曲标识与名称 / Validate track identity and name."""

        if not self.track_id.strip() or not self.name.strip():
            raise ValueError("track_id and name must not be blank")


@dataclass(frozen=True, slots=True)
class MusicSearchSession:
    """可跨重启翻页的平台搜索会话 / Restart-resilient paginated platform search."""

    search_id: MusicSearchId
    requester_id: UserId
    query: str
    platform: MusicPlatform
    tracks: tuple[MusicTrack, ...]
    expires_at: datetime
