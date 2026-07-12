"""@brief Bot 后台服务的统一结构化生命周期 / Unified structured lifecycle for Bot background services."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from fogmoe_bot.application.runtime.keyed_mailbox import (
    KeyedMailboxRuntime,
    ShutdownMode,
)


logger = logging.getLogger(__name__)

BOT_RUNTIME_DATA_KEY = "fogmoe.bot_runtime"
"""@brief 组合根保存顶层运行时的稳定键 / Stable composition-root key for the top-level runtime."""


class BackgroundService(Protocol):
    """@brief 由 BotRuntime 拥有的长驻服务端口 / Long-running service owned by BotRuntime."""

    async def run(self, stop_event: asyncio.Event) -> None:
        """@brief 运行至停止信号并排空已接收工作 / Run until stopped and drain accepted work.

        @param stop_event 共享停止信号 / Shared stop signal.
        @return None / None.
        """

        ...


@dataclass(frozen=True, slots=True)
class ServiceBinding:
    """@brief 为后台服务绑定稳定观测名称 / Bind a stable observable name to a background service.

    @param name 日志与 task 使用的稳定名称 / Stable name used by logs and tasks.
    @param service 长驻服务 / Long-running service.
    @param shutdown_phase drain 时的阶段；较小值先停止 / Drain phase; lower values stop first.
    """

    name: str
    service: BackgroundService
    shutdown_phase: int = 0

    def __post_init__(self) -> None:
        """@brief 校验服务名称 / Validate the service name.

        @return None / None.
        @raise ValueError 名称为空时抛出 / Raised when the name is blank.
        """

        normalized = self.name.strip()
        if not normalized:
            raise ValueError("Background service name cannot be blank")
        if isinstance(self.shutdown_phase, bool) or self.shutdown_phase < 0:
            raise ValueError("Background service shutdown phase cannot be negative")
        object.__setattr__(self, "name", normalized)


class BotRuntimeState(StrEnum):
    """@brief 顶层运行时生命周期状态 / Top-level runtime lifecycle state."""

    NEW = "new"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class BotRuntime:
    """@brief 统一拥有 keyed executor 与全部后台服务 / Own the keyed executor and all background services."""

    def __init__(
        self,
        *,
        execution_runtime: KeyedMailboxRuntime,
        services: Sequence[ServiceBinding],
    ) -> None:
        """@brief 创建未启动的运行时 / Create an unstarted runtime.

        @param execution_runtime 按聚合串行的有界执行器 / Bounded aggregate-serial executor.
        @param services 由同一 TaskGroup 拥有的后台服务 / Background services owned by one TaskGroup.
        @raise ValueError 服务名称重复时抛出 / Raised for duplicate service names.
        """

        names = tuple(binding.name for binding in services)
        if len(set(names)) != len(names):
            raise ValueError("Background service names must be unique")
        self._execution_runtime = execution_runtime
        self._services = tuple(services)
        self._state = BotRuntimeState.NEW
        self._stop_event: asyncio.Event | None = None
        self._started_event: asyncio.Event | None = None
        self._service_stop_events: dict[str, asyncio.Event] = {}
        self._supervisor: asyncio.Task[None] | None = None
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._failure: Exception | None = None

    @property
    def state(self) -> BotRuntimeState:
        """@brief 返回顶层生命周期状态 / Return the top-level lifecycle state.

        @return 当前状态 / Current state.
        """

        return self._state

    @property
    def execution_runtime(self) -> KeyedMailboxRuntime:
        """@brief 返回受统一生命周期拥有的执行器 / Return the lifecycle-owned executor.

        @return keyed mailbox runtime / Keyed mailbox runtime.
        """

        return self._execution_runtime

    @property
    def service_names(self) -> tuple[str, ...]:
        """@brief 返回不可变服务名称目录 / Return the immutable service-name catalog.

        @return 按启动声明顺序排列的名称 / Names in declared startup order.
        """

        return tuple(binding.name for binding in self._services)

    @property
    def failure(self) -> Exception | None:
        """@brief 返回后台服务终止原因 / Return the background-service terminal cause.

        @return 无失败为 None / None when no failure occurred.
        """

        return self._failure

    async def start(self) -> None:
        """@brief 启动 executor 与统一服务 TaskGroup / Start the executor and unified service TaskGroup.

        @return None / None.
        @raise RuntimeError 已启动、已终结或启动即失败时抛出 / Raised when already started/finalized or startup fails.
        """

        if self._state is not BotRuntimeState.NEW:
            raise RuntimeError(f"Bot runtime cannot start from {self._state.value}")
        self._owner_loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        self._started_event = asyncio.Event()
        self._service_stop_events = {
            binding.name: asyncio.Event() for binding in self._services
        }
        await self._execution_runtime.start()
        self._state = BotRuntimeState.RUNNING
        self._supervisor = asyncio.create_task(
            self._supervise(self._stop_event, self._started_event),
            name="bot-runtime-supervisor",
        )
        try:
            await self._started_event.wait()
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            await self._cancel_interrupted_start()
            raise
        if self._failure is not None:
            raise RuntimeError("Bot runtime failed during startup") from self._failure

    async def shutdown(self, mode: ShutdownMode = ShutdownMode.DRAIN) -> None:
        """@brief 先终结后台服务，再终结 keyed executor / Stop background services before the keyed executor.

        @param mode 排空或立即取消 / Drain or immediate cancellation.
        @return None / None.
        @raise RuntimeError 后台服务异常终结或跨 event loop 调用时抛出 /
        Raised for service failure or cross-event-loop calls.
        @note DRAIN 等待者被取消不会取消 supervisor；随后可调用 CANCEL 强制关停。/
        Cancelling a DRAIN waiter does not cancel the supervisor; a subsequent CANCEL can force shutdown.
        """

        if not isinstance(mode, ShutdownMode):
            raise TypeError("mode must be a ShutdownMode")
        if self._state is BotRuntimeState.NEW:
            await self._execution_runtime.shutdown(mode)
            self._state = BotRuntimeState.STOPPED
            return
        self._ensure_owner_loop()
        if self._state is BotRuntimeState.STOPPED:
            return
        supervisor = self._supervisor
        stop_event = self._stop_event
        if supervisor is None or stop_event is None:
            return

        if self._state is not BotRuntimeState.FAILED:
            self._state = BotRuntimeState.STOPPING
        stop_event.set()
        if mode is ShutdownMode.CANCEL and not supervisor.done():
            supervisor.cancel()
            await asyncio.gather(supervisor, return_exceptions=True)
        else:
            await asyncio.shield(supervisor)

        effective_mode = ShutdownMode.CANCEL if self._failure is not None else mode
        await self._execution_runtime.shutdown(effective_mode)
        if self._failure is not None:
            self._state = BotRuntimeState.FAILED
            raise RuntimeError("Bot runtime failed") from self._failure
        self._state = BotRuntimeState.STOPPED

    async def wait_terminated(self) -> None:
        """@brief 等待全部后台服务自然终结或失败 / Wait for all services to terminate or fail.

        @return None / None.
        @raise RuntimeError 尚未启动时抛出 / Raised before startup.
        @note 取消等待者不会取消运行时 / Cancelling the waiter does not cancel the runtime.
        """

        supervisor = self._supervisor
        if supervisor is None:
            raise RuntimeError("Bot runtime has not started")
        await asyncio.shield(supervisor)

    async def _cancel_interrupted_start(self) -> None:
        """@brief 取消被外部中断的部分启动 / Cancel a partially started runtime after external interruption.

        @return None / None.
        @note executor 已经启动且 supervisor 可能已拥有服务，因此两者都必须在
        ``start`` 传播 ``CancelledError`` 前终结。/ The executor is already running and the
        supervisor may already own services, so both must terminate before ``start`` propagates
        ``CancelledError``.
        """

        supervisor = self._supervisor
        stop_event = self._stop_event
        if stop_event is not None:
            stop_event.set()
        if supervisor is not None and not supervisor.done():
            supervisor.cancel()
        if supervisor is not None:
            await asyncio.gather(supervisor, return_exceptions=True)
        await self._execution_runtime.shutdown(ShutdownMode.CANCEL)
        self._state = (
            BotRuntimeState.FAILED
            if self._failure is not None
            else BotRuntimeState.STOPPED
        )

    async def _supervise(
        self,
        stop_event: asyncio.Event,
        started_event: asyncio.Event,
    ) -> None:
        """@brief 用一个 TaskGroup 监督全部服务 / Supervise all services with one TaskGroup.

        @param stop_event 共享停止信号 / Shared stop signal.
        @param started_event Task 已创建通知 / Notification that service tasks were created.
        @return None / None.
        """

        try:
            async with asyncio.TaskGroup() as task_group:
                service_tasks = {
                    binding.name: task_group.create_task(
                        self._run_service(
                            binding,
                            self._service_stop_events[binding.name],
                        ),
                        name=f"bot-service:{binding.name}",
                    )
                    for binding in self._services
                }
                started_event.set()
                await stop_event.wait()
                phases = sorted({binding.shutdown_phase for binding in self._services})
                for phase in phases:
                    phase_bindings = tuple(
                        binding
                        for binding in self._services
                        if binding.shutdown_phase == phase
                    )
                    for binding in phase_bindings:
                        self._service_stop_events[binding.name].set()
                    await asyncio.gather(
                        *(service_tasks[binding.name] for binding in phase_bindings)
                    )
        except asyncio.CancelledError:
            stop_event.set()
            raise
        except Exception as exc:
            self._failure = exc
            self._state = BotRuntimeState.FAILED
            stop_event.set()
            logger.exception("Bot runtime background service failed")
            await self._execution_runtime.shutdown(ShutdownMode.CANCEL)
        finally:
            started_event.set()

    @staticmethod
    async def _run_service(
        binding: ServiceBinding,
        stop_event: asyncio.Event,
    ) -> None:
        """@brief 运行服务并拒绝无信号提前返回 / Run a service and reject an unsolicited early return.

        @param binding 带名称的服务 / Named service binding.
        @param stop_event 该服务的阶段停止信号 / Phase-specific stop signal for this service.
        @return None / None.
        @raise RuntimeError 服务在停止前返回时抛出 / Raised when a service returns before shutdown.
        """

        await binding.service.run(stop_event)
        if not stop_event.is_set():
            raise RuntimeError(
                f"Background service {binding.name!r} returned before shutdown"
            )

    def _ensure_owner_loop(self) -> None:
        """@brief 确保生命周期调用位于 owner loop / Require lifecycle calls on the owner loop.

        @return None / None.
        @raise RuntimeError 跨 loop 调用时抛出 / Raised for cross-loop use.
        """

        if self._owner_loop is not asyncio.get_running_loop():
            raise RuntimeError("Bot runtime must be used on its owner event loop")


__all__ = [
    "BackgroundService",
    "BotRuntime",
    "BOT_RUNTIME_DATA_KEY",
    "BotRuntimeState",
    "ServiceBinding",
]
