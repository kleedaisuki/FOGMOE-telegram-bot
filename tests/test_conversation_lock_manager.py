import asyncio

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
