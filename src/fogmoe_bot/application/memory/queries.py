"""@brief 长期记忆查询端口与值对象 / Long-term-memory query ports and values."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from fogmoe_bot.domain.memory.models import MemoryRecord, MemorySearchHit


@dataclass(frozen=True, slots=True)
class MemoryPageQuery:
    """@brief 永久记忆分页查询 / Permanent-memory page query.

    @param owner_user_id 所有者 / Owning user.
    @param limit 最大返回数 / Maximum result count.
    @param offset 结果偏移 / Result offset.
    @param newest_first 是否最新优先 / Whether newest records come first.
    @param summaries_only 是否只返回摘要记录 / Whether to return only summarized records.
    """

    owner_user_id: int
    limit: int
    offset: int = 0
    newest_first: bool = True
    summaries_only: bool = False

    def __post_init__(self) -> None:
        """@brief 校验有界分页 / Validate bounded pagination.

        @return None / None.
        @raise ValueError 查询越界 / Query is outside supported bounds.
        """

        if isinstance(self.owner_user_id, bool) or self.owner_user_id <= 0:
            raise ValueError("Memory query owner_user_id must be positive")
        if not 1 <= self.limit <= 500 or self.offset < 0:
            raise ValueError("Memory pagination is outside its bounds")


@dataclass(frozen=True, slots=True)
class MemorySearchQuery:
    """@brief 永久记忆正则检索查询 / Permanent-memory regex-search query.

    @param owner_user_id 所有者 / Owning user.
    @param pattern 有界正则表达式 / Bounded regular expression.
    @param limit 最大命中数 / Maximum hit count.
    @param oldest_first 是否最旧优先 / Whether oldest records come first.
    """

    owner_user_id: int
    pattern: str
    limit: int
    oldest_first: bool = False

    def __post_init__(self) -> None:
        """@brief 校验检索输入 / Validate search input.

        @return None / None.
        @raise ValueError pattern 或 limit 非法 / Pattern or limit is invalid.
        """

        pattern = self.pattern.strip()
        if isinstance(self.owner_user_id, bool) or self.owner_user_id <= 0:
            raise ValueError("Memory search owner_user_id must be positive")
        if not pattern or len(pattern) > 1000:
            raise ValueError("Memory search pattern must contain 1-1000 characters")
        if not 1 <= self.limit <= 50:
            raise ValueError("Memory search limit must be between 1 and 50")
        object.__setattr__(self, "pattern", pattern)


@dataclass(frozen=True, slots=True)
class MemorySearchResult:
    """@brief 记忆检索结果与非致命诊断 / Memory-search result and non-fatal diagnostic.

    @param hits 有序命中 / Ordered hits.
    @param warning 可选稳定警告 / Optional stable warning.
    """

    hits: tuple[MemorySearchHit, ...]
    warning: str | None = None

    def __post_init__(self) -> None:
        """@brief 规范化可选警告 / Normalize the optional warning.

        @return None / None.
        @raise ValueError warning 为空 / Warning is blank.
        """

        if self.warning is not None:
            warning = self.warning.strip()
            if not warning:
                raise ValueError("Memory search warning cannot be blank")
            object.__setattr__(self, "warning", warning[:500])


class MemoryReader(Protocol):
    """@brief Assistant 所需的长期记忆只读端口 / Long-term-memory read port required by Assistant."""

    async def count_summaries(self, owner_user_id: int) -> int:
        """@brief 统计 quota 内可见摘要 / Count quota-visible summaries.

        @param owner_user_id 所有者 / Owning user.
        @return 可见摘要数 / Visible summary count.
        """

        ...

    async def read_page(self, query: MemoryPageQuery) -> Sequence[MemoryRecord]:
        """@brief 读取 quota 内的有界窗口 / Read a quota-visible bounded page.

        @param query 已校验分页查询 / Validated page query.
        @return 不可变记忆记录序列 / Immutable memory-record sequence.
        """

        ...

    async def search(self, query: MemorySearchQuery) -> MemorySearchResult:
        """@brief 执行有界记忆检索 / Execute bounded memory search.

        @param query 已校验检索查询 / Validated search query.
        @return 命中与非致命诊断 / Hits and non-fatal diagnostics.
        """

        ...


__all__ = [
    "MemoryPageQuery",
    "MemoryReader",
    "MemorySearchQuery",
    "MemorySearchResult",
]
