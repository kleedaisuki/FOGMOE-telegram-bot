"""@brief Assistant 错误语义 / Assistant error semantics."""

from datetime import timedelta
from enum import StrEnum

from .tool_runtime import RuntimeEvent


class ProviderFailureKind(StrEnum):
    """@brief Completion provider 失败的稳定类别 / Stable completion-provider failure categories.

    这是应用层 completion port 的错误契约，而不是某个 HTTP SDK 的私有异常。基础设施
    adapter 必须把自身错误投影为该集合，使 durable worker 无需依赖 infrastructure
    类型即可作出重试或终结决定。/
    This is the application completion-port error contract, not a private exception of an HTTP
    SDK. Infrastructure adapters project their failures into this set so the durable worker can
    decide retry or finalization without depending on infrastructure types.
    """

    CONTRACT = "contract"
    """@brief 请求或响应违反 completion contract / Request or response violates the completion contract."""

    TRANSPORT = "transport"
    """@brief DNS、连接或 TLS 传输失败 / DNS, connection, or TLS transport failure."""

    TIMEOUT = "timeout"
    """@brief 单次 completion 超过 deadline / One completion exceeded its deadline."""

    RATE_LIMITED = "rate_limited"
    """@brief Provider 明确限流 / Provider explicitly rate limited the request."""

    SERVER = "server"
    """@brief Provider 5xx 或暂态服务失败 / Provider 5xx or transient service failure."""

    REJECTED = "rejected"
    """@brief Provider 接受连接但拒绝请求 / Provider accepted the connection but rejected the request."""


class ProviderFailure(RuntimeError):
    """@brief 带 HTTP 与退避语义的 completion-port 异常 / Completion-port exception carrying HTTP and backoff semantics.

    @param kind 稳定失败类别 / Stable failure category.
    @param message 不含 provider body 或凭据的有界说明 / Bounded detail without provider body or credentials.
    @param status 可选 HTTP 状态码 / Optional HTTP status code.
    @param retry_after Provider 建议等待时间 / Provider-suggested wait duration.
    """

    kind: ProviderFailureKind
    status: int | None
    retry_after: timedelta | None

    def __init__(
        self,
        *,
        kind: ProviderFailureKind,
        message: str,
        status: int | None = None,
        retry_after: timedelta | None = None,
    ) -> None:
        """@brief 创建无重试副作用的失败值 / Create a failure value with no retry side effect.

        @param kind 稳定失败类别 / Stable failure category.
        @param message 不含 provider body 或凭据的有界说明 / Bounded detail without provider body or credentials.
        @param status 可选 HTTP 状态码 / Optional HTTP status code.
        @param retry_after Provider 建议等待时间 / Provider-suggested wait duration.
        @return None / None.
        @raise ValueError 状态码或等待时间非法时抛出 / Raised for an invalid status or delay.
        """

        normalized = message.strip() or kind.value
        if status is not None and not 100 <= status <= 599:
            raise ValueError("Provider failure status must be an HTTP status code")
        if retry_after is not None and retry_after <= timedelta(0):
            raise ValueError("Provider retry_after must be positive")
        super().__init__(normalized[:2_000])
        self.kind = kind
        self.status = status
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        """@brief 指示 route 是否可考虑 fallback/retry / Indicate whether a route may consider fallback or retry.

        @return 限流、超时、传输和服务端错误为 True / True for rate-limit, timeout, transport, and server failures.
        """

        return self.kind in {
            ProviderFailureKind.TRANSPORT,
            ProviderFailureKind.TIMEOUT,
            ProviderFailureKind.RATE_LIMITED,
            ProviderFailureKind.SERVER,
        }


class ProviderContractError(ProviderFailure):
    """@brief 本地或远端 completion contract 错误 / Local or remote completion-contract error.

    @param message 不含 provider body 或凭据的错误说明 / Error detail without provider body or credentials.
    @param status 可选 HTTP 状态码 / Optional HTTP status code.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        """@brief 创建不可重试的 contract 错误 / Create a non-retryable contract error.

        @param message 不含 provider body 或凭据的错误说明 / Error detail without provider body or credentials.
        @param status 可选 HTTP 状态码 / Optional HTTP status code.
        @return None / None.
        """

        super().__init__(
            kind=ProviderFailureKind.CONTRACT,
            message=message,
            status=status,
        )


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
    "ProviderContractError",
    "ProviderFailure",
    "ProviderFailureKind",
    "ResumableAgentInterruptedError",
    "SafetyBlockError",
]
