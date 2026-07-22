"""@brief 未注册 Telegram 命令的明确拒绝边界 / Explicit rejection boundary for unknown Telegram commands.

公开命令不是可扩展的兼容协议：未在当前 catalog 中声明的命令必须在进入 Assistant
之前被确定性拒绝。这样移除旧经济实现后，历史命令、拼写错误和未来已删除命令都不会
回落到任意文本推理或遗留处理器。
/ Public commands are not an extensible compatibility protocol: a command not declared in the
current catalog is deterministically rejected before it can reach the Assistant.  This prevents
historical commands, typos, and future removals from falling through to arbitrary text inference
or a legacy handler.
"""

from __future__ import annotations

from collections.abc import Collection

from fogmoe_bot.application.conversation.router import (
    RoutedOperation,
    conversation_aggregate_key,
)
from fogmoe_bot.application.conversation.standalone_outbound import (
    StandaloneOutboundCapability,
)
from fogmoe_bot.application.runtime import WorkPriority
from fogmoe_bot.domain.conversation.inbox import InboundUpdate

from .command_cooldown_guard import (
    MalformedTelegramCommandUpdate,
    ParsedTelegramCommand,
    parse_telegram_command,
)
from .delivery import enqueue_command_reply

_UNKNOWN_COMMAND_TEXT = "该命令不可用喵。请使用 /help 查看当前支持的功能。"
"""@brief 未知命令的固定帮助反馈 / Fixed help feedback for an unknown command."""


class TelegramUnknownCommandPrimaryRoute:
    """@brief 显式拒绝未声明的当前 Bot 命令 / Explicitly reject undeclared commands for this Bot.

    @note 该 route 不枚举、也不保留任何历史命令名；删除一个功能只需从其正式 catalog
    移除，随后自动由本 route 安全拒绝。/ This route neither enumerates nor retains historical
    command names.  Removing a feature only removes it from the formal catalog; this route then
    rejects it safely automatically.
    """

    def __init__(
        self,
        *,
        bot_username: str,
        known_commands: Collection[str],
        outbound: StandaloneOutboundCapability,
    ) -> None:
        """@brief 注入 Bot 身份、正式命令集合与 durable outbox / Inject Bot identity, formal commands, and durable outbox.

        @param bot_username 当前 Bot username / Current Bot username.
        @param known_commands 当前 catalog 与专用 route 所有命令 / Commands owned by the current catalog and dedicated routes.
        @param outbound 可靠回复投递能力 / Durable reply-delivery capability.
        @return None / None.
        @raise ValueError username 或命令名不规范时抛出 / Raised for an invalid username or command name.
        """

        username = bot_username.removeprefix("@").strip().casefold()
        if not username:
            raise ValueError("bot_username cannot be blank")
        normalized = frozenset(command.strip().casefold() for command in known_commands)
        if not normalized or "" in normalized:
            raise ValueError("known_commands must contain non-blank command names")
        self._bot_username = username
        """@brief 规范 Bot username / Canonical Bot username."""
        self._known_commands = normalized
        """@brief 唯一正式命令表 / Sole formal command table."""
        self._outbound = outbound
        """@brief durable standalone outbox 能力 / Durable standalone-outbox capability."""

    @property
    def name(self) -> str:
        """@brief 返回稳定 route 名 / Return the stable route name.

        @return ``telegram-unknown-command`` / ``telegram-unknown-command``.
        """

        return "telegram-unknown-command"

    def matches(self, update: InboundUpdate) -> bool:
        """@brief 判断 Update 是否是本 Bot 的未知命令 / Decide whether an Update is an unknown command for this Bot.

        @param update durable 来源 Update / Durable source Update.
        @return 未声明且目标为当前 Bot 的命令时为 True / True for an undeclared command targeting this Bot.
        @note 畸形 command payload 交给现有入口隔离机制处理，不在此处吞掉错误 /
            Malformed command payloads remain owned by the existing ingress quarantine path.
        """

        try:
            parsed = parse_telegram_command(update)
        except MalformedTelegramCommandUpdate:
            return False
        return parsed is not None and self._is_unknown_for_this_bot(parsed)

    async def operation(self, update: InboundUpdate) -> RoutedOperation:
        """@brief 构造固定帮助回复的幂等操作 / Build the idempotent fixed-help reply operation.

        @param update 已匹配的 durable Update / Matched durable Update.
        @return 当前会话串行执行的回复操作 / Reply operation serialized by the current conversation.
        @raise ValueError Update 不属于未知命令 route 时抛出 / Raised when the update is not owned by this route.
        """

        parsed = parse_telegram_command(update)
        if parsed is None or not self._is_unknown_for_this_bot(parsed):
            raise ValueError("Unknown-command operation requires an owned command")

        async def call() -> None:
            """@brief 向 durable outbox 投递固定帮助 / Enqueue the fixed help through the durable outbox.

            @return None / None.
            """

            await enqueue_command_reply(
                self._outbound,
                update,
                parsed,
                _UNKNOWN_COMMAND_TEXT,
            )

        return RoutedOperation(
            name=f"telegram-unknown-command:{parsed.command}:{int(update.update_id)}",
            key=conversation_aggregate_key(update.conversation_id),
            call=call,
            priority=WorkPriority.HIGH,
        )

    def _is_unknown_for_this_bot(self, command: ParsedTelegramCommand) -> bool:
        """@brief 验证 target 并排除正式命令 / Validate the target and exclude formally owned commands.

        @param command 已解析命令 envelope / Parsed command envelope.
        @return 应由本 route 拒绝时为 True / True when this route should reject it.
        """

        if (
            command.target is not None
            and command.target.casefold() != self._bot_username
        ):
            return False
        return command.command not in self._known_commands


__all__ = ["TelegramUnknownCommandPrimaryRoute"]
