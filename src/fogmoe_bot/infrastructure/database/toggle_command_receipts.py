"""@brief 治理开关命令的同事务幂等回执 / Same-transaction idempotency receipts for moderation toggles."""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import cast

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.domain.moderation.models import (
    ModerationCommandReceiptConflict,
    ModerationToggleResult,
)
from fogmoe_bot.infrastructure.database import connection as db_connection


async def lock_toggle_command(
    *,
    idempotency_key: str,
    operation_kind: str,
    chat_id: int,
    connection: AsyncConnection,
) -> None:
    """@brief 串行化同一 source key 与群组开关 / Serialize one source key and one group switch.

    @param idempotency_key source Update 派生的稳定键 / Stable key derived from the source Update.
    @param operation_kind 开关类别 / Toggle kind.
    @param chat_id 群组 ID / Group ID.
    @param connection 当前事务 / Current transaction.
    @return None / None.
    @raise ValueError 键或类别无效 / The key or kind is invalid.
    """

    if not idempotency_key.strip() or len(idempotency_key) > 200:
        raise ValueError("Invalid moderation-toggle idempotency key")
    if not operation_kind.strip() or len(operation_kind) > 80:
        raise ValueError("Invalid moderation-toggle operation kind")
    await db_connection.fetch_one(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"moderation-toggle-receipt:{idempotency_key}",),
        connection=connection,
    )
    await lock_toggle_scope(
        operation_kind=operation_kind,
        chat_id=chat_id,
        connection=connection,
    )


async def lock_toggle_scope(
    *,
    operation_kind: str,
    chat_id: int,
    connection: AsyncConnection,
) -> None:
    """@brief 串行化同一群组的同类 policy mutation / Serialize one policy kind within a group.

    @param operation_kind 开关类别 / Toggle kind.
    @param chat_id 群组 ID / Group ID.
    @param connection 当前事务 / Current transaction.
    @return None / None.
    """

    await db_connection.fetch_one(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"moderation-toggle-scope:{operation_kind}:{chat_id}",),
        connection=connection,
    )


async def load_toggle_receipt(
    *,
    idempotency_key: str,
    operation_kind: str,
    chat_id: int,
    actor_id: int,
    request_payload: Mapping[str, object],
    connection: AsyncConnection,
) -> ModerationToggleResult | None:
    """@brief 读取并验证首次开关结果 / Load and validate the first toggle result.

    @param idempotency_key source Update 派生的稳定键 / Stable key derived from the source Update.
    @param operation_kind 预期操作类别 / Expected operation kind.
    @param chat_id 预期群组 / Expected group.
    @param actor_id 预期管理员 / Expected administrator.
    @param request_payload 影响命令语义的输入 / Inputs affecting command semantics.
    @param connection 当前事务 / Current transaction.
    @return 首次结果；尚未执行为 None / First result, or None before execution.
    @raise ModerationCommandReceiptConflict 同一键被复用于不同命令 / The key was reused for a different command.
    """

    row = await db_connection.fetch_one(
        "SELECT operation_kind, chat_id, actor_id, request_payload, enabled "
        "FROM moderation.toggle_command_receipts WHERE idempotency_key = %s",
        (idempotency_key,),
        connection=connection,
    )
    if row is None:
        return None
    raw_payload: object = row[3]
    decoded: object = (
        json.loads(raw_payload) if isinstance(raw_payload, str | bytes) else raw_payload
    )
    expected = dict(request_payload)
    if (
        str(row[0]) != operation_kind
        or int(row[1]) != chat_id
        or int(row[2]) != actor_id
        or not isinstance(decoded, Mapping)
        or dict(cast(Mapping[str, object], decoded)) != expected
    ):
        raise ModerationCommandReceiptConflict(
            "Moderation toggle idempotency key changed ownership or semantics"
        )
    enabled = row[4]
    if not isinstance(enabled, bool):
        raise ValueError("Invalid moderation-toggle receipt result")
    return ModerationToggleResult(enabled=enabled, replayed=True)


async def save_toggle_receipt(
    *,
    idempotency_key: str,
    operation_kind: str,
    chat_id: int,
    actor_id: int,
    request_payload: Mapping[str, object],
    enabled: bool,
    connection: AsyncConnection,
) -> None:
    """@brief 在 policy mutation 同一事务保存结果 / Save the result in the policy-mutation transaction.

    @param idempotency_key source Update 派生的稳定键 / Stable key derived from the source Update.
    @param operation_kind 操作类别 / Operation kind.
    @param chat_id 群组 ID / Group ID.
    @param actor_id 管理员 ID / Administrator ID.
    @param request_payload 影响命令语义的输入 / Inputs affecting command semantics.
    @param enabled 首次提交结果 / First committed result.
    @param connection 当前事务 / Current transaction.
    @return None / None.
    """

    await db_connection.execute(
        "INSERT INTO moderation.toggle_command_receipts "
        "(idempotency_key, operation_kind, chat_id, actor_id, request_payload, enabled) "
        "VALUES (%s, %s, %s, %s, CAST(%s AS JSONB), %s)",
        (
            idempotency_key,
            operation_kind,
            chat_id,
            actor_id,
            json.dumps(dict(request_payload), ensure_ascii=False, sort_keys=True),
            enabled,
        ),
        connection=connection,
    )


__all__ = [
    "load_toggle_receipt",
    "lock_toggle_command",
    "lock_toggle_scope",
    "save_toggle_receipt",
]
