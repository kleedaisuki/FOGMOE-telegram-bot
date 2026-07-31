"""@brief Assistant diary 工具默认值边界测试 / Assistant diary tool-default boundary tests."""

import asyncio

import pytest

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
from fogmoe_bot.infrastructure.assistant.tool_operations import diary


class _DiaryPersistence:
    """@brief 将真实 diary operation 接到内存 receipt port / Connect the real diary operation to an in-memory receipt port."""

    def __init__(self) -> None:
        """@brief 初始化请求记录 / Initialize the request record.

        @return None / None.
        """

        self.requests: list[ToolEffectRequest] = []

    async def execute(self, request: ToolEffectRequest) -> PersistedToolResult:
        """@brief 记录请求并执行只读 diary operation / Record a request and execute the read-only diary operation.

        @param request 通过 catalog 校验后的工具请求 / Tool request validated by the catalog.
        @return 非重放的规范结果 / Canonical non-replayed result.
        """

        self.requests.append(request)
        return PersistedToolResult(
            await diary.execute_diary(request, connection=None),
            replayed=False,
        )


def _tool_context() -> ToolExecutionContext:
    """@brief 构造最小 diary 工具授权上下文 / Build the minimal diary tool authorization context.

    @return 受限于单个用户的 durable 工具上下文 / Durable tool context scoped to one user.
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


def test_diary_read_materializes_omitted_page_at_operation_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief Pydantic 省略默认值后 diary operation 仍读取第一页 / Diary reads page one after Pydantic omits its default.

    @param monkeypatch pytest 注入的替换工具 / pytest replacement utility.
    @return None / None.
    """

    async def scenario() -> None:
        """@brief 执行 runtime 到真实 diary operation 的完整只读路径 / Execute the complete read-only runtime-to-diary path.

        @return None / None.
        """

        fetches: list[tuple[str, tuple[object, ...]]] = []

        async def fetch_one(
            statement: str,
            parameters: tuple[object, ...],
            *,
            connection: object | None = None,
        ) -> None:
            """@brief 模拟空 diary 查询 / Simulate an empty diary query.

            @param statement SQL 文本 / SQL text.
            @param parameters 已绑定 SQL 参数 / Bound SQL parameters.
            @param connection 可选事务连接 / Optional transaction connection.
            @return 不存在 diary 页时的 None / None when the diary page is absent.
            """

            assert connection is None
            fetches.append((statement, parameters))
            return None

        monkeypatch.setattr(diary.db, "fetch_one", fetch_one)
        persistence = _DiaryPersistence()
        result = await AgentRuntime(
            catalog=DEFAULT_TOOL_CATALOG,
            persistence=persistence,
        ).execute(
            context=_tool_context(),
            step=0,
            ordinal=0,
            provider_call_id="diary-read",
            tool_name="user_diary",
            raw_arguments={"action": "read"},
        )

        assert persistence.requests[0].arguments == {"action": "read"}
        assert isinstance(result.public_result, dict)
        assert result.public_result["page"] == 1
        assert result.public_result["content"] == ""
        assert fetches[0][1] == (42, 1)

    asyncio.run(scenario())
