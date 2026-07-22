"""@brief 显式 durable-inbox router 测试 / Tests for the explicit durable-inbox router."""

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone

import pytest

from fogmoe_bot.application.conversation.router import (
    Allow,
    AmbiguousPrimaryRouteError,
    DispatchDeferred,
    Dispatched,
    Ignored,
    IngressRouter,
    Reject,
    Rejected,
    RoutedOperation,
)
from fogmoe_bot.application.runtime import (
    AggregateKey,
    KeyedMailboxRuntime,
    Overloaded,
    OverloadScope,
    ShutdownMode,
)
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    UpdateId,
)
from fogmoe_bot.domain.conversation.inbox import InboundUpdate


def _inbound(update_id: int = 1) -> InboundUpdate:
    """@brief 构造待路由 Update / Build a routable Update.

    @param update_id Update ID / Update identifier.
    @return 待处理入口实体 / Pending ingress entity.
    """

    return InboundUpdate.pending(
        update_id=UpdateId(update_id),
        conversation_id=ConversationId("assistant-user:7"),
        payload={"kind": "message"},
        received_at=datetime(2026, 7, 11, tzinfo=timezone.utc),
    )


class _Guard:
    """@brief 可控 guard 测试替身 / Controllable guard test double."""

    def __init__(self, name: str, decision: Allow | Reject) -> None:
        """@brief 创建 guard / Create a guard.

        @param name guard 名称 / Guard name.
        @param decision 固定返回决定 / Fixed decision.
        """

        self._name = name
        self._decision = decision

    @property
    def name(self) -> str:
        """@brief 返回名称 / Return the name.

        @return guard 名称 / Guard name.
        """

        return self._name

    async def evaluate(self, update: InboundUpdate) -> Allow | Reject:
        """@brief 返回固定决定 / Return the fixed decision.

        @param update 未使用的 Update / Unused Update.
        @return 固定决定 / Fixed decision.
        """

        del update
        return self._decision


class _Route:
    """@brief 可控 primary route 测试替身 / Controllable primary-route test double."""

    def __init__(self, name: str, matches: bool, operation: RoutedOperation) -> None:
        """@brief 创建 route / Create a route.

        @param name route 名称 / Route name.
        @param matches 固定匹配结果 / Fixed match result.
        @param operation 固定操作 / Fixed operation.
        """

        self._name = name
        self._matches = matches
        self._operation = operation

    @property
    def name(self) -> str:
        """@brief 返回名称 / Return the name.

        @return route 名称 / Route name.
        """

        return self._name

    def matches(self, update: InboundUpdate) -> bool:
        """@brief 返回固定匹配结果 / Return the fixed match result.

        @param update 未使用的 Update / Unused Update.
        @return 固定结果 / Fixed result.
        """

        del update
        return self._matches

    async def operation(self, update: InboundUpdate) -> RoutedOperation:
        """@brief 返回固定操作 / Return the fixed operation.

        @param update 未使用的 Update / Unused Update.
        @return 固定操作 / Fixed operation.
        """

        del update
        return self._operation


class _Observer:
    """@brief 可控 observer 测试替身 / Controllable observer test double."""

    def __init__(self, name: str, operation: RoutedOperation | None) -> None:
        """@brief 创建 observer / Create an observer.

        @param name observer 名称 / Observer name.
        @param operation 可选固定操作 / Optional fixed operation.
        """

        self._name = name
        self._operation = operation
        self.seen_primary: str | None = None

    @property
    def name(self) -> str:
        """@brief 返回名称 / Return the name.

        @return observer 名称 / Observer name.
        """

        return self._name

    async def operation(
        self,
        update: InboundUpdate,
        *,
        primary_route: str | None,
    ) -> RoutedOperation | None:
        """@brief 记录主 route 并返回固定操作 / Record the primary route and return a fixed operation.

        @param update 未使用的 Update / Unused Update.
        @param primary_route 匹配的主 route / Matched primary route.
        @return 可选固定操作 / Optional fixed operation.
        """

        del update
        self.seen_primary = primary_route
        return self._operation


def _recording_operation(name: str, events: list[str]) -> RoutedOperation:
    """@brief 构造记录执行顺序的操作 / Build an operation that records execution order.

    @param name 操作名与记录值 / Operation name and recorded value.
    @param events 执行记录 / Execution log.
    @return routed operation / Routed operation.
    """

    async def call() -> None:
        """@brief 记录一次执行 / Record one execution.

        @return None / None.
        """

        events.append(name)

    return RoutedOperation(
        name=name,
        key=AggregateKey.of("test", name),
        call=call,
    )


def _run_with_runtime(scenario: Callable[[KeyedMailboxRuntime], object]) -> object:
    """@brief 在已启动 runtime 中运行异步场景 / Run an async scenario inside a started runtime.

    @param scenario 返回 awaitable 的场景工厂 / Scenario factory returning an awaitable.
    @return 场景结果 / Scenario result.
    """

    async def run() -> object:
        """@brief 管理 runtime 生命周期 / Manage runtime lifecycle.

        @return 场景结果 / Scenario result.
        """

        runtime = KeyedMailboxRuntime(
            max_concurrency=2,
            global_capacity=8,
            per_key_capacity=4,
            idle_ttl=10,
        )
        await runtime.start()
        try:
            result = scenario(runtime)
            if not hasattr(result, "__await__"):
                raise TypeError("Scenario must return an awaitable")
            return await result  # type: ignore[misc]
        finally:
            await runtime.shutdown(ShutdownMode.CANCEL)

    return asyncio.run(run())


def test_router_executes_unique_primary_before_observers() -> None:
    """@brief primary 唯一匹配并先于 observers 执行 / A unique primary executes before observers."""

    async def scenario(runtime: KeyedMailboxRuntime) -> object:
        """@brief 运行主路由场景 / Run the primary-route scenario.

        @param runtime 已启动 runtime / Started runtime.
        @return route 结果 / Route outcome.
        """

        events: list[str] = []
        observer = _Observer("audit", _recording_operation("observer", events))
        router = IngressRouter(
            runtime=runtime,
            guards=(_Guard("spam", Allow()),),
            primary_routes=(
                _Route("assistant", True, _recording_operation("primary", events)),
            ),
            observers=(observer,),
        )
        outcome = await router.route(_inbound())
        assert events == ["primary", "observer"]
        assert observer.seen_primary == "assistant"
        return outcome

    outcome = _run_with_runtime(scenario)
    assert outcome == Dispatched(primary_route="assistant", observer_names=("audit",))


def test_guard_rejection_blocks_primary_and_observers_but_runs_feedback() -> None:
    """@brief guard 拒绝截断业务管线但执行反馈 / Guard rejection stops business routing but executes feedback."""

    async def scenario(runtime: KeyedMailboxRuntime) -> object:
        """@brief 运行拒绝场景 / Run the rejection scenario.

        @param runtime 已启动 runtime / Started runtime.
        @return route 结果 / Route outcome.
        """

        events: list[str] = []
        router = IngressRouter(
            runtime=runtime,
            guards=(
                _Guard(
                    "spam",
                    Reject("rate-limited", _recording_operation("feedback", events)),
                ),
            ),
            primary_routes=(
                _Route("assistant", True, _recording_operation("primary", events)),
            ),
            observers=(_Observer("audit", _recording_operation("observer", events)),),
        )
        outcome = await router.route(_inbound())
        assert events == ["feedback"]
        return outcome

    outcome = _run_with_runtime(scenario)
    assert outcome == Rejected(guard_name="spam", reason="rate-limited")


def test_router_rejects_ambiguous_primary_matches() -> None:
    """@brief 多个 primary 匹配时快速失败 / Multiple primary matches fail fast."""

    async def scenario(runtime: KeyedMailboxRuntime) -> None:
        """@brief 运行歧义场景 / Run the ambiguity scenario.

        @param runtime 已启动 runtime / Started runtime.
        @return None / None.
        """

        events: list[str] = []
        router = IngressRouter(
            runtime=runtime,
            primary_routes=(
                _Route("command", True, _recording_operation("one", events)),
                _Route("text", True, _recording_operation("two", events)),
            ),
        )
        with pytest.raises(AmbiguousPrimaryRouteError, match="command, text"):
            await router.route(_inbound(44))
        assert events == []

    _run_with_runtime(scenario)


def test_router_reports_runtime_admission_failure_without_calling_operation() -> None:
    """@brief runtime 关停时返回 deferred 且不创建 coroutine / Closed runtime yields deferred without creating a coroutine."""

    async def scenario() -> object:
        """@brief 使用未启动 runtime 路由 / Route through an unstarted runtime.

        @return route 结果 / Route outcome.
        """

        events: list[str] = []
        runtime = KeyedMailboxRuntime(
            max_concurrency=1,
            global_capacity=1,
            per_key_capacity=1,
            idle_ttl=1,
        )
        router = IngressRouter(
            runtime=runtime,
            primary_routes=(
                _Route("assistant", True, _recording_operation("primary", events)),
            ),
        )
        outcome = await router.route(_inbound())
        assert events == []
        return outcome

    outcome = asyncio.run(scenario())
    assert isinstance(outcome, DispatchDeferred)


def test_router_ignores_update_with_no_planned_operations() -> None:
    """@brief 无匹配和观察操作时明确返回 Ignored / No primary or observation explicitly yields Ignored."""

    async def scenario(runtime: KeyedMailboxRuntime) -> object:
        """@brief 运行忽略场景 / Run the ignored scenario.

        @param runtime 已启动 runtime / Started runtime.
        @return route 结果 / Route outcome.
        """

        events: list[str] = []
        router = IngressRouter(
            runtime=runtime,
            primary_routes=(
                _Route("assistant", False, _recording_operation("primary", events)),
            ),
            observers=(_Observer("audit", None),),
        )
        return await router.route(_inbound())

    assert _run_with_runtime(scenario) == Ignored()


def test_deferred_cause_retains_capacity_scope() -> None:
    """@brief deferred 结果保留精确过载边界 / Deferred outcomes retain the exact overload boundary."""

    cause = Overloaded(
        key=AggregateKey.of("test", 1),
        scope=OverloadScope.GLOBAL,
        capacity=1,
        pending=1,
    )
    outcome = DispatchDeferred(operation_name="assistant", cause=cause)

    assert outcome.cause.scope is OverloadScope.GLOBAL
