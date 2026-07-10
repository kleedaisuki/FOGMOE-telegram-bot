"""@brief Assistant 错误语义 / Assistant error semantics."""

from fogmoe_bot.domain.agent_runtime.events import RuntimeEvent


class SafetyBlockError(RuntimeError):
    """@brief Provider 内容安全拦截 / Provider content-safety block."""


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
