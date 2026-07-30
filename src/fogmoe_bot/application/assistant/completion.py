"""@brief Provider-neutral Assistant completion ports / Provider-neutral Assistant 完成端口.

业务层只交换规范消息 V2（Canonical Message V2）。具体 provider 的 JSON wire payload
只能停留在基础设施 adapter 内；这使 checkpoint、工具执行和历史投影不再依赖某一种
兼容协议。/
The business layer exchanges only Canonical Message V2. Provider-specific JSON wire payloads
remain inside infrastructure adapters, so checkpoints, tool execution, and history projection no
longer depend on a compatibility protocol.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol, TypeAlias

from fogmoe_bot.domain.assistant.messages import CanonicalMessage, ToolCallPart
from fogmoe_bot.domain.assistant.request_metadata import RequestMeta
from fogmoe_bot.domain.assistant.routing.models import ProviderRoute
from fogmoe_bot.domain.conversation.identity import TurnId
from fogmoe_bot.domain.conversation.inference import InferenceGenerationFence
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


PromptCacheMode: TypeAlias = Literal["automatic", "explicit"]
"""@brief Provider-neutral prompt-cache mode / Provider-neutral prompt-cache mode."""

PromptCacheTtl: TypeAlias = Literal["5m", "30m", "1h"]
"""@brief Provider-neutral prompt-cache TTL / Provider-neutral prompt-cache TTL."""


@dataclass(frozen=True, slots=True, init=False)
class PromptCacheKey:
    """@brief 不携带用户身份或内容的有界 opaque cache-routing key / Bounded opaque cache-routing key carrying no user identity or content.

    ``wire_value`` 是固定 64 字符 HMAC-SHA256 digest。唯一受支持的构造器只接受部署、
    route、model 与静态 policy revision 命名空间；不得传入用户 ID、对话 ID、prompt、
    WorkingMemory 或 secret。不同部署/route/model/policy revision 自动隔离；同一命名
    空间的请求共享 routing key，但 provider 仍要求稳定前缀逐字节完全匹配。固定
    HMAC key 只做 domain separation，并不是 secret；digest 碰撞仅会影响缓存路由
    效率，不会绕过 provider 的 exact-prefix 匹配。/
    ``wire_value`` is a fixed 64-character SHA-256 digest. The only supported factory accepts
    deployment, route, model, and static policy-revision namespaces; user IDs, conversation IDs,
    prompts, WorkingMemory, and secrets must never be supplied. Deployments/routes/models/policy
    revisions are isolated automatically. Requests in one namespace share a routing key, while
    the provider still requires a byte-exact stable-prefix match. The fixed HMAC key is only a
    domain separator, not a secret. A digest collision could affect cache routing efficiency but
    cannot bypass the provider's exact-prefix match.

    @param wire_value 可直接发送给 provider 的 64 字符 lowercase hex digest /
        64-character lowercase hex digest safe to send to a provider.
    """

    wire_value: str

    @classmethod
    def for_route_model(
        cls,
        *,
        deployment_namespace: str,
        route_id: str,
        model: str,
        policy_revision: str,
    ) -> PromptCacheKey:
        """@brief 从纯静态配置命名空间派生 opaque key / Derive an opaque key from static configuration namespaces only.

        @param deployment_namespace 静态部署隔离名 / Static deployment-isolation name.
        @param route_id 静态 route 配置 ID / Static route configuration ID.
        @param model 静态 provider model 名 / Static provider model name.
        @param policy_revision 稳定 system/tool policy 版本 / Stable system/tool policy revision.
        @return 不含原始命名空间文本的固定长度 key / Fixed-length key containing none of the source text.
        @raise ValueError 任一静态字段为空、过长或含控制字符时抛出 /
            Raised when a static field is blank, oversized, or contains control characters.
        @note 调用方不得把任何 per-user/per-conversation 值塞进这些字段 /
            Callers must not place any per-user or per-conversation value in these fields.
        """

        values = (
            ("deployment_namespace", deployment_namespace),
            ("route_id", route_id),
            ("model", model),
            ("policy_revision", policy_revision),
        )
        digest = hmac.new(
            b"fogmoe.prompt-cache-key.v1",
            digestmod=hashlib.sha256,
        )
        for field_name, value in values:
            if (
                not isinstance(value, str)
                or not value
                or len(value) > 256
                or any(ord(character) < 32 for character in value)
            ):
                raise ValueError(
                    f"PromptCacheKey {field_name} must be 1..256 printable characters"
                )
            encoded = value.encode("utf-8")
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)
        instance = object.__new__(cls)
        object.__setattr__(instance, "wire_value", digest.hexdigest())
        return instance


@dataclass(frozen=True, slots=True)
class PromptCacheDirective:
    """@brief 一次 completion 的稳定前缀缓存指令 / Stable-prefix cache directive for one completion.

    @param stable_prefix_message_count 可复用 canonical 消息前缀长度 /
        Length of the reusable canonical-message prefix.
    @param cache_key 从静态 route/model/policy 命名空间派生的 opaque 缓存键 /
        Opaque cache key derived from static route/model/policy namespaces.
    @param mode 自动或显式缓存模式 / Automatic or explicit cache mode.
    @param ttl 显式模式的 provider-neutral TTL / Provider-neutral TTL for explicit mode.
    @note ``automatic`` 不要求 adapter 写入任何 provider 私有字段；能力门控属于 route/model /
        ``automatic`` never asks an adapter to emit provider-private fields; route/model owns
        capability gating.
    """

    stable_prefix_message_count: int
    cache_key: PromptCacheKey
    mode: PromptCacheMode = "automatic"
    ttl: PromptCacheTtl | None = None

    def __post_init__(self) -> None:
        """@brief 校验缓存边界不变量 / Validate cache-boundary invariants.

        @return None / None.
        @raise ValueError 边界、键、模式或 TTL 不一致时抛出 /
            Raised when the boundary, key, mode, or TTL is inconsistent.
        """

        if (
            isinstance(self.stable_prefix_message_count, bool)
            or self.stable_prefix_message_count < 0
        ):
            raise ValueError("stable_prefix_message_count cannot be negative")
        if not isinstance(self.cache_key, PromptCacheKey):
            raise TypeError("prompt cache_key must be PromptCacheKey")
        if self.mode not in {"automatic", "explicit"}:
            raise ValueError("prompt cache mode must be automatic or explicit")
        if self.mode == "automatic" and self.ttl is not None:
            raise ValueError("automatic prompt caching must not select a TTL")
        if self.mode == "explicit" and self.ttl not in {"5m", "30m", "1h"}:
            raise ValueError("explicit prompt caching requires a supported TTL")


@dataclass(frozen=True, slots=True)
class CompletionTextDelta:
    """@brief Provider-neutral Assistant 文本增量 / Provider-neutral Assistant text delta.

    @param text 本事件新增的非空文本 / Non-empty text newly emitted by this event.
    """

    text: str

    def __post_init__(self) -> None:
        """@brief 拒绝无意义的空增量 / Reject meaningless empty deltas.

        @return None / None.
        @raise ValueError 文本为空时抛出 / Raised when text is empty.
        """

        if not isinstance(self.text, str) or not self.text:
            raise ValueError("CompletionTextDelta.text cannot be empty")


@dataclass(frozen=True, slots=True)
class CompletionFinished:
    """@brief 流式 completion 的唯一终态 / Sole terminal event of a streamed completion.

    @param completion 完整、可 checkpoint 的规范完成 /
        Complete canonical completion ready for checkpointing.
    """

    completion: AssistantCompletion

    def __post_init__(self) -> None:
        """@brief 校验终态 completion 类型 / Validate the terminal completion type.

        @return None / None.
        @raise TypeError completion 类型错误时抛出 / Raised for an invalid completion type.
        """

        if not isinstance(self.completion, AssistantCompletion):
            raise TypeError("CompletionFinished.completion must be AssistantCompletion")


AssistantCompletionStreamEvent: TypeAlias = CompletionTextDelta | CompletionFinished
"""@brief Provider-neutral completion stream event / Provider-neutral completion stream event."""


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
        prompt_cache: PromptCacheDirective | None = None,
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
        @param prompt_cache 已经由 route/model 能力门控的缓存指令 /
            Cache directive already gated by route/model capabilities.
        @return 规范完成 / Canonical completion.
        """

        ...


class AssistantStreamingCompletionPort(Protocol):
    """@brief Provider-neutral 异步流式完成端口 / Provider-neutral asynchronous streaming-completion port."""

    def stream(
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
        prompt_cache: PromptCacheDirective | None = None,
    ) -> AsyncIterator[AssistantCompletionStreamEvent]:
        """@brief 流式请求一次模型完成 / Stream one model completion.

        @param route 自包含 provider route / Self-contained provider route.
        @param model route 选中的模型 / Model selected within the route.
        @param messages 规范 V2 历史 / Canonical V2 history.
        @param tools 可用 typed tools / Available typed tools.
        @param tool_choice Provider-neutral 选择策略 / Provider-neutral selection policy.
        @param max_tokens 输出上限 / Output-token limit.
        @param timeout_seconds 本次请求总 deadline / Per-request total deadline.
        @param request_meta 调用方显式 metadata / Explicit caller metadata.
        @param prompt_cache 已经由 route/model 能力门控的缓存指令 /
            Cache directive already gated by route/model capabilities.
        @return 文本增量，随后恰好一个完整终态 /
            Text deltas followed by exactly one terminal completion.
        @note 取消必须直接传播，调用方不能 checkpoint 任何未出现 ``CompletionFinished`` 的流 /
            Cancellation propagates directly; callers must not checkpoint a stream lacking
            ``CompletionFinished``.
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
    @param generation checkpoint effect generation；等于 input revision /
        Checkpoint effect generation, equal to the input revision.
    @param generation_fence 仅本次写入使用、不持久化的 claim fence /
        Claim fence used only for this write and not persisted.
    """

    turn_id: TurnId
    step_no: int
    request_hash: str
    route_key: str
    completion: AssistantCompletion
    generation: int = 0
    generation_fence: InferenceGenerationFence | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        """@brief 校验 checkpoint generation / Validate checkpoint generation.

        @return None / None.
        """

        if isinstance(self.generation, bool) or self.generation < 0:
            raise ValueError("Agent checkpoint generation cannot be negative")
        if (
            self.generation_fence is not None
            and int(self.generation_fence.input_revision) != self.generation
        ):
            raise ValueError(
                "Agent checkpoint generation must match its generation fence"
            )


class AgentCheckpointPersistence(Protocol):
    """@brief Agent step checkpoint 持久化端口 / Persistence port for Agent-step checkpoints."""

    async def load_step(
        self,
        turn_id: TurnId,
        step_no: int,
        *,
        generation: int = 0,
    ) -> AgentStepCheckpoint | None:
        """@brief 读取一个已提交 step / Load one committed step.

        @param turn_id Turn ID / Turn identifier.
        @param step_no step 序号 / Step number.
        @param generation input-revision effect generation / Input-revision effect generation.
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


class InferenceGenerationFencePort(Protocol):
    """@brief 副作用前验证当前 inference generation 的端口 / Port validating the current inference generation before effects."""

    async def assert_current_generation(
        self,
        fence: InferenceGenerationFence,
    ) -> None:
        """@brief 拒绝已被 steer、恢复或完成的 generation / Reject a generation already steered, recovered, or completed.

        @param fence 当前 worker claim/revision / Current worker claim and revision.
        @return None / None.
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
    "AssistantCompletionStreamEvent",
    "AssistantStreamingCompletionPort",
    "CompletionFinished",
    "CompletionTextDelta",
    "CompletionToolCall",
    "InferenceGenerationFencePort",
    "PromptCacheDirective",
    "PromptCacheKey",
    "PromptCacheMode",
    "PromptCacheTtl",
]
