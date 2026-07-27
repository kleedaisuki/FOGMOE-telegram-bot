"""@brief Workspace 应用端口的失败语义 / Failure semantics for Workspace application ports."""

from __future__ import annotations

from fogmoe_bot.domain.workspace.runtime import WorkspaceRequestId


class WorkspaceRuntimeUnavailableError(RuntimeError):
    """@brief 隔离 runtime 无法安全使用 / The isolated runtime cannot be used safely.

    @note 包括 native module 缺失、supervisor 连接失败和执行期间的 host-side transport
        失败。调用方不得回退到宿主机 Bash/Python。/ This includes a missing native module,
        supervisor connection failures, and host-side transport failures during execution.
        Callers must not fall back to host Bash/Python.
    """


class WorkspaceRuntimeProtocolError(WorkspaceRuntimeUnavailableError):
    """@brief native supervisor 返回了不可信协议载荷 / The native supervisor returned an untrusted protocol payload.

    @note 协议不匹配按 unavailable 处理，避免忽略字段或猜测安全语义。/
        A protocol mismatch is handled as unavailable to avoid ignoring fields or guessing
        security semantics.
    """


class WorkspaceInvocationOutcomeUnknownError(RuntimeError):
    """@brief command 可能已产生副作用但结果不可恢复 / A command may have had side effects but its result is unrecoverable.

    @param request_id native journal 中保留为 pending 的稳定调用标识 /
        Stable invocation ID left pending in the native journal.
    @note 这不是 transport unavailable：自动用相同 id 重试会违反 at-most-once。
        operation adapter 必须把它固化成 terminal ``outcome_unknown`` receipt，让后续 Agent
        turn 明确决定是否发起一个新的 invocation。/ This is not transport unavailability:
        automatically retrying with the same ID violates at-most-once. The operation adapter must
        persist it as a terminal ``outcome_unknown`` receipt so a later Agent turn explicitly
        decides whether to issue a new invocation.
    """

    def __init__(self, request_id: WorkspaceRequestId) -> None:
        """@brief 构造不可判定结果异常 / Construct an indeterminate-outcome exception.

        @param request_id pending journal 的请求标识 / Request ID of the pending journal.
        @return None / None.
        @raise TypeError 请求标识不是强类型值对象时抛出 /
            Raised when the request ID is not the strong value object.
        """

        if not isinstance(request_id, WorkspaceRequestId):
            raise TypeError(
                "Workspace outcome-unknown error requires a WorkspaceRequestId"
            )
        self.request_id = request_id
        """@brief 结果不可判定的稳定请求标识 / Stable request ID with indeterminate outcome."""
        super().__init__(f"Workspace invocation outcome is unknown: {request_id}")


__all__ = [
    "WorkspaceInvocationOutcomeUnknownError",
    "WorkspaceRuntimeProtocolError",
    "WorkspaceRuntimeUnavailableError",
]
