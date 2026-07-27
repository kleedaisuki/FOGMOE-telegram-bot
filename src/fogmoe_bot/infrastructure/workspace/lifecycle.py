"""@brief Workspace RuntimeProcess cache 的受控生命周期 / Controlled lifecycle for the Workspace RuntimeProcess cache."""

from __future__ import annotations

import asyncio

from .wspctl import WspctlRuntimeProcess


class RuntimeProcessLifecycle:
    """@brief 在 Bot 关停阶段脱离 RuntimeProcess client cache / Detach the RuntimeProcess client cache during Bot shutdown.

    @note 此对象是 ``ServiceBinding`` 所需的长驻服务，不是第二个 Workspace 执行器；所有
        command 仍经同一个 ``WspctlRuntimeProcess``。关停时它不会等待 active command：一旦
        broker 已接受请求，journal/PID 1/cgroup 才是其 owner。/ This object is the long-running
        service required by ``ServiceBinding``, not a second Workspace executor; every command
        still goes through the same ``WspctlRuntimeProcess``. On shutdown it does not wait for
        active commands: once the broker accepts a request, the journal/PID 1/cgroup own it.
    """

    def __init__(self, runtime_process: WspctlRuntimeProcess) -> None:
        """@brief 绑定唯一的 Workspace RuntimeProcess / Bind the sole Workspace RuntimeProcess.

        @param runtime_process 拥有 RuntimeProcess cache 的适配器 / Adapter owning the RuntimeProcess cache.
        @return None / None.
        @raise TypeError runner 不是 wspctl runner 时抛出 / Raised when the runner is not a wspctl runner.
        """

        if not isinstance(runtime_process, WspctlRuntimeProcess):
            raise TypeError("RuntimeProcess lifecycle requires a WspctlRuntimeProcess")
        self._runtime_process = runtime_process
        """@brief 由该 lifecycle 排空的唯一 client cache / Sole client cache drained by this lifecycle."""

    async def run(self, stop_event: asyncio.Event) -> None:
        """@brief 等待停止，再脱离 broker-owned Workspace command / Wait for stop, then detach from broker-owned Workspace commands.

        @param stop_event BotRuntime 提供的服务级停止事件 / Service-level stop event provided by BotRuntime.
        @return None / None.
        @note supervisor 取消本服务时仍执行 ``detach``。这使 Bot 的 190 秒 graceful window
            不会被 300 秒 command 绑住，同时不把 client disconnect 误当 task cancellation。
            / A supervisor cancellation still executes ``detach``. This prevents the Bot's
            190-second graceful window from being held by a 300-second command while not
            mistaking client disconnect for task cancellation.
        """

        try:
            await stop_event.wait()
        finally:
            await _detach_runtime_process_without_cancellation(self._runtime_process)


async def _detach_runtime_process_without_cancellation(
    runtime_process: WspctlRuntimeProcess,
) -> None:
    """@brief 在 service cancellation 下仍脱离 RuntimeProcess / Detach a RuntimeProcess even while the service is cancelled.

    @param runtime_process 待脱离的唯一 Workspace RuntimeProcess / Sole Workspace RuntimeProcess to detach.
    @return None / None.
    @raise asyncio.CancelledError 排空完成后重新传播服务取消 / Re-raised after draining completes.
    @note ``asyncio.shield`` 只保护短暂的 inner detach task，不吞掉外层 cancellation。若
        supervisor 在 detach 期间再次取消 service，本函数继续等待同一 task，随后再传播取消。
        ``asyncio.shield`` protects only the short inner detach task and does not swallow outer
        cancellation. If the supervisor cancels the service again while detaching, this function
        keeps waiting for the same task and propagates cancellation afterward.
    """

    close_task = asyncio.create_task(
        runtime_process.detach(),
        name="workspace.lifecycle-detach",
    )
    deferred_cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            await asyncio.shield(close_task)
            break
        except asyncio.CancelledError as error:
            # A cancellation that originally entered ``finally`` is re-raised by Python after
            # this function returns. This branch handles *additional* cancellation requests
            # that arrive while close() is waiting for active native calls to drain.
            deferred_cancellation = error
    if deferred_cancellation is not None:
        raise deferred_cancellation


__all__ = ["RuntimeProcessLifecycle"]
