from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.infrastructure.database import connection as db_connection


@dataclass(frozen=True)
class UserAccount:
    """@brief 用户账户快照 / User account snapshot.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param permission 用户权限等级 / User permission level.
    @param coins 免费硬币余额 / Free coin balance.
    @param coins_paid 付费硬币余额 / Paid coin balance.
    @param info 用户个人信息 / User personal information.
    @param name 用户名 / User name.
    @note 这是读取时刻的不可变快照，不代表事务外的最新状态 / This is an immutable read snapshot, not a live value outside the transaction.
    """

    user_id: int
    permission: int
    coins: int
    coins_paid: int
    info: str
    name: str = ""

    @property
    def total_coins(self) -> int:
        """@brief 计算总硬币余额 / Calculate total coin balance.

        @return 免费硬币与付费硬币之和 / Sum of free and paid coins.
        """

        return self.coins + self.coins_paid


@dataclass(frozen=True)
class UserIdentityContext:
    """@brief 不含货币余额的用户身份快照 / User-identity snapshot without monetary balances.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param permission 用户权限等级 / User permission level.
    @param info 用户个人信息 / User personal information.
    @note Assistant acceptance 使用此对象，以避免读取任何余额投影 / Assistant acceptance uses this object to avoid reading any balance projection.
    """

    user_id: int
    """@brief 用户 ID / User ID."""

    permission: int
    """@brief 权限等级 / Permission level."""

    info: str
    """@brief 个人信息 / Personal information."""

def _coerce_user_account(row: Sequence[object] | None) -> UserAccount | None:
    """@brief 将数据库行转换为账户快照 / Convert a database row into an account snapshot.

    @param row 数据库结果行 / Database result row.
    @return 用户账户快照；无行时返回 None / User account snapshot, or None when no row exists.
    """

    if not row:
        return None
    return UserAccount(
        user_id=int(str(row[0])),
        permission=int(str(row[1] or 0)),
        coins=int(str(row[2] or 0)),
        coins_paid=int(str(row[3] or 0)),
        info="" if row[4] is None else str(row[4]),
        name="" if row[5] is None else str(row[5]),
    )


async def fetch_user_account(
    user_id: int,
    *,
    connection: AsyncConnection | None = None,
    for_update: bool = False,
) -> UserAccount | None:
    """@brief 读取用户账户 / Fetch a user account.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @param for_update 是否加行锁 / Whether to lock the row with FOR UPDATE.
    @return 用户账户；不存在时返回 None / User account, or None when it does not exist.
    @note for_update=True 时调用方应处于事务中 / Callers should be inside a transaction when for_update=True.
    """

    lock_clause = " FOR UPDATE OF users" if for_update else ""
    row = await db_connection.fetch_one(
        "SELECT users.id, users.permission, "
        "COALESCE(free_balance.balance, 0), "
        "COALESCE(paid_balance.balance, 0), users.info, users.name "
        "FROM identity.users AS users "
        "LEFT JOIN bank.account_balances AS free_balance "
        "ON free_balance.account_key = 'user:' || users.id::TEXT || ':free' "
        "LEFT JOIN bank.account_balances AS paid_balance "
        "ON paid_balance.account_key = 'user:' || users.id::TEXT || ':paid' "
        f"WHERE users.id = %s{lock_clause}",
        (user_id,),
        connection=connection,
    )
    return _coerce_user_account(row)


async def fetch_user_identity_context(
    user_id: int,
    *,
    connection: AsyncConnection | None = None,
    for_update: bool = False,
) -> UserIdentityContext | None:
    """@brief 读取不含任何余额的用户身份上下文 / Fetch user identity context without any balance.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @param for_update 是否锁定 identity 用户行 / Whether to lock the identity user row.
    @return 用户身份上下文；不存在时返回 None / User identity context, or None when absent.
    @note 仅供不应接触银行余额的工作流使用，例如零费用 Assistant acceptance /
        Intended only for workflows that must not access bank balances, such as zero-cost Assistant acceptance.
    """

    lock_clause = " FOR UPDATE" if for_update else ""
    row = await db_connection.fetch_one(
        "SELECT id, permission, info "
        f"FROM identity.users WHERE id = %s{lock_clause}",
        (user_id,),
        connection=connection,
    )
    if row is None:
        return None
    return UserIdentityContext(
        user_id=int(str(row[0])),
        permission=int(str(row[1] or 0)),
        info="" if row[2] is None else str(row[2]),
    )
