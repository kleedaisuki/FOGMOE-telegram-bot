"""@brief wspctl pybind ABI 的静态类型契约 / Static type contract for the wspctl pybind ABI."""

from collections.abc import Iterable, Mapping
from typing import TypedDict


class RuntimeStatusDump(TypedDict):
    """@brief RuntimeStatus.dump 的固定 allowlist / Fixed allowlist returned by RuntimeStatus.dump.

    @note 该形状不包含 command、request、payload、host path/PID、mount/cgroup 或 socket 字段。
        This shape contains no command, request, payload, host path/PID, mount/cgroup, or socket field.
    """

    runtime_key: str
    """@brief 被观察的 runtime UUID / Observed runtime UUID."""
    state: str
    """@brief 生命周期状态受限词表 / Constrained lifecycle-state vocabulary."""
    active_activation_id: str | None
    """@brief 当前 owner activation；不活跃时为空 / Current owner activation; empty when inactive."""
    handle_activation_matches: bool
    """@brief 此 handle 是否拥有当前 activation / Whether this handle owns the current activation."""
    supervisor_alive: bool
    """@brief 是否有健康可复用 supervisor / Whether a healthy reusable supervisor exists."""
    idle_for_ms: int | None
    """@brief ready 状态空闲年龄；其他状态为空 / Idle age while ready; empty otherwise."""
    idle_ttl_ms: int
    """@brief broker 空闲回收阈值 / Broker idle-retirement threshold."""
    borrowed_dispatches: int
    """@brief 当前借用 session 的 broker dispatch 数 / Current broker dispatches borrowing the session."""
    cleanup_pending: bool
    """@brief 是否有已知清理/隔离待办 / Whether known cleanup/quarantine is pending."""


class NativeError(RuntimeError):
    """@brief native control-plane 的结构化错误 / Structured error from the native control plane.

    @note ``code`` 是 Python adapter 可以依赖的稳定机器码；调用方不得从错误文本解析
        安全语义。/ ``code`` is the stable machine code the Python adapter may depend on;
        callers must not parse security semantics from error text.
    """

    code: str
    """@brief 稳定机器错误码 / Stable machine error code."""
    message: str
    """@brief 稳定错误说明 / Stable error message."""
    request_id: str
    """@brief 可选调用标识 / Optional invocation ID."""


class RuntimeProcessError(NativeError):
    """@brief RuntimeProcess 的普通控制面错误 / Ordinary RuntimeProcess control-plane error."""


class InvocationInDoubtError(RuntimeProcessError):
    """@brief 中断后不可安全重放的调用 / Invocation that cannot safely be replayed after interruption."""


class RuntimeStatus:
    """@brief 无副作用 runtime 状态快照 / Side-effect-free runtime-status snapshot.

    @note 此对象是不可变、allowlisted 的 pybind DTO。它只表达生命周期与有界健康指标，
        不暴露 command/payload/host 细节。/ This is an immutable, allowlisted pybind DTO. It
        expresses only lifecycle and bounded health indicators and exposes no command/payload/host detail.
    """

    runtime_key: str
    """@brief 被观察的 runtime UUID / Observed runtime UUID."""
    state: str
    """@brief ``dormant|activating|ready|executing|retiring|failed`` / Stable lifecycle state."""
    active_activation_id: str | None
    """@brief 当前 owner activation；不活跃时为空 / Current owner activation; empty when inactive."""
    handle_activation_matches: bool
    """@brief 调用 handle activation 是否为当前 owner / Whether the calling handle activation is current owner."""
    supervisor_alive: bool
    """@brief 是否有健康可复用 supervisor / Whether a healthy reusable supervisor exists."""
    idle_for_ms: int | None
    """@brief ready 状态空闲年龄；其他状态为空 / Idle age while ready; empty otherwise."""
    idle_ttl_ms: int
    """@brief broker 空闲回收阈值 / Broker idle-retirement threshold."""
    borrowed_dispatches: int
    """@brief 当前借用 session 的 broker dispatch 数 / Current broker dispatches borrowing the session."""
    cleanup_pending: bool
    """@brief 是否有已知清理/隔离待办 / Whether known cleanup/quarantine is pending."""

    def dump(self) -> RuntimeStatusDump:
        """@brief 显式导出为固定字段 dict / Explicitly export as a fixed-field dict.

        @return JSON-serializable allowlisted 状态字典 / JSON-serializable allowlisted status dictionary.
        """

        ...


class RuntimeProcess:
    """@brief 无特权 wspctld client handle / Unprivileged wspctld client handle.

    @note 它不提供 host subprocess、mount、namespace 或 cgroup API；每个 ``execute`` 或
        ``add_file`` 都是经认证、定长的 native control-plane RPC。/ It exposes no host
        subprocess, mount, namespace, or cgroup API; every ``execute`` or ``add_file`` is an
        authenticated, bounded native control-plane RPC.
    """

    def __init__(self, socket_path: str, runtime_key: str, activation_id: str) -> None:
        """@brief 构造一次 activation 绑定的惰性 client / Construct a lazy client bound to one activation.

        @param socket_path root-owned broker Unix socket / Root-owned broker Unix socket.
        @param runtime_key opaque persistent runtime key / Opaque persistent runtime key.
        @param activation_id stable activation uniquely owned by this handle / Stable activation uniquely owned by this handle.
        @return None / None.
        """
        ...

    def execute(
        self,
        argv: list[str],
        stdin: str = "",
        cwd: str = "",
        timeout_ms: int = 0,
        output_limit: int = 0,
        request_id: str = "",
        request_hash: str = "",
    ) -> Mapping[str, object]:
        """@brief 在隔离 Runtime 中执行命令 / Execute a command in the isolated Runtime.

        @param argv direct exec argv / Direct exec argv.
        @param stdin UTF-8 standard input / UTF-8 standard input.
        @param cwd runtime-internal working directory / Runtime-internal working directory.
        @param timeout_ms wall-clock timeout / Wall-clock timeout.
        @param output_limit combined stdout/stderr budget / Combined stdout/stderr budget.
        @param request_id stable journal idempotency ID / Stable journal idempotency ID.
        @param request_hash SHA-256 request semantic hash / SHA-256 request semantic hash.
        @return 完整 native result mapping / Complete native result mapping.
        @raise NativeError broker 返回结构化失败时抛出 / Raised for a structured broker failure.
        """
        ...

    def status(self) -> RuntimeStatus:
        """@brief 读取 runtime 的无副作用状态 / Read side-effect-free runtime status.

        @return 不可变、allowlisted 的状态 DTO / Immutable allowlisted status DTO.
        @raise NativeError broker 不可达或返回无效状态时抛出 / Raised when the broker is unreachable or returns invalid status.
        @note 此调用不会激活、替换或 retire runtime；closed handle 在本地拒绝且不连接 broker。
            This call never activates, replaces, or retires a runtime; a closed handle is rejected
            locally without connecting to the broker.
        """

        ...

    def add_file(
        self,
        opaque_id: str,
        chunks: Iterable[bytes],
        byte_size: int,
        sha256: str,
        request_id: str = "",
        request_hash: str = "",
    ) -> Mapping[str, object]:
        """@brief 向隔离 Runtime 的受限路径流式写入文件 / Stream a file to a constrained path in the isolated Runtime.

        @param opaque_id trusted opaque uploads-directory capability / Trusted opaque uploads-directory capability.
        @param chunks single-consumption iterable of raw bytes chunks / Single-consumption iterable of raw bytes chunks.
        @param byte_size declared complete file byte count / Declared complete file byte count.
        @param sha256 lowercase SHA-256 of complete file content / Lowercase SHA-256 of complete file content.
        @param request_id stable journal idempotency ID / Stable journal idempotency ID.
        @param request_hash SHA-256 file-ingress semantic hash / SHA-256 file-ingress semantic hash.
        @return request ID, replay flag, runtime path, byte size, and SHA-256 / Request ID, replay flag, runtime path, byte size, and SHA-256.
        @raise NativeError broker 返回结构化失败时抛出 / Raised for a structured broker failure.
        @note 本接口不接受 host path，也不会解释 MIME、文件名、扩展名或 shebang。/
            This API accepts no host path and does not interpret MIME, filenames, extensions, or shebangs.
        """
        ...

    def replay_file(
        self,
        opaque_id: str,
        byte_size: int,
        sha256: str,
        request_id: str = "",
        request_hash: str = "",
    ) -> Mapping[str, object]:
        """@brief 只读恢复一个已完成文件 ingress 的收据 / Read-only replay of one completed file-ingress receipt.

        @param opaque_id trusted persisted uploads-directory capability / Trusted persisted uploads-directory capability.
        @param byte_size persisted complete byte count / Persisted complete byte count.
        @param sha256 persisted complete-content SHA-256 / Persisted complete-content SHA-256.
        @param request_id original stable journal idempotency ID / Original stable journal idempotency ID.
        @param request_hash original SHA-256 file-ingress semantic hash / Original SHA-256 file-ingress semantic hash.
        @return ``replayed=True`` 的 request ID、runtime path、byte size 与 SHA-256 / Request ID, runtime path, byte size, and SHA-256 with ``replayed=True``.
        @raise NativeError ``not_found`` 表示没有 durable receipt；``invocation_in_doubt`` 表示
            pending/对象缺失等不能安全下载的情况。/ ``not_found`` means no durable receipt;
            ``invocation_in_doubt`` means pending/missing-object or another unsafe-to-download state.
        @note 本方法没有 activation 参数，且不会启动、替换或终止 RuntimeProcess，也不接受 bytes 或
            host path。/ This method has no activation argument and never starts, replaces, or
            retires a RuntimeProcess; it accepts neither bytes nor a host path.
        """
        ...

    def close(self) -> None:
        """@brief 释放本地 client 资源 / Release local client resources.

        @return None / None.
        @note 它不等价于终止已经由 broker 接收的 command。/ It is not equivalent to
            terminating a command already accepted by the broker.
        """
        ...
