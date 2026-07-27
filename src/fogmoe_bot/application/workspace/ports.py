"""@brief Workspace 应用端口 / Workspace application ports."""

from typing import Protocol

from fogmoe_bot.domain.workspace.runtime import WorkspaceRuntime
from fogmoe_bot.domain.workspace.scope import RuntimeScope

from .models import (
    AddFileCommand,
    AddFileResult,
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


__all__ = ["RuntimeProcess", "WorkspaceRuntimeRegistry"]
