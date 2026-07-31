"""@brief 群消息投影的应用端口 / Application ports for the group-message projection."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from fogmoe_bot.domain.chat.group_messages import (
    GroupContextQuery,
    GroupMessage,
    GroupMessageObservation,
)


class GroupMessageWriter(Protocol):
    """@brief 写入 canonical 群消息修订的窄端口 / Narrow port for writing canonical group-message revisions."""

    async def project(self, observation: GroupMessageObservation) -> None:
        """@brief 幂等推进一条消息修订 / Idempotently advance one message revision.

        @param observation 已验证领域观察 / Validated domain observation.
        @return None / None.
        """

        ...


class GroupContextReader(Protocol):
    """@brief 读取当前消息之前 canonical 群上下文的窄端口 / Narrow port for canonical group context before a message."""

    async def fetch_before(self, query: GroupContextQuery) -> Sequence[GroupMessage]:
        """@brief 读取一个已验证、有界的 Topic 查询 / Read one validated bounded topic query.

        @param query 领域查询值 / Domain query value.
        @return 最旧到最新的消息序列 / Messages ordered oldest to newest.
        """

        ...


__all__ = ["GroupContextReader", "GroupMessageWriter"]
