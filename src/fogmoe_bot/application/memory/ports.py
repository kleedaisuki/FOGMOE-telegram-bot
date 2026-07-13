"""@brief WorkingMemory 应用端口 / WorkingMemory application ports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from fogmoe_bot.domain.memory.models import MemoryScope, WorkingMemory


@dataclass(frozen=True, slots=True)
class WorkingMemoryQuery:
    """@brief 强租户、未改写的 WorkingMemory Query / Tenant-scoped, unrevised WorkingMemory query.

    @param scope 由授权上下文确定的个人或群聊域 / Personal or group scope derived from authorization context.
    @param text 原始当前 Query / Raw current query.
    @param limit 最大消息数 / Maximum message count.
    """

    scope: MemoryScope
    text: str
    limit: int

    def __post_init__(self) -> None:
        """@brief 校验 Query / Validate the query.

        @return None / None.
        @raise ValueError owner、文本或 limit 非法 / Invalid owner, text, or limit.
        """

        text = self.text.strip()
        if not text or len(text) > 20_000:
            raise ValueError("WorkingMemory query must contain 1-20000 characters")
        if not 1 <= self.limit <= 20:
            raise ValueError("WorkingMemory limit must be between 1 and 20")
        object.__setattr__(self, "text", text)


class WorkingMemoryReader(Protocol):
    """@brief 每次调用均执行新检索的 WorkingMemory 端口 / WorkingMemory port performing a fresh retrieval per call."""

    async def retrieve(self, query: WorkingMemoryQuery) -> WorkingMemory:
        """@brief 为一个模型 Query 重新抓取 WorkingMemory / Retrieve WorkingMemory afresh for one model query.

        @param query 已验证的原始 Query / Validated raw query.
        @return 本次 Query 独占的 WorkingMemory / WorkingMemory owned by this query.
        @note 实现不得跨 Query 缓存检索结果 / Implementations must not cache results across queries.
        """

        ...


__all__ = ["WorkingMemoryQuery", "WorkingMemoryReader"]
