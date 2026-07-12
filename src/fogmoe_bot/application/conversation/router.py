"""@brief 显式 durable-inbox 路由管线 / Explicit durable-inbox routing pipeline.

路由语义不再依赖 PTB handler group 或注册顺序。所有 primary predicate 都会被求值；
零个匹配是合法忽略，恰好一个匹配可执行，多个匹配是必须修复的配置错误。/
Routing semantics no longer depend on PTB handler groups or registration order. Every
primary predicate is evaluated: zero matches are a valid ignore, exactly one is executable,
and multiple matches are a configuration error that must be fixed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from fogmoe_bot.application.runtime import (
    Accepted,
    AggregateKey,
    KeyedMailboxRuntime,
    Overloaded,
    RuntimeUnavailable,
    WorkPriority,
)
from fogmoe_bot.domain.conversation.identity import ConversationId
from fogmoe_bot.domain.conversation.inbox import InboundUpdate


type RoutedCallable = Callable[[], Awaitable[None]]
"""@brief 可在聚合邮箱执行的延迟异步调用 / Lazy async call executable in an aggregate mailbox."""


def conversation_aggregate_key(conversation_id: ConversationId) -> AggregateKey:
    """@brief 将规范会话映射到唯一 runtime 顺序边界 / Map a canonical conversation to its sole runtime ordering boundary.

    @param conversation_id durable Conversation identity / Durable Conversation identity.
    @return 所有 route 必须共享的聚合键 / Aggregate key that every route must share.
    @note Assistant、命令与 observer 使用不同 namespace 会制造并发错觉；本函数是唯一
        映射点。/ Different namespaces for Assistant, commands, and observers create false
        concurrency; this function is the single mapping point.
    """

    return AggregateKey.of("conversation", str(conversation_id))


@dataclass(frozen=True, slots=True)
class RoutedOperation:
    """@brief 已解析身份和容量等级的应用操作 / Application operation with resolved identity and capacity class.

    @param name 用于观测和错误定位的稳定名称 / Stable name for observability and diagnostics.
    @param key 业务顺序所属聚合 / Aggregate owning business ordering.
    @param call 仅在 runtime 准入后创建 coroutine 的调用 / Call creating a coroutine only after runtime admission.
    @param priority 不同聚合间的调度优先级 / Scheduling priority across aggregates.
    @note 操作必须以 inbox/command identity 幂等；router 的交付语义是 at-least-once。/
    Operations must be idempotent by inbox/command identity; router delivery is at-least-once.
    """

    name: str
    key: AggregateKey
    call: RoutedCallable
    priority: WorkPriority = WorkPriority.NORMAL

    def __post_init__(self) -> None:
        """@brief 校验可观测名称 / Validate the observable name.

        @return None / None.
        @raise ValueError 名称为空时抛出 / Raised when the name is blank.
        """

        if not self.name.strip():
            raise ValueError("Routed operation name cannot be blank")


@dataclass(frozen=True, slots=True)
class Allow:
    """@brief guard 允许继续 / Guard permits routing to continue."""


@dataclass(frozen=True, slots=True)
class Reject:
    """@brief guard 以类型化原因拒绝 Update / Guard rejects an Update with a typed reason.

    @param reason 稳定、可观测的拒绝原因 / Stable observable rejection reason.
    @param feedback 可选、幂等的用户反馈操作 / Optional idempotent user-feedback operation.
    """

    reason: str
    feedback: RoutedOperation | None = None

    def __post_init__(self) -> None:
        """@brief 校验拒绝原因 / Validate the rejection reason.

        @return None / None.
        @raise ValueError 原因为空时抛出 / Raised when the reason is blank.
        """

        if not self.reason.strip():
            raise ValueError("Guard rejection reason cannot be blank")


type GuardDecision = Allow | Reject
"""@brief guard 的穷尽结果 / Exhaustive guard result."""


class Guard(Protocol):
    """@brief 显式入口 guard 协议 / Explicit ingress-guard protocol."""

    @property
    def name(self) -> str:
        """@brief 返回稳定 guard 名称 / Return the stable guard name.

        @return guard 名称 / Guard name.
        """

        ...

    async def evaluate(self, update: InboundUpdate) -> GuardDecision:
        """@brief 对 durable Update 作出允许或拒绝决定 / Allow or reject a durable Update.

        @param update 已领取 Update / Claimed Update.
        @return guard 决定 / Guard decision.
        """

        ...


class PrimaryRoute(Protocol):
    """@brief 恰好选择一个主业务命令的路由协议 / Route protocol selecting exactly one primary command."""

    @property
    def name(self) -> str:
        """@brief 返回稳定 route 名称 / Return the stable route name.

        @return route 名称 / Route name.
        """

        ...

    def matches(self, update: InboundUpdate) -> bool:
        """@brief 纯判断 Update 是否属于本 route / Purely test whether the Update belongs to this route.

        @param update 待路由 Update / Update to route.
        @return 匹配时为 True / True on match.
        """

        ...

    async def operation(self, update: InboundUpdate) -> RoutedOperation:
        """@brief 构造幂等主操作 / Build the idempotent primary operation.

        @param update 已匹配 Update / Matched Update.
        @return 待准入操作 / Operation awaiting admission.
        """

        ...


class Observer(Protocol):
    """@brief 不改变主路由选择的观察者协议 / Observer protocol that cannot alter primary-route selection."""

    @property
    def name(self) -> str:
        """@brief 返回稳定 observer 名称 / Return the stable observer name.

        @return observer 名称 / Observer name.
        """

        ...

    async def operation(
        self,
        update: InboundUpdate,
        *,
        primary_route: str | None,
    ) -> RoutedOperation | None:
        """@brief 为本 Update 构造可选观察操作 / Build an optional observation operation.

        @param update 已通过 guard 的 Update / Guard-approved Update.
        @param primary_route 匹配的主 route 名或 None / Matched primary-route name, or None.
        @return 可选幂等操作 / Optional idempotent operation.
        """

        ...


class AmbiguousPrimaryRouteError(RuntimeError):
    """@brief 多个 primary route 同时匹配 / Multiple primary routes matched simultaneously."""


@dataclass(frozen=True, slots=True)
class Dispatched:
    """@brief Update 的全部计划操作已经成功执行 / All planned operations for an Update completed.

    @param primary_route 匹配的主 route 或 None / Matched primary route, or None.
    @param observer_names 实际执行的 observer 名称 / Names of observers actually executed.
    """

    primary_route: str | None
    observer_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Ignored:
    """@brief Update 未产生主命令或 observer / Update produced neither a primary command nor an observer."""


@dataclass(frozen=True, slots=True)
class Rejected:
    """@brief Update 被 guard 拒绝且反馈已完成 / Update was guard-rejected and feedback completed.

    @param guard_name 拒绝它的 guard / Guard that rejected it.
    @param reason 类型化拒绝原因 / Typed rejection reason.
    """

    guard_name: str
    reason: str


@dataclass(frozen=True, slots=True)
class DispatchDeferred:
    """@brief runtime 暂时不能接收计划操作 / Runtime temporarily cannot accept a planned operation.

    @param operation_name 未获准入的操作 / Operation that was not admitted.
    @param cause 过载或生命周期拒绝 / Overload or lifecycle rejection.
    @note inbox worker 必须保留/重试该 Update；此前完成的操作依赖幂等键安全重放。/
    The inbox worker must retain/retry the Update; operations already completed rely on
    idempotency keys for safe replay.
    """

    operation_name: str
    cause: Overloaded | RuntimeUnavailable


type RouteOutcome = Dispatched | Ignored | Rejected | DispatchDeferred
"""@brief router 的穷尽业务结果 / Exhaustive router outcome."""


class IngressRouter:
    """@brief 运行 guard、唯一 primary 与 observers 的显式 router / Explicit router for guards, one primary, and observers."""

    def __init__(
        self,
        *,
        runtime: KeyedMailboxRuntime,
        guards: Sequence[Guard] = (),
        primary_routes: Sequence[PrimaryRoute] = (),
        observers: Sequence[Observer] = (),
    ) -> None:
        """@brief 创建不可变 route catalog / Create an immutable route catalog.

        @param runtime 有界键控执行运行时 / Bounded keyed execution runtime.
        @param guards 按显式声明顺序执行的 guard / Guards in explicit declaration order.
        @param primary_routes 必须互斥的主 routes / Primary routes required to be mutually exclusive.
        @param observers 主操作后执行的观察者 / Observers executed after the primary operation.
        @raise ValueError 任一类别名称重复或为空时抛出 / Raised for duplicate or blank names in a category.
        """

        self._runtime = runtime
        self._guards = tuple(guards)
        self._primary_routes = tuple(primary_routes)
        self._observers = tuple(observers)
        self._validate_names(self._guards, category="guard")
        self._validate_names(self._primary_routes, category="primary route")
        self._validate_names(self._observers, category="observer")

    async def route(self, update: InboundUpdate) -> RouteOutcome:
        """@brief 路由并等待本 Update 的全部操作完成 / Route and await all operations for one Update.

        @param update durable inbox 领取的 Update / Update claimed from the durable inbox.
        @return 可供 inbox worker 决定完成或重试的结果 / Outcome allowing the inbox worker to complete or retry.
        @raise AmbiguousPrimaryRouteError 多个主 route 匹配时抛出 / Raised when multiple primary routes match.
        @note operation 异常原样传播；inbox worker 负责错误分类与 retry schedule。/
        Operation exceptions propagate unchanged; the inbox worker owns error classification
        and retry scheduling.
        """

        guard_result = await self._run_guards(update)
        if isinstance(guard_result, DispatchDeferred):
            return guard_result
        if guard_result is not None:
            guard_name, decision = guard_result
            if decision.feedback is not None:
                deferred = await self._execute(decision.feedback)
                if deferred is not None:
                    return deferred
            return Rejected(guard_name=guard_name, reason=decision.reason)

        matching = tuple(
            route for route in self._primary_routes if route.matches(update)
        )
        if len(matching) > 1:
            names = ", ".join(route.name for route in matching)
            raise AmbiguousPrimaryRouteError(
                f"Update {update.update_id.value} matched multiple primary routes: {names}"
            )
        primary = matching[0] if matching else None
        primary_name = primary.name if primary is not None else None
        if primary is not None:
            deferred = await self._execute(await primary.operation(update))
            if deferred is not None:
                return deferred

        executed_observers: list[str] = []
        for observer in self._observers:
            operation = await observer.operation(update, primary_route=primary_name)
            if operation is None:
                continue
            deferred = await self._execute(operation)
            if deferred is not None:
                return deferred
            executed_observers.append(observer.name)

        if primary is None and not executed_observers:
            return Ignored()
        return Dispatched(
            primary_route=primary_name,
            observer_names=tuple(executed_observers),
        )

    async def _run_guards(
        self,
        update: InboundUpdate,
    ) -> tuple[str, Reject] | DispatchDeferred | None:
        """@brief 在 keyed runtime 内返回首个拒绝决定 / Return the first rejecting decision inside the keyed runtime.

        @param update 待检查 Update / Update to inspect.
        @return rejection、准入延迟或 None / Rejection, admission deferral, or None.
        @note guard 可能读取数据库或执行可恢复治理 effect，因此与 primary 一样必须经过
            runtime 容量、顺序和 shutdown 控制。/ A guard may read the database or execute a
            recoverable moderation effect, so it must use the same runtime capacity, ordering,
            and shutdown control as a primary operation.
        """

        for guard in self._guards:

            async def evaluate_guard() -> GuardDecision:
                """@brief 在 runtime worker 中执行当前 guard / Execute the current guard in a runtime worker.

                @return guard 决定 / Guard decision.
                """

                return await guard.evaluate(update)

            submission = self._runtime.submit(
                conversation_aggregate_key(update.conversation_id),
                evaluate_guard,
                priority=WorkPriority.CRITICAL,
            )
            if not isinstance(submission, Accepted):
                return DispatchDeferred(
                    operation_name=(f"guard:{guard.name}:{int(update.update_id)}"),
                    cause=submission,
                )
            decision = await submission.ticket.wait()
            if isinstance(decision, Reject):
                return guard.name, decision
        return None

    async def _execute(self, operation: RoutedOperation) -> DispatchDeferred | None:
        """@brief 准入并等待一个操作 / Admit and await one operation.

        @param operation 待执行操作 / Operation to execute.
        @return 成功时 None，否则为 deferred 结果 / None on completion, otherwise a deferred outcome.
        """

        submission = self._runtime.submit(
            operation.key,
            operation.call,
            priority=operation.priority,
        )
        if isinstance(submission, Accepted):
            await submission.ticket.wait()
            return None
        return DispatchDeferred(operation_name=operation.name, cause=submission)

    @staticmethod
    def _validate_names(
        items: Sequence[Guard | PrimaryRoute | Observer], *, category: str
    ) -> None:
        """@brief 验证 catalog 名称唯一且非空 / Validate unique, non-blank catalog names.

        @param items 同一 route 类别的声明 / Declarations in one route category.
        @param category 错误消息中的类别名 / Category name used in errors.
        @return None / None.
        @raise ValueError 名称为空或重复时抛出 / Raised for blank or duplicate names.
        """

        names = tuple(item.name for item in items)
        if any(not name.strip() for name in names):
            raise ValueError(f"{category} names cannot be blank")
        if len(set(names)) != len(names):
            raise ValueError(f"Duplicate {category} names are not allowed")


__all__ = [
    "Allow",
    "AmbiguousPrimaryRouteError",
    "DispatchDeferred",
    "Dispatched",
    "Guard",
    "GuardDecision",
    "Ignored",
    "IngressRouter",
    "Observer",
    "PrimaryRoute",
    "Reject",
    "Rejected",
    "RouteOutcome",
    "RoutedCallable",
    "RoutedOperation",
]
