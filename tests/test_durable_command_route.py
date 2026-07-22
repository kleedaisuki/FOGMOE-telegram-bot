"""@brief Durable Telegram command route 与静态 handler 测试 / Tests for the durable Telegram command route and static handler."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from fogmoe_bot.application.conversation.standalone_outbound import (
    StandaloneOutboundCommand,
)
from fogmoe_bot.application.conversation.telegram_identity import (
    TelegramConversationAddress,
)
from fogmoe_bot.domain.conversation.identity import (
    UpdateId,
)
from fogmoe_bot.domain.conversation.inbox import InboundUpdate
from fogmoe_bot.domain.conversation.payloads import (
    JsonObject,
    JsonValue,
)
from fogmoe_bot.presentation.telegram.basic_handlers import (
    StaticTelegramCommandHandler,
)
from fogmoe_bot.presentation.telegram.command_cooldown_guard import (
    ParsedTelegramCommand,
)
from fogmoe_bot.presentation.telegram.command_route import (
    TelegramDurableCommandPrimaryRoute,
)

NOW = datetime(2030, 1, 1, tzinfo=UTC)
"""@brief 固定接收时刻 / Fixed receipt time."""


class RecordingOutbound:
    """@brief 记录出站命令 / Record outbound commands."""

    def __init__(self) -> None:
        """@brief 初始化记录 / Initialize the recording."""

        self.commands: list[StandaloneOutboundCommand] = []
        """@brief 收到的命令 / Received commands."""

    async def enqueue(self, command: StandaloneOutboundCommand) -> None:
        """@brief 记录命令 / Record a command.

        @param command standalone outbound command / Standalone outbound command.
        @return None / None.
        """

        self.commands.append(command)


class RecordingHandler:
    """@brief 可控 durable command handler / Controllable durable command handler."""

    def __init__(self, *commands: str) -> None:
        """@brief 声明命令所有权 / Declare command ownership.

        @param commands owned commands / Owned commands.
        """

        self._commands = frozenset(commands)
        """@brief owned commands / Owned commands."""
        self.calls: list[tuple[InboundUpdate, ParsedTelegramCommand]] = []
        """@brief handler calls / Handler calls."""

    @property
    def commands(self) -> frozenset[str]:
        """@brief 返回命令所有权 / Return command ownership.

        @return owned commands / Owned commands.
        """

        return self._commands

    async def handle(
        self,
        update: InboundUpdate,
        command: ParsedTelegramCommand,
    ) -> None:
        """@brief 记录调用 / Record a call.

        @param update durable Update / Durable Update.
        @param command parsed command / Parsed command.
        @return None / None.
        """

        self.calls.append((update, command))


def _inbound(update_id: int, token: str) -> InboundUpdate:
    """@brief 构造命令 Update / Build a command Update.

    @param update_id Update ID / Update identifier.
    @param token command token / Command token.
    @return durable Update / Durable Update.
    """

    entity_values: list[JsonValue] = [
        {"type": "bot_command", "offset": 0, "length": len(token)}
    ]
    """@brief command entity JSON / Command-entity JSON."""
    message: JsonObject = {
        "message_id": 88,
        "date": 1_893_456_000,
        "message_thread_id": 7,
        "chat": {"id": -100, "type": "supergroup"},
        "from": {
            "id": 42,
            "is_bot": False,
            "first_name": "Klee",
            "username": "klee",
        },
        "text": f"{token} one two",
        "entities": entity_values,
    }
    return InboundUpdate.pending(
        update_id=UpdateId(update_id),
        conversation_id=TelegramConversationAddress(
            chat_type="supergroup",
            chat_id=-100,
            user_id=42,
            message_thread_id=7,
        ).conversation_id,
        payload={"update_id": update_id, "message": message},
        received_at=NOW,
    )


def test_route_owns_targeted_command_and_executes_one_handler() -> None:
    """@brief route 显式选择并执行唯一 handler / The route explicitly selects and executes one handler."""

    handler = RecordingHandler("help")
    route = TelegramDurableCommandPrimaryRoute(
        bot_username="FogMoeBot",
        handlers=(handler,),
    )
    update = _inbound(10, "/help@FogMoeBot")

    assert route.matches(update)
    operation = asyncio.run(route.operation(update))
    assert operation.key.aggregate_type == "conversation"
    assert operation.key.identity == ("assistant-group:-100:thread:7",)
    asyncio.run(operation.call())

    assert len(handler.calls) == 1
    parsed = handler.calls[0][1]
    assert parsed.argument_text == "one two"
    assert parsed.arguments == ("one", "two")
    assert parsed.username == "klee"


def test_route_rejects_duplicate_command_ownership() -> None:
    """@brief 重复命令所有权在 bootstrap 失败 / Duplicate command ownership fails at bootstrap."""

    with pytest.raises(ValueError, match="Duplicate"):
        TelegramDurableCommandPrimaryRoute(
            bot_username="FogMoeBot",
            handlers=(RecordingHandler("help"), RecordingHandler("help")),
        )


def test_static_handler_writes_deterministic_help_outbox() -> None:
    """@brief `/help` 只写 durable outbox / `/help` only writes the durable outbox."""

    outbound = RecordingOutbound()
    handler = StaticTelegramCommandHandler(
        outbound=outbound,
        help_text="**Help**\n/start",
    )
    route = TelegramDurableCommandPrimaryRoute(
        bot_username="FogMoeBot",
        handlers=(handler,),
    )
    update = _inbound(11, "/help")

    operation = asyncio.run(route.operation(update))
    asyncio.run(operation.call())

    assert len(outbound.commands) == 1
    command = outbound.commands[0]
    assert command.idempotency_key == "update:11:command:help:response"
    assert command.delivery_stream_id.value == "telegram:primary:chat:-100:thread:7"
    assert command.payload == {
        "chat_id": -100,
        "text": "**Help**\n/start",
        "parse_mode": "Markdown",
        "message_thread_id": 7,
        "reply_to_message_id": 88,
        "disable_web_page_preview": True,
    }
