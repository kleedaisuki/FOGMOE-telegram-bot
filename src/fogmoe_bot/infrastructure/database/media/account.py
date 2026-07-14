"""@brief 媒体账户只读准入 / Read-only media-account admission.

历史实现会在每次媒体 profile 查询时修复图片超时并直接写入
``identity.users.coins``。``/music`` 也共享这个 profile 端口，因此那条隐式修复会令一个
无金币功能的公开命令绕过银行账本。图片扣费迁移期间，这个端口严格只读。
/ The historical implementation repaired expired picture offers and directly wrote
``identity.users.coins`` during every media-profile read.  Because ``/music`` shares this
port, that implicit repair let a public non-money command bypass the bank ledger.  During the
picture-charge migration this port is strictly read-only.
"""

from dataclasses import dataclass

from fogmoe_bot.domain.media.identifiers import UserId
from fogmoe_bot.infrastructure.database import connection as db_connection


@dataclass(frozen=True, slots=True)
class MediaUserSnapshot:
    """@brief 媒体准入账户快照 / Media-admission account snapshot.

    @param registered 用户是否已注册 / Whether the user is registered.
    @param permission 用户权限等级 / User permission level.
    """

    registered: bool
    permission: int


class PostgresMediaAccountProfiles:
    """@brief 读取媒体准入资料而不执行任何余额变更 / Read media admission data without any balance mutation."""

    async def profile(self, user_id: UserId) -> MediaUserSnapshot:
        """@brief 返回只读用户资料 / Return a read-only user profile.

        @param user_id 用户标识 / User identity.
        @return 注册和权限快照 / Registration and permission snapshot.
        @note `/music` 与 `/pic` 只需要注册和权限，故本查询不读取任何余额列。
            / `/music` and `/pic` need only registration and permission, so this query reads no
            balance columns.
        """

        row = await db_connection.fetch_one(
            "SELECT permission FROM identity.users WHERE id = %s",
            (int(user_id),),
        )
        if row is None:
            return MediaUserSnapshot(False, 0)
        return MediaUserSnapshot(
            True,
            int(str(row[0] or 0)),
        )
