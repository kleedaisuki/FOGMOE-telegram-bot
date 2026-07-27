"""@brief Workspace 持久运行时标识 / Persistent Workspace runtime identifiers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from .scope import RuntimeScope

_OPERATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
"""@brief native supervisor 可安全持久化的操作 ID 语法 / Operation-ID grammar safe for native-supervisor persistence."""

_REQUEST_HASH_PATTERN = re.compile(r"^[0-9A-Fa-f]{64}$")
"""@brief SHA-256 请求摘要语法 / SHA-256 request-digest grammar."""


@dataclass(frozen=True, slots=True)
class WorkspaceRuntimeKey:
    """@brief 可恢复 Workspace 的不透明宿主键 / Opaque host key of a recoverable Workspace.

    @param value 由 UUIDv4 加密安全随机源生成的不透明值 / Opaque value generated from UUIDv4's cryptographically secure random source.
    @note 该键不是用户、聊天或文件系统路径；日志 ``repr`` 故意隐藏它，避免把可恢复运行时
        的能力标识泄露到普通日志。/ This key is neither a user, chat, nor filesystem path;
        its ``repr`` deliberately hides it to avoid leaking a capability-like recoverable-runtime
        identifier into ordinary logs.
    """

    value: UUID = field(repr=False)
    """@brief 不透明 runtime UUID / Opaque runtime UUID."""

    def __post_init__(self) -> None:
        """@brief 验证 runtime key 编码 / Validate the runtime-key encoding.

        @return None / None.
        @raise TypeError key 不是 UUID 时抛出 / Raised when the key is not a UUID.
        """

        if not isinstance(self.value, UUID):
            raise TypeError("Workspace runtime key must be a UUID")

    @classmethod
    def new(cls) -> WorkspaceRuntimeKey:
        """@brief 生成新的不可预测 runtime key / Generate a new unpredictable runtime key.

        @return 新 UUIDv4 runtime key / New UUIDv4 runtime key.
        @note UUIDv4 提供随机不透明 identity；数据库主键仍是多进程竞争和极低概率碰撞的
            最终防线。/ UUIDv4 provides a random opaque identity; the database primary key
            remains the final defense for multiprocess races and an extremely unlikely collision.
        """

        return cls(uuid4())

    @classmethod
    def parse(cls, value: UUID | str) -> WorkspaceRuntimeKey:
        """@brief 解析数据库返回的 runtime UUID / Parse a runtime UUID returned by the database.

        @param value UUID 对象或规范文本 / UUID object or canonical text.
        @return 强类型 runtime key / Strongly typed runtime key.
        @raise ValueError 文本不是合法 UUID 时抛出 / Raised when text is not a valid UUID.
        """

        return cls(value if isinstance(value, UUID) else UUID(str(value)))

    def __str__(self) -> str:
        """@brief 返回 native adapter 专用的原始键 / Return the raw key for the native adapter only.

        @return 不透明 runtime key / Opaque runtime key.
        """

        return str(self.value)


@dataclass(frozen=True, slots=True)
class WorkspaceRequestId:
    """@brief 可去重 Workspace 命令的稳定请求标识 / Stable request identity of a deduplicable Workspace command.

    @param value 调用方生成的、路径安全稳定请求标识 / Caller-generated path-safe stable request identifier.
    @note 同一 ``WorkspaceRuntimeKey`` 内重试必须复用该值；不同请求复用它时 native
        journal 会结合 ``WorkspaceRequestHash`` 拒绝冲突。/ Retries in one
        ``WorkspaceRuntimeKey`` must reuse this value; the native journal combines it with
        ``WorkspaceRequestHash`` to reject a collision from different requests.
    """

    value: str
    """@brief 请求标识原文 / Raw request identifier."""

    def __post_init__(self) -> None:
        """@brief 验证请求标识 / Validate the request identifier.

        @return None / None.
        @raise TypeError 标识不是字符串时抛出 / Raised when the identifier is not a string.
        @raise ValueError 标识含空白、路径分隔符或非法字符时抛出 /
            Raised when the identifier contains whitespace, a path separator, or invalid characters.
        """

        _validate_operation_identifier(self.value, label="Workspace request ID")

    def __str__(self) -> str:
        """@brief 返回 native supervisor 参数 / Return the native-supervisor argument.

        @return 请求标识 / Request identifier.
        """

        return self.value


@dataclass(frozen=True, slots=True)
class WorkspaceRequestHash:
    """@brief 命令语义的 SHA-256 摘要 / SHA-256 digest of command semantics.

    @param value 64 位十六进制 SHA-256 摘要 / 64-character hexadecimal SHA-256 digest.
    @note 该值必须由上层 durability boundary 从完整调用语义计算；不得由 adapter 从 shell
        文本临时猜测。/ This value must be calculated by the upper durability boundary from
        complete invocation semantics; the adapter must not guess it from shell text.
    """

    value: str
    """@brief 规范小写请求摘要 / Canonical lowercase request digest."""

    def __post_init__(self) -> None:
        """@brief 验证并规范化请求摘要 / Validate and normalize the request digest.

        @return None / None.
        @raise TypeError 摘要不是字符串时抛出 / Raised when the digest is not a string.
        @raise ValueError 摘要不是 64 位 SHA-256 十六进制值时抛出 /
            Raised when the digest is not a 64-character SHA-256 hexadecimal value.
        """

        if not isinstance(self.value, str):
            raise TypeError("Workspace request hash must be a string")
        if _REQUEST_HASH_PATTERN.fullmatch(self.value) is None:
            raise ValueError("Workspace request hash must be a SHA-256 hex digest")
        object.__setattr__(self, "value", self.value.lower())

    def __str__(self) -> str:
        """@brief 返回 native supervisor 参数 / Return the native-supervisor argument.

        @return 规范小写 SHA-256 摘要 / Canonical lowercase SHA-256 digest.
        """

        return self.value


@dataclass(frozen=True, slots=True)
class WorkspaceRuntime:
    """@brief 已注册的持久 Workspace 聚合根 / Registered persistent Workspace aggregate root.

    @param scope 个人或整群的强类型归属范围 / Strongly typed personal-or-whole-group ownership scope.
    @param key 不透明且可恢复的 host runtime key / Opaque recoverable host runtime key.
    @note ``scope → key`` 是这个限界上下文（bounded context）的唯一持久业务事实：进程、
        PID、mount、cgroup 和 activation 都是基础设施投影。这个 aggregate root 永远不可变；
        key 轮换必须以未来显式 generation 建模，而非悄悄更新现有绑定。/
        ``scope → key`` is this bounded context's only persistent business fact: processes, PIDs,
        mounts, cgroups, and activations are infrastructure projections. This aggregate root is
        immutable; key rotation must be modelled as an explicit future generation rather than a
        silent update of the existing binding.
    """

    scope: RuntimeScope
    key: WorkspaceRuntimeKey

    def __post_init__(self) -> None:
        """@brief 验证 runtime 归属绑定 / Validate the runtime ownership binding.

        @return None / None.
        @raise TypeError 范围或 key 不是 Workspace 值对象时抛出 /
            Raised when scope or key is not a Workspace value object.
        """

        from .scope import GroupRuntimeScope, PersonalRuntimeScope

        if not isinstance(self.scope, PersonalRuntimeScope | GroupRuntimeScope):
            raise TypeError("Workspace runtime must use a typed runtime scope")
        if not isinstance(self.key, WorkspaceRuntimeKey):
            raise TypeError("Workspace runtime must use a WorkspaceRuntimeKey")

    def belongs_to(self, scope: RuntimeScope) -> bool:
        """@brief 判断 aggregate 是否属于请求的强类型 scope / Determine whether this aggregate belongs to the requested typed scope.

        @param scope 调用者已认证的个人或整群 scope / Authenticated personal-or-whole-group scope of the caller.
        @return 仅当 scope 类型和值均相同才为 ``True`` / ``True`` only when both scope type and value match.
        @raise TypeError 输入不是已授权 Workspace scope 时抛出 /
            Raised when the input is not an authorized Workspace scope.
        @note 此检查是 infrastructure registry 与 runner 之间的租户隔离不变量；一个错误
            adapter 绝不能把别人的 opaque key 交给当前 command。/ This check is the tenant
            isolation invariant between the infrastructure registry and runner; a faulty adapter
            must never hand another scope's opaque key to the current command.
        """

        from .scope import GroupRuntimeScope, PersonalRuntimeScope

        if not isinstance(scope, PersonalRuntimeScope | GroupRuntimeScope):
            raise TypeError("Workspace runtime ownership requires a typed runtime scope")
        return self.scope == scope


def _validate_operation_identifier(value: str, *, label: str) -> None:
    """@brief 验证安全且有界的 native 操作标识 / Validate a safe bounded native operation identifier.

    @param value 候选标识 / Candidate identifier.
    @param label 面向开发者的值类别 / Developer-facing value category.
    @return None / None.
    @raise TypeError 候选值不是字符串时抛出 / Raised when the candidate is not a string.
    @raise ValueError 候选值不符合稳定 ID 语法时抛出 /
        Raised when the candidate violates the stable-ID grammar.
    """

    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    if _OPERATION_ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} has invalid syntax")


__all__ = [
    "WorkspaceRequestHash",
    "WorkspaceRequestId",
    "WorkspaceRuntime",
    "WorkspaceRuntimeKey",
]
