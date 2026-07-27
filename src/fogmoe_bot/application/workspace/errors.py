"""@brief Workspace 应用端口的失败语义 / Failure semantics for Workspace application ports."""

from __future__ import annotations

import re

from fogmoe_bot.domain.workspace.runtime import WorkspaceRequestId

_DIAGNOSTIC_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
"""@brief Workspace 机器诊断码的安全格式 / Safe format for Workspace machine diagnostic codes."""
_DIAGNOSTIC_MESSAGE_LIMIT = 1000
"""@brief Workspace 安全诊断消息字符上限 / Character limit for safe Workspace diagnostic messages."""


class WorkspaceRuntimeUnavailableError(RuntimeError):
    """@brief 隔离 runtime 无法安全使用 / The isolated runtime cannot be used safely.

    @param message 调用方安全的稳定错误摘要 / Stable caller-safe error summary.
    @param diagnostic_code 可选机器诊断码 / Optional machine diagnostic code.
    @param diagnostic_message 可选、受控且有界的运维诊断 / Optional controlled and bounded operator diagnostic.
    @note 包括 native module 缺失、supervisor 连接失败和执行期间的 host-side transport
        失败。调用方不得回退到宿主机 Bash/Python。/ This includes a missing native module,
        supervisor connection failures, and host-side transport failures during execution.
        Callers must not fall back to host Bash/Python.
    """

    def __init__(
        self,
        message: str,
        *,
        diagnostic_code: str | None = None,
        diagnostic_message: str | None = None,
    ) -> None:
        """@brief 构造带安全运维诊断的 unavailable 错误 / Construct an unavailable error with safe operator diagnostics.

        @param message 调用方可见的稳定摘要 / Stable caller-visible summary.
        @param diagnostic_code native binding 明确声明的机器码 / Machine code explicitly declared by the native binding.
        @param diagnostic_message native binding 明确声明的安全消息 / Safe message explicitly declared by the native binding.
        @return None / None.
        @note 诊断字段只接受受控 native metadata；不得放入 Bash 命令、stdin 或 payload。/
            Diagnostic fields accept only controlled native metadata and must never contain Bash
            commands, stdin, or payload bytes.
        """

        self.diagnostic_code = _normalize_diagnostic_code(diagnostic_code)
        """@brief 可用于 span/receipt 的稳定机器码 / Stable machine code suitable for spans and receipts."""
        self.diagnostic_message = _normalize_diagnostic_message(diagnostic_message)
        """@brief 可用于受限运维面的安全消息 / Safe message suitable for bounded operator surfaces."""
        super().__init__(message)

    def diagnostic_summary(self) -> str | None:
        """@brief 生成不含请求载荷的有界诊断摘要 / Build a bounded diagnostic summary without request payloads.

        @return 可持久化摘要；没有结构化诊断时为 None /
            Persistable summary, or None when no structured diagnostic is available.
        """

        if self.diagnostic_code is None:
            return None
        if self.diagnostic_message is None:
            return f"workspace native error [{self.diagnostic_code}]"
        return (
            f"workspace native error [{self.diagnostic_code}]: "
            f"{self.diagnostic_message}"
        )


class WorkspaceRuntimeProtocolError(WorkspaceRuntimeUnavailableError):
    """@brief native supervisor 返回了不可信协议载荷 / The native supervisor returned an untrusted protocol payload.

    @note 协议不匹配按 unavailable 处理，避免忽略字段或猜测安全语义。/
        A protocol mismatch is handled as unavailable to avoid ignoring fields or guessing
        security semantics.
    """


class WorkspaceFileReplayNotFoundError(RuntimeError):
    """@brief native 已明确证明 payload journal 不存在 / Native explicitly proved that a payload journal does not exist.

    @param request_id 已查询但不存在的稳定文件调用 ID / Stable file invocation ID that was queried and is absent.
    @note 这不是 runtime unavailable 或副作用不确定：只有 native 完成只读查询且确认既无
        completed 也无 pending journal 时才能抛出。调用方此时才可重新下载 provider bytes。
        / This is neither runtime unavailability nor an indeterminate side effect: it may be
        raised only after native finishes a read-only lookup and confirms there is neither a
        completed nor pending journal. Only then may a caller download provider bytes again.
    """

    def __init__(self, request_id: WorkspaceRequestId) -> None:
        """@brief 构造明确不存在的 journal 异常 / Construct an explicitly absent-journal error.

        @param request_id 已查询的强类型请求标识 / Strongly typed request identifier queried.
        @return None / None.
        @raise TypeError 请求标识不是强类型值对象时抛出 / Raised when the request ID is not a strong value object.
        """

        if not isinstance(request_id, WorkspaceRequestId):
            raise TypeError(
                "Workspace replay-not-found error requires a WorkspaceRequestId"
            )
        self.request_id = request_id
        """@brief 已明确不存在的 journal 请求标识 / Request ID of the journal explicitly found absent."""
        super().__init__(f"Workspace file replay was not found: {request_id}")


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


def _normalize_diagnostic_code(value: str | None) -> str | None:
    """@brief 校验 native 机器诊断码 / Validate a native machine diagnostic code.

    @param value 候选机器码 / Candidate machine code.
    @return 安全机器码；非法或缺失时为 None / Safe machine code, or None when absent or invalid.
    """

    if value is None or _DIAGNOSTIC_CODE_PATTERN.fullmatch(value) is None:
        return None
    return value


def _normalize_diagnostic_message(value: str | None) -> str | None:
    """@brief 规范化 native 运维消息并移除控制字符 / Normalize a native operator message and remove control characters.

    @param value native binding 显式提供的候选消息 / Candidate message explicitly supplied by the native binding.
    @return 单行有界消息；缺失或空白时为 None / Bounded single-line message, or None when absent or blank.
    """

    if value is None:
        return None
    normalized = "".join(
        character if character.isprintable() else " " for character in value
    )
    normalized = " ".join(normalized.split())
    return normalized[:_DIAGNOSTIC_MESSAGE_LIMIT] or None


__all__ = [
    "WorkspaceFileReplayNotFoundError",
    "WorkspaceInvocationOutcomeUnknownError",
    "WorkspaceRuntimeProtocolError",
    "WorkspaceRuntimeUnavailableError",
]
