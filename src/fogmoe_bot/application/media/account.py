"""图片与音乐共享的媒体账户准入端口 / Media-account admission port shared by picture and music capabilities."""

from typing import Protocol

from fogmoe_bot.domain.media.identifiers import UserId


class MediaAccountProfile(Protocol):
    """媒体准入所需账户快照 / Account snapshot required for media admission."""

    @property
    def registered(self) -> bool: ...

    @property
    def permission(self) -> int: ...

class MediaAccountProfiles(Protocol):
    """@brief 读取媒体准入资料 / Read media-admission profiles."""

    async def profile(self, user_id: UserId) -> MediaAccountProfile:
        """@brief 读取媒体准入快照 / Read a media-admission snapshot.

        @param user_id 用户标识 / User identity.
        @return 注册与权限快照 / Registration and permission snapshot.
        """

        ...
