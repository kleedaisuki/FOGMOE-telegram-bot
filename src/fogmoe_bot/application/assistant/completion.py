"""@brief Provider-neutral Assistant completion ports / Provider-neutral Assistant 完成端口."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from fogmoe_bot.domain.conversation.payloads import (
    JsonObject,
    JsonValue,
)
from fogmoe_bot.domain.conversation.identity import TurnId

from .tools.catalog import ToolDefinition


@dataclass(frozen=True, slots=True)
class CompletionToolCall:
    """@brief 一个已归一化工具调用 / One normalized tool call.

    @param provider_call_id Provider correlation ID / Provider correlation identifier.
    @param name 工具名称 / Tool name.
    @param arguments Provider 解码前或解码后的参数 / Raw or decoded provider arguments.
    """

    provider_call_id: str | None
    name: str
    arguments: JsonValue


@dataclass(frozen=True, slots=True)
class AssistantCompletion:
    """@brief 一个 provider-neutral Assistant message / One provider-neutral Assistant message.

    @param content 文本内容 / Text content.
    @param message 可安全持久化的完整消息 / Complete persistable message.
    @param tool_calls 已归一化调用 / Normalized calls.
    """

    content: str
    message: JsonObject
    tool_calls: tuple[CompletionToolCall, ...] = ()

    def __post_init__(self) -> None:
        """@brief 隔离可变消息 / Isolate the mutable message.

        @return None / None.
        """

        object.__setattr__(self, "message", dict(self.message))


class AssistantCompletionPort(Protocol):
    """@brief 异步模型完成端口 / Asynchronous model-completion port."""

    async def complete(
        self,
        *,
        provider: str,
        model: str,
        messages: Sequence[JsonObject],
        tools: Sequence[ToolDefinition],
        tool_choice: str | JsonObject | None,
        max_tokens: int,
        request_options: Mapping[str, JsonValue],
    ) -> AssistantCompletion:
        """@brief 请求一次模型完成 / Request one model completion.

        @param provider provider 名称 / Provider name.
        @param model 模型名称 / Model name.
        @param messages 规范历史 / Canonical history.
        @param tools 可用 typed tools / Available typed tools.
        @param tool_choice Provider-neutral 选择策略 / Provider-neutral selection policy.
        @param max_tokens 输出上限 / Output-token limit.
        @param request_options 有界 route 选项 / Bounded route options.
        @return 归一化完成 / Normalized completion.
        """

        ...


@dataclass(frozen=True, slots=True)
class AgentStepCheckpoint:
    """@brief provider response 先于 effect 的 durable checkpoint / Durable provider-response checkpoint preceding effects.

    @param turn_id Turn ID / Turn identifier.
    @param step_no 模型 step 序号 / Model-step number.
    @param request_hash 输入摘要 / Input digest.
    @param route_key provider/model 稳定键 / Stable provider/model key.
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
