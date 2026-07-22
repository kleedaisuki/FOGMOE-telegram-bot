"""@brief 未知 Telegram 命令边界测试 / Unknown Telegram command-boundary tests."""

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
from fogmoe_bot.domain.conversation.identity import UpdateId
from fogmoe_bot.domain.conversation.inbox import InboundUpdate
from fogmoe_bot.presentation.telegram.unknown_command_route import (
    TelegramUnknownCommandPrimaryRoute,
)

_NOW = datetime(2030, 1, 2, 3, 4, tzinfo=UTC)
"""@brief 固定 durable 接收时刻 / Fixed durable receipt instant."""

_UNKNOWN_TEXT = "该命令不可用喵。请使用 /help 查看当前支持的功能。"
"""@brief 未知命令期望文案 / Expected unknown-command text."""


class _Outbound:
    """@brief 记录 durable 回包的内存替身 / In-memory double recording durable replies."""

    def __init__(self) -> None:
        """@brief 初始化空回包集合 / Initialize an empty reply collection.

        @return None / None.
        """

        self.commands: list[StandaloneOutboundCommand] = []
        """@brief 收到的 outbox command / Received outbox commands."""

    async def enqueue(self, command: StandaloneOutboundCommand) -> None:
        """@brief 记录一条 outbox command / Record one outbox command.

        @param command 待投递命令 / Command to deliver.
        @return None / None.
        """

        self.commands.append(command)


def _update(update_id: int, command: str) -> InboundUpdate:
    """@brief 构造当前 Bot 的完整 command Update / Build a complete command update for the current Bot.

    @param update_id Telegram Update 标识 / Telegram Update identifier.
    @param command 无 slash 命令名 / Command name without slash.
    @return durable 来源 Update / Durable source update.
    """

    chat_id = -100_001
    user_id = 42
    payload = {
        "update_id": update_id,
        "message": {
            "message_id": update_id + 10,
            "date": 1_893_456_000,
            "chat": {"id": chat_id, "type": "supergroup"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Klee"},
            "text": f"/{command}@FogMoeBot extra",
            "entities": [
                {
                    "type": "bot_command",
                    "offset": 0,
                    "length": len(command) + len("/@FogMoeBot"),
                }
            ],
        },
    }
    return InboundUpdate.pending(
        update_id=UpdateId(update_id),
        conversation_id=TelegramConversationAddress(
            chat_type="supergroup",
            chat_id=chat_id,
            user_id=user_id,
            message_thread_id=None,
        ).conversation_id,
        payload=payload,
        received_at=_NOW,
    )


@pytest.mark.parametrize(
    "command",
    (
        "gamble",
        "sicbo",
        "rps_game",
        "stake",
        "btc_predict",
        "shop",
        "charge",
        "create_code",
        "rpg",
        "swap",
    ),
)
def test_removed_legacy_command_receives_generic_unknown_command_reply(
    command: str,
) -> None:
    """@brief 已删除旧命令不再保留专用兼容语义 / Removed legacy commands retain no dedicated compatibility behavior.

    @param command 旧命令名 / Legacy command name.
    @return None / None.
    """

    async def scenario() -> None:
        """@brief 执行单个未知命令场景 / Run one unknown-command scenario.

        @return None / None.
        """

        outbound = _Outbound()
        route = TelegramUnknownCommandPrimaryRoute(
            bot_username="FogMoeBot",
            known_commands={"help", "bank", "adventure"},
            outbound=outbound,
        )
        update = _update(17, command)

        assert route.matches(update)
        operation = await route.operation(update)
        await operation.call()

        assert len(outbound.commands) == 1
        reply = outbound.commands[0]
        assert reply.idempotency_key == f"update:17:command:{command}:response"
        assert reply.payload["text"] == _UNKNOWN_TEXT

    asyncio.run(scenario())


def test_known_and_other_bot_commands_are_not_owned_by_unknown_route() -> None:
    """@brief 正式命令及其他 Bot 目标不被 fallback 抢占 / Formal commands and other-Bot targets are not claimed by fallback.

    @return None / None.
    """

    route = TelegramUnknownCommandPrimaryRoute(
        bot_username="FogMoeBot",
        known_commands={"help", "bank", "adventure"},
        outbound=_Outbound(),
    )
    assert not route.matches(_update(18, "bank"))

    update = _update(19, "obsolete")
    message = update.payload["message"]
    assert isinstance(message, dict)
    text = message["text"]
    assert isinstance(text, str)
    # Keep the original command-entity width intact: Telegram offsets describe
    # the received payload, so changing it here would create an invalid update
    # rather than exercising another bot target.
    message["text"] = text.replace("@FogMoeBot", "@OtherBotX")
    assert not route.matches(update)
