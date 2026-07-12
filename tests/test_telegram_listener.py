"""@brief Telegram durable listener 测试 / Tests for the durable Telegram listener."""

import asyncio
import json
from datetime import datetime, timezone

import pytest
from telegram import Update

from fogmoe_bot.domain.conversation.inbox import InboundUpdate
from fogmoe_bot.presentation.telegram.listener import (
    PollingBackoff,
    TelegramPollingListener,
)


NOW = datetime(2026, 7, 11, 12, tzinfo=timezone.utc)
"""@brief listener 测试时间 / Listener test time."""


def _update(update_id: int, user_id: int = 7) -> Update:
    """@brief 构造真实 PTB Update / Build a real PTB Update.

    @param update_id Update ID / Update identifier.
    @param user_id 用户 ID / User identifier.
    @return PTB Update / PTB Update.
    """

    return Update.de_json(
        json.loads(
            '{"update_id": %d, "message": {"message_id": %d, "date": 1, '
            '"chat": {"id": %d, "type": "private"}, '
            '"from": {"id": %d, "is_bot": false, "first_name": "Klee"}, '
            '"text": "hello"}}' % (update_id, update_id, user_id, user_id)
        ),
        bot=None,
    )


class _Clock:
    """@brief 固定 listener 时钟 / Fixed listener clock."""

    def now(self) -> datetime:
        """@brief 返回固定时间 / Return fixed time.

        @return 测试 UTC 时间 / Test UTC time.
        """

        return NOW


class _Source:
    """@brief 可控 Telegram source / Controllable Telegram source."""

    def __init__(self, batches: list[tuple[Update, ...]]) -> None:
        """@brief 创建 source / Create source.

        @param batches 各轮返回批次 / Batches returned by successive polls.
        """

        self.batches = batches
        self.offsets: list[int | None] = []
        self.called = asyncio.Event()
        self.block = False

    async def get_updates(
        self,
        *,
        offset: int | None,
        timeout: float,
        allowed_updates: tuple[str, ...] | None,
    ) -> tuple[Update, ...]:
        """@brief 记录 offset 并返回下一批 / Record offset and return the next batch.

        @param offset 请求 offset / Requested offset.
        @param timeout 未使用超时 / Unused timeout.
        @param allowed_updates 未使用 allow-list / Unused allow-list.
        @return 下一批 / Next batch.
        """

        del timeout, allowed_updates
        self.offsets.append(offset)
        self.called.set()
        if self.block:
            await asyncio.Event().wait()
        if self.batches:
            return self.batches.pop(0)
        await asyncio.sleep(0)
        return ()


class _Sink:
    """@brief 记录 durable writes 的 sink / Sink recording durable writes."""

    def __init__(self, *, fail_once_on: int | None = None) -> None:
        """@brief 创建 sink / Create sink.

        @param fail_once_on 首次遇到该 Update ID 时失败 / Fail the first time this Update ID is seen.
        """

        self.writes: list[int] = []
        self.fail_once_on = fail_once_on
        self.failed = False

    async def add_inbound(self, update: InboundUpdate) -> bool:
        """@brief 记录写入并可注入一次失败 / Record a write and optionally inject one failure.

        @param update 待写实体 / Entity to write.
        @return True / True.
        """

        update_id = update.update_id.value
        self.writes.append(update_id)
        if update_id == self.fail_once_on and not self.failed:
            self.failed = True
            raise OSError("database unavailable")
        return True


def _listener(source: _Source, sink: _Sink) -> TelegramPollingListener:
    """@brief 构造无延迟测试 listener / Build a zero-delay test listener.

    @param source Telegram source / Telegram source.
    @param sink durable sink / Durable sink.
    @return listener / Listener.
    """

    return TelegramPollingListener(
        source=source,
        sink=sink,
        poll_timeout=1,
        clock=_Clock(),
        backoff=PollingBackoff(
            initial_delay=0.001,
            max_delay=0.001,
            jitter=lambda lower, upper: 0.0,
        ),
    )


def test_listener_acknowledges_only_after_complete_batch_persistence() -> None:
    """@brief 完整批次落盘后下一 poll 才推进 offset / The next poll advances offset only after the full batch persists."""

    async def scenario() -> None:
        """@brief 运行批次确认场景 / Run batch-acknowledgement scenario.

        @return None / None.
        """

        source = _Source([(_update(10), _update(11)), ()])
        sink = _Sink()
        stop = asyncio.Event()
        listener = _listener(source, sink)
        task = asyncio.create_task(listener.run(stop))

        while len(source.offsets) < 2:
            await asyncio.sleep(0)
        stop.set()
        await asyncio.wait_for(task, timeout=1)

        assert sink.writes == [10, 11]
        assert source.offsets[:2] == [None, 12]

    asyncio.run(scenario())


def test_listener_does_not_advance_offset_after_partial_persistence_failure() -> None:
    """@brief 部分持久化失败不推进 offset / Partial persistence failure does not advance the offset."""

    async def scenario() -> None:
        """@brief 运行失败重放场景 / Run failure-replay scenario.

        @return None / None.
        """

        batch = (_update(20), _update(21))
        source = _Source([batch, batch, ()])
        sink = _Sink(fail_once_on=21)
        stop = asyncio.Event()
        task = asyncio.create_task(_listener(source, sink).run(stop))

        while len(source.offsets) < 3:
            await asyncio.sleep(0)
        stop.set()
        await asyncio.wait_for(task, timeout=1)

        assert source.offsets[:3] == [None, None, 22]
        assert sink.writes == [20, 21, 20, 21]

    asyncio.run(scenario())


def test_stop_cancels_in_flight_long_poll() -> None:
    """@brief stop 信号取消正在等待的 long poll / Stop cancels an in-flight long poll."""

    async def scenario() -> None:
        """@brief 运行取消场景 / Run cancellation scenario.

        @return None / None.
        """

        source = _Source([])
        source.block = True
        stop = asyncio.Event()
        task = asyncio.create_task(_listener(source, _Sink()).run(stop))
        await source.called.wait()

        stop.set()
        await asyncio.wait_for(task, timeout=1)

        assert source.offsets == [None]

    asyncio.run(scenario())


def test_external_cancellation_reaps_long_poll_and_stop_tasks() -> None:
    """@brief 强制取消 listener 会回收两个内部 race task / Forced listener cancellation reaps both internal race tasks."""

    async def scenario() -> None:
        """@brief 取消阻塞中的 listener / Cancel a blocked listener.

        @return None / None.
        """

        source = _Source([])
        source.block = True
        task = asyncio.create_task(_listener(source, _Sink()).run(asyncio.Event()))
        await source.called.wait()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.1)
        names = {pending.get_name() for pending in asyncio.all_tasks()}
        assert "telegram-get-updates" not in names
        assert "telegram-listener-stop" not in names

    asyncio.run(scenario())
