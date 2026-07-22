"""@brief Assistant 用户上下文读取模型 / Assistant user-context read model."""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.infrastructure.database import db


_ASSISTANT_USER_SNAPSHOT_SELECT = (
    "SELECT users.id, users.permission, "
    "COALESCE(free_balance.balance, 0), "
    "COALESCE(paid_balance.balance, 0), users.info, users.name "
    "FROM identity.users AS users "
    "LEFT JOIN bank.account_balances AS free_balance "
    "ON free_balance.account_key = 'user:' || users.id::TEXT || ':free' "
    "LEFT JOIN bank.account_balances AS paid_balance "
    "ON paid_balance.account_key = 'user:' || users.id::TEXT || ':paid' "
    "WHERE users.id = %s"
)
"""@brief Assistant 用户快照的权威事实查询 / Authoritative-fact query for an Assistant user snapshot."""

_ASSISTANT_IDENTITY_CONTEXT_SELECT = (
    "SELECT id, permission, info FROM identity.users WHERE id = %s"
)
"""@brief 不含余额的 Assistant 身份查询 / Balance-free Assistant identity query."""


@dataclass(frozen=True, slots=True)
class AssistantUserSnapshot:
    """@brief Assistant 用户完整快照 / Complete Assistant user snapshot.

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


@dataclass(frozen=True, slots=True)
class AssistantIdentityContext:
    """@brief 不含货币余额的 Assistant 身份上下文 / Assistant identity context without monetary balances.

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


def _coerce_assistant_user_snapshot(
    row: Sequence[object] | None,
) -> AssistantUserSnapshot | None:
    """@brief 将数据库行转换为 Assistant 用户快照 / Convert a database row into an Assistant user snapshot.

    @param row 数据库结果行 / Database result row.
    @return 用户账户快照；无行时返回 None / User account snapshot, or None when no row exists.
    """

    if not row:
        return None
    return AssistantUserSnapshot(
        user_id=int(str(row[0])),
        permission=int(str(row[1] or 0)),
        coins=int(str(row[2] or 0)),
        coins_paid=int(str(row[3] or 0)),
        info="" if row[4] is None else str(row[4]),
        name="" if row[5] is None else str(row[5]),
    )


async def fetch_assistant_user_snapshot(
    user_id: int,
    *,
    connection: AsyncConnection | None = None,
) -> AssistantUserSnapshot | None:
    """@brief 读取 Assistant 用户完整快照 / Fetch a complete Assistant user snapshot.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return 用户账户；不存在时返回 None / User account, or None when it does not exist.
    """

    row = await db.fetch_one(
        _ASSISTANT_USER_SNAPSHOT_SELECT,
        (user_id,),
        connection=connection,
    )
    return _coerce_assistant_user_snapshot(row)


async def fetch_assistant_identity_context(
    user_id: int,
    *,
    connection: AsyncConnection | None = None,
) -> AssistantIdentityContext | None:
    """@brief 读取不含任何余额的 Assistant 身份上下文 / Fetch Assistant identity context without any balance.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return 用户身份上下文；不存在时返回 None / User identity context, or None when absent.
    @note 仅供不应接触银行余额的工作流使用，例如零费用 Assistant acceptance /
        Intended only for workflows that must not access bank balances, such as zero-cost Assistant acceptance.
    """

    row = await db.fetch_one(
        _ASSISTANT_IDENTITY_CONTEXT_SELECT,
        (user_id,),
        connection=connection,
    )
    if row is None:
        return None
    return AssistantIdentityContext(
        user_id=int(str(row[0])),
        permission=int(str(row[1] or 0)),
        info="" if row[2] is None else str(row[2]),
    )


async def lock_assistant_identity_context_in_transaction(
    user_id: int,
    *,
    connection: AsyncConnection,
) -> AssistantIdentityContext | None:
    """@brief 在调用方事务中锁定不含余额的 Assistant 身份 / Lock the balance-free Assistant identity in the caller transaction.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param connection 必需的调用方事务连接 / Required caller-owned transaction connection.
    @return 已锁定身份上下文；不存在时返回 None / Locked identity context, or None when absent.
    @note 类型签名排除了“锁查询却不持有事务”的无效状态 / The type signature excludes a locking read without an owned transaction.
    """

    row = await db.fetch_one(
        f"{_ASSISTANT_IDENTITY_CONTEXT_SELECT} FOR UPDATE",
        (user_id,),
        connection=connection,
    )
    if row is None:
        return None
    return AssistantIdentityContext(
        user_id=int(str(row[0])),
        permission=int(str(row[1] or 0)),
        info="" if row[2] is None else str(row[2]),
    )


async def assistant_diary_exists(
    user_id: int,
    *,
    connection: AsyncConnection | None = None,
) -> bool:
    """@brief 判断 Assistant 用户日记是否存在 / Check whether Assistant user diary content exists.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return 存在非空日记页返回 True / True when a non-empty diary page exists.
    """

    row = await db.fetch_one(
        "SELECT 1 FROM conversation.ai_user_diary_pages "
        "WHERE user_id = %s AND content != '' LIMIT 1",
        (user_id,),
        connection=connection,
    )
    return bool(row)
