"""@brief Durable Assistant tool runtime 单元测试 / Unit tests for the durable Assistant tool runtime."""

import asyncio

from fogmoe_bot.application.assistant.tool_runtime import (
    AgentRuntime,
    PersistedToolResult,
    ToolEffectRequest,
    ToolExecutionContext,
)
from fogmoe_bot.application.assistant.tools.catalog import DEFAULT_TOOL_CATALOG
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    TurnId,
)


class _Persistence:
    """@brief 记录 receipt 请求的替身 / Double recording receipt requests."""

    def __init__(self) -> None:
        """@brief 初始化记录 / Initialize records."""

        self.requests: list[ToolEffectRequest] = []

    async def execute(self, request: ToolEffectRequest) -> PersistedToolResult:
        """@brief 返回固定结果 / Return a fixed result.

        @param request 工具请求 / Tool request.
        @return 结果 / Result.
        """

        self.requests.append(request)
        if request.tool_name == "generate_voice":
            return PersistedToolResult(
                {
                    "status": "generated",
                    "artifacts": [{"artifact_id": "private-artifact"}],
                },
                False,
            )
        return PersistedToolResult({"status": "updated"}, len(self.requests) > 1)


def _context() -> ToolExecutionContext:
    """@brief 构造 durable context / Build a durable context.

    @return context / Context.
    """

    return ToolExecutionContext(
        turn_id=TurnId.new(),
        conversation_id=ConversationId("assistant-user:42"),
        delivery_stream_id=DeliveryStreamId("telegram:primary:chat:42:thread:0"),
        user_id=42,
        chat_id=42,
        is_group=False,
        group_id=None,
        message_id=7,
    )


def test_runtime_derives_stable_receipt_identity_and_classifies_mutation() -> None:
    """@brief ordinal identity 与 mutation kind 稳定 / Ordinal identity and mutation kind are stable."""

    async def scenario() -> None:
        """@brief 执行两次重放 / Execute two replays."""

        persistence = _Persistence()
        runtime = AgentRuntime(catalog=DEFAULT_TOOL_CATALOG, persistence=persistence)
        context = _context()
        first = await runtime.execute(
            context=context,
            step=2,
            ordinal=1,
            provider_call_id="provider-random-a",
            tool_name="update_impression",
            raw_arguments={"impression": "curious"},
        )
        second = await runtime.execute(
            context=context,
            step=2,
            ordinal=1,
            provider_call_id="provider-random-b",
            tool_name="update_impression",
            raw_arguments={"impression": "curious"},
        )

        assert first.invocation_id == second.invocation_id == "step:2:call:1"
        assert first.effect_kind == "account.update_impression"
        assert persistence.requests[0].mutating is True
        assert (
            persistence.requests[0].request_hash == persistence.requests[1].request_hash
        )
        assert second.replayed is True

    asyncio.run(scenario())


def test_runtime_hides_media_artifact_identity_from_model_feedback() -> None:
    """@brief artifact ID 只留在 receipt/outbox / Artifact ID remains only in receipt/outbox."""

    async def scenario() -> None:
        """@brief 执行媒体调用 / Execute a media invocation."""

        runtime = AgentRuntime(catalog=DEFAULT_TOOL_CATALOG, persistence=_Persistence())
        result = await runtime.execute(
            context=_context(),
            step=0,
            ordinal=0,
            provider_call_id=None,
            tool_name="generate_voice",
            raw_arguments={"text": "hello"},
        )

        assert result.public_result == {
            "status": "generated",
            "message": "Generated media was durably queued for delivery.",
        }
        assert "private-artifact" not in str(result.public_result)

    asyncio.run(scenario())
