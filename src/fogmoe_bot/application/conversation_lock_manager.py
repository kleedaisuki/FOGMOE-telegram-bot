"""@brief 会话级并发协调 / Conversation-level concurrency coordination."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator


@dataclass
class _LockSlot:
    """@brief 单个会话的锁槽位 / Lock slot for one conversation."""

    lock: asyncio.Lock
    users: int = 0


class ConversationLockManager:
    """@brief 串行化同一会话的应用任务 / Serialize application tasks for one conversation.

    @note 此协调器仅保证单个 bot 进程内的互斥；多实例部署需要共享锁实现。
    / This coordinator only guarantees mutual exclusion within one bot process;
    multi-instance deployments require a shared lock implementation.
    """

    def __init__(self) -> None:
        """@brief 初始化锁管理器 / Initialize the lock manager."""
        self._slots: dict[int, _LockSlot] = {}

    @property
    def managed_conversation_count(self) -> int:
        """@brief 当前受管理会话数 / Number of currently managed conversations.

        @return 正在持有或等待锁的会话数 / Conversations holding or awaiting a lock.
        """
        return len(self._slots)

    @asynccontextmanager
    async def hold(self, conversation_id: int) -> AsyncIterator[None]:
        """@brief 在会话锁内运行 / Run inside a conversation lock.

        @param conversation_id 对话标识 / Conversation identifier.
        @return 异步上下文管理器 / Asynchronous context manager.
        """
        slot = self._slots.get(conversation_id)
        if slot is None:
            slot = _LockSlot(lock=asyncio.Lock())
            self._slots[conversation_id] = slot
        slot.users += 1
        try:
            async with slot.lock:
                yield
        finally:
            slot.users -= 1
            if slot.users == 0 and self._slots.get(conversation_id) is slot:
                self._slots.pop(conversation_id)


CONVERSATION_LOCK_MANAGER = ConversationLockManager()
"""@brief 进程内共享锁管理器 / Process-local shared lock manager."""
