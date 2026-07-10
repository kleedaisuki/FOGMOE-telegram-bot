"""@brief 会话级并发协调 / Conversation-level concurrency coordination."""

import asyncio
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator


@dataclass
class _LockSlot:
    """@brief 单个会话的锁槽位 / Lock slot for one conversation."""

    lock: threading.Lock
    users: int = 0


class ConversationLockManager:
    """@brief 串行化同一会话的应用任务 / Serialize application tasks for one conversation.

    @note 该锁可跨本进程的 event loop 与线程使用；多实例部署仍需要共享锁实现。
    / This lock works across event loops and threads in this process; multi-instance
    deployments still require a shared lock implementation.
    """

    def __init__(self) -> None:
        """@brief 初始化锁管理器 / Initialize the lock manager."""
        self._slots: dict[int, _LockSlot] = {}
        self._slots_lock = threading.Lock()

    @property
    def managed_conversation_count(self) -> int:
        """@brief 当前受管理会话数 / Number of currently managed conversations.

        @return 正在持有或等待锁的会话数 / Conversations holding or awaiting a lock.
        """
        with self._slots_lock:
            return len(self._slots)

    @asynccontextmanager
    async def hold(self, conversation_id: int) -> AsyncIterator[None]:
        """@brief 在会话锁内运行 / Run inside a conversation lock.

        @param conversation_id 对话标识 / Conversation identifier.
        @return 异步上下文管理器 / Asynchronous context manager.
        """
        with self._slots_lock:
            slot = self._slots.get(conversation_id)
            if slot is None:
                slot = _LockSlot(lock=threading.Lock())
                self._slots[conversation_id] = slot
            slot.users += 1
        try:
            delay = 0.001
            while not slot.lock.acquire(blocking=False):
                await asyncio.sleep(delay)
                delay = min(delay * 2, 0.05)
            try:
                yield
            finally:
                slot.lock.release()
        finally:
            with self._slots_lock:
                slot.users -= 1
                if slot.users == 0 and self._slots.get(conversation_id) is slot:
                    self._slots.pop(conversation_id)


CONVERSATION_LOCK_MANAGER = ConversationLockManager()
"""@brief 进程内共享锁管理器 / Process-local shared lock manager."""
