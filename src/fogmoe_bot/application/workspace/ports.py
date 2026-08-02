"""@brief Workspace 应用端口 / Workspace application ports."""

from typing import Protocol

from fogmoe_bot.domain.workspace.runtime import WorkspaceRuntime
from fogmoe_bot.domain.workspace.scope import RuntimeScope

from .models import (
    AddFileCommand,
    AddFileResult,
    FetchFileCommand,
    FetchFileResult,
    ReplayFileCommand,
    RunBashCommand,
    RunBashResult,
)


class WorkspaceRuntimeRegistry(Protocol):
    """@brief 可恢复 runtime 身份映射端口 / Recoverable runtime-identity mapping port.

    @note 此端口仅拥有 ``scope → opaque key`` 映射；PID、namespace、mount、cgroup、
        seccomp、OverlayFS 和 activation cache 都属于 native runtime，而非数据库镜像。
        This port owns only the ``scope → opaque key`` mapping; PID, namespaces, mounts,
        cgroups, seccomp, OverlayFS, and activation cache belong to the native runtime rather
        than a database mirror.
    """

    async def resolve(self, scope: RuntimeScope) -> WorkspaceRuntime:
        """@brief 读取或惰性创建一个持久 runtime 身份 / Read or lazily create one persistent runtime identity.

        @param scope 个人或整群的强类型归属范围 / Strongly typed personal-or-whole-group ownership scope.
        @return 稳定 scope/key 绑定 / Stable scope/key binding.
        """

        ...


class RuntimeProcess(Protocol):
    """@brief Bot 使用的受控 runtime 能力端口 / Controlled runtime capability port used by the Bot.

    @note 这是 Bot 与 native ``wspctl`` 之间唯一的应用层边界；调用方不会得到 host
        subprocess、host 文件路径或 mount 控制能力。/ This is the sole application-layer
        boundary between the Bot and native ``wspctl``; callers receive neither host subprocesses,
        host file paths, nor mount-control capability.
    """

    async def run_bash(self, command: RunBashCommand) -> RunBashResult:
        """@brief 惰性激活 runtime 后执行 Bash / Execute Bash after lazily activating the runtime.

        @param command 已验证的 Bash 执行命令 / Validated Bash execution command.
        @return 规范、可去重的执行结果 / Canonical deduplicable execution result.
        @raise WorkspaceRuntimeUnavailableError native runtime 不可安全使用时抛出 /
            Raised when the native runtime cannot be used safely.
        """

        ...

    async def add_file(self, command: AddFileCommand) -> AddFileResult:
        """@brief 将不解释文件原子写入已认证 Workspace / Atomically write an uninterpreted file to the authenticated Workspace.

        @param command 已验证的文件写入命令 / Validated file-ingress command.
        @return 规范、可去重的文件收据 / Canonical deduplicable file receipt.
        @raise WorkspaceRuntimeUnavailableError native runtime 无法安全使用时抛出 /
            Raised when the native runtime cannot be used safely.
        @note 实现必须与 ``run_bash`` 共享同一 runtime cache entry、per-runtime execution lock
            和全局 admission slot；否则文件写入与命令会并发修改同一 OverlayFS workspace。
            Implementations must share the same runtime cache entry, per-runtime execution lock,
            and global admission slot with ``run_bash``; otherwise a file ingress and a command could
            concurrently modify the same OverlayFS workspace.
        """

        ...

    async def replay_file(self, command: ReplayFileCommand) -> AddFileResult:
        """@brief 只读回放已完成的 payload journal / Read-only replay of a completed payload journal.

        @param command 只含 immutable intent 元数据的 journal 查询 / Journal lookup containing only immutable intent metadata.
        @return 已完成 payload 的规范文件收据；``replayed`` 必须为 ``True`` /
            Canonical receipt of a completed payload; ``replayed`` must be ``True``.
        @raise WorkspaceFileReplayNotFoundError 仅当 native 明确证实没有该 journal 时抛出 /
            Raised only when native explicitly proves that this journal does not exist.
        @raise WorkspaceInvocationOutcomeUnknownError 存在 pending journal 或 payload 对象不可验证时抛出 /
            Raised when a pending journal exists or the payload object cannot be verified.
        @raise WorkspaceRuntimeUnavailableError native runtime 无法安全使用时抛出 /
            Raised when the native runtime cannot be used safely.
        @note 该方法绝不创建 journal、不消费 bytes，也不对 Workspace 作任何写入；它与
            ``add_file`` 共享 cache、串行锁及 admission，只是因为同一 broker session 的
            payload 恢复检查必须和其他 Workspace mutation 有序。/ This method never creates a
            journal, consumes bytes, or writes the Workspace; it shares ``add_file``'s cache,
            serialization lock, and admission because payload recovery checks for one broker
            session must remain ordered with other Workspace mutations.
        """

        ...

    async def fetch_file(self, command: FetchFileCommand) -> FetchFileResult:
        """@brief 从已认证 persistent workspace 读取普通文件 / Fetch a regular file from the authenticated persistent workspace.

        @param command 强类型 scope、相对路径与字节预算 / Typed scope, relative path, and byte budget.
        @return 已验证完整 bytes 与摘要 / Verified complete bytes and digest.
        @raise WorkspaceRuntimeUnavailableError native runtime 无法安全读取时抛出 /
            Raised when the native runtime cannot read safely.
        @note 实现必须与 ``run_bash``/``add_file`` 共享 per-runtime 串行边界，且不得回退到
            Bot 宿主机路径读取。/ Implementations must share the per-runtime serialization
            boundary with ``run_bash``/``add_file`` and must never fall back to Bot-host paths.
        """

        ...


__all__ = ["RuntimeProcess", "WorkspaceRuntimeRegistry"]
