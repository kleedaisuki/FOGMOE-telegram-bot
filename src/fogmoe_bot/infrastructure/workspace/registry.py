"""@brief PostgreSQL Workspace runtime 身份注册表 / PostgreSQL registry of Workspace runtime identities."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from fogmoe_bot.domain.workspace.runtime import (
    WorkspaceRuntime,
    WorkspaceRuntimeKey,
)
from fogmoe_bot.domain.workspace.scope import (
    RuntimeScope,
    runtime_scope_parts,
)
from fogmoe_bot.infrastructure.database import db

_MAX_KEY_GENERATION_ATTEMPTS = 4
"""@brief 处理极低概率 runtime-key 碰撞的生成次数 / Generation attempts for an extremely unlikely runtime-key collision."""


class PostgresWorkspaceRuntimeRegistry:
    """@brief 将强类型范围原子映射到一个永久 opaque key / Atomically map a typed scope to one permanent opaque key.

    @note 表只保存可恢复 identity，绝不保存 PID、socket、mount、cgroup、namespace、
        seccomp 或 overlay 状态；这些都是 native supervisor 的易失或 host-local 事实。
        The table stores only recoverable identity, never PID, socket, mount, cgroup, namespace,
        seccomp, or overlay state; those are volatile or host-local facts owned by the native
        supervisor.
    """

    async def resolve(self, scope: RuntimeScope) -> WorkspaceRuntime:
        """@brief 读取或一次性创建 scope 的 runtime key / Read or create once the runtime key of a scope.

        @param scope 个人或整群的强类型归属范围 / Strongly typed personal-or-whole-group ownership scope.
        @return 永不自动轮换的 scope/key 绑定 / Scope/key binding that is never rotated automatically.
        @raise RuntimeError 数据库返回了丢失或格式错误的映射时抛出 /
            Raised when the database returns a missing or malformed mapping.
        @note ``INSERT .. ON CONFLICT DO NOTHING`` 与同一事务中的读取共同处理多 Bot
            process 的首次竞争；不会把 scope 的既有 key 更新为新随机值。/ ``INSERT ..
            ON CONFLICT DO NOTHING`` plus a read in the same transaction handles first-use races
            among Bot processes; it never updates an existing scope to a newly random key.
        """

        scope_kind, scope_id = runtime_scope_parts(scope)
        for _attempt in range(_MAX_KEY_GENERATION_ATTEMPTS):
            candidate = WorkspaceRuntimeKey.new()
            async with db.transaction() as connection:
                await db.execute(
                    "INSERT INTO workspace.runtimes "
                    "(scope_kind, scope_id, runtime_key) "
                    "VALUES (%s, %s, %s) "
                    "ON CONFLICT DO NOTHING",
                    (scope_kind.value, scope_id, str(candidate)),
                    connection=connection,
                )
                row = await db.fetch_one(
                    "SELECT runtime_key FROM workspace.runtimes "
                    "WHERE scope_kind = %s AND scope_id = %s",
                    (scope_kind.value, scope_id),
                    connection=connection,
                )
            if row is None:
                # The only plausible path is a collision on the independent runtime_key
                # uniqueness constraint.  Do not manufacture a replacement binding.
                continue
            try:
                key = WorkspaceRuntimeKey.parse(cast(UUID | str, row[0]))
            except (TypeError, ValueError) as error:
                raise RuntimeError("Workspace runtime registry returned an invalid key") from error
            return WorkspaceRuntime(scope=scope, key=key)
        raise RuntimeError("Workspace runtime key allocation exhausted collision retries")


__all__ = ["PostgresWorkspaceRuntimeRegistry"]
