"""@brief Durable Assistant 的异步工具运行时 / Async tool runtime for durable Assistant turns.

运行时无 pending-map、线程锁、ContextVar 或 adapter import。每个调用具有由 Turn、
模型 step 和 ordinal 派生的稳定 invocation ID；所有结果都经 persistence port 固化，
mutation 由该 port 在 durable effect receipt 保护下执行。/
The runtime owns no pending map, thread lock, ContextVar, or adapter import. Every invocation has
a stable ID derived from its Turn, model step, and ordinal; every result is persisted through a
port, and mutations execute behind a durable effect receipt owned by that port.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal, NotRequired, Protocol, TypedDict, cast

from fogmoe_bot.domain.conversation.payloads import (
    JsonObject,
    JsonValue,
)
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    TurnId,
)

from .tools.catalog import (
    InvalidToolArguments,
    ToolCatalog,
    ToolDefinition,
    ToolValidationIssue,
    ToolResultResidency,
    UnknownTool,
    ValidatedToolInvocation,
)


class ValidationIssue(TypedDict):
    """@brief 单个工具参数错误 / One tool-argument validation issue."""

    field: str
    message: str
    type: str


class ToolValidationFailure(TypedDict):
    """@brief 可安全反馈模型的校验失败 / Validation failure safe for model feedback."""

    error: str
    details: list[ValidationIssue]


class AssistantVisibleEvent(TypedDict):
    """@brief Assistant 可见文本事件 / Assistant-visible text event."""

    type: Literal["assistant_visible"]
    content: str


class AssistantToolCallEvent(TypedDict):
    """@brief 已 checkpoint 的工具调用事件 / Checkpointed tool-call event."""

    type: Literal["assistant_tool_call"]
    tool_name: str
    arguments: JsonValue
    tool_call_id: str
    invocation_id: str
    validation_error: NotRequired[ToolValidationFailure]
    assistant_message: NotRequired[JsonObject]
    ephemeral: NotRequired[bool]


class ToolResultEvent(TypedDict):
    """@brief receipt 固化的工具结果事件 / Receipt-backed tool-result event."""

    type: Literal["tool_result"]
    tool_name: str
    arguments: JsonObject
    result: JsonValue
    tool_call_id: str
    invocation_id: str
    effect_kind: str
    replayed: bool
    ephemeral: NotRequired[bool]


type RuntimeEvent = AssistantVisibleEvent | AssistantToolCallEvent | ToolResultEvent
"""@brief 可持久化 Assistant 运行事件 / Persistable Assistant runtime event."""


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    """@brief 一个 durable Turn 的工具授权上下文 / Tool authorization context for one durable Turn.

    @param turn_id durable Turn ID / Durable Turn identifier.
    @param conversation_id 会话聚合 ID / Conversation aggregate identifier.
    @param delivery_stream_id 有序投递流 / Ordered delivery stream.
    @param user_id 已认证用户 ID / Authenticated user identifier.
    @param chat_id 外部 chat ID / External chat identifier.
    @param is_group 是否群聊 / Whether this is a group chat.
    @param group_id 可选群组 ID / Optional group identifier.
    @param message_id 当前消息 ID / Current message identifier.
    @param message_thread_id 可选 Telegram 话题 ID / Optional Telegram topic identifier.
    @param allowed_tools 可选 Turn 级工具 allowlist；None 表示完整目录 /
        Optional turn-level tool allowlist; None means the complete catalog.
    """

    turn_id: TurnId
    conversation_id: ConversationId
    delivery_stream_id: DeliveryStreamId
    user_id: int
    chat_id: int | str
    is_group: bool
    group_id: int | None
    message_id: int | None
    message_thread_id: int | None = None
    allowed_tools: frozenset[str] | None = None


@dataclass(frozen=True, slots=True)
class ToolEffectRequest:
    """@brief persistence port 消费的完整调用 / Complete invocation consumed by persistence.

    @param context Turn 授权上下文 / Turn authorization context.
    @param invocation_id Turn 内稳定 ordinal ID / Stable ordinal ID within the Turn.
    @param provider_call_id Provider correlation ID / Provider correlation identifier.
    @param tool_name 目录工具名 / Catalog tool name.
    @param effect_kind receipt 类别 / Receipt kind.
    @param mutating 是否改变业务事实 / Whether business facts are mutated.
    @param arguments 已校验 JSON 参数 / Validated JSON arguments.
    @param request_hash 身份与参数摘要 / Identity-and-argument digest.
    @param result_cacheable 结果是否可写 durable receipt / Whether the result may be written to a durable receipt.
    """

    context: ToolExecutionContext
    invocation_id: str
    provider_call_id: str
    tool_name: str
    effect_kind: str
    mutating: bool
    arguments: JsonObject
    request_hash: str
    result_cacheable: bool = True


@dataclass(frozen=True, slots=True)
class PersistedToolResult:
    """@brief 一个规范 receipt 结果 / One canonical receipt result.

    @param result 完整内部 JSON 结果 / Complete internal JSON result.
    @param replayed 是否读取已有 receipt / Whether an existing receipt was replayed.
    """

    result: JsonValue
    replayed: bool


class ToolEffectPersistence(Protocol):
    """@brief 工具读取快照与 mutation receipt 的唯一执行端口 / Sole execution port for tool read snapshots and mutation receipts."""

    async def execute(self, request: ToolEffectRequest) -> PersistedToolResult:
        """@brief 幂等执行或重放一个调用 / Idempotently execute or replay an invocation.

        @param request 完整、已校验调用 / Complete validated invocation.
        @return 规范结果 / Canonical result.
        @note mutating 请求的业务 mutation 与 succeeded receipt 必须在同一 UoW；外部
            mutation 必须先形成 durable activity 或使用目的端幂等键。/
            A mutating business operation and its succeeded receipt must share one UoW; external
            mutations must first become durable activities or use destination idempotency keys.
        """

        ...


class ToolEffectBusyError(RuntimeError):
    """@brief 同一 receipt 仍被有效租约执行 / The same receipt is under a live execution lease."""


class ToolEffectConflictError(RuntimeError):
    """@brief 稳定 invocation ID 被不同请求重用 / A stable invocation ID was reused for a different request."""


@dataclass(frozen=True, slots=True)
class ToolRuntimeResult:
    """@brief Agent loop 消费的工具结果 / Tool result consumed by the Agent loop.

    @param invocation_id 稳定内部 invocation ID / Stable internal invocation ID.
    @param provider_call_id Provider correlation ID / Provider correlation ID.
    @param name 工具名称 / Tool name.
    @param arguments 已校验参数 / Validated arguments.
    @param effect_kind receipt 类别 / Receipt kind.
    @param validation_error 可选校验失败 / Optional validation failure.
    @param public_result 回填模型的安全结果 / Safe result fed back to the model.
    @param replayed 是否 receipt replay / Whether this was a receipt replay.
    @param result_residency 工具结果驻留期 / Tool-result residency.
    """

    invocation_id: str
    provider_call_id: str
    name: str
    arguments: JsonObject
    effect_kind: str
    validation_error: ToolValidationFailure | None
    public_result: JsonValue
    replayed: bool
    result_residency: ToolResultResidency


class AgentRuntime:
    """@brief 无状态异步工具协调器 / Stateless asynchronous tool coordinator."""

    def __init__(
        self, *, catalog: ToolCatalog, persistence: ToolEffectPersistence
    ) -> None:
        """@brief 创建工具协调器 / Create the tool coordinator.

        @param catalog 权威工具目录 / Authoritative tool catalog.
        @param persistence receipt 与 operation port / Receipt-and-operation port.
        """

        self._catalog = catalog
        self._persistence = persistence

    @property
    def tool_definitions(self) -> tuple[ToolDefinition, ...]:
        """@brief 返回 provider-neutral 工具定义 / Return provider-neutral definitions.

        @return 有序不可变定义 / Ordered immutable definitions.
        """

        return self._catalog.definitions

    async def execute(
        self,
        *,
        context: ToolExecutionContext,
        step: int,
        ordinal: int,
        provider_call_id: str | None,
        tool_name: str,
        raw_arguments: object,
    ) -> ToolRuntimeResult:
        """@brief 校验并经 receipt port 执行工具 / Validate and execute through the receipt port.

        @param context durable Turn 上下文 / Durable Turn context.
        @param step checkpoint 模型 step / Checkpointed model step.
        @param ordinal step 内调用序号 / Call ordinal within the step.
        @param provider_call_id Provider correlation ID / Provider correlation ID.
        @param tool_name 工具名 / Tool name.
        @param raw_arguments Provider 原始参数 / Raw provider arguments.
        @return 可回填模型的结果 / Result suitable for model feedback.
        """

        if step < 0 or ordinal < 0:
            raise ValueError("step and ordinal must be non-negative")
        invocation_id = f"step:{step}:call:{ordinal}"
        correlation_id = provider_call_id or invocation_id
        if context.allowed_tools is not None and tool_name not in context.allowed_tools:
            return ToolRuntimeResult(
                invocation_id=invocation_id,
                provider_call_id=correlation_id,
                name=tool_name,
                arguments={},
                effect_kind=f"read.{tool_name}",
                validation_error=None,
                public_result={
                    "error": f"Tool is not authorized for this turn: {tool_name}"
                },
                replayed=False,
                result_residency=ToolResultResidency.AGENT_TURN,
            )
        validation = self._catalog.validate(tool_name, raw_arguments)
        if isinstance(validation, UnknownTool):
            return ToolRuntimeResult(
                invocation_id=invocation_id,
                provider_call_id=correlation_id,
                name=tool_name,
                arguments={},
                effect_kind=f"read.{tool_name}",
                validation_error=None,
                public_result={"error": f"Unknown tool: {tool_name}"},
                replayed=False,
                result_residency=ToolResultResidency.CONVERSATION,
            )
        if isinstance(validation, InvalidToolArguments):
            failure = _validation_failure(validation.issues)
            return ToolRuntimeResult(
                invocation_id=invocation_id,
                provider_call_id=correlation_id,
                name=tool_name,
                arguments={},
                effect_kind=f"read.{tool_name}",
                validation_error=failure,
                public_result=cast(JsonValue, failure),
                replayed=False,
                result_residency=ToolResultResidency.CONVERSATION,
            )
        if not isinstance(validation, ValidatedToolInvocation):
            raise AssertionError("unhandled tool validation result")
        arguments = cast(
            JsonObject,
            validation.arguments.model_dump(
                mode="json",
                exclude_none=True,
                exclude_unset=True,
            ),
        )
        effect_kind = str(validation.effect_kind)
        request_hash = _request_hash(
            context=context,
            invocation_id=invocation_id,
            tool_name=tool_name,
            effect_kind=effect_kind,
            arguments=arguments,
        )
        persisted = await self._persistence.execute(
            ToolEffectRequest(
                context=context,
                invocation_id=invocation_id,
                provider_call_id=correlation_id,
                tool_name=tool_name,
                effect_kind=effect_kind,
                mutating=validation.mutating,
                arguments=arguments,
                request_hash=request_hash,
                result_cacheable=validation.result_cacheable,
            )
        )
        return ToolRuntimeResult(
            invocation_id=invocation_id,
            provider_call_id=correlation_id,
            name=tool_name,
            arguments=arguments,
            effect_kind=effect_kind,
            validation_error=None,
            public_result=_public_result(tool_name, persisted.result),
            replayed=persisted.replayed,
            result_residency=validation.result_residency,
        )


def _validation_failure(
    issues: tuple[ToolValidationIssue, ...],
) -> ToolValidationFailure:
    """@brief 构造模型安全校验失败 / Build a model-safe validation failure.

    @param issues 类型化 issues / Typed issues.
    @return JSON-safe failure / JSON-safe failure.
    """

    return {
        "error": "Tool arguments failed validation",
        "details": [
            {"field": issue.field, "message": issue.message, "type": issue.code}
            for issue in issues
        ],
    }


def _request_hash(
    *,
    context: ToolExecutionContext,
    invocation_id: str,
    tool_name: str,
    effect_kind: str,
    arguments: JsonObject,
) -> str:
    """@brief 计算 receipt 冲突摘要 / Compute a receipt-conflict digest.

    @param context Turn 上下文 / Turn context.
    @param invocation_id 稳定 invocation ID / Stable invocation ID.
    @param tool_name 工具名 / Tool name.
    @param effect_kind 类别 / Effect kind.
    @param arguments 规范参数 / Canonical arguments.
    @return 小写 SHA-256 / Lowercase SHA-256.
    """

    payload = {
        "turn_id": str(context.turn_id),
        "conversation_id": str(context.conversation_id),
        "delivery_stream_id": str(context.delivery_stream_id),
        "user_id": context.user_id,
        "chat_id": context.chat_id,
        "is_group": context.is_group,
        "group_id": context.group_id,
        "message_id": context.message_id,
        "message_thread_id": context.message_thread_id,
        "allowed_tools": (
            None if context.allowed_tools is None else sorted(context.allowed_tools)
        ),
        "invocation_id": invocation_id,
        "tool_name": tool_name,
        "effect_kind": effect_kind,
        "arguments": arguments,
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _public_result(tool_name: str, result: JsonValue) -> JsonValue:
    """@brief 去除模型不应看到的媒体内部标识 / Remove media identifiers hidden from the model.

    @param tool_name 工具名 / Tool name.
    @param result 完整 receipt 结果 / Complete receipt result.
    @return 模型安全结果 / Model-safe result.
    """

    if tool_name not in {"generate_image", "generate_voice"} or not isinstance(
        result, dict
    ):
        return result
    if "error" in result:
        return {
            key: value
            for key, value in result.items()
            if key in {"error", "status", "warnings", "retry_after_seconds"}
        }
    return {
        "status": result.get("status", "queued"),
        "message": "Generated media was durably queued for delivery.",
    }


__all__ = [
    "AgentRuntime",
    "AssistantToolCallEvent",
    "AssistantVisibleEvent",
    "PersistedToolResult",
    "RuntimeEvent",
    "ToolEffectBusyError",
    "ToolEffectConflictError",
    "ToolEffectPersistence",
    "ToolEffectRequest",
    "ToolExecutionContext",
    "ToolResultEvent",
    "ToolRuntimeResult",
    "ToolValidationFailure",
]
