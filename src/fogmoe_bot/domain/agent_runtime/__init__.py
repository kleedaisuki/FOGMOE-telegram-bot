"""@brief Agent 任务运行时 / Agent task runtime.

该包是 Agent 与工具执行环境之间的唯一边界。Agent 只提交任务并消费结果，
不依赖具体工具 handler、参数校验或用户可见媒体投递细节。
"""

from .runtime import (
    DEFAULT_AGENT_RUNTIME,
    AgentRuntime,
    TaskHandle,
    ToolTask,
    ToolTaskResult,
)

__all__ = [
    "DEFAULT_AGENT_RUNTIME",
    "AgentRuntime",
    "TaskHandle",
    "ToolTask",
    "ToolTaskResult",
]
