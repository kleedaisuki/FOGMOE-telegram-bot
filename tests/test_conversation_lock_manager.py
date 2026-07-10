import asyncio
import threading

from fogmoe_bot.application.conversation_lock_manager import ConversationLockManager


def test_lock_manager_serializes_one_conversation_and_releases_slot():
    async def run() -> tuple[list[str], int]:
        manager = ConversationLockManager()
        entered_first = asyncio.Event()
        release_first = asyncio.Event()
        events: list[str] = []

        async def first() -> None:
            async with manager.hold(7):
                events.append("first-enter")
                entered_first.set()
                await release_first.wait()
                events.append("first-exit")

        async def second() -> None:
            await entered_first.wait()
            async with manager.hold(7):
                events.append("second-enter")

        first_task = asyncio.create_task(first())
        second_task = asyncio.create_task(second())
        await entered_first.wait()
        await asyncio.sleep(0)
        assert events == ["first-enter"]
        release_first.set()
        await asyncio.gather(first_task, second_task)
        return events, manager.managed_conversation_count

    events, managed_count = asyncio.run(run())

    assert events == ["first-enter", "first-exit", "second-enter"]
    assert managed_count == 0


def test_lock_manager_serializes_across_event_loop_threads():
    """@brief 不同 event loop 的同会话仍互斥 / Same conversation stays serialized across event loops."""

    manager = ConversationLockManager()
    entered_first = threading.Event()
    release_first = threading.Event()
    entered_second = threading.Event()
    events: list[str] = []

    async def first() -> None:
        async with manager.hold(7):
            events.append("first-enter")
            entered_first.set()
            await asyncio.to_thread(release_first.wait)
            events.append("first-exit")

    async def second() -> None:
        async with manager.hold(7):
            events.append("second-enter")
            entered_second.set()

    first_thread = threading.Thread(target=lambda: asyncio.run(first()))
    second_thread = threading.Thread(target=lambda: asyncio.run(second()))
    first_thread.start()
    assert entered_first.wait(timeout=1)
    second_thread.start()
    assert not entered_second.wait(timeout=0.05)
    release_first.set()
    first_thread.join(timeout=1)
    second_thread.join(timeout=1)

    assert events == ["first-enter", "first-exit", "second-enter"]
    assert manager.managed_conversation_count == 0
