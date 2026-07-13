"""@brief 基于通用 Retrieval 的 WorkingMemory 服务 / WorkingMemory service backed by generic Retrieval."""

from __future__ import annotations

from fogmoe_bot.application.memory.ports import WorkingMemoryQuery
from fogmoe_bot.application.retrieval.service import (
    SemanticRecallQuery,
    SemanticRecallReader,
)
from fogmoe_bot.domain.memory.models import (
    GroupMemoryScope,
    PersonalMemoryScope,
    WorkingMemory,
    WorkingMemoryMessage,
)
from fogmoe_bot.domain.retrieval.models import RetrievalScope


class RetrievalWorkingMemory:
    """@brief 将通用 Retrieval evidence 映射为瞬时 WorkingMemory / Map generic retrieval evidence to ephemeral WorkingMemory."""

    def __init__(self, *, recall: SemanticRecallReader) -> None:
        """@brief 注入语义召回端口 / Inject the semantic-recall port.

        @param recall provider-neutral 语义召回 / Provider-neutral semantic recall.
        """

        self._recall = recall

    async def retrieve(self, query: WorkingMemoryQuery) -> WorkingMemory:
        """@brief 不经 Query rewrite 地重新检索历史消息 / Freshly retrieve historical messages without query rewriting.

        @param query 强租户原始 Query / Tenant-scoped raw query.
        @return 独立 WorkingMemory / Independent WorkingMemory.
        """

        evidence = await self._recall.recall(
            SemanticRecallQuery(
                scope=_retrieval_scope(query.scope),
                text=query.text,
                limit=query.limit,
            )
        )
        return WorkingMemory(
            scope=query.scope,
            query=query.text,
            messages=tuple(
                WorkingMemoryMessage(
                    passage_id=item.passage.passage_id,
                    source_kind=item.passage.source_kind,
                    source_id=item.passage.source_id,
                    occurred_at=item.passage.occurred_at,
                    content=item.passage.text,
                    cosine_distance=item.cosine_distance,
                )
                for item in evidence[: query.limit]
            ),
        )


def _retrieval_scope(scope: PersonalMemoryScope | GroupMemoryScope) -> RetrievalScope:
    """@brief 将产品 Memory 域映射为通用检索分区 / Map a product Memory scope to a generic retrieval partition.

    @param scope 个人或群聊 Memory 域 / Personal or group Memory scope.
    @return 通用检索域 / Generic retrieval scope.
    """

    match scope:
        case PersonalMemoryScope(user_id=user_id):
            return RetrievalScope("personal", user_id)
        case GroupMemoryScope(group_id=group_id):
            return RetrievalScope("group", group_id)


__all__ = ["RetrievalWorkingMemory"]
