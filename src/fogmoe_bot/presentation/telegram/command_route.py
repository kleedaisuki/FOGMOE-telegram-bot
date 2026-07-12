"""@brief Provider-neutral durable Telegram command route / Provider-neutral durable Telegram command route."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from fogmoe_bot.application.conversation.router import (
    RoutedOperation,
    conversation_aggregate_key,
)
from fogmoe_bot.application.runtime import WorkPriority
from fogmoe_bot.domain.conversation.inbox import InboundUpdate

from .command_cooldown_guard import (
    MalformedTelegramCommandUpdate,
    ParsedTelegramCommand,
    parse_telegram_command,
)


class DurableTelegramCommandHandler(Protocol):
    """@brief 一个或多个 durable Telegram command 的状态转移函数 / State-transition function for one or more durable Telegram commands."""

    @property
    def commands(self) -> frozenset[str]:
        """@brief 返回互斥命令所有权 / Return exclusive command ownership.

        @return 无 slash 的小写命令 / Lowercase commands without slashes.
        """

        ...

    async def handle(
        self,
        update: InboundUpdate,
        command: ParsedTelegramCommand,
    ) -> None:
        """@brief 执行幂等状态转移 / Execute an idempotent state transition.

        @param update durable source Update / Durable source Update.
        @param command 已解析命令 envelope / Parsed command envelope.
        @return None / None.
        """

        ...


class TelegramDurableCommandPrimaryRoute:
    """@brief 将显式命令表映射到 durable handlers / Map an explicit command table to durable handlers."""

    def __init__(
        self,
        *,
        bot_username: str,
        handlers: Sequence[DurableTelegramCommandHandler],
    ) -> None:
        """@brief 构造互斥命令 route / Build an exclusive command route.

        @param bot_username 当前 Bot username / Current Bot username.
        @param handlers durable command handlers / Durable command handlers.
        @raise ValueError username、handler 列表或命令所有权非法 / Invalid username, handler list, or command ownership.
        """

        username = bot_username.removeprefix("@").strip().casefold()
        if not username:
            raise ValueError("bot_username cannot be blank")
        if not handlers:
            raise ValueError("At least one durable command handler is required")
        by_command: dict[str, DurableTelegramCommandHandler] = {}
        """@brief 命令到唯一 handler 的映射 / Command-to-unique-handler mapping."""
        for handler in handlers:
            if not handler.commands:
                raise ValueError("A durable command handler cannot own zero commands")
            for raw_command in handler.commands:
                command = raw_command.strip().casefold()
                if not command or command != raw_command:
                    raise ValueError(
                        "Durable command names must be normalized lowercase"
                    )
                if command in by_command:
                    raise ValueError(f"Duplicate durable command ownership: {command}")
                by_command[command] = handler
        self._bot_username = username
        self._handlers = by_command

    @property
    def name(self) -> str:
        """@brief 返回稳定 route 名 / Return the stable route name.

        @return ``telegram-durable-commands`` / ``telegram-durable-commands``.
        """

        return "telegram-durable-commands"

    @property
    def commands(self) -> frozenset[str]:
        """@brief 返回本 route 拥有的命令 / Return commands owned by this route.

        @return 不可变 command set / Immutable command set.
        """

        return frozenset(self._handlers)

    def matches(self, update: InboundUpdate) -> bool:
        """@brief 判断 Update 是否由 durable command table 独占 / Decide whether the durable command table exclusively owns an Update.

        @param update durable Update / Durable Update.
        @return 当前 Bot 的已知命令为 True / True for a known command targeting this Bot.
        """

        try:
            parsed = parse_telegram_command(update)
        except MalformedTelegramCommandUpdate:
            return False
        return parsed is not None and self._owns(parsed)

    async def operation(self, update: InboundUpdate) -> RoutedOperation:
        """@brief 构造 runtime-admitted command 操作 / Build a runtime-admitted command operation.

        @param update 已匹配 durable Update / Matched durable Update.
        @return canonical-conversation keyed operation / Canonical-conversation keyed operation.
        @raise ValueError route 不拥有 Update / The route does not own the Update.
        """

        parsed = parse_telegram_command(update)
        if parsed is None or not self._owns(parsed):
            raise ValueError("Durable command operation requires a matching Update")
        handler = self._handlers[parsed.command]

        async def call() -> None:
            """@brief 调用唯一命令状态转移函数 / Invoke the unique command state-transition function.

            @return None / None.
            """

            await handler.handle(update, parsed)

        return RoutedOperation(
            name=f"telegram-command:{parsed.command}:{int(update.update_id)}",
            key=conversation_aggregate_key(update.conversation_id),
            call=call,
            priority=WorkPriority.HIGH,
        )

    def _owns(self, parsed: ParsedTelegramCommand) -> bool:
        """@brief 校验命令及 target ownership / Validate command and target ownership.

        @param parsed 已解析命令 / Parsed command.
        @return owned 为 True / True when owned.
        """

        return parsed.command in self._handlers and (
            parsed.target is None or parsed.target.casefold() == self._bot_username
        )


__all__ = [
    "DurableTelegramCommandHandler",
    "TelegramDurableCommandPrimaryRoute",
]
