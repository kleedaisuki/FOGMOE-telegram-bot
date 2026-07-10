"""@brief Agent 任务发布与完成运行时 / Agent task submission and completion runtime."""

import logging
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping

from .execution import execute_tool_call
from .tools import AI_TOOL_ARG_MODELS, AI_TOOL_HANDLERS, OPENAI_TOOLS


@dataclass(frozen=True)
class ToolTask:
    """@brief Agent 发布的能力任务 / Capability task published by an Agent.

    @param name 能力名称 / Capability name.
    @param arguments 已解析的原始参数 / Parsed raw arguments.
    @param invocation_id LLM provider 的 tool-call 标识 / LLM provider tool-call identifier.
    @param producer_name Agent/provider 名称，用于审计 / Agent/provider name for auditing.
    """

    name: str
    arguments: Any
    invocation_id: str | None
    producer_name: str


@dataclass(frozen=True)
class TaskHandle:
    """@brief 已提交任务的不可伪造句柄 / Opaque handle for a submitted task.

    @param task_id Runtime 内部关联标识 / Runtime-internal correlation identifier.
    """

    task_id: str


@dataclass(frozen=True)
class ToolTaskResult:
    """@brief Agent 可消费的任务完成结果 / Completion result consumable by an Agent.

    @note public_result 是唯一应回填 LLM 的结果；internal_result 仅用于审计与
    Telegram 后续媒体投递 / public_result is the only result to feed back to the
    LLM; internal_result is only for audit and later Telegram media delivery.
    """

    task_id: str
    invocation_id: str | None
    name: str
    arguments: Dict[str, Any]
    logged_arguments: Any
    validation_error: Dict[str, Any] | None
    public_result: Any
    internal_result: Any
    media_sent: bool
    sent_message_count: int


class AgentRuntime:
    """@brief 隔离 Agent 与能力执行细节 / Isolate Agents from capability execution details.

    Runtime 采用提交—完成模型：Agent 先 submit，再 consume。当前实现刻意保持
    同步完成语义以兼容既有 bot 行为；调用方不会依赖这一事实，后续可在此替换为
    有界异步调度器。
    """

    def __init__(
        self,
        *,
        tool_definitions: list[dict[str, Any]],
        arg_models: Mapping[str, Any],
        handlers: Mapping[str, Callable[..., Any]],
    ) -> None:
        """@brief 创建 Runtime / Create a Runtime.

        @param tool_definitions 提供给 LLM 的能力定义 / Capability definitions exposed to the LLM.
        @param arg_models 参数校验模型 / Argument validation models.
        @param handlers 能力执行器映射 / Capability executor mapping.
        """
        self._tool_definitions = tool_definitions
        self._arg_models = arg_models
        self._handlers = handlers
        self._pending: dict[str, ToolTask] = {}
        self._lock = threading.Lock()

    @property
    def tool_definitions(self) -> list[dict[str, Any]]:
        """@brief 读取 LLM 能力定义 / Read LLM capability definitions.

        @return OpenAI 兼容工具定义 / OpenAI-compatible tool definitions.
        """
        return self._tool_definitions

    @property
    def handlers(self) -> Mapping[str, Callable[..., Any]]:
        """@brief 暴露执行器映射以支持组合根注入 / Expose executors for composition-root injection.

        @return 能力名称到执行器的映射 / Capability-name to executor mapping.
        """
        return self._handlers

    def submit(self, task: ToolTask) -> TaskHandle:
        """@brief 发布任务到 Runtime / Publish a task to the Runtime.

        @param task Agent 任务描述 / Agent task descriptor.
        @return 可用于消费结果的句柄 / Handle used to consume the result.
        """
        task_id = uuid.uuid4().hex
        with self._lock:
            self._pending[task_id] = task
        return TaskHandle(task_id)

    def consume(
        self,
        handle: TaskHandle,
        *,
        visible_content_handler: Any = None,
    ) -> ToolTaskResult:
        """@brief 消费任务完成结果 / Consume a task completion result.

        @param handle submit 返回的任务句柄 / Task handle returned by submit.
        @param visible_content_handler 可选用户可见输出端口 / Optional user-visible output port.
        @return 可回填给 Agent 的完成结果 / Completion result for the Agent.
        @raise RuntimeError 当句柄无效或已被消费 / When handle is invalid or consumed.
        """
        with self._lock:
            task = self._pending.pop(handle.task_id, None)
        if task is None:
            raise RuntimeError(f"Unknown or consumed agent-runtime task: {handle.task_id}")

        execution = execute_tool_call(
            function_name=task.name,
            raw_function_args=task.arguments,
            provider_name=task.producer_name,
            arg_models=self._arg_models,
            handlers=self._handlers,
        )
        internal_result = execution.internal_result
        sent_messages = self._send_tool_media(
            visible_content_handler=visible_content_handler,
            tool_name=task.name,
            tool_result=internal_result,
            producer_name=task.producer_name,
        )
        return ToolTaskResult(
            task_id=handle.task_id,
            invocation_id=task.invocation_id,
            name=task.name,
            arguments=execution.function_args,
            logged_arguments=execution.logged_args,
            validation_error=execution.validation_error,
            public_result=self._public_result(
                task.name,
                internal_result,
                media_sent=bool(sent_messages),
            ),
            internal_result=internal_result,
            media_sent=bool(sent_messages),
            sent_message_count=len(sent_messages),
        )

    @staticmethod
    def _public_result(
        tool_name: str,
        tool_result: Any,
        *,
        media_sent: bool,
    ) -> Any:
        """@brief 构造可回填模型的安全结果 / Build a safe result for model feedback.

        @param tool_name 能力名称 / Capability name.
        @param tool_result 执行器内部结果 / Internal executor result.
        @param media_sent 是否已经投递媒体 / Whether media was already delivered.
        @return 对 Agent 公开的结果 / Result exposed to the Agent.
        """
        if tool_name not in {"generate_image", "generate_voice"} or not isinstance(tool_result, dict):
            return tool_result
        if tool_result.get("error"):
            return {
                key: tool_result[key]
                for key in ("error", "status_code", "details", "response_preview", "warnings", "retry_after_seconds")
                if key in tool_result
            }
        if tool_result.get("status") != "generated":
            return {"status": tool_result.get("status") or "unknown"}
        medium = "image" if tool_name == "generate_image" else "audio"
        return {
            "status": "generated",
            "message": (
                f"Generated {medium} has been sent to Telegram."
                if media_sent
                else (
                    "Generated image is ready and will be sent to Telegram. "
                    "If you need to inspect the image yourself later, ask the user "
                    "to forward the sent image back to you."
                    if medium == "image"
                    else "Generated audio is ready and will be sent to Telegram."
                )
            ),
        }

    @staticmethod
    def _send_tool_media(
        *,
        visible_content_handler: Any,
        tool_name: str,
        tool_result: Any,
        producer_name: str,
    ) -> list[Any]:
        """@brief 投递工具生成的媒体 / Deliver tool-generated media.

        @param visible_content_handler 用户可见输出端口 / User-visible output port.
        @param tool_name 能力名称 / Capability name.
        @param tool_result 工具内部结果 / Internal tool result.
        @param producer_name Agent/provider 名称 / Agent/provider name.
        @return 已发送消息 / Sent messages.
        """
        if (
            visible_content_handler is None
            or tool_name not in {"generate_image", "generate_voice"}
            or not isinstance(tool_result, dict)
            or tool_result.get("status") != "generated"
        ):
            return []
        send_func = getattr(visible_content_handler, "send_media", None)
        if not callable(send_func):
            return []
        try:
            sent_messages = send_func(tool_name, tool_result)
        except Exception as exc:
            logging.exception("%s failed to send %s result immediately: %s", producer_name, tool_name, exc)
            return []
        return sent_messages if isinstance(sent_messages, list) else []


DEFAULT_AGENT_RUNTIME = AgentRuntime(
    tool_definitions=OPENAI_TOOLS,
    arg_models=AI_TOOL_ARG_MODELS,
    handlers=AI_TOOL_HANDLERS,
)
"""@brief Domain 管理的默认 Agent 工具运行时 / Domain-managed default Agent tool runtime."""
