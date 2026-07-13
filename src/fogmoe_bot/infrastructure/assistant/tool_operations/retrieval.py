"""@brief Retrieval-backed Assistant operations / 基于 Retrieval 的 Assistant operations."""

from fogmoe_bot.application.assistant.tool_runtime import ToolEffectRequest
from fogmoe_bot.application.retrieval import SemanticRecallQuery, SemanticRecallReader
from fogmoe_bot.domain.conversation.payloads import JsonValue

from .parsing import bounded_int, iso_instant, required_text


async def recall_conversation_history(
    request: ToolEffectRequest,
    *,
    recall: SemanticRecallReader,
) -> JsonValue:
    """@brief 召回当前认证用户的私聊情景证据 / Recall private episodic evidence for the authenticated user.

    @param request 已验证工具请求 / Validated tool request.
    @param recall 语义召回端口 / Semantic-recall port.
    @return 有 provenance 的有界结果 / Bounded results with provenance.
    """

    query = required_text(request.arguments, "query")
    limit = bounded_int(request.arguments, "limit", minimum=1, maximum=20)
    evidence = await recall.recall(
        SemanticRecallQuery(
            owner_user_id=request.context.user_id,
            text=query,
            limit=limit,
        )
    )
    return {
        "user_id": request.context.user_id,
        "query": query,
        "trust": "untrusted_historical_data",
        "results": [
            {
                "source_turn_id": str(item.passage.source_id),
                "occurred_at": iso_instant(item.passage.occurred_at),
                "excerpt": item.passage.text,
                "cosine_distance": item.cosine_distance,
            }
            for item in evidence[:limit]
        ],
    }


__all__ = ["recall_conversation_history"]
