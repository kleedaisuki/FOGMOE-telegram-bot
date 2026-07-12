"""@brief PostgreSQL 游戏共享账户、回执与 JSON primitives / Shared PostgreSQL account, receipt, and JSON primitives for games."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.infrastructure.database import connection as db_connection


@dataclass(frozen=True, slots=True)
class _Account:
    """@brief 已读取或锁定的账户快照 / Read or locked account snapshot."""

    user_id: int
    """@brief 用户 ID / User ID."""
    free: int
    """@brief 免费金币 / Free coins."""
    paid: int
    """@brief 付费金币 / Paid coins."""
    plan: str
    """@brief 账户计划 / Account plan."""
    permission: int
    """@brief 权限等级 / Permission level."""
    name: str
    """@brief 持久化展示名 / Persisted display name."""

    @property
    def total(self) -> int:
        """@brief 返回总余额 / Return total balance.

        @return 免费与付费金币之和 / Sum of free and paid coins.
        """

        return self.free + self.paid


def _encode_json(value: Mapping[str, object]) -> str:
    """@brief 编码规范 JSON / Encode canonical JSON.

    @param value JSON 对象 / JSON object.
    @return 紧凑稳定文本 / Compact stable text.
    """

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_object(value: object) -> dict[str, Any]:
    """@brief 将 driver JSONB 值规范为字典 / Normalize a driver JSONB value to a dictionary.

    @param value driver 值 / Driver value.
    @return JSON 字典 / JSON dictionary.
    """

    decoded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, Mapping):
        raise ValueError("Persisted game JSON must be an object")
    return {str(key): item for key, item in decoded.items()}


def _integer(value: object) -> int:
    """@brief 严格解析数据库整数 / Strictly parse a database integer.

    @param value driver 值 / Driver value.
    @return Python 整数 / Python integer.
    """

    if isinstance(value, bool):
        raise ValueError("Boolean is not a game integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    raise ValueError(f"Invalid game integer: {type(value).__name__}")


async def _lock_receipt_key(idempotency_key: str, connection: AsyncConnection) -> None:
    """@brief 用事务 advisory lock 串行化回执键 / Serialize a receipt key with a transaction advisory lock.

    @param idempotency_key 业务幂等键 / Business idempotency key.
    @param connection 活动事务 / Active transaction.
    @return None / None.
    """

    if not idempotency_key.strip() or len(idempotency_key) > 255:
        raise ValueError("Invalid game idempotency key")
    await db_connection.fetch_one(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"game-receipt:{idempotency_key}",),
        connection=connection,
    )


async def _load_receipt(
    idempotency_key: str,
    operation: str,
    user_id: int,
    connection: AsyncConnection,
) -> dict[str, Any] | None:
    """@brief 读取并验证已提交回执 / Read and validate a committed receipt.

    @param idempotency_key 幂等键 / Idempotency key.
    @param operation 操作种类 / Operation kind.
    @param user_id 业务用户 / Business user.
    @param connection 活动事务 / Active transaction.
    @return 结果 JSON；不存在为 None / Result JSON, or None.
    """

    row = await db_connection.fetch_one(
        "SELECT operation, user_id, result FROM game.game_receipts "
        "WHERE idempotency_key = %s",
        (idempotency_key,),
        connection=connection,
    )
    if row is None:
        return None
    if str(row[0]) != operation or int(row[1]) != user_id:
        raise ValueError("Game idempotency key changed meaning")
    return _json_object(row[2])


async def _save_receipt(
    idempotency_key: str,
    operation: str,
    user_id: int,
    result: Mapping[str, object],
    connection: AsyncConnection,
) -> None:
    """@brief 在业务事务内保存回执 / Save a receipt inside the business transaction.

    @param idempotency_key 幂等键 / Idempotency key.
    @param operation 操作种类 / Operation kind.
    @param user_id 业务用户 / Business user.
    @param result 版本化结果 / Versioned result.
    @param connection 活动事务 / Active transaction.
    @return None / None.
    """

    await db_connection.execute(
        "INSERT INTO game.game_receipts "
        "(idempotency_key, operation, user_id, result) VALUES (%s, %s, %s, CAST(%s AS JSONB))",
        (idempotency_key, operation, user_id, _encode_json(result)),
        connection=connection,
    )


def _map_account(row: Sequence[object]) -> _Account:
    """@brief 映射账户行 / Map an account row.

    @param row SQL 行 / SQL row.
    @return 类型化账户 / Typed account.
    """

    return _Account(
        user_id=_integer(row[0]),
        free=_integer(row[1]),
        paid=_integer(row[2]),
        plan=str(row[3]),
        permission=_integer(row[4]),
        name=str(row[5]),
    )


async def _read_account(
    user_id: int, connection: AsyncConnection | None
) -> _Account | None:
    """@brief 不加锁读取账户 / Read an account without a row lock.

    @param user_id 用户 ID / User ID.
    @param connection 可选事务 / Optional transaction.
    @return 账户或 None / Account or None.
    """

    row = await db_connection.fetch_one(
        "SELECT id, coins, coins_paid, user_plan, permission, name "
        "FROM identity.users WHERE id = %s",
        (user_id,),
        connection=connection,
    )
    return _map_account(row) if row is not None else None


async def _lock_account(user_id: int, connection: AsyncConnection) -> _Account | None:
    """@brief 加锁读取账户 / Read and lock an account.

    @param user_id 用户 ID / User ID.
    @param connection 活动事务 / Active transaction.
    @return 账户或 None / Account or None.
    """

    row = await db_connection.fetch_one(
        "SELECT id, coins, coins_paid, user_plan, permission, name "
        "FROM identity.users WHERE id = %s FOR UPDATE",
        (user_id,),
        connection=connection,
    )
    return _map_account(row) if row is not None else None


async def _lock_accounts(
    user_ids: Sequence[int], connection: AsyncConnection
) -> dict[int, _Account]:
    """@brief 按用户 ID 升序锁账户 / Lock accounts in ascending user-ID order.

    @param user_ids 用户 ID / User IDs.
    @param connection 活动事务 / Active transaction.
    @return ID 到账户映射 / ID-to-account mapping.
    """

    accounts: dict[int, _Account] = {}
    for user_id in sorted(set(user_ids)):
        account = await _lock_account(user_id, connection)
        if account is not None:
            accounts[user_id] = account
    return accounts


async def _credit_free(user_id: int, amount: int, connection: AsyncConnection) -> None:
    """@brief 增加已锁账户的免费金币 / Credit free coins to an already locked account.

    @param user_id 用户 ID / User ID.
    @param amount 正入账额 / Positive credit.
    @param connection 活动事务 / Active transaction.
    @return None / None.
    """

    if amount <= 0:
        raise ValueError("Game credit must be positive")
    affected = await db_connection.execute(
        "UPDATE identity.users SET coins = coins + %s WHERE id = %s",
        (amount, user_id),
        connection=connection,
    )
    if affected != 1:
        raise RuntimeError("Game credit target account disappeared")


class _AccountOperations:
    """@brief 跨游戏复用的账户扣费 primitive / Account-spending primitive shared across games."""

    def __init__(self, *, admin_user_id: int) -> None:
        """@brief 注入管理员身份 / Inject the administrator identity.

        @param admin_user_id 管理员用户 ID / Administrator user ID.
        """

        self._admin_user_id = admin_user_id
        """@brief 扣费后 plan 解析所需管理员 ID / Administrator ID used for post-spend plan resolution."""

    async def _spend_account(
        self,
        account: _Account,
        amount: int,
        connection: AsyncConnection,
    ) -> bool:
        """@brief 按免费后付费规则扣除已锁账户 / Spend a locked account free-first.

        @param account 已锁账户 / Locked account.
        @param amount 正扣费额 / Positive amount.
        @param connection 活动事务 / Active transaction.
        @return 余额足够时为 True / True when sufficient.
        """

        if amount <= 0:
            raise ValueError("Game charge must be positive")
        if account.total < amount:
            return False
        free = max(0, account.free - amount)
        paid_spend = max(0, amount - account.free)
        paid = account.paid - paid_spend
        plan = (
            "admin"
            if account.user_id == self._admin_user_id
            else ("paid" if paid > 0 else "free")
        )
        await db_connection.execute(
            "UPDATE identity.users SET coins = %s, coins_paid = %s, user_plan = %s "
            "WHERE id = %s",
            (free, paid, plan, account.user_id),
            connection=connection,
        )
        return True
