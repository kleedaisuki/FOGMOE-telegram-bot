"""@brief Provider-neutral Assistant completion ports / Provider-neutral Assistant 完成端口.

业务层只交换规范消息 V2（Canonical Message V2）。具体 provider 的 JSON wire payload
只能停留在基础设施 adapter 内；这使 checkpoint、工具执行和历史投影不再依赖某一种
兼容协议。/
The business layer exchanges only Canonical Message V2. Provider-specific JSON wire payloads
remain inside infrastructure adapters, so checkpoints, tool execution, and history projection no
longer depend on a compatibility protocol.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from fogmoe_bot.domain.assistant.messages import CanonicalMessage, ToolCallPart
from fogmoe_bot.domain.assistant.request_metadata import RequestMeta
from fogmoe_bot.domain.assistant.routing.models import ProviderRoute
from fogmoe_bot.domain.conversation.identity import TurnId
from fogmoe_bot.domain.conversation.message import MessageRole
from fogmoe_bot.domain.conversation.payloads import JsonObject, JsonValue

from .tools.catalog import ToolDefinition


@dataclass(frozen=True, slots=True)
class CompletionToolCall:
    """@brief 从 canonical Assistant 消息派生的工具调用 / Tool call derived from a canonical Assistant message.

    @param provider_call_id 跨协议非空调用 ID / Non-empty cross-protocol call identifier.
    @param name 工具名称 / Tool name.
    @param arguments 已解析 JSON 参数 / Parsed JSON arguments.
    """

    provider_call_id: str
    name: str
    arguments: JsonValue

    def __post_init__(self) -> None:
        """@brief 校验不可歧义的调用身份 / Validate unambiguous call identity.

        @return None / None.
        """

        if not isinstance(self.provider_call_id, str) or not self.provider_call_id.strip():
            raise ValueError("provider_call_id cannot be blank")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("tool call name cannot be blank")


@dataclass(frozen=True, slots=True)
class AssistantCompletion:
    """@brief 一个规范的 Assistant 完成 / One canonical Assistant completion.

    @param message provider 已解析的 canonical V2 Assistant 消息 / Provider-decoded canonical V2 Assistant message.
    """

    message: CanonicalMessage

    def __post_init__(self) -> None:
        """@brief 限制 completion 为 Assistant 消息 / Restrict a completion to an Assistant message.

        @return None / None.
        """

        if not isinstance(self.message, CanonicalMessage):
            raise TypeError("AssistantCompletion.message must be CanonicalMessage")
        if self.message.role is not MessageRole.ASSISTANT:
            raise ValueError("AssistantCompletion.message must have assistant role")
        call_ids = [
            part.call_id
            for part in self.message.parts
            if isinstance(part, ToolCallPart)
        ]
        if len(set(call_ids)) != len(call_ids):
            raise ValueError("AssistantCompletion tool call identifiers must be unique")

    @property
    def content(self) -> str:
        """@brief 读取 Assistant 文本部分 / Read Assistant text parts.

        @return 拼接后的可展示文本 / Concatenated displayable text.
        """

        return self.message.text

    @property
    def tool_calls(self) -> tuple[CompletionToolCall, ...]:
        """@brief 从 canonical parts 派生工具调用 / Derive tool calls from canonical parts.

        @return 按消息顺序排列的工具调用 / Tool calls in message order.
        """

        calls: list[CompletionToolCall] = []
        for part in self.message.parts:
            if not isinstance(part, ToolCallPart):
                continue
            raw_arguments = part.to_json()["arguments"]
            calls.append(
                CompletionToolCall(
                    provider_call_id=part.call_id,
                    name=part.name,
                    arguments=raw_arguments,
                )
            )
        return tuple(calls)


class AssistantCompletionPort(Protocol):
    """@brief 异步模型完成端口 / Asynchronous model-completion port."""

    async def complete(
        self,
        *,
        route: ProviderRoute,
        model: str,
        messages: Sequence[CanonicalMessage],
        tools: Sequence[ToolDefinition],
        tool_choice: str | JsonObject | None,
        max_tokens: int,
        timeout_seconds: float | None,
        request_meta: RequestMeta,
    ) -> AssistantCompletion:
        """@brief 请求一次模型完成 / Request one model completion.

        @param route 自包含 provider route / Self-contained provider route.
        @param model route 选中的模型 / Model selected within the route.
        @param messages 规范 V2 历史 / Canonical V2 history.
        @param tools 可用 typed tools / Available typed tools.
        @param tool_choice Provider-neutral 选择策略 / Provider-neutral selection policy.
        @param max_tokens 输出上限 / Output-token limit.
        @param timeout_seconds 本次请求总 deadline / Per-request total deadline.
        @param request_meta 调用方显式 metadata；adapter 按 route style 映射 /
            Explicit caller metadata, mapped by the adapter according to route style.
        @return 规范完成 / Canonical completion.
        """

        ...


@dataclass(frozen=True, slots=True)
class AgentStepCheckpoint:
    """@brief provider response 先于 effect 的 durable checkpoint / Durable provider-response checkpoint preceding effects.

    @param turn_id Turn ID / Turn identifier.
    @param step_no 模型 step 序号 / Model-step number.
    @param request_hash 输入摘要 / Input digest.
    @param route_key route/model 稳定键 / Stable route/model key.
    @param completion 规范完成 / Canonical completion.
    """

    turn_id: TurnId
    step_no: int
    request_hash: str
    route_key: str
    completion: AssistantCompletion


class AgentCheckpointPersistence(Protocol):
    """@brief Agent step checkpoint 持久化端口 / Persistence port for Agent-step checkpoints."""

    async def load_step(
        self, turn_id: TurnId, step_no: int
    ) -> AgentStepCheckpoint | None:
        """@brief 读取一个已提交 step / Load one committed step.

        @param turn_id Turn ID / Turn identifier.
        @param step_no step 序号 / Step number.
        @return checkpoint 或 None / Checkpoint or None.
        """

        ...

    async def save_step(self, checkpoint: AgentStepCheckpoint) -> AgentStepCheckpoint:
        """@brief 幂等保存并返回规范 checkpoint / Idempotently save and return the canonical checkpoint.

        @param checkpoint 待保存值 / Checkpoint to save.
        @return 数据库中的规范值 / Canonical persisted value.
        @raise AgentCheckpointConflictError 同 Turn/step 输入或 route 冲突 / Same Turn/step has a conflicting input or route.
        """

        ...


class AgentCheckpointConflictError(RuntimeError):
    """@brief checkpoint 身份冲突 / Checkpoint identity conflict."""


__all__ = [
    "AgentCheckpointConflictError",
    "AgentCheckpointPersistence",
    "AgentStepCheckpoint",
    "AssistantCompletion",
    "AssistantCompletionPort",
    "CompletionToolCall",
]
