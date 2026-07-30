"""@brief Provider-neutral 可恢复推理活动 worker / Provider-neutral recoverable inference-activity worker.

外部推理 I/O 只发生在 repository 事务之外。固定数量的 ``TaskGroup`` consumer 与
容量令牌共同限制“已领取但未终结”的活动数；取消不会清理数据库 claim，而是保留
租约供其他实例在到期后恢复。/ External inference I/O occurs only outside repository
transactions. Fixed ``TaskGroup`` consumers and capacity tokens bound claimed-but-unfinalized
activities. Cancellation deliberately leaves the database claim leased for recovery by another
instance after expiry.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol, cast

from fogmoe_bot.application.assistant.streaming import AssistantStreamSession
from fogmoe_bot.domain.assistant.messages import text_message
from fogmoe_bot.application.observability.telemetry import Telemetry
from fogmoe_bot.application.runtime import (
    AdaptivePollingPolicy,
    Jitter,
    LeaseRecoveryCadence,
    SystemUtcClock,
    UtcClock,
)
from fogmoe_bot.domain.conversation.errors import StaleClaimError
from fogmoe_bot.domain.conversation.identity import (
    ConversationMessageId,
    DeliveryStreamId,
    OutboundMessageId,
)
from fogmoe_bot.domain.conversation.inference import (
    InferenceActivityClaim,
    InferenceGenerationFence,
)
from fogmoe_bot.domain.conversation.message import (
    MessageDraft,
    MessageRole,
)
from fogmoe_bot.domain.conversation.outbox import (
    SEND_TELEGRAM_MESSAGE,
    OutboundDraft,
    OutboundKind,
)
from fogmoe_bot.domain.conversation.payloads import JsonObject, JsonValue
from fogmoe_bot.domain.conversation.workflow_results import (
    InferenceCompletionResult,
    InferenceFailureDeliveryResult,
)
from fogmoe_bot.domain.observability.conventions import EventName, MetricName, Outcome
from fogmoe_bot.domain.observability.signals import SpanKind, SpanStatus
from fogmoe_bot.domain.temporal import ensure_utc

logger = logging.getLogger(__name__)


class InferencePersistence(Protocol):
    """@brief 推理 worker 所需最小持久化端口 / Minimal persistence port for the inference worker."""

    async def claim_inference_activities(
        self,
        *,
        now: datetime,
        limit: int,
        lease_for: timedelta,
    ) -> Sequence[InferenceActivityClaim]:
        """@brief 原子领取可执行活动 / Atomically claim runnable activities.

        @param now 当前 UTC 时间 / Current UTC time.
        @param limit 最大领取数 / Maximum claim count.
        @param lease_for fencing 租约时长 / Fencing lease duration.
        @return 活动 claims / Activity claims.
        """

        ...

    async def complete_inference_activity(
        self,
        claim: InferenceActivityClaim,
        *,
        assistant_message: MessageDraft,
        outbounds: Sequence[OutboundDraft],
        completed_at: datetime,
    ) -> InferenceCompletionResult:
        """@brief 原子完成活动、历史与 outbox / Atomically complete the activity, history, and outbox.

        @param claim 当前 claim / Current claim.
        @param assistant_message 确定性助手消息 / Deterministic assistant message.
        @param outbounds 有序、确定性的出站意图 / Ordered deterministic outbound intents.
        @param completed_at 完成时间 / Completion time.
        @return 完成回执 / Completion receipt.
        """

        ...

    async def retry_inference_activity(
        self,
        claim: InferenceActivityClaim,
        *,
        failed_at: datetime,
        retry_at: datetime,
        error: str,
        retry_budget_used: int,
    ) -> None:
        """@brief 原子安排活动与 Turn 重试 / Atomically schedule activity and Turn retry.

        @param claim 当前 claim / Current claim.
        @param failed_at 失败时间 / Failure time.
        @param retry_at 下次领取时间 / Next claim time.
        @param error 错误摘要 / Error summary.
        @param retry_budget_used 本次决定后已使用的普通失败预算 /
            Ordinary failure budget used after this decision.
        @return None / None.
        """

        ...

    async def fail_inference_activity(
        self,
        claim: InferenceActivityClaim,
        *,
        assistant_message: MessageDraft,
        outbounds: Sequence[OutboundDraft],
        failed_at: datetime,
        error: str,
        retry_budget_used: int,
    ) -> InferenceFailureDeliveryResult:
        """@brief 原子终结活动并持久化安全失败上下文/outbox / Atomically fail activity and persist safe failure context/outbox.

        @param claim 当前 claim / Current claim.
        @param assistant_message 不含内部诊断的 canonical 失败消息 /
            Canonical failure message without internal diagnostics.
        @param outbounds 安全失败反馈出站 / Safe failure-feedback outbounds.
        @param failed_at 最终失败时间 / Final-failure time.
        @param error 错误摘要 / Error summary.
        @param retry_budget_used 本次决定后已使用的普通失败预算 /
            Ordinary failure budget used after this decision.
        @return 原子失败反馈回执 / Atomic failure-feedback receipt.
        @note 若该 durable request 含当前附件，持久化实现必须在同一事务内仅将其严格
            ``pending`` marker 终结为 ``unavailable``。已有 receipt 的 ``imported`` 行必须
            保持不变；可重试失败绝不能调用此方法。/ When the durable request carries a current
            attachment, the persistence implementation must terminalize only its strict
            ``pending`` marker to ``unavailable`` in the same transaction. An ``imported`` row
            with a receipt must remain unchanged; retryable failures must never call this method.
        """

        ...

    async def recover_expired_inference_leases(self, *, now: datetime) -> int:
        """@brief 回收崩溃或取消留下的过期租约 / Recover leases left by crashes or cancellation.

        @param now 当前 UTC 时间 / Current UTC time.
        @return 回收数量 / Number recovered.
        """

        ...


@dataclass(frozen=True, slots=True)
class InferenceOutboundIntent:
    """@brief 推理结果携带的类型化出站意图 / Typed outbound intent carried by an inference result.

    @param delivery_stream_id 外部有序投递流 / External ordered-delivery stream.
    @param kind 可扩展动作 kind / Extensible action kind.
    @param payload connector-neutral 结构载荷 / Connector-neutral structured payload.
    """

    delivery_stream_id: DeliveryStreamId
    kind: OutboundKind
    payload: JsonObject

    def __post_init__(self) -> None:
        """@brief 隔离可变 payload / Isolate the mutable payload.

        @return None / None.
        """

        object.__setattr__(self, "payload", dict(self.payload))


@dataclass(frozen=True, slots=True)
class InferenceResult:
    """@brief Provider-neutral 推理成功结果 / Provider-neutral successful inference result.

    @param assistant_content 结构化助手历史内容 / Structured assistant-history content.
    @param outbounds 一次发送的有序出站意图 / Ordered outbound intents for one delivery.
    """

    assistant_content: JsonObject
    outbounds: tuple[InferenceOutboundIntent, ...]

    def __post_init__(self) -> None:
        """@brief 隔离可变助手内容 / Isolate mutable assistant content.

        @return None / None.
        """

        object.__setattr__(self, "assistant_content", dict(self.assistant_content))
        if not self.outbounds:
            raise ValueError("Inference results require at least one outbound intent")
        first_stream = self.outbounds[0].delivery_stream_id
        if any(intent.delivery_stream_id != first_stream for intent in self.outbounds):
            raise ValueError(
                "Inference delivery intents must share one delivery stream"
            )


@dataclass(frozen=True, slots=True)
class _FailureDeliveryTarget:
    """@brief 从 durable request 投影出的最小失败投递目标 / Minimal failure-delivery target projected from a durable request.

    @param delivery_stream_id 外部有序投递流 / External ordered-delivery stream.
    @param chat_id Telegram chat ID 或频道 username / Telegram chat ID or channel username.
    @param task_kind 进入 canonical 历史的任务类别 / Task kind stored in canonical history.
    @param reply_to_message_id 可选回复目标 / Optional reply target.
    @param message_thread_id 可选 Topic ID / Optional topic identifier.
    @param disable_notification 是否静默投递 / Whether delivery is silent.
    @param protect_content 是否保护消息 / Whether content is protected.
    @param disable_web_page_preview 是否禁用链接预览 / Whether link previews are disabled.
    """

    delivery_stream_id: DeliveryStreamId
    chat_id: int | str
    task_kind: str
    reply_to_message_id: int | None
    message_thread_id: int | None
    disable_notification: bool
    protect_content: bool
    disable_web_page_preview: bool

    @classmethod
    def from_request(cls, request: JsonObject) -> "_FailureDeliveryTarget":
        """@brief 对失败投递所需字段做 fail-closed 解析 / Fail-closed parse of fields required for failure delivery.

        @param request acceptance 持久化的 durable request / Durable request persisted by acceptance.
        @return 已验证的最小投递目标 / Validated minimal delivery target.
        @raise ValueError durable request 无法安全定位用户时抛出 /
            Raised when the durable request cannot safely locate the user.
        """

        stream = request.get("delivery_stream_id")
        if not isinstance(stream, str) or not stream.strip():
            raise ValueError("durable inference request has no delivery_stream_id")
        raw_chat_id = request.get("chat_id")
        if isinstance(raw_chat_id, bool) or not isinstance(raw_chat_id, int | str):
            raise ValueError("durable inference request has an invalid chat_id")
        chat_id: int | str
        if isinstance(raw_chat_id, int):
            if raw_chat_id == 0:
                raise ValueError("durable inference request chat_id cannot be zero")
            chat_id = raw_chat_id
        else:
            chat_id = raw_chat_id.strip()
            if not chat_id:
                raise ValueError("durable inference request chat_id cannot be blank")
        task_kind = request.get("task_kind", "assistant")
        if task_kind not in {"assistant", "translation"}:
            raise ValueError("durable inference request has an invalid task_kind")
        return cls(
            delivery_stream_id=DeliveryStreamId(stream),
            chat_id=chat_id,
            task_kind=task_kind,
            reply_to_message_id=_optional_positive_int(
                request,
                "reply_to_message_id",
            ),
            message_thread_id=_optional_positive_int(
                request,
                "message_thread_id",
            ),
            disable_notification=_boolean_request_field(
                request,
                "disable_notification",
                default=False,
            ),
            protect_content=_boolean_request_field(
                request,
                "protect_content",
                default=False,
            ),
            disable_web_page_preview=_boolean_request_field(
                request,
                "disable_web_page_preview",
                default=True,
            ),
        )


def _optional_positive_int(request: JsonObject, field_name: str) -> int | None:
    """@brief 读取可选正整数 request 字段 / Read an optional positive-integer request field.

    @param request durable request / Durable request.
    @param field_name 字段名 / Field name.
    @return 正整数或 None / Positive integer or None.
    @raise ValueError 字段类型或范围非法时抛出 / Raised for an invalid type or range.
    """

    value = request.get(field_name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"durable inference request has an invalid {field_name}")
    return value


def _boolean_request_field(
    request: JsonObject,
    field_name: str,
    *,
    default: bool,
) -> bool:
    """@brief 读取严格布尔 request 字段 / Read a strict boolean request field.

    @param request durable request / Durable request.
    @param field_name 字段名 / Field name.
    @param default 字段缺省值 / Default for an omitted field.
    @return 已验证布尔值 / Validated boolean.
    @raise ValueError 字段不是布尔值时抛出 / Raised when the field is not boolean.
    """

    value = request.get(field_name, default)
    if not isinstance(value, bool):
        raise ValueError(f"durable inference request has an invalid {field_name}")
    return value


def _safe_failure_code(error: Exception) -> str:
    """@brief 将内部异常降格为可公开稳定错误码 / Reduce an internal exception to a safe stable public code.

    @param error 推理异常 / Inference exception.
    @return 不含内部诊断的错误码 / Error code containing no internal diagnostics.
    """

    return (
        error.category.value
        if isinstance(error, InferenceError)
        else InferenceErrorCategory.INTERNAL.value
    )


def _build_failure_effects(
    claim: InferenceActivityClaim,
    *,
    failed_at: datetime,
    error: Exception,
) -> tuple[MessageDraft, tuple[OutboundDraft, ...]]:
    """@brief 构造确定性、安全且可进入后续上下文的失败副作用 / Build deterministic, safe failure effects that enter later context.

    @param claim 当前 processing claim / Current processing claim.
    @param failed_at 最终失败时刻 / Final-failure instant.
    @param error 原始内部异常；只用于稳定分类 / Original internal error, used only for stable classification.
    @return canonical Assistant 消息与单条 Telegram outbox /
        Canonical Assistant message and one Telegram outbox.
    @note 原始异常文本、provider 响应、路径和 token 永远不会进入用户可见载荷或模型历史。/
        Raw exception text, provider responses, paths, and tokens never enter the user-visible
        payload or model history.
    """

    activity = claim.activity
    target = _FailureDeliveryTarget.from_request(activity.request)
    code = _safe_failure_code(error)
    text = f"这次处理没有完成（错误码：{code}）。你可以继续发送补充信息，或稍后重试。"
    history_message = text_message(MessageRole.ASSISTANT, text)
    assistant_content: JsonObject = {
        "schema_version": 2,
        "history_format": "canonical-v2",
        "task_kind": target.task_kind,
        "text": text,
        "history_messages": cast(list[JsonValue], [history_message.to_json()]),
        "runtime_events": [],
        "failure": cast(JsonValue, {"code": code}),
    }
    if target.task_kind == "translation":
        assistant_content["exclude_from_assistant"] = True
    assistant_message = MessageDraft(
        message_id=ConversationMessageId.for_turn(
            activity.turn_id,
            "assistant.failure",
        ),
        conversation_id=activity.conversation_id,
        turn_id=activity.turn_id,
        source_update_id=None,
        role=MessageRole.ASSISTANT,
        content=assistant_content,
        idempotency_key=f"turn:{activity.turn_id}:assistant:failure",
        created_at=failed_at,
    )
    outbound_payload: JsonObject = {
        "chat_id": cast(JsonValue, target.chat_id),
        "text": text,
        "disable_notification": target.disable_notification,
        "protect_content": target.protect_content,
        "disable_web_page_preview": target.disable_web_page_preview,
    }
    if target.reply_to_message_id is not None:
        outbound_payload["reply_to_message_id"] = target.reply_to_message_id
    if target.message_thread_id is not None:
        outbound_payload["message_thread_id"] = target.message_thread_id
    outbound = OutboundDraft(
        message_id=OutboundMessageId.for_turn(
            activity.turn_id,
            "failure.outbound.0",
        ),
        conversation_id=activity.conversation_id,
        turn_id=activity.turn_id,
        delivery_stream_id=target.delivery_stream_id,
        kind=SEND_TELEGRAM_MESSAGE,
        payload=outbound_payload,
        idempotency_key=f"turn:{activity.turn_id}:failure:outbound:0",
        created_at=failed_at,
        trace_context=activity.draft.trace_context,
    )
    return assistant_message, (outbound,)


class InferencePort(Protocol):
    """@brief 单次 provider-neutral 推理端口 / Port for one provider-neutral inference attempt."""

    async def infer(
        self,
        request: JsonObject,
        *,
        execution_deadline_monotonic: float | None = None,
        generation_fence: InferenceGenerationFence | None = None,
        stream: AssistantStreamSession | None = None,
    ) -> InferenceResult:
        """@brief 执行一次外部推理尝试 / Perform one external inference attempt.

        @param request durable provider-neutral 请求 / Durable provider-neutral request.
        @param execution_deadline_monotonic worker 建立的 attempt 单调截止点；直接调用时可为 None /
            Attempt monotonic deadline established by the worker; may be None for direct calls.
        @param generation_fence processing claim 的 attempt/revision/token 身份；直接纯函数测试可为 None /
            Attempt/revision/token identity of the processing claim; may be None for direct pure tests.
        @param stream 由 durable worker 拥有终态的易失流会话 /
            Ephemeral stream session whose terminal state is owned by the durable worker.
        @return 类型化推理结果 / Typed inference result.
        @note 实现不得吞掉 CancelledError，也不得自行写 conversation 表。/
        Implementations must not swallow CancelledError or write conversation tables themselves.
            截止点仅用于在发送不可逆 external effect 前做 budget admission，绝不能成为
            持久化 request 的一部分。/ The deadline is used only for budget admission before
            an irreversible external effect is sent; it must never become part of the persisted
            request.
        """

        ...


class InferenceStreamStarter(Protocol):
    """@brief 在慢依赖前建立易失推理流 / Start an ephemeral inference stream before slow dependencies."""

    async def start_stream(
        self,
        request: JsonObject,
        *,
        generation_fence: InferenceGenerationFence | None = None,
    ) -> AssistantStreamSession | None:
        """@brief 为当前 durable generation 建立流会话 / Start a stream session for the current durable generation.

        @param request durable provider-neutral 请求 / Durable provider-neutral request.
        @param generation_fence 当前 processing generation 身份 / Current processing-generation identity.
        @return 已投影首帧的会话；未配置时为 None /
            Session whose first frame has been projected, or None when streaming is disabled.
        @note 本端口只建立会话；COMPLETED、FAILED、SUSPENDED 必须由 worker 在对应
            repository 事务成功后决定。/ This port only starts a session. COMPLETED, FAILED,
            and SUSPENDED are decided by the worker after the corresponding repository
            transaction succeeds.
        """

        ...


class InferenceErrorCategory(StrEnum):
    """@brief 可持久化推理错误分类 / Persistable inference-error category."""

    RATE_LIMIT = "rate_limit"
    NETWORK = "network"
    TIMEOUT = "timeout"
    INVALID_REQUEST = "invalid_request"
    INVALID_OUTPUT = "invalid_output"
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    CONFIGURATION = "configuration"
    CONTEXT_WINDOW = "context_window"
    SAFETY = "safety"
    PARTIAL_EFFECT = "partial_effect"
    INTERNAL = "internal"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER = "provider"


class InferenceError(RuntimeError):
    """@brief 已分类推理错误 / Classified inference error.

    @param message 错误详情 / Error detail.
    @param category 稳定错误分类 / Stable error category.
    """

    category: InferenceErrorCategory

    def __init__(
        self,
        message: str,
        *,
        category: InferenceErrorCategory,
    ) -> None:
        """@brief 创建分类错误 / Create a classified error.

        @param message 错误详情 / Error detail.
        @param category 稳定错误分类 / Stable error category.
        """

        super().__init__(message)
        self.category = category


class RetryableInferenceError(InferenceError):
    """@brief 可在预算内重试的推理错误 / Inference error retryable within budget.

    @param retry_after Provider 指定的最小等待 / Provider-specified minimum delay.
    """

    retry_after: timedelta | None

    def __init__(
        self,
        message: str,
        *,
        category: InferenceErrorCategory,
        retry_after: timedelta | None = None,
    ) -> None:
        """@brief 创建可重试错误 / Create a retryable error.

        @param message 错误详情 / Error detail.
        @param category 稳定错误分类 / Stable error category.
        @param retry_after Provider 最小等待 / Provider minimum delay.
        @raise ValueError retry_after 非正时抛出 / Raised for a non-positive retry_after.
        """

        if retry_after is not None and retry_after <= timedelta():
            raise ValueError("retry_after must be positive")
        super().__init__(message, category=category)
        self.retry_after = retry_after


class InferenceDependencyPending(RetryableInferenceError):
    """@brief 等待另一个 durable activity 的非计费重试信号 / Retry signal that waits for another durable activity without consuming the provider-attempt budget.

    @param retry_after 再检查 dependency 的等待 / Delay before checking the dependency again.
    @note dependency 自身必须拥有 retry/fallback/final 状态机；一旦进入终态，下一次
    inference projection 会成功或返回 permanent error。/ The dependency must own its own
    retry/fallback/final state machine; once terminal, the next inference projection either
    succeeds or returns a permanent error.
    """

    def __init__(self, message: str, *, retry_after: timedelta) -> None:
        """@brief 创建 durable dependency gate / Create a durable-dependency gate.

        @param message dependency detail / Dependency detail.
        @param retry_after 正等待 / Positive wait.
        """

        super().__init__(
            message,
            category=InferenceErrorCategory.CONTEXT_WINDOW,
            retry_after=retry_after,
        )


class InferenceAttemptTimeout(RetryableInferenceError):
    """@brief Worker 强制终止的推理超时 / Inference timeout enforced by the worker."""

    def __init__(self, message: str) -> None:
        """@brief 创建超时错误 / Create a timeout error.

        @param message 超时详情 / Timeout detail.
        """

        super().__init__(message, category=InferenceErrorCategory.TIMEOUT)


class PermanentInferenceError(InferenceError):
    """@brief 不应自动重试的推理错误 / Inference error that must not be retried automatically."""


class InferenceOutputError(PermanentInferenceError):
    """@brief Provider 返回不合法结构 / Provider returned an invalid structured result."""

    def __init__(self, message: str) -> None:
        """@brief 创建输出错误 / Create an output error.

        @param message 错误详情 / Error detail.
        """

        super().__init__(message, category=InferenceErrorCategory.INVALID_OUTPUT)


@dataclass(frozen=True, slots=True)
class RetryInferenceAt:
    """@brief 在指定时刻重试推理 / Retry inference at a specified time.

    @param at 下次可领取时间 / Next claimable time.
    """

    at: datetime


@dataclass(frozen=True, slots=True)
class FailInferenceFinal:
    """@brief 将推理活动标记永久失败 / Mark an inference activity finally failed."""


type InferenceFailureDecision = RetryInferenceAt | FailInferenceFinal
"""@brief 推理失败的穷尽策略决定 / Exhaustive inference-failure decision."""


@dataclass(frozen=True, slots=True)
class FullJitterInferenceRetryPolicy:
    """@brief 指数退避、Full Jitter 与 Retry-After 策略 / Exponential-backoff, Full-Jitter, and Retry-After policy.

    @param max_attempts 包含首次 claim 的最大尝试数 / Maximum attempts including the first claim.
    @param initial_delay 第一次重试指数上限 / Exponential cap for the first retry.
    @param max_delay 最大指数上限 / Maximum exponential cap.
    @param retry_after_jitter Provider 延迟后的最大附加抖动 / Maximum jitter added after provider delay.
    @param jitter 可注入随机源 / Injectable random source.
    """

    max_attempts: int = 8
    initial_delay: timedelta = timedelta(seconds=1)
    max_delay: timedelta = timedelta(minutes=5)
    retry_after_jitter: timedelta = timedelta(seconds=1)
    jitter: Jitter = random.uniform

    def __post_init__(self) -> None:
        """@brief 校验策略参数 / Validate policy parameters.

        @return None / None.
        @raise ValueError 次数或延迟非法时抛出 / Raised for invalid attempts or delays.
        """

        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if self.initial_delay <= timedelta():
            raise ValueError("initial_delay must be positive")
        if self.max_delay < self.initial_delay:
            raise ValueError("max_delay cannot be smaller than initial_delay")
        if self.retry_after_jitter < timedelta():
            raise ValueError("retry_after_jitter cannot be negative")

    def decide(
        self,
        *,
        retry_budget_used: int,
        failed_at: datetime,
        error: Exception,
    ) -> InferenceFailureDecision:
        """@brief 决定重试时间或永久失败 / Decide a retry time or final failure.

        @param retry_budget_used 包含当前普通失败、排除 dependency wait 的 durable 预算计数 /
            Durable budget count including the current ordinary failure and excluding dependency waits.
        @param failed_at 本次失败时间 / Failure time.
        @param error 推理异常 / Inference exception.
        @return 重试或永久失败决定 / Retry or final-failure decision.
        """

        failure_time = ensure_utc(failed_at)
        if retry_budget_used < 0:
            raise ValueError("retry_budget_used cannot be negative")
        if isinstance(error, PermanentInferenceError | ValueError | TypeError):
            return FailInferenceFinal()
        if isinstance(error, InferenceDependencyPending):
            retry_after = error.retry_after
            if retry_after is None:  # pragma: no cover - constructor requires it.
                raise RuntimeError("Inference dependency gate lost its retry delay")
            provider_seconds = retry_after.total_seconds()
            jitter_cap = min(
                self.retry_after_jitter.total_seconds(),
                provider_seconds * 0.1,
            )
            return RetryInferenceAt(
                failure_time
                + retry_after
                + timedelta(seconds=self._sample(0.0, jitter_cap))
            )
        if retry_budget_used >= self.max_attempts:
            return FailInferenceFinal()
        if isinstance(error, RetryableInferenceError) and error.retry_after is not None:
            provider_seconds = error.retry_after.total_seconds()
            jitter_cap = min(
                self.retry_after_jitter.total_seconds(),
                provider_seconds * 0.1,
            )
            return RetryInferenceAt(
                failure_time
                + error.retry_after
                + timedelta(seconds=self._sample(0.0, jitter_cap))
            )
        exponent = max(0, retry_budget_used - 1)
        cap_seconds = min(
            self.max_delay.total_seconds(),
            self.initial_delay.total_seconds() * (2**exponent),
        )
        delay = timedelta(seconds=self._sample(0.0, cap_seconds))
        return RetryInferenceAt(
            failure_time + (delay if delay > timedelta() else timedelta.resolution)
        )

    def _sample(self, lower: float, upper: float) -> float:
        """@brief 验证并返回 jitter 样本 / Validate and return a jitter sample.

        @param lower 闭区间下界 / Inclusive lower bound.
        @param upper 闭区间上界 / Inclusive upper bound.
        @return 合法样本秒数 / Valid sample in seconds.
        @raise ValueError 随机源越界或非有限时抛出 / Raised for out-of-range or non-finite samples.
        """

        value: float = self.jitter(lower, upper)
        if not math.isfinite(value) or not lower <= value <= upper:
            raise ValueError("jitter returned a value outside its requested interval")
        return value


@dataclass(frozen=True, slots=True)
class _ClaimWork:
    """@brief consumer queue 中的 claim / Claim in the consumer queue.

    @param claim 待推理 claim / Claim to infer.
    """

    claim: InferenceActivityClaim


@dataclass(frozen=True, slots=True)
class _StopConsumer:
    """@brief consumer 正常 drain 后的停止哨兵 / Stop sentinel after normal consumer drain."""


type _WorkItem = _ClaimWork | _StopConsumer
"""@brief 推理 consumer 工作项 / Inference-consumer work item."""


@dataclass(frozen=True, slots=True)
class InferenceRuntimeLimits:
    """@brief 推理各层严格递增的超时预算 / Strictly increasing timeout budgets for inference layers.

    @param provider_timeout 单次 provider 请求预算 / Per-provider request budget.
    @param attempt_timeout worker 整体推理尝试预算 / Whole worker-attempt budget.
    @param lease_for 数据库 claim 租约预算 / Database-claim lease budget.
    @note 必须满足 ``provider_timeout < attempt_timeout < lease_for``，从而先由最内层
    provider 收敛，再由 worker 取消整次尝试，最后才允许另一实例回收 lease。/
    ``provider_timeout < attempt_timeout < lease_for`` is required so the innermost provider
    converges first, the worker cancels the whole attempt second, and only then may another
    instance recover the lease.
    """

    provider_timeout: timedelta
    attempt_timeout: timedelta
    lease_for: timedelta

    def __post_init__(self) -> None:
        """@brief 校验超时层级不变量 / Validate the timeout-layer invariant.

        @return None / None.
        @raise ValueError 任一预算非正或顺序不安全时抛出 / Raised when a budget is non-positive or unsafely ordered.
        """

        if self.provider_timeout <= timedelta():
            raise ValueError("provider_timeout must be positive")
        if self.attempt_timeout <= timedelta():
            raise ValueError("attempt_timeout must be positive")
        if self.lease_for <= timedelta():
            raise ValueError("lease_for must be positive")
        if self.provider_timeout >= self.attempt_timeout:
            raise ValueError("provider_timeout must be shorter than attempt_timeout")
        if self.attempt_timeout >= self.lease_for:
            raise ValueError("attempt_timeout must be shorter than lease_for")


class InferenceWorker:
    """@brief 有界领取、执行并终结推理活动 / Bounded worker claiming, executing, and finalizing inference activities."""

    def __init__(
        self,
        *,
        repository: InferencePersistence,
        inference: InferencePort,
        streaming: InferenceStreamStarter | None = None,
        worker_count: int,
        polling_policy: AdaptivePollingPolicy,
        runtime_limits: InferenceRuntimeLimits,
        retry_policy: FullJitterInferenceRetryPolicy | None = None,
        clock: UtcClock | None = None,
        telemetry: Telemetry,
    ) -> None:
        """@brief 创建推理 worker / Create an inference worker.

        @param repository 活动持久化端口 / Activity persistence port.
        @param inference 外部推理端口 / External inference port.
        @param streaming 可选 generation 流启动端口 / Optional generation-stream starter.
        @param worker_count 已领取未终结活动上限 / Maximum claimed-but-unfinalized activities.
        @param polling_policy 自适应空闲轮询策略 / Adaptive idle-polling policy.
        @param runtime_limits provider、attempt 与 lease 的统一预算 / Shared provider, attempt, and lease budgets.
        @param retry_policy 失败策略 / Failure policy.
        @param clock 可替换 UTC 时钟 / Replaceable UTC clock.
        @param telemetry 进程 typed telemetry / Process typed telemetry.
        @return None / None.
        @raise ValueError 容量或时间参数非法时抛出 / Raised for invalid capacity or timing parameters.
        """

        if worker_count < 1:
            raise ValueError("worker_count must be at least one")
        self._repository = repository
        self._inference = inference
        self._streaming = streaming
        self._worker_count = worker_count
        self._polling_policy = polling_policy
        self._lease_for = runtime_limits.lease_for
        self._attempt_timeout = runtime_limits.attempt_timeout
        self._retry_policy = retry_policy or FullJitterInferenceRetryPolicy()
        self._clock = clock or SystemUtcClock()
        self._telemetry = telemetry

    async def run(self, stop_event: asyncio.Event) -> None:
        """@brief 运行 producer 与固定 consumers / Run one producer and fixed consumers.

        @param stop_event 置位后停止领取并 drain / Stops claiming and drains when set.
        @return None / None.
        @note 正常 shutdown 会 drain；Task 取消立即传播并保留 processing lease。/
        Normal shutdown drains; task cancellation propagates and leaves the processing lease.
        """

        work_queue: asyncio.Queue[_WorkItem] = asyncio.Queue(maxsize=self._worker_count)
        capacity: asyncio.Queue[None] = asyncio.Queue(maxsize=self._worker_count)
        for _ in range(self._worker_count):
            capacity.put_nowait(None)
        async with asyncio.TaskGroup() as task_group:
            for index in range(self._worker_count):
                task_group.create_task(
                    self._consume(work_queue, capacity),
                    name=f"inference-consumer-{index}",
                )
            try:
                await self._produce(work_queue, capacity, stop_event)
            except asyncio.CancelledError:
                stop_event.set()
                raise
            else:
                await work_queue.join()
                for _ in range(self._worker_count):
                    await work_queue.put(_StopConsumer())

    async def process_claim(self, claim: InferenceActivityClaim) -> None:
        """@brief 在事务外推理并以 fencing token 终结 / Infer outside a transaction and finalize with a fencing token.

        @param claim 当前活动 claim / Current activity claim.
        @return None / None.
        @note CancelledError 不被捕获；claim 保持 processing 直至租约恢复。/
        CancelledError is not caught; the claim remains processing until lease recovery.
        """

        activity = claim.activity
        stream: AssistantStreamSession | None = None
        """@brief 本次 claim 的易失投影会话 / Ephemeral projection session for this claim."""
        with self._telemetry.span(
            "inference.attempt",
            kind=SpanKind.CONSUMER,
            parent=activity.draft.trace_context,
            attributes={
                "fogmoe.turn.id": str(activity.turn_id),
                "fogmoe.activity.id": str(activity.activity_id),
                "fogmoe.inference.attempt": activity.attempt_count,
            },
        ) as span:
            try:
                loop = asyncio.get_running_loop()
                execution_deadline_monotonic = (
                    loop.time() + self._attempt_timeout.total_seconds()
                )
                async with asyncio.timeout_at(execution_deadline_monotonic):
                    request = dict(activity.request)
                    if self._streaming is not None:
                        stream = await self._streaming.start_stream(
                            request,
                            generation_fence=claim.generation_fence,
                        )
                    if stream is None:
                        result = await self._inference.infer(
                            request,
                            execution_deadline_monotonic=execution_deadline_monotonic,
                            generation_fence=claim.generation_fence,
                        )
                    else:
                        result = await self._inference.infer(
                            request,
                            execution_deadline_monotonic=execution_deadline_monotonic,
                            generation_fence=claim.generation_fence,
                            stream=stream,
                        )
            except asyncio.CancelledError:
                await self._suspend_stream(stream)
                raise
            except StaleClaimError:
                logger.info(
                    "Inference generation was superseded during execution: activity_id=%s",
                    activity.activity_id,
                )
                await self._suspend_stream(stream)
                return
            except TimeoutError:
                error = InferenceAttemptTimeout(
                    f"inference attempt exceeded {self._attempt_timeout.total_seconds():g}s"
                )
                span.set_status(SpanStatus.ERROR, str(error))
                span.set_attribute("error.type", error.__class__.__name__)
                self._telemetry.counter(
                    MetricName.INFERENCE_OUTCOMES,
                    attributes={"outcome": Outcome.TIMEOUT},
                )
                await self._finalize_failure(claim, error, stream)
                return
            except Exception as error:
                span.set_status(SpanStatus.ERROR, str(error))
                span.set_attribute("error.type", error.__class__.__name__)
                self._telemetry.counter(
                    MetricName.INFERENCE_OUTCOMES,
                    attributes={"outcome": Outcome.FAILURE},
                )
                await self._finalize_failure(claim, error, stream)
                return

            completed_at = self._clock.now()
            assistant_message = MessageDraft(
                message_id=ConversationMessageId.for_turn(
                    activity.turn_id,
                    "assistant.final",
                ),
                conversation_id=activity.conversation_id,
                turn_id=activity.turn_id,
                source_update_id=None,
                role=MessageRole.ASSISTANT,
                content=result.assistant_content,
                idempotency_key=f"turn:{activity.turn_id}:assistant:final",
                created_at=completed_at,
            )
            outbounds = tuple(
                OutboundDraft(
                    message_id=OutboundMessageId.for_turn(
                        activity.turn_id,
                        f"outbound.{ordinal}",
                    ),
                    conversation_id=activity.conversation_id,
                    turn_id=activity.turn_id,
                    delivery_stream_id=intent.delivery_stream_id,
                    kind=intent.kind,
                    payload=intent.payload,
                    idempotency_key=(f"turn:{activity.turn_id}:outbound:{ordinal}"),
                    created_at=completed_at,
                    trace_context=span.context,
                )
                for ordinal, intent in enumerate(result.outbounds)
            )
            try:
                await self._repository.complete_inference_activity(
                    claim,
                    assistant_message=assistant_message,
                    outbounds=outbounds,
                    completed_at=completed_at,
                )
            except BaseException:
                await self._suspend_stream(stream)
                raise
            if stream is not None:
                await stream.complete(emitted_at=self._clock.now())
            self._telemetry.counter(
                MetricName.INFERENCE_OUTCOMES,
                attributes={"outcome": Outcome.SUCCESS},
            )

    async def _produce(
        self,
        work_queue: asyncio.Queue[_WorkItem],
        capacity: asyncio.Queue[None],
        stop_event: asyncio.Event,
    ) -> None:
        """@brief 按容量回收租约并领取活动 / Recover leases and claim activities up to capacity.

        @param work_queue 有界 claim 队列 / Bounded claim queue.
        @param capacity 空闲容量令牌 / Free-capacity tokens.
        @param stop_event 停止信号 / Stop signal.
        @return None / None.
        """

        polling = self._polling_policy.start()
        recovery = LeaseRecoveryCadence.for_lease(self._lease_for)
        while not stop_event.is_set():
            if recovery.take_due():
                await self._recover_expired_leases(self._clock.now())
            tokens = self._take_available(capacity)
            if tokens:
                now = self._clock.now()
                try:
                    claims = tuple(
                        await self._repository.claim_inference_activities(
                            now=now,
                            limit=len(tokens),
                            lease_for=self._lease_for,
                        )
                    )
                    if len(claims) > len(tokens):
                        raise RuntimeError(
                            "Inference repository returned more claims than requested"
                        )
                except Exception:
                    self._return_capacity(capacity, tokens)
                    logger.exception("Inference producer failed to claim activities")
                    await polling.wait(stop_event)
                    continue
                else:
                    for claim in claims:
                        await work_queue.put(_ClaimWork(claim))
                    self._return_capacity(capacity, tokens[len(claims) :])
                    if claims:
                        polling.reset()
                        continue
            await polling.wait(stop_event)

    async def _recover_expired_leases(self, now: datetime) -> None:
        """@brief 低频回收到期 inference leases / Recover expired inference leases at a low cadence.

        @param now 当前 UTC 时刻 / Current UTC instant.
        @return None；恢复查询失败不会阻断正常 claim / None; a failed recovery query does not block normal claims.
        """

        try:
            recovered = await self._repository.recover_expired_inference_leases(now=now)
            if not recovered:
                return
            self._telemetry.counter(
                MetricName.LEASE_RECOVERIES,
                float(recovered),
                attributes={"pipeline.stage": "inference"},
            )
            logger.warning(
                "Recovered expired inference leases: count=%s",
                recovered,
                extra={
                    "event_name": EventName.INFERENCE_LEASE_RECOVERED,
                    "telemetry_attributes": {"pipeline.stage": "inference"},
                },
            )
        except Exception:
            logger.exception("Inference lease recovery failed; claim polling continues")

    async def _consume(
        self,
        work_queue: asyncio.Queue[_WorkItem],
        capacity: asyncio.Queue[None],
    ) -> None:
        """@brief 消费 claims 并归还容量 / Consume claims and return capacity.

        @param work_queue 有界 claim 队列 / Bounded claim queue.
        @param capacity 容量令牌队列 / Capacity-token queue.
        @return None / None.
        """

        while True:
            work = await work_queue.get()
            try:
                if isinstance(work, _StopConsumer):
                    return
                try:
                    await self.process_claim(work.claim)
                except StaleClaimError:
                    logger.info(
                        "Inference claim was superseded before finalization: "
                        "activity_id=%s",
                        work.claim.activity.activity_id,
                        extra={
                            "event_name": "inference.claim.superseded",
                            "telemetry_attributes": {"pipeline.stage": "inference"},
                        },
                    )
                except Exception:
                    logger.exception(
                        "Inference claim could not be finalized: activity_id=%s",
                        work.claim.activity.activity_id,
                    )
                finally:
                    capacity.put_nowait(None)
            finally:
                work_queue.task_done()

    async def _finalize_failure(
        self,
        claim: InferenceActivityClaim,
        error: Exception,
        stream: AssistantStreamSession | None,
    ) -> None:
        """@brief 先提交 durable 失败决定，再投影对应流终态 / Commit the durable failure decision before projecting its stream terminal.

        @param claim 失败 claim / Failed claim.
        @param error 推理异常 / Inference exception.
        @param stream 可选易失流会话 / Optional ephemeral stream session.
        @return None / None.
        @note repository 不可用时只能 SUSPEND；绝不能向用户预告尚未提交的最终失败。/
            A repository failure may only SUSPEND the preview; it must never announce a final
            failure that was not durably committed.
        """

        try:
            failed_at = self._clock.now()
            retry_budget_used = claim.activity.retry_budget_used + (
                0 if isinstance(error, InferenceDependencyPending) else 1
            )
            """@brief dependency gate 不消耗普通失败预算 / Dependency gates do not consume the ordinary failure budget."""
            decision = self._retry_policy.decide(
                retry_budget_used=retry_budget_used,
                failed_at=failed_at,
                error=error,
            )
            error_text = self._error_text(error)
            if isinstance(decision, RetryInferenceAt):
                await self._repository.retry_inference_activity(
                    claim,
                    failed_at=failed_at,
                    retry_at=decision.at,
                    error=error_text,
                    retry_budget_used=retry_budget_used,
                )
                self._telemetry.counter(
                    MetricName.INFERENCE_OUTCOMES,
                    attributes={"outcome": Outcome.RETRY},
                )
                await self._suspend_stream(stream)
                return
            assistant_message, outbounds = _build_failure_effects(
                claim,
                failed_at=failed_at,
                error=error,
            )
            await self._repository.fail_inference_activity(
                claim,
                assistant_message=assistant_message,
                outbounds=outbounds,
                failed_at=failed_at,
                error=error_text,
                retry_budget_used=retry_budget_used,
            )
            self._telemetry.counter(
                MetricName.INFERENCE_OUTCOMES,
                attributes={"outcome": Outcome.DROPPED},
            )
            if stream is not None:
                await stream.fail(
                    _safe_failure_code(error),
                    emitted_at=self._clock.now(),
                )
        except BaseException:
            await self._suspend_stream(stream)
            raise

    async def _suspend_stream(
        self,
        stream: AssistantStreamSession | None,
    ) -> None:
        """@brief 最佳努力停止未提交终态的 typing/draft / Best-effort stop typing/drafts without claiming a durable terminal.

        @param stream 可选易失流会话 / Optional ephemeral stream session.
        @return None / None.
        """

        if stream is not None:
            await stream.suspend(emitted_at=self._clock.now())

    @staticmethod
    def _error_text(error: Exception) -> str:
        """@brief 构造有界可观测错误文本 / Build bounded observable error text.

        @param error 推理异常 / Inference exception.
        @return 最多 2000 字符摘要 / Error summary of at most 2,000 characters.
        """

        detail = str(error).strip() or error.__class__.__name__
        if not isinstance(error, InferenceError):
            return f"{error.__class__.__name__}: {detail}"[:2000]
        attributes = [f"category={error.category.value}"]
        if isinstance(error, RetryableInferenceError) and error.retry_after is not None:
            attributes.append(
                f"retry_after_seconds={error.retry_after.total_seconds():g}"
            )
        return (f"{error.__class__.__name__}[{','.join(attributes)}]: {detail}")[:2000]

    @staticmethod
    def _take_available(capacity: asyncio.Queue[None]) -> list[None]:
        """@brief 非阻塞取出全部空闲容量 / Non-blockingly take all free capacity.

        @param capacity 容量令牌队列 / Capacity-token queue.
        @return 本轮可用令牌 / Available tokens for this poll.
        """

        tokens: list[None] = []
        while True:
            try:
                tokens.append(capacity.get_nowait())
            except asyncio.QueueEmpty:
                return tokens

    @staticmethod
    def _return_capacity(
        capacity: asyncio.Queue[None],
        tokens: Sequence[None],
    ) -> None:
        """@brief 归还未使用容量 / Return unused capacity.

        @param capacity 容量令牌队列 / Capacity-token queue.
        @param tokens 待归还令牌 / Tokens to return.
        @return None / None.
        """

        for token in tokens:
            capacity.put_nowait(token)


__all__ = [
    "FailInferenceFinal",
    "FullJitterInferenceRetryPolicy",
    "InferenceAttemptTimeout",
    "InferenceError",
    "InferenceErrorCategory",
    "InferenceDependencyPending",
    "InferenceFailureDecision",
    "InferenceOutboundIntent",
    "InferenceOutputError",
    "InferencePersistence",
    "InferencePort",
    "InferenceResult",
    "InferenceRuntimeLimits",
    "InferenceStreamStarter",
    "InferenceWorker",
    "PermanentInferenceError",
    "RetryInferenceAt",
    "RetryableInferenceError",
]
