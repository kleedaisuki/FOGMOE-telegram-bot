"""@brief Telegram durable Conversation reset route 测试 / Telegram durable Conversation-reset route tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast

from fogmoe_bot.application.conversation.reset import (
    ConversationResetResult,
    ResetConversation,
)
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    UpdateId,
)
from fogmoe_bot.domain.conversation.inbox import InboundUpdate
from fogmoe_bot.presentation.telegram.reset_route import (
    TelegramConversationResetPrimaryRoute,
)


NOW = datetime(2030, 1, 1, tzinfo=UTC)
"""@brief 固定 listener 接收时间 / Fixed listener receipt time."""


class _Persistence:
    """@brief 记录 reset 命令的持久化替身 / Persistence double recording reset commands."""

    def __init__(self) -> None:
        """@brief 初始化命令列表 / Initialize the command list."""

        self.commands: list[ResetConversation] = []

    async def reset(self, command: ResetConversation) -> ConversationResetResult:
        """@brief 记录命令 / Record a command.

        @param command reset 命令 / Reset command.
        @return 未由 route 解释的测试回执 / Test receipt not interpreted by the route.
        """

        self.commands.append(command)
        return cast(ConversationResetResult, object())


def _inbound(command: str = "/clear") -> InboundUpdate:
    """@brief 构造 Telegram command Update / Build a Telegram command Update.

    @param command 完整 command 文本 / Full command text.
    @return durable inbound Update / Durable inbound Update.
    """

    return InboundUpdate.pending(
        update_id=UpdateId(99),
        conversation_id=ConversationId("assistant-user:42"),
        payload={
            "update_id": 99,
            "message": {
                "message_id": 7,
                "date": 1_893_456_000,
                "message_thread_id": 3,
                "chat": {"id": 42, "type": "private"},
                "from": {
                    "id": 42,
                    "is_bot": False,
                    "first_name": "Klee",
                },
                "text": command,
                "entities": [
                    {
                        "type": "bot_command",
                        "offset": 0,
                        "length": len(command),
                    }
                ],
            },
        },
        received_at=NOW,
    )


def test_route_exclusively_matches_clear_for_the_current_bot() -> None:
    """@brief route 只拥有 `/clear` 与当前 Bot 定向形式 / The route owns only `/clear` and its current-Bot-qualified form."""

    route = TelegramConversationResetPrimaryRoute(
        persistence=_Persistence(),
        bot_username="FogMoeBot",
    )

    assert route.matches(_inbound())
    assert route.matches(_inbound("/clear@FogMoeBot"))
    assert not route.matches(_inbound("/clear@OtherBot"))
    assert not route.matches(_inbound("/help"))


def test_operation_persists_reset_and_confirmation_without_direct_delivery() -> None:
    """@brief operation 只写 reset+outbox，重放保持同一 identity / The operation writes only reset plus outbox and preserves identity on replay."""

    async def scenario() -> None:
        """@brief 两次执行同一 durable operation / Execute one durable operation twice.

        @return None / None.
        """

        persistence = _Persistence()
        route = TelegramConversationResetPrimaryRoute(
            persistence=persistence,
            bot_username="FogMoeBot",
        )
        operation = await route.operation(_inbound())

        assert operation.key.aggregate_type == "conversation"
        assert operation.key.identity == ("assistant-user:42",)
        await operation.call()
        await operation.call()

        first, replay = persistence.commands
        assert first == replay
        assert first.source.update_id == UpdateId(99)
        assert first.conversation_id == ConversationId("assistant-user:42")
        assert first.requested_at == NOW
        assert first.confirmation.turn_id is None
        assert first.confirmation.idempotency_key == (
            "update:99:conversation-reset-confirmation"
        )
        assert first.confirmation.delivery_stream_id.value == (
            "telegram:primary:chat:42:thread:3"
        )
        assert first.confirmation.payload["chat_id"] == 42
        assert first.confirmation.payload["reply_to_message_id"] == 7
        text = str(first.confirmation.payload["text"])
        assert "context has been cleared" in text
        assert "Memory and User Profile are unchanged" in text

    asyncio.run(scenario())
