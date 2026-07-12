"""@brief 有界键控邮箱执行运行时 / Bounded keyed-mailbox execution runtime.

该模块只负责进程内的执行顺序、准入控制与生命周期管理。它不拥有领域状态，也不
假装提供跨进程一致性；持久化和多实例租约应由更外层的运行时能力实现。
/ This module owns only in-process execution ordering, admission control, and lifecycle
management. It owns no domain state and makes no cross-process consistency claim;
durability and multi-instance leases belong to outer runtime capabilities.
"""

from __future__ import annotations

import asyncio
import heapq
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Any, Self


type AggregateIdentityPart = str | int
"""@brief 聚合标识的一个稳定组成部分 / One stable aggregate-identity component."""

type AsyncOperation[T] = Callable[[], Awaitable[T]]
"""@brief 延迟创建的异步工作单元 / Lazily-created asynchronous work unit."""


@dataclass(frozen=True, slots=True)
class AggregateKey:
    """@brief 领域聚合的进程内串行化键 / In-process serialization key for a domain aggregate.

    @param aggregate_type 聚合种类，如 ``conversation`` 或 ``account`` /
        Aggregate kind, such as ``conversation`` or ``account``.
    @param identity 在该聚合种类内唯一且稳定的标识元组 /
        Stable identity tuple unique within the aggregate kind.
    @note 该键表达顺序边界，不等同于数据库主键或分布式锁。/
        This key expresses an ordering boundary, not a database primary key or distributed lock.
    """

    aggregate_type: str
    identity: tuple[AggregateIdentityPart, ...]

    def __post_init__(self) -> None:
        """@brief 校验聚合键的不变量 / Validate aggregate-key invariants.

        @return None / None.
        @raises ValueError 聚合种类或标识为空，或字符串标识为空白 /
            If the aggregate kind/identity is empty or a string identity is blank.
        @raises TypeError 标识包含非 ``str``/``int`` 值或布尔值 /
            If an identity contains a non-``str``/``int`` value or a boolean.
        """

        if not self.aggregate_type or not self.aggregate_type.strip():
            raise ValueError("aggregate_type must not be blank")
        if not self.identity:
            raise ValueError("identity must contain at least one part")
        for part in self.identity:
            if isinstance(part, bool) or not isinstance(part, str | int):
                raise TypeError("identity parts must be str or int values")
            if isinstance(part, str) and not part.strip():
                raise ValueError("string identity parts must not be blank")

    @classmethod
    def of(
        cls,
        aggregate_type: str,
        *identity: AggregateIdentityPart,
    ) -> AggregateKey:
        """@brief 由可变参数构造聚合键 / Build an aggregate key from variadic parts.

        @param aggregate_type 聚合种类 / Aggregate kind.
        @param identity 聚合标识的有序组成部分 / Ordered aggregate-identity parts.
        @return 已校验的不可变聚合键 / Validated immutable aggregate key.

        @example
        ``AggregateKey.of("conversation", chat_id, user_id, thread_id)`` 将同一会话的事件
        映射到同一邮箱。/ The example maps events for one conversation to the same mailbox.
        """

        return cls(aggregate_type=aggregate_type, identity=identity)


class WorkPriority(IntEnum):
    """@brief 跨聚合调度优先级 / Cross-aggregate scheduling priority.

    @note 数值越小优先级越高；优先级从不改变同一聚合内的 FIFO 顺序。/
        Lower values run first; priority never changes FIFO order inside one aggregate.
    """

    CRITICAL = 0
    """@brief 控制面或安全关键工作 / Control-plane or safety-critical work."""

    HIGH = 10
    """@brief 面向用户的高优先级工作 / High-priority user-facing work."""

    NORMAL = 20
    """@brief 默认业务工作 / Default business work."""

    LOW = 30
    """@brief 可延迟的后台工作 / Deferrable background work."""


class OverloadScope(StrEnum):
    """@brief 准入失败的容量边界 / Capacity boundary that rejected admission."""

    GLOBAL = "global"
    """@brief 整个运行时已满 / The whole runtime is full."""

    AGGREGATE = "aggregate"
    """@brief 单个聚合邮箱已满 / One aggregate mailbox is full."""


class RuntimeState(StrEnum):
    """@brief 键控邮箱运行时生命周期 / Keyed-mailbox runtime lifecycle."""

    NEW = "new"
    """@brief 尚未启动 / Not started."""

    RUNNING = "running"
    """@brief 正在接收并执行工作 / Accepting and executing work."""

    DRAINING = "draining"
    """@brief 拒绝新工作并排空已接收工作 / Rejecting new work while draining accepted work."""

    CANCELLING = "cancelling"
    """@brief 正在取消所有已接收工作 / Cancelling all accepted work."""

    CLOSED = "closed"
    """@brief 已终止且不可重启 / Terminated and not restartable."""


class ShutdownMode(StrEnum):
    """@brief 结构化关停策略 / Structured shutdown policy."""

    DRAIN = "drain"
    """@brief 完成全部已接收工作后关停 / Stop after all accepted work completes."""

    CANCEL = "cancel"
    """@brief 取消排队与执行中的工作后关停 / Stop after cancelling queued and running work."""


@dataclass(frozen=True, slots=True)
class WorkTicket[T]:
    """@brief 已接收工作的类型化完成凭据 / Typed completion ticket for accepted work.

    @param key 工作所属聚合键 / Aggregate key owning the work.
    @param _completion 由运行时独占写入的完成 Future /
        Completion future written exclusively by the runtime.
    """

    key: AggregateKey
    _completion: asyncio.Future[T] = field(repr=False)

    @property
    def done(self) -> bool:
        """@brief 工作是否已经完成或取消 / Whether the work has completed or been cancelled.

        @return 完成状态 / Completion state.
        """

        return self._completion.done()

    @property
    def cancelled(self) -> bool:
        """@brief 工作是否已被运行时取消 / Whether the runtime cancelled the work.

        @return 取消状态 / Cancellation state.
        """

        return self._completion.cancelled()

    async def wait(self) -> T:
        """@brief 等待工作结果 / Wait for the work result.

        @return 异步工作返回值 / Value returned by the asynchronous operation.
        @raises BaseException 原样传播工作抛出的异常 / Propagates the operation exception unchanged.
        @note 等待者自身被取消不会取消已经准入的工作。/
            Cancelling a waiter does not cancel already-admitted work.
        """

        return await asyncio.shield(self._completion)


@dataclass(frozen=True, slots=True)
class Accepted[T]:
    """@brief 成功准入结果 / Successful admission result.

    @param ticket 用于观察最终结果的类型化凭据 / Typed ticket for observing completion.
    """

    ticket: WorkTicket[T]


@dataclass(frozen=True, slots=True)
class Overloaded:
    """@brief 明确的容量过载结果 / Explicit capacity-overload result.

    @param key 被拒绝工作的聚合键 / Aggregate key of the rejected work.
    @param scope 触发拒绝的容量边界 / Capacity boundary that rejected admission.
    @param capacity 该边界的配置容量 / Configured capacity of that boundary.
    @param pending 拒绝时该边界内未完成的工作数 / Pending work at that boundary on rejection.
    """

    key: AggregateKey
    scope: OverloadScope
    capacity: int
    pending: int


@dataclass(frozen=True, slots=True)
class RuntimeUnavailable:
    """@brief 生命周期导致的准入拒绝 / Admission rejection caused by lifecycle state.

    @param key 被拒绝工作的聚合键 / Aggregate key of the rejected work.
    @param state 拒绝时运行时状态 / Runtime state at rejection time.
    """

    key: AggregateKey
    state: RuntimeState


type Submission[T] = Accepted[T] | Overloaded | RuntimeUnavailable
"""@brief 可穷尽模式匹配的准入结果 / Exhaustively matchable admission result."""


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    """@brief 无副作用的运行时观测快照 / Side-effect-free runtime observation snapshot.

    @param state 当前生命周期状态 / Current lifecycle state.
    @param mailbox_count 当前保留的聚合邮箱数 / Number of retained aggregate mailboxes.
    @param pending_count 已准入但尚未终结的工作数 / Admitted but unfinished work count.
    @param active_count 当前执行中的工作数 / Currently executing work count.
    @param queued_count 当前等待执行的工作数 / Currently queued work count.
    @param ready_mailbox_count 当前可被 worker 领取的邮箱数 / Mailboxes currently ready for workers.
    """

    state: RuntimeState
    mailbox_count: int
    pending_count: int
    active_count: int
    queued_count: int
    ready_mailbox_count: int


@dataclass(slots=True)
class _WorkItem[T]:
    """@brief 运行时内部的单个工作项 / One internal runtime work item.

    @param key 聚合键 / Aggregate key.
    @param priority 跨邮箱优先级 / Cross-mailbox priority.
    @param sequence 全局单调准入序号 / Globally monotonic admission sequence.
    @param operation 延迟创建的异步操作 / Lazily-created asynchronous operation.
    @param completion 工作完成 Future / Work completion future.
    """

    key: AggregateKey
    priority: WorkPriority
    sequence: int
    operation: AsyncOperation[T]
    completion: asyncio.Future[T]


@dataclass(slots=True)
class _Mailbox:
    """@brief 单个聚合的 FIFO 邮箱状态 / FIFO mailbox state for one aggregate.

    @param queue 尚未开始的 FIFO 工作队列 / FIFO queue of not-yet-started work.
    @param running 当前是否有一个工作正在执行 / Whether one work item is executing.
    @param ready 邮箱是否已有一项进入全局 ready heap / Whether one entry is in the global ready heap.
    @param idle_since 邮箱最后一次变为空闲的单调时钟时间 / Monotonic time when the mailbox became idle.
    """

    queue: deque[_WorkItem[Any]] = field(default_factory=deque)
    running: bool = False
    ready: bool = False
    idle_since: float | None = None

    @property
    def pending_count(self) -> int:
        """@brief 返回邮箱内全部未终结工作数 / Return all unfinished work in the mailbox.

        @return 排队工作数加至多一个执行中工作 / Queued work plus at most one running work.
        """

        return len(self.queue) + int(self.running)


class KeyedMailboxRuntime:
    """@brief 有界、按聚合串行的 asyncio 执行运行时 / Bounded aggregate-serial asyncio runtime.

    同一个 ``AggregateKey`` 的工作严格按准入顺序、一次一个地执行；不同 key 最多以
    ``max_concurrency`` 并发。准入是同步且非阻塞的，因此过载会作为值返回，而不是让
    Telegram ingress 无限等待。/ Work for one ``AggregateKey`` executes strictly in admission
    order and one at a time; distinct keys execute with up to ``max_concurrency`` concurrency.
    Admission is synchronous and non-blocking, so overload is returned as data instead of making
    Telegram ingress wait without bound.

    @note 运行时绑定到调用 ``start`` 的 event loop；它不是跨线程调度器。/
        The runtime is bound to the event loop that calls ``start``; it is not a cross-thread scheduler.
    """

    def __init__(
        self,
        *,
        max_concurrency: int,
        global_capacity: int,
        per_key_capacity: int,
        idle_ttl: float = 60.0,
        reap_interval: float | None = None,
    ) -> None:
        """@brief 配置运行时资源边界 / Configure runtime resource boundaries.

        @param max_concurrency 固定 worker 数量 / Fixed worker count.
        @param global_capacity 全局已准入未完成工作上限 / Global admitted-unfinished work limit.
        @param per_key_capacity 单个 key 已准入未完成工作上限 / Per-key admitted-unfinished work limit.
        @param idle_ttl 空邮箱保留秒数 / Seconds to retain an empty mailbox.
        @param reap_interval 回收扫描间隔；None 时由 ``idle_ttl`` 推导 /
            Reaper interval, derived from ``idle_ttl`` when None.
        @return None / None.
        @raises ValueError 任一容量或时间参数不为正 / If any capacity or duration is not positive.
        """

        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        if global_capacity <= 0:
            raise ValueError("global_capacity must be positive")
        if per_key_capacity <= 0:
            raise ValueError("per_key_capacity must be positive")
        if idle_ttl <= 0:
            raise ValueError("idle_ttl must be positive")
        if reap_interval is not None and reap_interval <= 0:
            raise ValueError("reap_interval must be positive")

        self._max_concurrency = max_concurrency
        """@brief 固定 worker 数量 / Fixed worker count."""
        self._global_capacity = global_capacity
        """@brief 全局容量 / Global capacity."""
        self._per_key_capacity = per_key_capacity
        """@brief 单 key 容量 / Per-key capacity."""
        self._idle_ttl = idle_ttl
        """@brief 空邮箱生存时间 / Empty-mailbox time-to-live."""
        self._reap_interval = reap_interval or min(idle_ttl, 1.0)
        """@brief 空邮箱回收间隔 / Empty-mailbox reaping interval."""

        self._state = RuntimeState.NEW
        """@brief 当前生命周期状态 / Current lifecycle state."""
        self._loop: asyncio.AbstractEventLoop | None = None
        """@brief 所属 asyncio event loop / Owning asyncio event loop."""
        self._task_group: asyncio.TaskGroup | None = None
        """@brief 固定后台任务的结构化所有者 / Structured owner of fixed background tasks."""
        self._worker_tasks: list[asyncio.Task[None]] = []
        """@brief 固定数量的 worker task / Fixed set of worker tasks."""
        self._reaper_task: asyncio.Task[None] | None = None
        """@brief 唯一的空邮箱回收 task / Sole idle-mailbox reaper task."""

        self._mailboxes: dict[AggregateKey, _Mailbox] = {}
        """@brief 按聚合键索引的邮箱 / Mailboxes indexed by aggregate key."""
        self._ready_heap: list[tuple[int, int, AggregateKey]] = []
        """@brief 跨邮箱优先级 ready heap / Cross-mailbox priority ready heap."""
        self._next_sequence = 0
        """@brief 下一全局准入序号 / Next global admission sequence."""
        self._pending_count = 0
        """@brief 已准入未终结工作数 / Admitted-unfinished work count."""
        self._active_count = 0
        """@brief 当前执行中的工作数 / Currently executing work count."""
        self._active_items: dict[int, _WorkItem[Any]] = {}
        """@brief 按准入序号索引的执行中工作 / Running work indexed by admission sequence."""

        self._wake_event = asyncio.Event()
        """@brief ready heap 或生命周期变化通知 / Ready-heap or lifecycle-change notification."""
        self._idle_event = asyncio.Event()
        """@brief 全部已准入工作已终结通知 / Notification that all admitted work is terminal."""
        self._idle_event.set()
        self._stop_reaper_event = asyncio.Event()
        """@brief 回收 task 的停止通知 / Stop notification for the reaper task."""
        self._shutdown_lock = asyncio.Lock()
        """@brief 串行化幂等关停调用 / Serialize idempotent shutdown calls."""

    @property
    def state(self) -> RuntimeState:
        """@brief 返回当前生命周期状态 / Return the current lifecycle state.

        @return 当前状态 / Current state.
        """

        return self._state

    @property
    def mailbox_count(self) -> int:
        """@brief 返回当前保留邮箱数 / Return the retained-mailbox count.

        @return 邮箱数 / Mailbox count.
        """

        return len(self._mailboxes)

    @property
    def pending_count(self) -> int:
        """@brief 返回全局未终结工作数 / Return global unfinished-work count.

        @return 未终结工作数 / Unfinished-work count.
        """

        return self._pending_count

    def snapshot(self) -> RuntimeSnapshot:
        """@brief 捕获当前观测快照 / Capture the current observation snapshot.

        @return 不可变快照 / Immutable snapshot.
        @note 应从运行时所属 event loop 调用，以获得一致视图。/
            Call from the owning event loop for a coherent view.
        """

        return RuntimeSnapshot(
            state=self._state,
            mailbox_count=len(self._mailboxes),
            pending_count=self._pending_count,
            active_count=self._active_count,
            queued_count=self._pending_count - self._active_count,
            ready_mailbox_count=sum(
                mailbox.ready for mailbox in self._mailboxes.values()
            ),
        )

    async def __aenter__(self) -> Self:
        """@brief 启动并进入结构化运行时作用域 / Start and enter the structured runtime scope.

        @return 已启动运行时自身 / The started runtime itself.
        """

        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        """@brief 离开作用域并确定性关停 / Leave the scope and shut down deterministically.

        @param exc_type 作用域异常类型 / Scope exception type.
        @param exc 作用域异常实例 / Scope exception instance.
        @param traceback 作用域异常 traceback / Scope exception traceback.
        @return None / None.
        @note 正常退出会排空；异常或取消退出会取消全部工作。/
            Normal exit drains; exceptional or cancelled exit cancels all work.
        """

        del exc, traceback
        mode = ShutdownMode.DRAIN if exc_type is None else ShutdownMode.CANCEL
        await self.shutdown(mode)

    async def start(self) -> None:
        """@brief 启动固定 worker 与回收任务 / Start fixed workers and the reaper.

        @return None / None.
        @raises RuntimeError 运行时不是 NEW 状态 / If the runtime is not in NEW state.
        """

        if self._state is not RuntimeState.NEW:
            raise RuntimeError(f"runtime cannot start from state {self._state}")
        self._loop = asyncio.get_running_loop()
        self._state = RuntimeState.RUNNING

        task_group = asyncio.TaskGroup()
        await task_group.__aenter__()
        self._task_group = task_group
        try:
            self._worker_tasks = [
                task_group.create_task(
                    self._worker_loop(worker_id),
                    name=f"keyed-mailbox-worker-{worker_id}",
                )
                for worker_id in range(self._max_concurrency)
            ]
            self._reaper_task = task_group.create_task(
                self._reaper_loop(),
                name="keyed-mailbox-reaper",
            )
        except BaseException:
            self._state = RuntimeState.CANCELLING
            self._stop_reaper_event.set()
            for task in self._worker_tasks:
                task.cancel()
            await task_group.__aexit__(None, None, None)
            self._state = RuntimeState.CLOSED
            raise

    def submit[T](
        self,
        key: AggregateKey,
        operation: AsyncOperation[T],
        *,
        priority: WorkPriority = WorkPriority.NORMAL,
    ) -> Submission[T]:
        """@brief 非阻塞地尝试准入工作 / Try to admit work without blocking.

        @param key 决定 FIFO 串行边界的聚合键 / Aggregate key defining the FIFO boundary.
        @param operation 被 worker 延迟调用一次的异步操作 / Async operation called lazily once by a worker.
        @param priority 仅参与不同 ready 邮箱间的选择 / Priority used only across distinct ready mailboxes.
        @return ``Accepted``、``Overloaded`` 或 ``RuntimeUnavailable`` 的判别联合 /
            Discriminated union of ``Accepted``, ``Overloaded``, or ``RuntimeUnavailable``.
        @note 必须传 callable 而非 coroutine 对象，使拒绝准入时不会泄漏未 await 的 coroutine。/
            A callable, not a coroutine object, prevents leaked un-awaited coroutines on rejection.
        """

        if not isinstance(key, AggregateKey):
            raise TypeError("key must be an AggregateKey")
        if not callable(operation):
            raise TypeError("operation must be callable")
        if not isinstance(priority, WorkPriority):
            raise TypeError("priority must be a WorkPriority")
        if self._state is RuntimeState.NEW:
            return RuntimeUnavailable(key=key, state=self._state)
        self._ensure_owner_loop()
        if self._state is not RuntimeState.RUNNING:
            return RuntimeUnavailable(key=key, state=self._state)

        mailbox = self._mailboxes.get(key)
        key_pending = 0 if mailbox is None else mailbox.pending_count
        if key_pending >= self._per_key_capacity:
            return Overloaded(
                key=key,
                scope=OverloadScope.AGGREGATE,
                capacity=self._per_key_capacity,
                pending=key_pending,
            )
        if self._pending_count >= self._global_capacity:
            return Overloaded(
                key=key,
                scope=OverloadScope.GLOBAL,
                capacity=self._global_capacity,
                pending=self._pending_count,
            )

        if mailbox is None:
            mailbox = _Mailbox()
            self._mailboxes[key] = mailbox
        mailbox.idle_since = None

        completion: asyncio.Future[T] = self._owner_loop().create_future()
        completion.add_done_callback(_observe_completion)
        sequence = self._next_sequence
        self._next_sequence += 1
        item = _WorkItem(
            key=key,
            priority=priority,
            sequence=sequence,
            operation=operation,
            completion=completion,
        )
        mailbox.queue.append(item)
        self._pending_count += 1
        self._idle_event.clear()

        if not mailbox.running and not mailbox.ready:
            self._mark_ready(key, mailbox)
        return Accepted(ticket=WorkTicket(key=key, _completion=completion))

    async def shutdown(self, mode: ShutdownMode = ShutdownMode.DRAIN) -> None:
        """@brief 以给定策略确定性关停 / Shut down deterministically with the given policy.

        @param mode 排空或取消策略 / Drain or cancellation policy.
        @return None / None.
        @raises RuntimeError 从 worker 自身调用导致自等待 / If called from a worker, which would self-wait.
        @note 该方法幂等；CANCEL 会让所有尚未终结 ticket 进入 cancelled 状态。/
            This method is idempotent; CANCEL makes every unfinished ticket cancelled.
        """

        if not isinstance(mode, ShutdownMode):
            raise TypeError("mode must be a ShutdownMode")
        if asyncio.current_task() in self._worker_tasks:
            raise RuntimeError("shutdown cannot be called from a mailbox operation")
        if self._loop is not None:
            self._ensure_owner_loop()

        if mode is ShutdownMode.CANCEL:
            async with self._shutdown_lock:
                if self._state is RuntimeState.CLOSED:
                    return
                if self._state is RuntimeState.NEW:
                    self._state = RuntimeState.CLOSED
                    return
                await self._cancel_all_locked()
            return

        async with self._shutdown_lock:
            if self._state is RuntimeState.CLOSED:
                return
            if self._state is RuntimeState.NEW:
                self._state = RuntimeState.CLOSED
                return
            if self._state is RuntimeState.RUNNING:
                self._state = RuntimeState.DRAINING
                self._wake_event.set()
        try:
            await self._idle_event.wait()
        except asyncio.CancelledError:
            async with self._shutdown_lock:
                if self._state is not RuntimeState.CLOSED:
                    await self._cancel_all_locked()
            raise

        async with self._shutdown_lock:
            if self._state is not RuntimeState.CLOSED:
                await self._close_task_group_locked()

    def reap_idle(self) -> int:
        """@brief 立即回收超过 TTL 的空邮箱 / Immediately reap empty mailboxes older than the TTL.

        @return 本次回收邮箱数 / Number of mailboxes reaped.
        @note 正在执行、已有排队工作或处于 ready heap 的邮箱绝不会被回收。/
            Running, queued, or ready mailboxes are never reaped.
        """

        self._ensure_owner_loop()
        now = self._owner_loop().time()
        expired_keys = [
            key
            for key, mailbox in self._mailboxes.items()
            if not mailbox.running
            and not mailbox.queue
            and not mailbox.ready
            and mailbox.idle_since is not None
            and now - mailbox.idle_since >= self._idle_ttl
        ]
        for key in expired_keys:
            self._mailboxes.pop(key, None)
        return len(expired_keys)

    def _ensure_owner_loop(self) -> None:
        """@brief 断言调用发生在所属 event loop / Assert execution on the owning event loop.

        @return None / None.
        @raises RuntimeError 运行时未启动或调用来自其他 loop /
            If the runtime is not started or called from another loop.
        """

        current_loop = asyncio.get_running_loop()
        if self._loop is None:
            raise RuntimeError("runtime has not been started")
        if current_loop is not self._loop:
            raise RuntimeError("runtime cannot be used from another event loop")

    def _owner_loop(self) -> asyncio.AbstractEventLoop:
        """@brief 返回已校验的所属 event loop / Return the validated owning event loop.

        @return 所属 event loop / Owning event loop.
        @raises RuntimeError 运行时尚未绑定 loop / If the runtime is not bound to a loop.
        """

        if self._loop is None:
            raise RuntimeError("runtime has not been started")
        return self._loop

    def _mark_ready(self, key: AggregateKey, mailbox: _Mailbox) -> None:
        """@brief 将非空闲邮箱头部加入 ready heap / Put a non-running mailbox head on the ready heap.

        @param key 邮箱聚合键 / Mailbox aggregate key.
        @param mailbox 待标记邮箱 / Mailbox to mark ready.
        @return None / None.
        """

        if mailbox.running or mailbox.ready or not mailbox.queue:
            raise RuntimeError(
                "only a non-running, non-ready, non-empty mailbox can become ready"
            )
        head = mailbox.queue[0]
        mailbox.ready = True
        heapq.heappush(
            self._ready_heap,
            (int(head.priority), head.sequence, key),
        )
        self._wake_event.set()

    async def _take_next(self) -> _WorkItem[Any] | None:
        """@brief 领取下一个跨 key 优先、单 key FIFO 的工作 / Take the next cross-key-priority, per-key-FIFO work.

        @return 工作项；关停且无可领取工作时返回 None /
            Work item, or None when shutdown leaves no runnable work.
        """

        while True:
            while self._ready_heap:
                _priority, _sequence, key = heapq.heappop(self._ready_heap)
                mailbox = self._mailboxes.get(key)
                if (
                    mailbox is None
                    or not mailbox.ready
                    or mailbox.running
                    or not mailbox.queue
                ):
                    continue
                mailbox.ready = False
                mailbox.running = True
                item = mailbox.queue.popleft()
                self._active_count += 1
                self._active_items[item.sequence] = item
                return item

            if self._state is not RuntimeState.RUNNING and self._pending_count == 0:
                return None
            if self._state is RuntimeState.CANCELLING:
                return None

            self._wake_event.clear()
            await self._wake_event.wait()

    async def _worker_loop(self, worker_id: int) -> None:
        """@brief 固定 worker 的领取与执行循环 / Take-and-execute loop for one fixed worker.

        @param worker_id 仅用于诊断和 task 命名的序号 / Sequence used only for diagnostics and task naming.
        @return None / None.
        """

        del worker_id
        while True:
            item = await self._take_next()
            if item is None:
                return

            stop_worker = False
            try:
                await self._execute_item(item)
            except asyncio.CancelledError:
                if not item.completion.done():
                    item.completion.cancel()
                if self._state is RuntimeState.CANCELLING:
                    stop_worker = True
                else:
                    _clear_current_task_cancellation()
            finally:
                self._finish_item(item)
            if stop_worker:
                return

    async def _execute_item(self, item: _WorkItem[Any]) -> None:
        """@brief 隔离执行一个 operation 并终结其 Future / Execute one operation and settle its future in isolation.

        @param item 待执行工作 / Work item to execute.
        @return None / None.
        @note 普通异常（包括 ``SystemExit``）仅传播到 ticket，不会杀死固定 worker。/
            Ordinary failures (including ``SystemExit``) propagate only to the ticket and never kill a worker.
        """

        try:
            result = await item.operation()
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            if not item.completion.done():
                item.completion.set_exception(error)
        else:
            if not item.completion.done():
                item.completion.set_result(result)

    def _finish_item(self, item: _WorkItem[Any]) -> None:
        """@brief 维护工作终结后的调度不变量 / Maintain scheduler invariants after terminal work.

        @param item 已终结工作 / Terminal work item.
        @return None / None.
        """

        mailbox = self._mailboxes.get(item.key)
        if mailbox is None or not mailbox.running:
            raise RuntimeError("running mailbox disappeared before work completion")
        mailbox.running = False
        self._active_count -= 1
        self._active_items.pop(item.sequence, None)
        self._pending_count -= 1
        if mailbox.queue and self._state is not RuntimeState.CANCELLING:
            self._mark_ready(item.key, mailbox)
        else:
            mailbox.idle_since = self._owner_loop().time()
        if self._pending_count == 0:
            self._idle_event.set()
        self._wake_event.set()

    async def _reaper_loop(self) -> None:
        """@brief 按固定间隔回收空闲邮箱 / Reap idle mailboxes at a fixed interval.

        @return None / None.
        @note 使用 ``asyncio.timeout``，不会为每次 tick 创建新的 task。/
            Uses ``asyncio.timeout`` and creates no task per tick.
        """

        while not self._stop_reaper_event.is_set():
            try:
                async with asyncio.timeout(self._reap_interval):
                    await self._stop_reaper_event.wait()
            except TimeoutError:
                self.reap_idle()

    async def _cancel_all_locked(self) -> None:
        """@brief 在持有 shutdown lock 时取消全部工作 / Cancel all work while holding the shutdown lock.

        @return None / None.
        """

        self._state = RuntimeState.CANCELLING
        self._stop_reaper_event.set()
        self._ready_heap.clear()

        cancelled_queued = 0
        for mailbox in self._mailboxes.values():
            mailbox.ready = False
            while mailbox.queue:
                item = mailbox.queue.popleft()
                if not item.completion.done():
                    item.completion.cancel()
                cancelled_queued += 1
            if not mailbox.running:
                mailbox.idle_since = self._owner_loop().time()
        self._pending_count -= cancelled_queued
        if self._pending_count == 0:
            self._idle_event.set()

        for item in self._active_items.values():
            if not item.completion.done():
                item.completion.cancel()
        for task in self._worker_tasks:
            if not task.done():
                task.cancel()
        self._wake_event.set()
        await self._close_task_group_locked()

    async def _close_task_group_locked(self) -> None:
        """@brief 终结固定 task 集并释放全部索引 / Finish fixed tasks and release every index.

        @return None / None.
        @note 调用方必须持有 shutdown lock / Caller must hold the shutdown lock.
        """

        self._stop_reaper_event.set()
        self._wake_event.set()
        task_group = self._task_group
        if task_group is not None:
            await task_group.__aexit__(None, None, None)
        self._task_group = None
        self._worker_tasks.clear()
        self._reaper_task = None
        self._mailboxes.clear()
        self._ready_heap.clear()
        self._pending_count = 0
        self._active_count = 0
        self._active_items.clear()
        self._idle_event.set()
        self._state = RuntimeState.CLOSED


def _observe_completion[T](future: asyncio.Future[T]) -> None:
    """@brief 标记被忽略的 ticket 异常已观察 / Mark an ignored ticket exception as observed.

    @param future 已终结的工作 Future / Terminal work future.
    @return None / None.
    @note 观察异常只抑制 asyncio 警告；之后 ``ticket.wait`` 仍会原样抛出它。/
        Observing suppresses asyncio warnings; a later ``ticket.wait`` still raises unchanged.
    """

    if not future.cancelled():
        future.exception()


def _clear_current_task_cancellation() -> None:
    """@brief 隔离 operation 自发取消，保留固定 worker / Isolate operation self-cancellation and retain its fixed worker.

    @return None / None.
    @note 运行时关停取消不会走此路径 / Runtime-shutdown cancellation never takes this path.
    """

    current_task = asyncio.current_task()
    if current_task is None:
        return
    while current_task.cancelling():
        current_task.uncancel()
