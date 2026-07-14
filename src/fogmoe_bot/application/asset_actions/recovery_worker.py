"""@brief 已确认资产动作的 lease 恢复 worker / Lease-recovery worker for confirmed asset actions."""

from __future__ import annotations

import asyncio
import logging

from fogmoe_bot.application.runtime import SystemUtcClock, UtcClock

from .service import AssetActionConfirmationService


logger = logging.getLogger(__name__)
"""@brief 资产确认恢复日志器 / Asset-confirmation recovery logger."""


class AssetActionRecoveryWorker:
    """@brief 小批量恢复失联执行租约 / Recover abandoned execution leases in small batches.

    worker 只调用 confirmation store 的 ``executing AND lease_expired`` 领取查询，随后在
    数据库事务外调用银行，并由 fencing token 原子终结。它不是 Telegram inbox consumer，
    也不会扫描或占用普通 Agent mailbox。/ The worker calls only the confirmation store's
    ``executing AND lease_expired`` claim query, invokes the bank outside database transactions,
    and finalizes atomically with a fencing token. It is not a Telegram inbox consumer and neither
    scans nor occupies the ordinary Agent mailbox.
    """

    def __init__(
        self,
        *,
        service: AssetActionConfirmationService,
        poll_interval: float = 5.0,
        batch_size: int = 8,
        clock: UtcClock | None = None,
    ) -> None:
        """@brief 注入恢复服务、轮询与容量边界 / Inject recovery service, polling, and capacity bounds.

        @param service 拥有 fenced 执行编排的确认服务 / Confirmation service owning fenced execution orchestration.
        @param poll_interval 空闲轮询秒数 / Idle polling interval in seconds.
        @param batch_size 单个短领取事务的最大 claim 数 / Maximum claims in one short claim transaction.
        @param clock 可替换 UTC 时钟 / Replaceable UTC clock.
        @return None / None.
        @raise ValueError 轮询或批量边界非法时抛出 / Raised for invalid poll or batch bounds.
        """

        if poll_interval <= 0:
            raise ValueError("Asset-action recovery poll_interval must be positive")
        if isinstance(batch_size, bool) or not 1 <= batch_size <= 100:
            raise ValueError("Asset-action recovery batch_size must be between 1 and 100")
        self._service = service
        self._poll_interval = poll_interval
        self._batch_size = batch_size
        self._clock = clock or SystemUtcClock()

    async def run(self, stop_event: asyncio.Event) -> None:
        """@brief 持续恢复 expired execution leases 直到停止 / Recover expired execution leases until stopped.

        @param stop_event 结构化运行时停止信号 / Structured runtime stop signal.
        @return None / None.
        @note 单次失败会保留/重新过期 fencing lease，由下一轮重试；取消会直接传播，避免
            伪造成功。/ One failure leaves or re-expires the fencing lease for a later poll;
            cancellation propagates directly and never fabricates success.
        """

        while not stop_event.is_set():
            try:
                completed = await self._service.recover_expired(
                    now=self._clock.now(),
                    limit=self._batch_size,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Asset-action execution recovery poll failed")
                completed = 0
            if completed >= self._batch_size:
                await asyncio.sleep(0)
                continue
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self._poll_interval,
                )
            except TimeoutError:
                continue


__all__ = ["AssetActionRecoveryWorker"]
