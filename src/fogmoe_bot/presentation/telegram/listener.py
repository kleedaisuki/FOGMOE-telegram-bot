"""@brief 先持久化再确认 offset 的 Telegram long-poll listener / Telegram long-poll listener that persists before acknowledging offsets.

``getUpdates`` 的确认动作不是 handler 返回，而是下一次请求携带更大的 ``offset``。
因此 listener 只会在当前批次全部幂等写入 durable inbox 后推进 offset；在 commit 与
下一次 poll 之间崩溃只会导致安全重放。/ A ``getUpdates`` response is acknowledged not
when a handler returns, but when a later request carries a greater ``offset``. The listener
therefore advances its offset only after the full batch has been idempotently persisted; a
crash between commit and the next poll causes only a safe replay.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from telegram import Bot, Update
from telegram.error import (
    BadRequest,
    Conflict,
    EndPointNotFound,
    Forbidden,
    InvalidToken,
    NetworkError,
    RetryAfter,
)

from fogmoe_bot.application.runtime import Jitter, SystemUtcClock, UtcClock
from fogmoe_bot.domain.conversation.inbox import InboundUpdate

from .update_mapper import TelegramUpdateMapper

logger = logging.getLogger(__name__)


class TelegramUpdateSource(Protocol):
    """@brief Telegram long-poll 输入端口 / Telegram long-poll input port."""

    async def get_updates(
        self,
        *,
        offset: int | None,
        timeout: float,
        allowed_updates: Sequence[str] | None,
    ) -> Sequence[Update]:
        """@brief 获取一批 Updates / Fetch one batch of Updates.

        @param offset 下一批起始 offset；None 表示 Telegram 当前未确认队列 /
        Next batch offset; None means Telegram's current unacknowledged queue.
        @param timeout long-poll 超时 / Long-poll timeout.
        @param allowed_updates 允许的 Update kinds / Allowed Update kinds.
        @return Update 批次 / Update batch.
        """

        ...


class TelegramBotUpdateSource:
    """@brief 将 PTB Bot 适配为 listener 输入端口 / Adapt a PTB Bot to the listener input port."""

    def __init__(self, bot: Bot) -> None:
        """@brief 创建输入适配器 / Create the input adapter.

        @param bot 与所有 Telegram 输出共享的已初始化 Bot / Initialized Bot shared with all Telegram output.
        """

        self._bot = bot

    async def get_updates(
        self,
        *,
        offset: int | None,
        timeout: float,
        allowed_updates: Sequence[str] | None,
    ) -> Sequence[Update]:
        """@brief 执行一次 Telegram long poll / Perform one Telegram long poll.

        @param offset 下一批起始 offset / Start offset for the next batch.
        @param timeout 服务端 long-poll 时长 / Server-side long-poll duration.
        @param allowed_updates 允许的 Update kinds / Allowed Update kinds.
        @return Telegram Update 批次 / Batch of Telegram updates.
        """

        return await self._bot.get_updates(
            offset=offset,
            timeout=timedelta(seconds=timeout),
            allowed_updates=allowed_updates,
        )


class InboundUpdateSink(Protocol):
    """@brief durable inbox 写入端口 / Durable-inbox write port."""

    async def add_inbound(self, update: InboundUpdate) -> bool:
        """@brief 幂等持久化 Update / Idempotently persist an Update.

        @param update 待持久化实体 / Entity to persist.
        @return 新插入为 True，重放为 False / True for insertion, False for replay.
        """

        ...


@dataclass(frozen=True, slots=True)
class PollingBackoff:
    """@brief Telegram poll 失败的 capped full-jitter 退避 / Capped full-jitter backoff for Telegram poll failures.

    @param initial_delay 首次失败的最大延迟秒数 / Maximum delay in seconds for the first failure.
    @param max_delay 延迟上限秒数 / Maximum delay in seconds.
    @param jitter 可注入随机源 / Injectable random source.
    """

    initial_delay: float = 1.0
    max_delay: float = 30.0
    jitter: Jitter = random.uniform

    def __post_init__(self) -> None:
        """@brief 校验退避边界 / Validate backoff bounds.

        @return None / None.
        @raise ValueError 延迟非法时抛出 / Raised for invalid delays.
        """

        if self.initial_delay <= 0:
            raise ValueError("initial_delay must be positive")
        if self.max_delay < self.initial_delay:
            raise ValueError("max_delay cannot be smaller than initial_delay")

    def delay(self, failure_count: int, error: Exception) -> float:
        """@brief 计算一次失败后的延迟 / Compute the delay after one failure.

        @param failure_count 从 1 开始的连续失败数 / Consecutive failure count starting at one.
        @param error Telegram 或持久化异常 / Telegram or persistence exception.
        @return 延迟秒数 / Delay in seconds.
        """

        if failure_count < 1:
            raise ValueError("failure_count must be at least one")
        if isinstance(error, RetryAfter):
            retry_after = error.retry_after
            if isinstance(retry_after, timedelta):
                return max(0.0, retry_after.total_seconds())
            return max(0.0, float(retry_after))
        cap = min(self.max_delay, self.initial_delay * (2 ** (failure_count - 1)))
        return self.jitter(0.0, cap)


class TelegramPollingListener:
    """@brief Telegram Update 的可靠入口循环 / Reliable ingress loop for Telegram Updates."""

    def __init__(
        self,
        *,
        source: TelegramUpdateSource,
        sink: InboundUpdateSink,
        poll_timeout: float,
        allowed_updates: Sequence[str] | None = None,
        mapper: TelegramUpdateMapper | None = None,
        clock: UtcClock | None = None,
        backoff: PollingBackoff | None = None,
    ) -> None:
        """@brief 创建 listener / Create the listener.

        @param source Telegram long-poll 端口 / Telegram long-poll port.
        @param sink durable inbox 端口 / Durable-inbox port.
        @param poll_timeout Telegram long-poll 超时秒数 / Telegram long-poll timeout in seconds.
        @param allowed_updates 可选 Update kind allow-list / Optional Update-kind allow-list.
        @param mapper SDK 到领域入口 mapper / SDK-to-domain ingress mapper.
        @param clock 可替换 UTC 时钟 / Replaceable UTC clock.
        @param backoff 失败退避策略 / Failure-backoff policy.
        """

        if poll_timeout <= 0:
            raise ValueError("poll_timeout must be positive")
        self._source = source
        self._sink = sink
        self._poll_timeout = poll_timeout
        self._allowed_updates = (
            tuple(allowed_updates) if allowed_updates is not None else None
        )
        self._mapper = mapper or TelegramUpdateMapper()
        self._clock = clock or SystemUtcClock()
        self._backoff = backoff or PollingBackoff()

    async def run(self, stop_event: asyncio.Event) -> None:
        """@brief 轮询直至停止，不在持久化前确认 offset / Poll until stopped without acknowledging before persistence.

        @param stop_event 置位后取消当前 long poll 并退出 / Cancels the active long poll and exits when set.
        @return None / None.
        @raise TelegramError 配置、鉴权或 poll ownership 永久错误原样暴露 /
        Permanent configuration, authentication, or poll-ownership errors propagate.
        """

        offset: int | None = None
        failures = 0
        while not stop_event.is_set():
            try:
                updates = await self._poll_or_stop(offset, stop_event)
                if updates is None:
                    return
                next_offset = await self._persist_batch(updates)
            except InvalidToken, BadRequest, Conflict, EndPointNotFound, Forbidden:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as error:
                failures += 1
                delay = self._backoff.delay(failures, error)
                logger.warning(
                    "Telegram ingress poll/persist failed; retrying in %.3fs: %s",
                    delay,
                    error,
                    exc_info=not isinstance(error, (NetworkError, RetryAfter)),
                )
                if await self._wait_or_stop(delay, stop_event):
                    return
                continue

            failures = 0
            if next_offset is not None:
                offset = next_offset

    async def _persist_batch(self, updates: Sequence[Update]) -> int | None:
        """@brief 依次幂等写入完整批次 / Idempotently persist a complete batch in order.

        @param updates Telegram 返回批次 / Batch returned by Telegram.
        @return 空批次为 None，否则为下一 offset / None for an empty batch; otherwise the next offset.
        @note 任一写入失败会阻止 offset 推进，已成功前缀将在下一轮安全去重。/
        Any write failure prevents offset advancement; an already-persisted prefix is safely
        deduplicated on the next attempt.
        """

        if not updates:
            return None
        maximum_update_id = -1
        for update in updates:
            inbound = self._mapper.map(update, received_at=self._clock.now())
            await self._sink.add_inbound(inbound)
            maximum_update_id = max(maximum_update_id, inbound.update_id.value)
        return maximum_update_id + 1

    async def _poll_or_stop(
        self,
        offset: int | None,
        stop_event: asyncio.Event,
    ) -> Sequence[Update] | None:
        """@brief 竞争 long poll 与停止信号 / Race the long poll against the stop signal.

        @param offset 当前未确认 offset / Current unacknowledged offset.
        @param stop_event 停止信号 / Stop signal.
        @return Update 批次；停止获胜时为 None / Update batch, or None when stop wins.
        """

        poll = asyncio.create_task(
            self._source.get_updates(
                offset=offset,
                timeout=self._poll_timeout,
                allowed_updates=self._allowed_updates,
            ),
            name="telegram-get-updates",
        )
        stop = asyncio.create_task(stop_event.wait(), name="telegram-listener-stop")
        try:
            done, _ = await asyncio.wait(
                (poll, stop),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stop in done and stop.result():
                return None
            return await poll
        finally:
            for task in (poll, stop):
                if not task.done():
                    task.cancel()
            await asyncio.gather(poll, return_exceptions=True)
            await asyncio.gather(stop, return_exceptions=True)

    @staticmethod
    async def _wait_or_stop(delay: float, stop_event: asyncio.Event) -> bool:
        """@brief 可由停止信号中断退避等待 / Let the stop signal interrupt backoff sleep.

        @param delay 最大等待秒数 / Maximum delay in seconds.
        @param stop_event 停止信号 / Stop signal.
        @return 停止已请求为 True / True when stop was requested.
        """

        if delay <= 0:
            return stop_event.is_set()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        except TimeoutError:
            return False
        return True


__all__ = [
    "InboundUpdateSink",
    "PollingBackoff",
    "TelegramBotUpdateSource",
    "TelegramPollingListener",
    "TelegramUpdateSource",
]
