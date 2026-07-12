"""@brief PostgreSQL account-operation adapter / PostgreSQL account-operation adapter."""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.accounts.operations import (
    AccountCode,
    AccountOperations,
    AccountProfile,
    AccountRegistrationResult,
    PersonalInfoCommand,
    PersonalInfoResult,
    RegisterAccount,
)
from fogmoe_bot.infrastructure.database import connection as db_connection


class PostgresAccountOperations(AccountOperations):
    """@brief 以账户行锁和 identity receipt 执行账户命令 / Execute account commands with account-row locks and identity receipts."""

    async def register(
        self,
        command: RegisterAccount,
    ) -> AccountRegistrationResult:
        """@brief 幂等注册，并冻结首次命令的展示快照 / Idempotently register and freeze the first command's display snapshot.

        @param command 注册命令 / Registration command.
        @return 稳定注册结果 / Stable registration result.
        """

        operation_kind = "register_account"
        async with db_connection.transaction() as connection:
            await db_connection.execute(
                "INSERT INTO identity.users "
                "(id, tg_uid, provider, name, coins, user_plan) "
                "VALUES (%s, %s, 'telegram', %s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                (
                    command.user_id,
                    command.user_id,
                    command.username,
                    command.initial_coins,
                    "admin" if command.user_id == command.admin_user_id else "free",
                ),
                connection=connection,
            )
            row = await _lock_profile(command.user_id, connection)
            if row is None:
                raise RuntimeError("Inserted account disappeared before row lock")
            replay = await _load_receipt(
                command.idempotency_key,
                operation_kind,
                command.user_id,
                connection,
            )
            if replay is not None:
                _validate_registration_semantics(command, replay)
                return AccountRegistrationResult(
                    _profile_from_mapping(replay),
                    replayed=True,
                )

            paid_coins = cast(int, row[3])
            plan = _plan(command.user_id, paid_coins, command.admin_user_id)
            await db_connection.execute(
                "UPDATE identity.users SET tg_uid = %s, provider = 'telegram', "
                "name = %s, user_plan = %s WHERE id = %s",
                (command.user_id, command.username, plan, command.user_id),
                connection=connection,
            )
            profile = AccountProfile(
                user_id=command.user_id,
                username=command.username,
                permission=cast(int, row[1]),
                plan=plan,
                free_coins=cast(int, row[2]),
                paid_coins=paid_coins,
            )
            await _save_receipt(
                command.idempotency_key,
                operation_kind,
                command.user_id,
                {
                    **_profile_mapping(profile),
                    "requested_initial_coins": command.initial_coins,
                    "requested_admin_user_id": command.admin_user_id,
                },
                connection,
            )
            return AccountRegistrationResult(profile)

    async def personal_info(self, command: PersonalInfoCommand) -> PersonalInfoResult:
        """@brief 幂等查看或更新个人信息 / Idempotently inspect or update personal information.

        @param command 个人信息命令 / Personal-info command.
        @return 稳定结果 / Stable result.
        """

        operation_kind = "personal_info"
        async with db_connection.transaction() as connection:
            row = await db_connection.fetch_one(
                "SELECT info FROM identity.users WHERE id = %s FOR UPDATE",
                (command.user_id,),
                connection=connection,
            )
            if row is None:
                return PersonalInfoResult(AccountCode.NOT_REGISTERED)
            replay = await _load_receipt(
                command.idempotency_key,
                operation_kind,
                command.user_id,
                connection,
            )
            if replay is not None:
                if replay.get("requested_info") != command.new_info:
                    raise ValueError(
                        "Personal-info idempotency key changed command semantics"
                    )
                return _personal_info_from_mapping(replay, replayed=True)

            previous = str(row[0] or "")
            current = previous if command.new_info is None else command.new_info
            updated = command.new_info is not None
            if updated:
                await db_connection.execute(
                    "UPDATE identity.users SET info = %s WHERE id = %s",
                    (current, command.user_id),
                    connection=connection,
                )
            result = PersonalInfoResult(
                code=AccountCode.SUCCESS,
                previous_info=previous,
                current_info=current,
                updated=updated,
            )
            await _save_receipt(
                command.idempotency_key,
                operation_kind,
                command.user_id,
                {
                    "code": result.code.value,
                    "previous_info": previous,
                    "current_info": current,
                    "updated": updated,
                    "requested_info": command.new_info,
                },
                connection,
            )
            return result


async def _lock_profile(
    user_id: int,
    connection: AsyncConnection,
) -> tuple[object, ...] | None:
    """@brief 锁定并读取账户展示字段 / Lock and read account display fields.

    @param user_id 用户 ID / User ID.
    @param connection 当前事务 / Current transaction.
    @return raw row 或 None / Raw row or None.
    """

    row = await db_connection.fetch_one(
        "SELECT id, permission, coins, coins_paid, user_plan, name "
        "FROM identity.users WHERE id = %s FOR UPDATE",
        (user_id,),
        connection=connection,
    )
    return tuple(row) if row is not None else None


async def _load_receipt(
    idempotency_key: str,
    expected_kind: str,
    expected_user_id: int,
    connection: AsyncConnection,
) -> Mapping[str, Any] | None:
    """@brief 读取并校验 identity receipt ownership / Read and validate identity-receipt ownership.

    @param idempotency_key 幂等键 / Idempotency key.
    @param expected_kind 预期操作类型 / Expected operation kind.
    @param expected_user_id 预期用户 / Expected user.
    @param connection 当前事务 / Current transaction.
    @return result mapping 或 None / Result mapping or None.
    """

    row = await db_connection.fetch_one(
        "SELECT operation_kind, user_id, result "
        "FROM identity.operation_receipts WHERE idempotency_key = %s",
        (idempotency_key,),
        connection=connection,
    )
    if row is None:
        return None
    if cast(str, row[0]) != expected_kind or cast(int, row[1]) != expected_user_id:
        raise ValueError("Identity idempotency key changed ownership")
    raw: object = row[2]
    decoded: object = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(decoded, Mapping):
        raise ValueError("Invalid identity operation receipt")
    return cast(Mapping[str, Any], decoded)


async def _save_receipt(
    idempotency_key: str,
    operation_kind: str,
    user_id: int,
    result: Mapping[str, object],
    connection: AsyncConnection,
) -> None:
    """@brief 与账户写同事务保存 receipt / Save a receipt in the account-write transaction.

    @param idempotency_key 幂等键 / Idempotency key.
    @param operation_kind 操作类型 / Operation kind.
    @param user_id 用户 ID / User ID.
    @param result JSON result / JSON result.
    @param connection 当前事务 / Current transaction.
    @return None / None.
    """

    await db_connection.execute(
        "INSERT INTO identity.operation_receipts "
        "(idempotency_key, operation_kind, user_id, result) "
        "VALUES (%s, %s, %s, CAST(%s AS JSONB))",
        (
            idempotency_key,
            operation_kind,
            user_id,
            json.dumps(dict(result)),
        ),
        connection=connection,
    )


def _plan(user_id: int, paid_coins: int, admin_user_id: int) -> str:
    """@brief 解析账户 plan / Resolve an account plan.

    @param user_id 用户 ID / User ID.
    @param paid_coins 付费余额 / Paid balance.
    @param admin_user_id 管理员 ID / Administrator ID.
    @return admin/paid/free / admin/paid/free.
    """

    if user_id == admin_user_id:
        return "admin"
    return "paid" if paid_coins > 0 else "free"


def _profile_mapping(profile: AccountProfile) -> dict[str, object]:
    """@brief 序列化账户快照 / Serialize an account snapshot.

    @param profile account profile / Account profile.
    @return JSON mapping / JSON mapping.
    """

    return {
        "user_id": profile.user_id,
        "username": profile.username,
        "permission": profile.permission,
        "plan": profile.plan,
        "free_coins": profile.free_coins,
        "paid_coins": profile.paid_coins,
    }


def _profile_from_mapping(value: Mapping[str, Any]) -> AccountProfile:
    """@brief 从 receipt 恢复账户快照 / Restore an account snapshot from a receipt.

    @param value receipt mapping / Receipt mapping.
    @return account profile / Account profile.
    """

    return AccountProfile(
        user_id=int(value["user_id"]),
        username=str(value["username"]),
        permission=int(value["permission"]),
        plan=str(value["plan"]),
        free_coins=int(value["free_coins"]),
        paid_coins=int(value["paid_coins"]),
    )


def _validate_registration_semantics(
    command: RegisterAccount,
    value: Mapping[str, Any],
) -> None:
    """@brief 拒绝同键异义注册 / Reject a registration key reused with different semantics.

    @param command 当前命令 / Current command.
    @param value receipt mapping / Receipt mapping.
    @return None / None.
    """

    if (
        int(value.get("user_id", -1)) != command.user_id
        or str(value.get("username", "")) != command.username
        or int(value.get("requested_initial_coins", -1)) != command.initial_coins
        or int(value.get("requested_admin_user_id", -1)) != command.admin_user_id
    ):
        raise ValueError("Account-registration idempotency key changed semantics")


def _personal_info_from_mapping(
    value: Mapping[str, Any],
    *,
    replayed: bool,
) -> PersonalInfoResult:
    """@brief 从 receipt 恢复个人信息结果 / Restore a personal-info result from a receipt.

    @param value receipt mapping / Receipt mapping.
    @param replayed 是否回放 / Whether replayed.
    @return personal-info result / Personal-info result.
    """

    return PersonalInfoResult(
        code=AccountCode(str(value["code"])),
        previous_info=str(value.get("previous_info", "")),
        current_info=str(value.get("current_info", "")),
        updated=bool(value.get("updated", False)),
        replayed=replayed,
    )


__all__ = ["PostgresAccountOperations"]
