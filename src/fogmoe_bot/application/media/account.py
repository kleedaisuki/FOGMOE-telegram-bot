"""图片与音乐共享的媒体账户准入端口 / Media-account admission port shared by picture and music capabilities."""

from typing import Protocol

from fogmoe_bot.domain.media.identifiers import UserId


class MediaAccountProfile(Protocol):
    """媒体准入所需账户快照 / Account snapshot required for media admission."""

    @property
    def registered(self) -> bool: ...

    @property
    def permission(self) -> int: ...

    @property
    def coins(self) -> int: ...


class MediaAccountProfiles(Protocol):
    """读取媒体账户并推进既有预览恢复窗口 / Read media accounts and advance established preview recovery."""

    async def profile(self, user_id: UserId) -> MediaAccountProfile:
        """读取媒体准入快照 / Read a media-admission snapshot."""

        ...
