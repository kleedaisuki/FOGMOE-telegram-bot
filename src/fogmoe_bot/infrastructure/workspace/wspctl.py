"""@brief wspctl native RuntimeProcess 的 fail-closed Workspace adapter / Fail-closed Workspace adapter for wspctl native RuntimeProcess."""

from __future__ import annotations

import asyncio
import importlib
import logging
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, cast
from uuid import uuid4

from fogmoe_bot.application.workspace.errors import (
    WorkspaceFileReplayNotFoundError,
    WorkspaceInvocationOutcomeUnknownError,
    WorkspaceRuntimeProtocolError,
    WorkspaceRuntimeUnavailableError,
)
from fogmoe_bot.application.workspace.models import (
    AddFileCommand,
    AddFileResult,
    ReplayFileCommand,
    RunBashCommand,
    RunBashResult,
)
from fogmoe_bot.application.workspace.ports import (
    RuntimeProcess,
    WorkspaceRuntimeRegistry,
)
from fogmoe_bot.domain.workspace.runtime import (
    WorkspaceRequestId,
    WorkspaceRuntimeKey,
)

_DEFAULT_IDLE_TTL_SECONDS = 15 * 60
"""@brief RuntimeProcess 空闲缓存时长（15 分钟） / Idle RuntimeProcess cache duration (15 minutes)."""

_DEFAULT_MAX_CONCURRENT_EXECUTIONS = 32
"""@brief 单一 Python Bot 进程允许的最大 native 执行数 / Maximum native executions admitted by one Python Bot process.

@note 此值与 ``wspctld`` 的有界 worker 容量保持一致；Python 侧过度排队不会增加
    broker 的真实吞吐，反而会放大不公平。/ This value is kept aligned with the bounded
    ``wspctld`` worker capacity; over-admitting in Python cannot increase broker throughput and
    amplifies unfairness instead.
"""

_DEFAULT_MAX_CONCURRENT_CLIENT_LIFECYCLE_OFFLOADS = 4
"""@brief 同时进入默认线程池的 native client 生命周期操作上限 /
Maximum native-client lifecycle operations admitted to the default thread pool.

@note client 创建与关闭不是用户命令，不能占用按 runtime 公平调度的 command slot；但它们仍
    可能阻塞在动态扩展加载或 Unix socket 清理上。因此以较小的独立隔舱限制其对默认线程池的
    占用。/ Client creation and closure are not user commands, so they must not consume the
    runtime-fair command slots; they can nevertheless block on dynamic-extension loading or
    Unix-socket cleanup. A small independent bulkhead therefore limits their use of the default
    thread pool.
"""

_LOGGER = logging.getLogger(__name__)
"""@brief 不记录 key、命令或输出的清理失败日志 / Cleanup-failure logger that records no key, command, or output."""


def _new_runtime_process_activation_id() -> str:
    """@brief 生成一个 RuntimeProcess cache entry 专属的 activation ID / Generate an activation ID owned by one RuntimeProcess cache entry.

    @return 仅供 native control plane 使用的安全 activation 标识 / Safe activation identifier used only by the native control plane.
    @note activation 是易失的 broker-session 细节，不是持久 Workspace command 的领域语义，
        因此不得进入 ``RunBashCommand`` 或 durable receipt。/ Activation is volatile
        broker-session detail rather than persistent Workspace-command domain semantics, so it
        must not enter ``RunBashCommand`` or a durable receipt.
    """

    return f"activation:{uuid4().hex}"


class NativeRuntimeProcess(Protocol):
    """@brief pybind ``RuntimeProcess`` 的最小安全调用面 / Minimal safe call surface of pybind ``RuntimeProcess``."""

    def execute(
        self,
        argv: list[str],
        stdin: str = "",
        cwd: str = "",
        timeout_ms: int = 0,
        output_limit: int = 0,
        request_id: str = "",
        request_hash: str = "",
    ) -> Mapping[str, object]:
        """@brief 经 native supervisor 执行一条命令 / Execute one command through the native supervisor.

        @param argv 不经 host shell 解析的 argv / argv not parsed by a host shell.
        @param stdin 命令标准输入 / Command standard input.
        @param cwd runtime 内工作目录 / Runtime-internal working directory.
        @param timeout_ms wall-clock deadline / Wall-clock deadline.
        @param output_limit stdout/stderr 合并输出预算 / Combined stdout/stderr output budget.
        @param request_id 稳定、可去重请求标识 / Stable deduplicable request identifier.
        @param request_hash 请求语义 SHA-256 / SHA-256 of request semantics.
        @return native result mapping / Native result mapping.
        """

        ...

    def add_file(
        self,
        opaque_id: str,
        chunks: Iterable[bytes],
        byte_size: int,
        sha256: str,
        request_id: str = "",
        request_hash: str = "",
    ) -> Mapping[str, object]:
        """@brief 经 native ingress 原子写入一段不解释文件 / Atomically write an uninterpreted file through native ingress.

        @param opaque_id 受限 uploads 目录的 opaque component / Opaque component of the constrained uploads directory.
        @param chunks 一次性 binary chunk iterable / Single-consumption binary chunk iterable.
        @param byte_size 声明的完整字节数 / Declared total byte count.
        @param sha256 完整内容的规范小写 SHA-256 / Canonical lowercase SHA-256 of complete content.
        @param request_id 稳定、可去重请求标识 / Stable deduplicable request identifier.
        @param request_hash 上传意图语义 SHA-256 / SHA-256 of upload-intent semantics.
        @return native 文件收据 mapping / Native file-receipt mapping.
        @note native 只能转运 bytes；它不得按 shebang、MIME、扩展名或内容选择 host 行为。
            Native can transport bytes only; it must not choose host behavior from shebang, MIME,
            extension, or content.
        """

        ...

    def replay_file(
        self,
        opaque_id: str,
        byte_size: int,
        sha256: str,
        request_id: str = "",
        request_hash: str = "",
    ) -> Mapping[str, object]:
        """@brief 只读查询已完成 payload journal / Read-only lookup of a completed payload journal.

        @param opaque_id 受限 uploads 目录的 opaque component / Opaque component of the constrained uploads directory.
        @param byte_size 已持久 import intent 的完整字节数 / Complete byte size from the persisted import intent.
        @param sha256 已持久 import intent 的完整 SHA-256 / Complete SHA-256 from the persisted import intent.
        @param request_id 原始稳定 journal 调用 ID / Original stable journal invocation ID.
        @param request_hash 原始上传意图语义 SHA-256 / SHA-256 of original upload-intent semantics.
        @return ``replayed=true`` 的 native 文件收据 mapping / Native file-receipt mapping with ``replayed=true``.
        @note 该调用没有 chunks，binding 不得创建 pending journal、读取 payload bytes 或激活
            runtime。/ This call has no chunks; the binding must not create a pending journal,
            read payload bytes, or activate a runtime.
        """

        ...

    def close(self) -> object:
        """@brief 关闭 native client 资源 / Close native client resources.

        @return native implementation-defined cleanup result / Native implementation-defined cleanup result.
        """

        ...


class RuntimeProcessFactory(Protocol):
    """@brief 为一个持久 key 与一次 activation 创建惰性 native client / Create a lazy native client for one persistent key and activation."""

    def create(
        self,
        key: WorkspaceRuntimeKey,
        activation_id: str,
    ) -> NativeRuntimeProcess:
        """@brief 创建已唯一绑定 activation、尚未执行命令的 native RuntimeProcess / Create a native RuntimeProcess uniquely bound to an activation and not yet executing a command.

        @param key 数据库登记的不透明 runtime key / Opaque runtime key registered in the database.
        @param activation_id 此新 handle 唯一拥有的安全 activation 标识 / Safe activation identifier uniquely owned by the new handle.
        @return native client / Native client.
        """

        ...


class WspctlRuntimeProcessFactory:
    """@brief 延迟导入 ``wspctl._native`` 的 RuntimeProcess 工厂 / RuntimeProcess factory that lazily imports ``wspctl._native``.

    @note 此工厂绝不回退为 ``subprocess``、host Bash 或 host Python；编译扩展缺失、ABI
        错误或 socket client 初始化失败都立即 fail closed。/ This factory never falls back to
        ``subprocess``, host Bash, or host Python; a missing compiled extension, ABI error, or
        socket-client initialization failure immediately fails closed.
    """

    def __init__(self, socket_path: str) -> None:
        """@brief 配置 wspctld Unix socket 路径 / Configure the wspctld Unix-socket path.

        @param socket_path host control-plane Unix socket path / Host control-plane Unix socket path.
        @return None / None.
        @raise TypeError socket 路径不是字符串时抛出 / Raised when the socket path is not a string.
        @raise ValueError socket 路径为空或含 NUL 时抛出 / Raised when the socket path is blank or contains NUL.
        """

        if not isinstance(socket_path, str):
            raise TypeError("wspctl socket path must be a string")
        if not socket_path.strip() or "\x00" in socket_path:
            raise ValueError("wspctl socket path must be non-blank and NUL-free")
        self._socket_path = socket_path
        """@brief 由受信任组合根提供的 control socket / Control socket supplied by the trusted composition root."""

    def create(
        self,
        key: WorkspaceRuntimeKey,
        activation_id: str,
    ) -> NativeRuntimeProcess:
        """@brief 惰性加载并构造 activation-bound pybind RuntimeProcess / Lazily load and construct an activation-bound pybind RuntimeProcess.

        @param key 数据库登记的 runtime key / Database-registered runtime key.
        @param activation_id 此 handle 的唯一 activation 标识 / Unique activation identifier of this handle.
        @return 尚未被命令激活的 native client / Native client not yet activated by a command.
        @raise WorkspaceRuntimeUnavailableError native module 或 client 无法安全使用时抛出 /
            Raised when the native module or client cannot be safely used.
        """

        try:
            native_module = importlib.import_module("wspctl._native")
            process_type = getattr(native_module, "RuntimeProcess")
            process = process_type(self._socket_path, str(key), activation_id)
        except Exception as error:
            raise WorkspaceRuntimeUnavailableError(
                "wspctl native runtime is unavailable",
                diagnostic_code=_native_error_code(error),
                diagnostic_message=_native_error_message(error),
            ) from error
        if (
            not callable(getattr(process, "execute", None))
            or not callable(getattr(process, "add_file", None))
            or not callable(getattr(process, "replay_file", None))
            or not callable(getattr(process, "close", None))
        ):
            raise WorkspaceRuntimeUnavailableError(
                "wspctl native runtime does not implement the required protocol"
            )
        return cast(NativeRuntimeProcess, process)


@dataclass(slots=True)
class _CachedRuntimeProcess:
    """@brief 一个 runtime key 的进程内缓存条目 / In-process cache entry for one runtime key.

    @param process native RuntimeProcess client / Native RuntimeProcess client.
    @param execution_lock 同 key workspace mutation 的串行化锁 / Serialization lock for workspace mutations with the same key.
    @param active_count 已借出的 workspace operation 数 / Number of borrowed workspace operations.
    @param idle_task 可取消的 15 分钟回收任务 / Cancellable 15-minute retirement task.
    @param idle_event 所有已借出任务结束时置位 / Set when all borrowed tasks have finished.
    @param close_lock 串行化 native client close 的锁 / Lock serializing native-client close.
    @param closed native client 是否已关闭 / Whether the native client has been closed.
    """

    process: NativeRuntimeProcess
    execution_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active_count: int = 0
    idle_task: asyncio.Task[None] | None = None
    idle_event: asyncio.Event = field(default_factory=asyncio.Event)
    close_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed: bool = False

    def __post_init__(self) -> None:
        """@brief 将新 cache entry 初始化为空闲状态 / Initialize a new cache entry as idle.

        @return None / None.
        """

        self.idle_event.set()


@dataclass(frozen=True, slots=True)
class _RuntimeProcessCreationOutcome:
    """@brief 单次异步 native client 创建的无异常结果 / Exception-free result of one asynchronous native-client creation.

    @param entry 成功创建并由 cache 接管的条目；失败时为 None /
        Entry successfully created and owned by the cache, or None on failure.
    @param error 失败根因；成功时为 None / Failure cause, or None on success.
    @note 将失败作为值而非 ``Future`` exception 保存，避免所有等待者取消时留下未读取的
        Future exception。/ Failure is stored as a value rather than a ``Future`` exception so
        cancelling every waiter cannot leave an unobserved Future exception.
    """

    entry: _CachedRuntimeProcess | None
    """@brief cache 已接管的 client 条目 / Cache-owned client entry."""
    error: Exception | None = field(default=None, repr=False)
    """@brief 创建失败的根因 / Root cause of a creation failure."""


@dataclass(slots=True)
class _PendingRuntimeProcess:
    """@brief 同一 runtime key 的合并中 native-client 创建 / Coalesced native-client creation for one runtime key.

    @param completion 创建完成后的值结果 Future / Value-result Future completed after creation.
    @param waiter_count 已承诺但尚未转为 cache lease 的调用者数 /
        Callers committed but not yet converted into cache leases.
    @param task 持有实际 ``to_thread`` 创建的后台任务 / Background task performing the actual ``to_thread`` creation.
    """

    completion: asyncio.Future[_RuntimeProcessCreationOutcome]
    """@brief 不抛异常的创建结局 / Non-throwing creation outcome."""
    waiter_count: int = 0
    """@brief 等待并预借该 entry 的调用者数 / Callers waiting for and pre-borrowing the entry."""
    task: asyncio.Task[None] | None = None
    """@brief 完成 factory 调用的后台任务 / Background task completing the factory call."""


@dataclass(slots=True)
class _RuntimeAdmissionQueue:
    """@brief 一个 runtime 在公平全局 admission 中的队列状态 / Queue state for one runtime in fair global admission.

    @param waiters 尚未获准的 FIFO 等待者 / FIFO waiters that have not received admission.
    @param queued runtime 是否已在全局 ready ring 中 / Whether the runtime is already in the global ready ring.
    @param active_waiter 当前持有 slot 的等待者 / Waiter currently holding the slot.
    """

    waiters: deque[asyncio.Future[None]] = field(default_factory=deque)
    """@brief 此 runtime 尚未获准的等待者 / Waiters not yet admitted for this runtime."""
    queued: bool = False
    """@brief 是否已加入全局 ready ring / Whether this runtime is in the global ready ring."""
    active_waiter: asyncio.Future[None] | None = None
    """@brief 当前 slot 的唯一持有者 / Sole current holder of this runtime's slot."""


class _RuntimeAdmissionLease:
    """@brief 由公平调度器发放的一次性 slot lease / One-shot slot lease issued by the fair scheduler.

    Lease 绑定具体等待者，而非只有 runtime key；这让取消与重复释放都能精确检测，避免一个
    已过期任务误归还另一个任务的 slot。/ A lease is bound to its exact waiter rather than only
    a runtime key, so cancellation and duplicate release are detected precisely and a stale task
    cannot release another task's slot.
    """

    def __init__(
        self,
        scheduler: _FairRuntimeAdmissionScheduler,
        key: WorkspaceRuntimeKey,
        waiter: asyncio.Future[None],
    ) -> None:
        """@brief 创建已获准的 slot lease / Create an admitted slot lease.

        @param scheduler 发放 lease 的调度器 / Scheduler that issued the lease.
        @param key 该 lease 所属 runtime key / Runtime key that owns this lease.
        @param waiter 对应的唯一等待者 token / Unique corresponding waiter token.
        @return None / None.
        """

        self._scheduler = scheduler
        """@brief lease 所属的调度器 / Scheduler owning this lease."""
        self._key = key
        """@brief lease 所属 runtime / Runtime owning this lease."""
        self._waiter = waiter
        """@brief 由调度器认定的持有者 token / Holder token recognized by the scheduler."""
        self._released = False
        """@brief 是否已归还 slot / Whether the slot has been returned."""

    async def release(self) -> None:
        """@brief 恰好一次地归还 slot / Return the slot exactly once.

        @return None / None.
        @raise RuntimeError lease 被重复归还时抛出 / Raised when the lease is returned twice.
        """

        if self._released:
            raise RuntimeError("Workspace runtime admission lease released twice")
        self._released = True
        await self._scheduler._release(self._key, self._waiter)


class _FairRuntimeAdmissionScheduler:
    """@brief 按 runtime 轮转的有界全局执行调度器 / Bounded global execution scheduler rotating by runtime.

    一个 runtime 最多持有一个 native slot。每次释放时，该 runtime 若仍有等待者，只能从
    ready ring 的尾部重新排队；因此一个高频 scope 不会反复抢走刚归还的 slot。不同 runtime
    的 ready heads 以 FIFO 顺序调度。/ A runtime holds at most one native slot. On release, a
    runtime with more waiters re-enters only at the tail of the ready ring, so a noisy scope
    cannot repeatedly recapture a just-returned slot. Ready heads from distinct runtimes are
    scheduled FIFO.
    """

    def __init__(self, capacity: int) -> None:
        """@brief 创建固定容量的公平调度器 / Create a fixed-capacity fair scheduler.

        @param capacity 同时获准的最大 runtime 数 / Maximum number of simultaneously admitted runtimes.
        @return None / None.
        @raise ValueError 容量不为正时抛出 / Raised when capacity is not positive.
        """

        if capacity < 1:
            raise ValueError("Workspace admission capacity must be positive")
        self._capacity = capacity
        """@brief 固定全局 slot 上限 / Fixed global slot limit."""
        self._active_count = 0
        """@brief 已发放但未归还的 slot 数 / Number of issued but unreleased slots."""
        self._queues: dict[WorkspaceRuntimeKey, _RuntimeAdmissionQueue] = {}
        """@brief 每个 runtime 的等待队列 / Wait queue for each runtime."""
        self._ready_keys: deque[WorkspaceRuntimeKey] = deque()
        """@brief 不同 runtime ready head 的 FIFO ring / FIFO ring of ready heads from distinct runtimes."""
        self._lock = asyncio.Lock()
        """@brief 保护计数器与所有 queue/ring 转换 / Guards counters and all queue/ring transitions."""

    async def acquire(self, key: WorkspaceRuntimeKey) -> _RuntimeAdmissionLease:
        """@brief 等待该 runtime 的下一次公平 slot / Wait for this runtime's next fair slot.

        @param key 请求执行的 runtime key / Runtime key requesting execution.
        @return 需要由调用方在 native 调用结束后归还的 lease / Lease to return after the native call.
        @raise asyncio.CancelledError 排队期间取消时抛出，且绝不遗留 slot / Raised on cancellation while queued, without leaking a slot.
        """

        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        async with self._lock:
            queue = self._queues.setdefault(key, _RuntimeAdmissionQueue())
            queue.waiters.append(waiter)
            self._enqueue_ready_locked(key, queue)
            self._grant_available_locked()
        try:
            # Do not let cancellation cancel the scheduler-owned Future.  The cancellation path
            # below can then distinguish an ungranted queue node from the narrow race where this
            # task was granted a slot immediately before its caller stopped waiting.
            await asyncio.shield(waiter)
        except asyncio.CancelledError:
            await self._abandon_waiter(key, waiter)
            raise
        return _RuntimeAdmissionLease(self, key, waiter)

    async def _release(
        self,
        key: WorkspaceRuntimeKey,
        waiter: asyncio.Future[None],
    ) -> None:
        """@brief 归还一个已发放的 runtime slot / Return one issued runtime slot.

        @param key lease 所属 runtime key / Runtime key owning the lease.
        @param waiter lease 的唯一 waiter token / Unique waiter token of the lease.
        @return None / None.
        @raise RuntimeError token 与当前 slot 不匹配时抛出 / Raised when the token does not match the active slot.
        """

        async with self._lock:
            queue = self._queues.get(key)
            if queue is None or queue.active_waiter is not waiter:
                raise RuntimeError("Workspace runtime admission lease is not active")
            queue.active_waiter = None
            self._active_count -= 1
            if self._active_count < 0:
                raise RuntimeError("Workspace runtime admission slot underflow")
            self._enqueue_ready_locked(key, queue)
            self._drop_empty_queue_locked(key, queue)
            self._grant_available_locked()

    async def _abandon_waiter(
        self,
        key: WorkspaceRuntimeKey,
        waiter: asyncio.Future[None],
    ) -> None:
        """@brief 取消排队者，或归还取消竞态中已授予的 slot / Cancel a waiter or return a slot granted during a cancellation race.

        @param key waiter 所属 runtime key / Runtime key owning the waiter.
        @param waiter 被取消的唯一 waiter token / Unique waiter token being cancelled.
        @return None / None.
        """

        async with self._lock:
            queue = self._queues.get(key)
            if queue is None:
                return
            if queue.active_waiter is waiter:
                queue.active_waiter = None
                self._active_count -= 1
                if self._active_count < 0:
                    raise RuntimeError("Workspace runtime admission slot underflow")
            else:
                if not waiter.done():
                    waiter.cancel()
                try:
                    queue.waiters.remove(waiter)
                except ValueError:
                    pass
            self._enqueue_ready_locked(key, queue)
            self._drop_empty_queue_locked(key, queue)
            self._grant_available_locked()

    def _enqueue_ready_locked(
        self,
        key: WorkspaceRuntimeKey,
        queue: _RuntimeAdmissionQueue,
    ) -> None:
        """@brief 将可运行 runtime 的 head 追加到 ready ring 尾部 / Append one runnable runtime head to the ready-ring tail.

        @param key runtime key / Runtime key.
        @param queue 该 runtime 的队列状态 / Queue state for the runtime.
        @return None / None.
        @note 调用者必须持有 ``_lock``。/ The caller must hold ``_lock``.
        """

        self._discard_cancelled_waiters_locked(queue)
        if queue.active_waiter is None and queue.waiters and not queue.queued:
            self._ready_keys.append(key)
            queue.queued = True

    def _grant_available_locked(self) -> None:
        """@brief 按 ready ring FIFO 发放所有可用 slot / Issue every available slot in ready-ring FIFO order.

        @return None / None.
        @note 调用者必须持有 ``_lock``。/ The caller must hold ``_lock``.
        """

        while self._active_count < self._capacity and self._ready_keys:
            key = self._ready_keys.popleft()
            queue = self._queues.get(key)
            if queue is None:
                continue
            queue.queued = False
            if queue.active_waiter is not None:
                continue
            self._discard_cancelled_waiters_locked(queue)
            if not queue.waiters:
                self._drop_empty_queue_locked(key, queue)
                continue
            waiter = queue.waiters.popleft()
            if waiter.done():
                self._enqueue_ready_locked(key, queue)
                continue
            queue.active_waiter = waiter
            self._active_count += 1
            waiter.set_result(None)

    @staticmethod
    def _discard_cancelled_waiters_locked(queue: _RuntimeAdmissionQueue) -> None:
        """@brief 丢弃队首已取消的 waiter / Discard cancelled waiters from a queue head.

        @param queue 待整理的 runtime queue / Runtime queue to compact.
        @return None / None.
        @note 仅调度器持有 waiter 的引用；已取消 waiter 可以安全地延迟到队首清理。/
            The scheduler is the only owner of waiter references, so cancelled waiters can safely
            be cleaned lazily at the queue head.
        """

        while queue.waiters and queue.waiters[0].cancelled():
            queue.waiters.popleft()

    def _drop_empty_queue_locked(
        self,
        key: WorkspaceRuntimeKey,
        queue: _RuntimeAdmissionQueue,
    ) -> None:
        """@brief 删除既不活跃也无等待者的 runtime queue / Drop a runtime queue that is neither active nor waiting.

        @param key runtime key / Runtime key.
        @param queue 候选 queue / Candidate queue.
        @return None / None.
        @note 调用者必须持有 ``_lock``。/ The caller must hold ``_lock``.
        """

        if (
            queue.active_waiter is None
            and not queue.waiters
            and not queue.queued
            and self._queues.get(key) is queue
        ):
            del self._queues[key]


class _NativeClientLifecycleOffloadGate:
    """@brief 为 native client 创建和关闭提供有界默认线程池准入 /
    Bounded default-thread-pool admission for native-client creation and closure.

    此隔舱故意不复用 command 的 ``_FairRuntimeAdmissionScheduler``：创建和关闭没有
    user-visible command 顺序语义，若占用 command ready ring 会反而改变不同 workspace 的
    Bash 公平性。它只限制真正已投递到默认 executor 的 lifecycle 调用数；调用方取消后，slot
    仍保留到线程真实返回。/ This bulkhead intentionally does not reuse the command
    ``_FairRuntimeAdmissionScheduler``: creation and closure have no user-visible command-order
    semantics, and occupying the command ready ring would distort Bash fairness across
    workspaces. It limits only lifecycle calls actually submitted to the default executor; a
    cancelled caller still retains its slot until the thread truly returns.
    """

    def __init__(
        self,
        capacity: int = _DEFAULT_MAX_CONCURRENT_CLIENT_LIFECYCLE_OFFLOADS,
    ) -> None:
        """@brief 创建固定容量的 client 生命周期隔舱 / Create a fixed-capacity client-lifecycle bulkhead.

        @param capacity 同时运行的创建或关闭调用数 / Number of simultaneously running create or close calls.
        @return None / None.
        @raise ValueError 容量不为正时抛出 / Raised when capacity is not positive.
        """

        if capacity < 1:
            raise ValueError(
                "Native client lifecycle-offload capacity must be positive"
            )
        self._slots = asyncio.BoundedSemaphore(capacity)
        """@brief 默认 executor 的已提交 lifecycle 调用槽位 / Slots for submitted default-executor lifecycle calls."""
        self._pending_tasks: set[asyncio.Task[object]] = set()
        """@brief 尚未真实结束的 worker 任务 / Worker tasks that have not truly completed."""

    async def call[T](self, operation: Callable[[], T]) -> T:
        """@brief 在已获准 slot 中运行一个同步生命周期操作 / Run one synchronous lifecycle operation in an admitted slot.

        @param operation 无参数的同步创建或关闭操作 / Zero-argument synchronous create or close operation.
        @return 同步操作的返回值 / Return value from the synchronous operation.
        @note ``asyncio.to_thread`` 不能杀死已经开始的线程；故 worker 完成回调，而不是
            awaiter 的取消路径，负责归还 slot。/ ``asyncio.to_thread`` cannot kill a thread
            that has already started; the worker completion callback, rather than an awaiter's
            cancellation path, returns the slot.
        """

        await self._slots.acquire()
        try:
            task = asyncio.create_task(
                asyncio.to_thread(operation),
                name="workspace.native-client-lifecycle",
            )
        except BaseException:
            self._slots.release()
            raise
        owned_task = cast(asyncio.Task[object], task)
        self._pending_tasks.add(owned_task)
        owned_task.add_done_callback(self._release_when_finished)
        return await asyncio.shield(task)

    def _release_when_finished(self, task: asyncio.Task[object]) -> None:
        """@brief 在 worker 真实结束时归还 lifecycle slot / Return a lifecycle slot when its worker truly finishes.

        @param task 已终结的默认 executor worker / Completed default-executor worker.
        @return None / None.
        """

        self._pending_tasks.discard(task)
        self._slots.release()
        if not task.cancelled():
            task.exception()


class WspctlRuntimeProcess(RuntimeProcess):
    """@brief registry + wspctl RuntimeProcess 的惰性激活适配器 / Lazy-activation adapter built from a registry and wspctl RuntimeProcess.

    每个持久 key 在一个 Python Bot process 内只缓存一个 native client；同 key 的 Bash
    命令串行执行，避免共同 Workspace 的并发写入产生未定义的 Overlay 观察。不同 keys 可在
    有界全局并发下并行。/ Each persistent key caches one native client inside one Python Bot
    process; Bash commands with the same key execute serially so concurrent writes to a shared
    Workspace cannot create undefined Overlay observations. Different keys run in bounded global
    parallelism.
    """

    def __init__(
        self,
        *,
        registry: WorkspaceRuntimeRegistry,
        process_factory: RuntimeProcessFactory,
        idle_ttl_seconds: float = _DEFAULT_IDLE_TTL_SECONDS,
        max_concurrent_executions: int = _DEFAULT_MAX_CONCURRENT_EXECUTIONS,
    ) -> None:
        """@brief 创建 Workspace RuntimeProcess 适配器 / Create the Workspace RuntimeProcess adapter.

        @param registry 可恢复 scope/key 映射 / Recoverable scope/key mapping.
        @param process_factory native RuntimeProcess 工厂 / Native RuntimeProcess factory.
        @param idle_ttl_seconds 命令结束后缓存 client 的秒数 / Seconds to cache a client after command completion.
        @param max_concurrent_executions 进程级同时 native command 数 / Process-wide number of simultaneous native commands.
        @return None / None.
        @raise ValueError 缓存 TTL 或并发限制不为正时抛出 /
            Raised when cache TTL or concurrency limit is not positive.
        """

        if idle_ttl_seconds <= 0:
            raise ValueError("Workspace idle_ttl_seconds must be positive")
        if max_concurrent_executions < 1:
            raise ValueError("Workspace max_concurrent_executions must be positive")
        self._registry = registry
        """@brief 数据库支持的持久 scope/key registry / Database-backed persistent scope/key registry."""
        self._process_factory = process_factory
        """@brief fail-closed native client factory / Fail-closed native client factory."""
        self._idle_ttl_seconds = idle_ttl_seconds
        """@brief native client 的空闲回收阈值 / Idle-retirement threshold for native clients."""
        self._cache: dict[WorkspaceRuntimeKey, _CachedRuntimeProcess] = {}
        """@brief runtime key 到本进程 native client 的缓存 / Cache from runtime key to this-process native client."""
        self._pending_creations: dict[WorkspaceRuntimeKey, _PendingRuntimeProcess] = {}
        """@brief 每个 key 至多一个合并中的 native client 创建 / At most one coalesced native-client creation per key."""
        self._cache_lock = asyncio.Lock()
        """@brief 保护 cache 与借出计数的异步锁 / Async lock protecting cache and borrow counts."""
        self._execution_admission = _FairRuntimeAdmissionScheduler(
            max_concurrent_executions
        )
        """@brief 不同 runtime key 间按 FIFO 轮转的有界 native 调用 slot / FIFO-rotated bounded native-call slots across runtime keys."""
        self._client_lifecycle_offloads = _NativeClientLifecycleOffloadGate()
        """@brief 不改变 command 公平性的 native client 创建/关闭隔舱 /
        Native-client create/close bulkhead that does not alter command fairness."""
        self._closed = False
        """@brief runner 是否停止接收新命令 / Whether the runner has stopped accepting new commands."""

    async def run_bash(self, command: RunBashCommand) -> RunBashResult:
        """@brief 解析 runtime key、惰性创建 client 并执行 Bash / Resolve a runtime key, lazily create a client, and execute Bash.

        @param command 已验证的 Bash 执行命令 / Validated Bash execution command.
        @return native journal 执行或回放的规范结果 / Canonical result executed or replayed by the native journal.
        @raise WorkspaceRuntimeUnavailableError native runtime 不可用或 runner 已关闭时抛出 /
            Raised when the native runtime is unavailable or the runner is closed.
        @raise WorkspaceRuntimeProtocolError native result 违反固定协议时抛出 /
            Raised when the native result violates the fixed protocol.
        @note 调用方取消不会取消已发送到 native supervisor 的命令；独立任务会继续持有
            per-runtime lock 和 cache lease，直到 native 调用真正返回。/ Caller cancellation
            does not cancel a command already sent to the native supervisor; an independent task
            keeps the per-runtime lock and cache lease until the native call really returns.
        """

        runtime = await self._registry.resolve(command.scope)
        if not runtime.belongs_to(command.scope):
            raise WorkspaceRuntimeUnavailableError(
                "Workspace runtime registry returned a cross-scope binding"
            )
        entry = await self._acquire(runtime.key)
        try:
            task = asyncio.create_task(
                self._execute_entry(runtime.key, entry, command),
                name="workspace.run-bash",
            )
        except BaseException:
            await self._release(runtime.key, entry)
            raise
        task.add_done_callback(
            lambda completed: self._schedule_release(runtime.key, entry, completed)
        )
        return await asyncio.shield(task)

    async def add_file(self, command: AddFileCommand) -> AddFileResult:
        """@brief 解析 runtime key、惰性创建 client 并原子写入文件 / Resolve a runtime key, lazily create a client, and atomically write a file.

        @param command 已验证的文件写入命令 / Validated file-ingress command.
        @return native journal 已写入或已回放的规范收据 / Canonical receipt written or replayed by the native journal.
        @raise WorkspaceRuntimeUnavailableError native runtime 不可用或 runner 已关闭时抛出 /
            Raised when the native runtime is unavailable or the runner is closed.
        @raise WorkspaceRuntimeProtocolError native result 违反固定协议时抛出 /
            Raised when the native result violates the fixed protocol.
        @note 此路径刻意使用与 ``run_bash`` 相同的 cache lease、``execution_lock`` 与
            fairness admission；文件写入和 Bash 不会并发写同一个 OverlayFS workspace。/ This path
            intentionally uses the same cache lease, ``execution_lock``, and fairness admission
            as ``run_bash``; file ingress and Bash never write the same OverlayFS workspace concurrently.
        """

        runtime = await self._registry.resolve(command.scope)
        if not runtime.belongs_to(command.scope):
            raise WorkspaceRuntimeUnavailableError(
                "Workspace runtime registry returned a cross-scope binding"
            )
        entry = await self._acquire(runtime.key)
        try:
            task = asyncio.create_task(
                self._add_file_entry(runtime.key, entry, command),
                name="workspace.add-file",
            )
        except BaseException:
            await self._release(runtime.key, entry)
            raise
        task.add_done_callback(
            lambda completed: self._schedule_release(runtime.key, entry, completed)
        )
        return await asyncio.shield(task)

    async def replay_file(self, command: ReplayFileCommand) -> AddFileResult:
        """@brief 查询一个已完成 payload journal 而不创建 native 副作用 / Query a completed payload journal without creating a native side effect.

        @param command 只含 immutable intent metadata 的只读 replay command /
            Read-only replay command containing only immutable intent metadata.
        @return 已验证的 ``replayed=true`` 文件收据 / Verified file receipt with ``replayed=true``.
        @raise WorkspaceFileReplayNotFoundError native 明确证实 journal 不存在时抛出 /
            Raised when native explicitly proves the journal does not exist.
        @raise WorkspaceInvocationOutcomeUnknownError native 发现 pending 或不可证明 payload 时抛出 /
            Raised when native finds pending or an unprovable payload.
        @raise WorkspaceRuntimeUnavailableError native runtime 或 runner 不可安全使用时抛出 /
            Raised when native runtime or runner cannot be used safely.
        @note client 对象会像其他 command 一样复用 cache/lock/admission，但 native RPC 本身不
            含 activation/chunks，因而不会启动 RuntimeProcess 或创建 pending journal。/ The
            client object shares cache/lock/admission with other commands, but the native RPC
            itself carries neither activation nor chunks, so it cannot start a RuntimeProcess or
            create a pending journal.
        """

        runtime = await self._registry.resolve(command.scope)
        if not runtime.belongs_to(command.scope):
            raise WorkspaceRuntimeUnavailableError(
                "Workspace runtime registry returned a cross-scope binding"
            )
        entry = await self._acquire(runtime.key)
        try:
            task = asyncio.create_task(
                self._replay_file_entry(runtime.key, entry, command),
                name="workspace.replay-file",
            )
        except BaseException:
            await self._release(runtime.key, entry)
            raise
        task.add_done_callback(
            lambda completed: self._schedule_release(runtime.key, entry, completed)
        )
        return await asyncio.shield(task)

    async def close(self) -> None:
        """@brief 停止新命令、排空已借出命令并关闭所有 client / Stop new commands, drain borrowed commands, and close all clients.

        @return None / None.
        @note 关闭采用 graceful drain：不能在 host command 仍运行时抢先关闭其 RPC client。
        调用者若需要强制终止，必须向 native control plane 发出显式管理请求。/ Closing is a
        graceful drain: it does not close an RPC client before its host command has finished.
        A caller requiring forced termination must issue an explicit native-control-plane request.
        """

        async with self._cache_lock:
            if self._closed:
                return
            self._closed = True
            entries = tuple(self._cache.values())
            self._cache.clear()
            for entry in entries:
                _cancel_idle_retirement(entry)
        await asyncio.gather(*(entry.idle_event.wait() for entry in entries))
        await asyncio.gather(
            *(self._close_process(entry) for entry in entries),
            return_exceptions=True,
        )

    async def detach(self) -> None:
        """@brief 停止接收新命令并立即脱离 broker-owned active command / Stop accepting commands and immediately detach from broker-owned active commands.

        @return None / None.
        @note 这是 Bot 进程终止路径，不是 native task kill。已完整发送给 broker 的命令由
            broker 的 journal、PID 1 与 cgroup 继续负责；本方法只关闭空闲 client，绝不等待
            最长 300 秒的 active command。/ This is the Bot-process termination path, not a
            native-task kill. Commands already fully sent to the broker remain owned by its
            journal, PID 1, and cgroup; this method closes only idle clients and never waits for
            a potentially 300-second active command.
        """

        async with self._cache_lock:
            if self._closed:
                return
            self._closed = True
            entries = tuple(self._cache.values())
            self._cache.clear()
            for entry in entries:
                _cancel_idle_retirement(entry)
            inactive_entries = tuple(
                entry for entry in entries if entry.active_count == 0
            )
        await asyncio.gather(
            *(self._close_process(entry) for entry in inactive_entries),
            return_exceptions=True,
        )

    async def _acquire(self, key: WorkspaceRuntimeKey) -> _CachedRuntimeProcess:
        """@brief 借出一个 runtime cache entry / Borrow one runtime cache entry.

        @param key 已登记的不透明 runtime key / Registered opaque runtime key.
        @return 已增加 lease count 的 cache entry / Cache entry with incremented lease count.
        @raise WorkspaceRuntimeUnavailableError runner 已关闭时抛出 / Raised when the runner is closed.
        """

        async with self._cache_lock:
            if self._closed:
                raise WorkspaceRuntimeUnavailableError("Workspace runner is closed")
            entry = self._cache.get(key)
            if entry is not None:
                self._borrow_cached_entry_locked(entry)
                return entry
            pending = self._pending_creations.get(key)
            if pending is None:
                pending = _PendingRuntimeProcess(
                    completion=asyncio.get_running_loop().create_future()
                )
                self._pending_creations[key] = pending
                pending.task = asyncio.create_task(
                    self._create_pending_runtime_process(key, pending),
                    name="workspace.create-runtime-process",
                )
            pending.waiter_count += 1

        try:
            outcome = await asyncio.shield(pending.completion)
            if outcome.entry is None:
                self._raise_creation_failure(outcome)
            entry = outcome.entry
            assert entry is not None
            async with self._cache_lock:
                if not self._closed and self._cache.get(key) is entry:
                    return entry
                close_after_release = self._release_entry_locked(key, entry)
            if close_after_release:
                await self._close_process(entry)
            raise WorkspaceRuntimeUnavailableError("Workspace runner is closed")
        except asyncio.CancelledError:
            await self._abandon_pending_borrow(key, pending)
            raise

    async def _create_pending_runtime_process(
        self,
        key: WorkspaceRuntimeKey,
        pending: _PendingRuntimeProcess,
    ) -> None:
        """@brief 在 cache 锁外创建一个 native client，再原子交接给等待者 / Create one native client outside the cache lock, then atomically hand it to waiters.

        @param key 待创建 client 的 runtime key / Runtime key for the client being created.
        @param pending 此 key 的唯一合并创建记录 / The sole coalesced creation record for this key.
        @return None / None.
        @note 首次 Unix socket client 构造可能阻塞或触发动态扩展加载；它绝不能持有全局
            ``_cache_lock``，否则一个 runtime 会把所有其他 scope 的惰性激活串行化。/
            Initial Unix-socket client construction may block or load a dynamic extension; it
            must never hold the global ``_cache_lock``, otherwise one runtime serializes lazy
            activation for every other scope.
        """

        #: @brief 即将绑定给新 native handle 的唯一 activation / Unique activation about to be bound to the new native handle.
        activation_id = _new_runtime_process_activation_id()
        try:
            process = await self._client_lifecycle_offloads.call(
                lambda: self._process_factory.create(
                    key,
                    activation_id,
                )
            )
        except BaseException as error:
            cause = (
                error
                if isinstance(error, Exception)
                else RuntimeError("native RuntimeProcess creation was cancelled")
            )
            async with self._cache_lock:
                if self._pending_creations.get(key) is pending:
                    del self._pending_creations[key]
                if not pending.completion.done():
                    pending.completion.set_result(
                        _RuntimeProcessCreationOutcome(entry=None, error=cause)
                    )
            return

        close_unclaimed = False
        async with self._cache_lock:
            if self._pending_creations.get(key) is pending:
                del self._pending_creations[key]
            if self._closed:
                close_unclaimed = True
                outcome = _RuntimeProcessCreationOutcome(
                    entry=None,
                    error=WorkspaceRuntimeUnavailableError(
                        "Workspace runner is closed"
                    ),
                )
            elif pending.waiter_count == 0:
                # Every caller cancelled before creation finished.  A client with no borrower is
                # not worth retaining for the idle TTL; close it now rather than keeping an
                # otherwise unowned privileged Unix-socket handle alive.
                close_unclaimed = True
                outcome = _RuntimeProcessCreationOutcome(
                    entry=None,
                    error=WorkspaceRuntimeUnavailableError(
                        "Workspace RuntimeProcess creation lost every requester"
                    ),
                )
            else:
                entry = _CachedRuntimeProcess(process=process)
                entry.active_count = pending.waiter_count
                entry.idle_event.clear()
                self._cache[key] = entry
                outcome = _RuntimeProcessCreationOutcome(entry=entry)
            if not pending.completion.done():
                pending.completion.set_result(outcome)
        if close_unclaimed:
            await self._close_unclaimed_process(process)

    def _borrow_cached_entry_locked(self, entry: _CachedRuntimeProcess) -> None:
        """@brief 将一个已缓存 client 借给新调用者 / Borrow an already-cached client for one new caller.

        @param entry 待借出的 cache 条目 / Cache entry to borrow.
        @return None / None.
        @note 调用者必须持有 ``_cache_lock``。/ The caller must hold ``_cache_lock``.
        """

        _cancel_idle_retirement(entry)
        entry.active_count += 1
        entry.idle_event.clear()

    def _release_entry_locked(
        self,
        key: WorkspaceRuntimeKey,
        entry: _CachedRuntimeProcess,
    ) -> bool:
        """@brief 在锁内归还一个已预借的 cache entry / Return one pre-borrowed cache entry while holding the lock.

        @param key entry 所属 runtime key / Runtime key owning the entry.
        @param entry 待归还的 cache entry / Cache entry to return.
        @return 若调用方应在锁外 close client 则为 True / ``True`` when the caller should close the client outside the lock.
        @raise RuntimeError 借出计数下溢时抛出 / Raised when the borrow count underflows.
        @note 调用者必须持有 ``_cache_lock``。/ The caller must hold ``_cache_lock``.
        """

        if entry.active_count <= 0:
            raise RuntimeError("Workspace runtime cache lease underflow")
        entry.active_count -= 1
        if entry.active_count != 0:
            return False
        entry.idle_event.set()
        if self._closed or self._cache.get(key) is not entry:
            return True
        entry.idle_task = asyncio.create_task(
            self._retire_after_idle(key, entry),
            name="workspace.retire-runtime-process",
        )
        return False

    async def _abandon_pending_borrow(
        self,
        key: WorkspaceRuntimeKey,
        pending: _PendingRuntimeProcess,
    ) -> None:
        """@brief 撤销在 client 创建期间取消的预借计数 / Undo a pre-borrow count when a caller cancels during client creation.

        @param key pending client 所属 runtime key / Runtime key of the pending client.
        @param pending 调用者曾等待的合并创建记录 / Coalesced creation record awaited by the caller.
        @return None / None.
        @note completion 与 cancellation 可以竞争：若 entry 已交接给 cache，本方法会归还
            entry lease；否则只减少 pending waiter。/ Completion and cancellation can race: if
            the entry was already handed to the cache, this method returns the entry lease;
            otherwise it only decrements the pending waiter count.
        """

        close_after_release = False
        entry_to_close: _CachedRuntimeProcess | None = None
        async with self._cache_lock:
            if self._pending_creations.get(key) is pending:
                if pending.waiter_count <= 0:
                    raise RuntimeError("Workspace pending runtime waiter underflow")
                pending.waiter_count -= 1
                return
            if not pending.completion.done():
                return
            outcome = pending.completion.result()
            entry = outcome.entry
            if entry is None:
                return
            close_after_release = self._release_entry_locked(key, entry)
            entry_to_close = entry
        if close_after_release and entry_to_close is not None:
            await self._close_process(entry_to_close)

    @staticmethod
    def _raise_creation_failure(
        outcome: _RuntimeProcessCreationOutcome,
    ) -> None:
        """@brief 将合并创建失败恢复为 fail-closed 应用异常 / Reconstitute a coalesced creation failure as a fail-closed application exception.

        @param outcome 已完成的创建结局 / Completed creation outcome.
        @return 此函数永不返回 / This function never returns.
        @raise WorkspaceRuntimeUnavailableError native client 无法安全创建时抛出 /
            Raised when the native client cannot be safely created.
        """

        if outcome.entry is not None:
            raise AssertionError(
                "Successful runtime creation cannot be raised as a failure"
            )
        if outcome.error is None:
            raise WorkspaceRuntimeUnavailableError(
                "wspctl RuntimeProcess creation failed without an error"
            )
        diagnostic_code, diagnostic_message = _workspace_error_diagnostics(
            outcome.error
        )
        raise WorkspaceRuntimeUnavailableError(
            "wspctl RuntimeProcess creation failed",
            diagnostic_code=diagnostic_code,
            diagnostic_message=diagnostic_message,
        ) from outcome.error

    async def _execute_entry(
        self,
        key: WorkspaceRuntimeKey,
        entry: _CachedRuntimeProcess,
        command: RunBashCommand,
    ) -> RunBashResult:
        """@brief 在持有 per-runtime 锁与公平全局 slot 时调用 native 进程 / Call the native process while holding its lock and a fair global slot.

        @param key 请求的 runtime key / Runtime key requested for execution.
        @param entry 已借出的 runtime cache entry / Borrowed runtime cache entry.
        @param command 已验证 Bash 命令 / Validated Bash command.
        @return 规范执行结果 / Canonical execution result.
        """

        # The native handle owned by this cache entry binds the session identity at construction:
        # it is stable across many Turns, while request_id remains Turn+invocation scoped for
        # journal idempotency.  It must not be part of the transport-neutral application command.
        async with entry.execution_lock:
            lease = await self._execution_admission.acquire(key)
            try:
                return await asyncio.to_thread(
                    _execute_native_process,
                    entry.process,
                    command,
                )
            finally:
                await lease.release()

    async def _add_file_entry(
        self,
        key: WorkspaceRuntimeKey,
        entry: _CachedRuntimeProcess,
        command: AddFileCommand,
    ) -> AddFileResult:
        """@brief 在与 Bash 相同的 serial/fair admission 下写入文件 / Write a file under the same serial and fair admission as Bash.

        @param key 请求的 runtime key / Runtime key requested for file ingress.
        @param entry 已借出的 runtime cache entry / Borrowed runtime cache entry.
        @param command 已验证文件写入命令 / Validated file-ingress command.
        @return 规范文件收据 / Canonical file receipt.
        """

        async with entry.execution_lock:
            lease = await self._execution_admission.acquire(key)
            try:
                return await asyncio.to_thread(
                    _add_file_native_process,
                    entry.process,
                    command,
                )
            finally:
                await lease.release()

    async def _replay_file_entry(
        self,
        key: WorkspaceRuntimeKey,
        entry: _CachedRuntimeProcess,
        command: ReplayFileCommand,
    ) -> AddFileResult:
        """@brief 在共享 serial/fair admission 下执行只读 journal replay / Execute a read-only journal replay under shared serial/fair admission.

        @param key 请求 runtime key / Requested runtime key.
        @param entry 已借出的 native client cache entry / Borrowed native-client cache entry.
        @param command 已验证只读 replay command / Validated read-only replay command.
        @return 规范 ``replayed=true`` 文件收据 / Canonical ``replayed=true`` file receipt.
        @note 虽然该 native RPC 不修改 Workspace，仍与同 key 的 ``add_file``/Bash 有序，避免
            恢复检查跨越同一 runtime session 的 payload journal 边界。/ Although this native
            RPC does not modify the Workspace, it remains ordered with same-key ``add_file``/Bash
            so a recovery check cannot cross a payload-journal boundary in the same runtime session.
        """

        async with entry.execution_lock:
            lease = await self._execution_admission.acquire(key)
            try:
                return await asyncio.to_thread(
                    _replay_file_native_process,
                    entry.process,
                    command,
                )
            finally:
                await lease.release()

    def _schedule_release(
        self,
        key: WorkspaceRuntimeKey,
        entry: _CachedRuntimeProcess,
        completed: asyncio.Future[Any],
    ) -> None:
        """@brief 在执行任务真正结束后归还 cache lease / Return a cache lease after the execution task truly finishes.

        @param key runtime key / Runtime key.
        @param entry 已借出的 cache entry / Borrowed cache entry.
        @param completed 已终结的执行任务 / Completed execution task.
        @return None / None.
        @note 读取 task exception 防止被取消调用方留下未读取异常警告；异常仍会由原 awaiter
            正常收到。/ Reading the task exception prevents an unobserved-exception warning
            after a caller cancels; the original awaiter still receives the exception normally.
        """

        try:
            completed.exception()
        except asyncio.CancelledError:
            pass
        asyncio.get_running_loop().create_task(
            self._release(key, entry),
            name="workspace.release-runtime-process",
        )

    async def _release(
        self,
        key: WorkspaceRuntimeKey,
        entry: _CachedRuntimeProcess,
    ) -> None:
        """@brief 归还一个 runtime cache lease，并在空闲时安排回收 / Return one runtime cache lease and schedule retirement when idle.

        @param key runtime key / Runtime key.
        @param entry 已借出的 cache entry / Borrowed cache entry.
        @return None / None.
        """

        async with self._cache_lock:
            close_after_release = self._release_entry_locked(key, entry)
        if close_after_release:
            await self._close_process(entry)

    async def _retire_after_idle(
        self,
        key: WorkspaceRuntimeKey,
        entry: _CachedRuntimeProcess,
    ) -> None:
        """@brief 15 分钟空闲后关闭一个 cache entry / Close a cache entry after 15 minutes of idleness.

        @param key runtime key / Runtime key.
        @param entry 候选 cache entry / Candidate cache entry.
        @return None / None.
        """

        try:
            await asyncio.sleep(self._idle_ttl_seconds)
        except asyncio.CancelledError:
            return
        async with self._cache_lock:
            if (
                self._closed
                or self._cache.get(key) is not entry
                or entry.active_count != 0
            ):
                return
            del self._cache[key]
            entry.idle_task = None
        await self._close_process(entry)

    async def _close_process(self, entry: _CachedRuntimeProcess) -> None:
        """@brief 尽力关闭一个已不再借出的 native client / Best-effort close a native client no longer borrowed.

        @param entry 待关闭 cache entry / Cache entry to close.
        @return None / None.
        @note 清理失败不会把已完成的 Bash 结果变为失败；只记录无敏感数据的警告。/
            Cleanup failure never turns a completed Bash result into a failure; it only records a
            warning without sensitive data.
        """

        async with entry.close_lock:
            if entry.closed:
                return
            entry.closed = True
            try:
                await self._client_lifecycle_offloads.call(entry.process.close)
            except Exception:
                _LOGGER.warning("Failed to close a wspctl RuntimeProcess")

    async def _close_unclaimed_process(self, process: NativeRuntimeProcess) -> None:
        """@brief 经 lifecycle 隔舱关闭未放入 cache 的 native client /
        Close an uncached native client through the lifecycle bulkhead.

        @param process 未被任何 cache entry 拥有的 native client / Native client owned by no cache entry.
        @return None / None.
        """

        try:
            await self._client_lifecycle_offloads.call(process.close)
        except Exception:
            _LOGGER.warning("Failed to close an unclaimed wspctl RuntimeProcess")


def _cancel_idle_retirement(entry: _CachedRuntimeProcess) -> None:
    """@brief 取消仍未触发的 cache 空闲回收 / Cancel a cache idle retirement that has not fired yet.

    @param entry cache entry / Cache entry.
    @return None / None.
    """

    task = entry.idle_task
    entry.idle_task = None
    if task is not None:
        task.cancel()


def _execute_native_process(
    process: NativeRuntimeProcess,
    command: RunBashCommand,
) -> RunBashResult:
    """@brief 在工作线程内执行并严格解码 native 调用 / Execute and strictly decode a native call in a worker thread.

    @param process native RuntimeProcess / Native RuntimeProcess.
    @param command 已验证 Bash 命令 / Validated Bash command.
    @return 规范 Bash 结果 / Canonical Bash result.
    @raise WorkspaceRuntimeUnavailableError native client 抛出时抛出 / Raised when the native client raises.
    @raise WorkspaceRuntimeProtocolError native 返回值不可信时抛出 / Raised when the native return value is untrusted.
    """

    try:
        raw_result = process.execute(
            ["/bin/bash", "--noprofile", "--norc", "-c", command.command, "run_bash"],
            stdin=command.stdin,
            cwd=command.working_directory.runtime_path,
            timeout_ms=command.timeout_seconds * 1_000,
            output_limit=command.output_limit_bytes,
            request_id=str(command.request_id),
            request_hash=str(command.request_hash),
        )
    except WorkspaceRuntimeUnavailableError:
        raise
    except Exception as error:
        if _native_error_code(error) == "invocation_in_doubt":
            raise WorkspaceInvocationOutcomeUnknownError(command.request_id) from error
        raise WorkspaceRuntimeUnavailableError(
            "wspctl RuntimeProcess execution failed",
            diagnostic_code=_native_error_code(error),
            diagnostic_message=_native_error_message(error),
        ) from error
    return _decode_native_result(raw_result, command)


def _add_file_native_process(
    process: NativeRuntimeProcess,
    command: AddFileCommand,
) -> AddFileResult:
    """@brief 在工作线程内流式写入文件并严格解码收据 / Stream-write a file in a worker thread and strictly decode its receipt.

    @param process native RuntimeProcess / Native RuntimeProcess.
    @param command 已验证文件写入命令 / Validated file-ingress command.
    @return 规范文件收据 / Canonical file receipt.
    @raise WorkspaceRuntimeUnavailableError native client 抛出时抛出 / Raised when the native client raises.
    @raise WorkspaceRuntimeProtocolError native 返回值不可信时抛出 / Raised when the native return value is untrusted.
    @note ``chunks`` 只能向 native binding 传递一次；此层不得读取、拼接、解码或按内容分类
        payload。/ ``chunks`` is passed to the native binding exactly once; this layer must not
        read, concatenate, decode, or classify payload content.
    """

    try:
        raw_result = process.add_file(
            command.opaque_id,
            command.chunks,
            command.byte_size,
            command.sha256,
            request_id=str(command.request_id),
            request_hash=str(command.request_hash),
        )
    except WorkspaceRuntimeUnavailableError:
        raise
    except Exception as error:
        if _native_error_code(error) == "invocation_in_doubt":
            raise WorkspaceInvocationOutcomeUnknownError(command.request_id) from error
        raise WorkspaceRuntimeUnavailableError(
            "wspctl RuntimeProcess payload ingress failed",
            diagnostic_code=_native_error_code(error),
            diagnostic_message=_native_error_message(error),
        ) from error
    return _decode_native_file_result(raw_result, command)


def _replay_file_native_process(
    process: NativeRuntimeProcess,
    command: ReplayFileCommand,
) -> AddFileResult:
    """@brief 在工作线程内只读查询 native payload journal / Query the native payload journal read-only in a worker thread.

    @param process native RuntimeProcess / Native RuntimeProcess.
    @param command 已验证、无 chunks 的 replay command / Validated replay command with no chunks.
    @return ``replayed=true`` 的规范文件收据 / Canonical file receipt with ``replayed=true``.
    @raise WorkspaceFileReplayNotFoundError 仅 native 明确报告 ``not_found`` 时抛出 /
        Raised only when native explicitly reports ``not_found``.
    @raise WorkspaceInvocationOutcomeUnknownError native 报告 pending 或不可判定 payload 时抛出 /
        Raised when native reports pending or an indeterminate payload.
    @raise WorkspaceRuntimeUnavailableError 其他 native/transport 故障时抛出 /
        Raised for other native or transport failures.
    @note 此函数没有 fallback、chunks 或 host 文件 I/O；它只将 immutable metadata 传给
        pybind read-only entry。/ This function has no fallback, chunks, or host file I/O; it
        passes immutable metadata only to the pybind read-only entry.
    """

    try:
        raw_result = process.replay_file(
            command.opaque_id,
            command.byte_size,
            command.sha256,
            request_id=str(command.request_id),
            request_hash=str(command.request_hash),
        )
    except WorkspaceRuntimeUnavailableError:
        raise
    except Exception as error:
        error_code = _native_error_code(error)
        if error_code == "not_found":
            raise WorkspaceFileReplayNotFoundError(command.request_id) from error
        if error_code == "invocation_in_doubt":
            raise WorkspaceInvocationOutcomeUnknownError(command.request_id) from error
        raise WorkspaceRuntimeUnavailableError(
            "wspctl RuntimeProcess payload replay failed",
            diagnostic_code=error_code,
            diagnostic_message=_native_error_message(error),
        ) from error
    return _decode_native_file_result(raw_result, command, require_replayed=True)


def _decode_native_file_result(
    raw_result: Mapping[str, object] | object,
    command: AddFileCommand | ReplayFileCommand,
    *,
    require_replayed: bool = False,
) -> AddFileResult:
    """@brief 验证 native 文件收据完整、有限且绑定原请求 / Verify a native file receipt is complete, bounded, and bound to its request.

    @param raw_result native pybind 返回值 / Return value from native pybind.
    @param command 发起该 publication 或只读 replay 的命令 / Command that initiated the publication or read-only replay.
    @param require_replayed 为 True 时强制 native 标记 completed journal replay /
        When True, require native to mark a completed journal replay.
    @return 已验证规范文件收据 / Verified canonical file receipt.
    @raise WorkspaceRuntimeProtocolError 返回结构、类型、路径、摘要或请求 ID 不匹配时抛出 /
        Raised when returned structure, types, path, digest, or request ID does not match.
    """

    if not isinstance(raw_result, Mapping):
        raise WorkspaceRuntimeProtocolError("wspctl file result must be a mapping")
    required_fields = {"request_id", "replayed", "path", "byte_size", "sha256"}
    if set(raw_result) != required_fields:
        raise WorkspaceRuntimeProtocolError(
            "wspctl file result has an unsupported field set"
        )
    request_id = raw_result["request_id"]
    replayed = raw_result["replayed"]
    path = raw_result["path"]
    byte_size = raw_result["byte_size"]
    sha256 = raw_result["sha256"]
    if not isinstance(request_id, str) or request_id != str(command.request_id):
        raise WorkspaceRuntimeProtocolError(
            "wspctl file result request ID does not match"
        )
    if not isinstance(replayed, bool):
        raise WorkspaceRuntimeProtocolError("wspctl file replay flag must be a bool")
    if require_replayed and replayed is not True:
        raise WorkspaceRuntimeProtocolError(
            "wspctl replay_file result must be marked replayed"
        )
    if not isinstance(path, str) or path != command.runtime_path:
        raise WorkspaceRuntimeProtocolError("wspctl file result path does not match")
    if (
        isinstance(byte_size, bool)
        or not isinstance(byte_size, int)
        or byte_size != command.byte_size
    ):
        raise WorkspaceRuntimeProtocolError(
            "wspctl file result byte size does not match"
        )
    if not isinstance(sha256, str) or sha256 != command.sha256:
        raise WorkspaceRuntimeProtocolError("wspctl file result SHA-256 does not match")
    try:
        return AddFileResult(
            request_id=WorkspaceRequestId(request_id),
            replayed=replayed,
            path=path,
            byte_size=byte_size,
            sha256=sha256,
        )
    except (TypeError, ValueError) as error:
        raise WorkspaceRuntimeProtocolError(
            "wspctl file result violates the file contract"
        ) from error


def _decode_native_result(
    raw_result: Mapping[str, object] | object,
    command: RunBashCommand,
) -> RunBashResult:
    """@brief 验证 native result 是否完整、有限且绑定原请求 / Verify a native result is complete, bounded, and bound to its request.

    @param raw_result native pybind 返回值 / Return value from native pybind.
    @param command 发起该调用的命令 / Command that initiated the call.
    @return 已验证规范结果 / Verified canonical result.
    @raise WorkspaceRuntimeProtocolError 返回结构、类型、输出预算或 request ID 不匹配时抛出 /
        Raised when result structure, types, output budget, or request ID does not match.
    """

    if not isinstance(raw_result, Mapping):
        raise WorkspaceRuntimeProtocolError("wspctl result must be a mapping")
    required_fields = {
        "stdout",
        "stderr",
        "exit_code",
        "timed_out",
        "truncated",
        "replayed",
        "request_id",
    }
    if set(raw_result) != required_fields:
        raise WorkspaceRuntimeProtocolError(
            "wspctl result has an unsupported field set"
        )
    stdout = raw_result["stdout"]
    stderr = raw_result["stderr"]
    request_id = raw_result["request_id"]
    if not isinstance(stdout, str) or not isinstance(stderr, str):
        raise WorkspaceRuntimeProtocolError("wspctl result output must be text")
    if not isinstance(request_id, str) or request_id != str(command.request_id):
        raise WorkspaceRuntimeProtocolError("wspctl result request ID does not match")
    if (
        _utf8_byte_length(stdout) + _utf8_byte_length(stderr)
        > command.output_limit_bytes
    ):
        raise WorkspaceRuntimeProtocolError(
            "wspctl result exceeds the requested output limit"
        )
    try:
        return RunBashResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=cast(int | None, raw_result["exit_code"]),
            timed_out=cast(bool, raw_result["timed_out"]),
            truncated=cast(bool, raw_result["truncated"]),
            replayed=cast(bool, raw_result["replayed"]),
            request_id=WorkspaceRequestId(request_id),
        )
    except (TypeError, ValueError) as error:
        raise WorkspaceRuntimeProtocolError(
            "wspctl result violates the execution contract"
        ) from error


def _native_error_code(error: Exception) -> str | None:
    """@brief 读取 pybind 专用异常的稳定错误码 / Read the stable error code of a pybind-specific exception.

    @param error native client 抛出的异常 / Exception raised by the native client.
    @return 已验证的机器错误码；普通异常为 ``None`` /
        Validated machine error code, or ``None`` for ordinary exceptions.
    @note 不解析 exception message；只有 native binding 显式提供的 ``code`` 字符串才可改变
        receipt 的幂等语义。/ Exception messages are never parsed; only an explicit ``code``
        string supplied by the native binding may change receipt idempotency semantics.
    """

    code = getattr(error, "code", None)
    return code if isinstance(code, str) else None


def _native_error_message(error: Exception) -> str | None:
    """@brief 读取 pybind 专用异常显式声明的安全消息 / Read the safe message explicitly declared by a pybind exception.

    @param error native client 抛出的异常 / Exception raised by the native client.
    @return binding 显式提供的消息；普通异常为 None /
        Message explicitly provided by the binding, or None for ordinary exceptions.
    @note 不回退到 ``str(error)``，避免把任意 SDK 异常或请求载荷提升为可信诊断。/
        This deliberately does not fall back to ``str(error)`` so arbitrary SDK exceptions or
        request payloads cannot be promoted to trusted diagnostics.
    """

    message = getattr(error, "message", None)
    return message if isinstance(message, str) else None


def _workspace_error_diagnostics(
    error: Exception,
) -> tuple[str | None, str | None]:
    """@brief 从已翻译 Workspace 错误复制结构化诊断 / Copy structured diagnostics from a translated Workspace error.

    @param error runtime 创建路径捕获的异常 / Exception captured by the runtime creation path.
    @return 机器码与安全消息 / Machine code and safe message.
    """

    if not isinstance(error, WorkspaceRuntimeUnavailableError):
        return None, None
    return error.diagnostic_code, error.diagnostic_message


def _utf8_byte_length(value: str) -> int:
    """@brief 计算可传输文本的 UTF-8 字节数 / Calculate UTF-8 byte length of transferable text.

    @param value 候选文本 / Candidate text.
    @return UTF-8 字节数 / UTF-8 byte count.
    @raise WorkspaceRuntimeProtocolError 文本含无法 UTF-8 编码的 surrogate 时抛出 /
        Raised when text contains a surrogate that cannot be UTF-8 encoded.
    """

    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise WorkspaceRuntimeProtocolError(
            "wspctl result contains non-UTF-8 text"
        ) from error


__all__ = [
    "NativeRuntimeProcess",
    "RuntimeProcessFactory",
    "WspctlRuntimeProcessFactory",
    "WspctlRuntimeProcess",
]
