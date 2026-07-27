"""@brief wspctl pybind ABI 的静态类型契约 / Static type contract for the wspctl pybind ABI."""

from collections.abc import Iterable, Mapping


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

    def close(self) -> None:
        """@brief 释放本地 client 资源 / Release local client resources.

        @return None / None.
        @note 它不等价于终止已经由 broker 接收的 command。/ It is not equivalent to
            terminating a command already accepted by the broker.
        """
        ...
