"""@brief Assistant 错误语义 / Assistant error semantics."""

from .tool_runtime import RuntimeEvent


class SafetyBlockError(RuntimeError):
    """@brief Provider 内容安全拦截 / Provider content-safety block."""


class AssistantInferenceUnavailableError(RuntimeError):
    """@brief 所有可用 provider route 均未完成推理 / Every available provider route failed to complete inference.

    @param message 稳定错误摘要 / Stable error summary.
    @param last_error 最后一个 provider 异常 / Last provider exception.
    """

    last_error: Exception | None

    def __init__(self, message: str, *, last_error: Exception | None) -> None:
        """@brief 创建 provider 耗尽错误 / Create an exhausted-provider error.

        @param message 稳定错误摘要 / Stable error summary.
        @param last_error 最后一个 provider 异常 / Last provider exception.
        """

        super().__init__(message)
        self.last_error = last_error


class PartialAgentResponseError(RuntimeError):
    """@brief Runtime 已产生事件但 Agent 未完成 / Agent failed after Runtime events.

    @param message 失败原因 / Failure reason.
    @param events 已产生且必须保留的 Runtime 事件 / Runtime events that must be retained.
    """

    def __init__(self, message: str, events: list[RuntimeEvent]) -> None:
        """@brief 创建部分响应错误 / Create a partial response error.

        @param message 失败原因 / Failure reason.
        @param events 已产生事件 / Emitted events.
        """
        super().__init__(message)
        self.events = list(events)


class ResumableAgentInterruptedError(RuntimeError):
    """@brief checkpoint 后 provider 中断，可安全重试 / Provider interruption after a checkpoint, safe to retry.

    已提交的 provider steps 与工具结果均由 checkpoint/receipt 拥有，因此该异常不是
    ``partial effect`` 永久失败。/ Committed provider steps and tool results are owned by
    checkpoints and receipts, so this is not a permanent partial-effect failure.
    """


__all__ = [
    "AssistantInferenceUnavailableError",
    "PartialAgentResponseError",
    "ResumableAgentInterruptedError",
    "SafetyBlockError",
]
