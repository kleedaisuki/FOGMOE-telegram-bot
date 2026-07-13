"""@brief Telegram durable 命令冷却 Guard / Telegram durable command-cooldown Guard."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
import re

from fogmoe_bot.application.conversation.inbox_worker import PermanentIngressError
from fogmoe_bot.application.conversation.router import (
    Allow,
    Reject,
    RoutedOperation,
    conversation_aggregate_key,
)
from fogmoe_bot.application.conversation.standalone_outbound import (
    StandaloneOutboundCapability,
    StandaloneOutboundCommand,
)
from fogmoe_bot.application.runtime import ReplayAwareCooldownGate, WorkPriority
from fogmoe_bot.domain.conversation.payloads import (
    JsonObject,
    JsonValue,
)
from fogmoe_bot.domain.conversation.identity import ConversationId
from fogmoe_bot.domain.conversation.inbox import InboundUpdate
from fogmoe_bot.domain.conversation.outbox import SEND_TELEGRAM_MESSAGE

from fogmoe_bot.application.conversation.telegram_identity import (
    TelegramConversationAddress,
)

from .delivery import delivery_stream_for_chat


_COMMAND_TOKEN = re.compile(r"^/([A-Za-z0-9_]{1,32})(?:@([A-Za-z0-9_]{1,64}))?$")
"""@brief Telegram Bot command token schema / Telegram Bot command-token schema."""

_COOLDOWN_TEXT = (
    "请稍等片刻再使用此命令。\nPlease wait a moment before using this command again."
)
"""@brief 固定、可幂等重放的冷却反馈 / Fixed replay-safe cooldown feedback."""


class MalformedTelegramCommandUpdate(PermanentIngressError):
    """@brief 带 command entity 的 durable Telegram payload 非法 / Durable Telegram payload with a command entity is malformed."""


@dataclass(frozen=True, slots=True)
class ParsedTelegramCommand:
    """@brief 从 durable payload 提取的最小命令 envelope / Minimal command envelope parsed from a durable payload.

    @param command 无 slash 的小写命令 / Lowercase command without a slash.
    @param target 可选 Bot username / Optional target Bot username.
    @param user_id 发送者 ID / Sender identifier.
    @param chat_id 目标 chat ID / Destination chat identifier.
    @param message_id 来源 message ID / Source message identifier.
    @param message_thread_id 可选 topic ID / Optional topic identifier.
    @param username 可选 Telegram username / Optional Telegram username.
    @param argument_text 命令 token 后的原始参数文本 / Raw argument text after the command token.
    @param arguments 按空白切分的兼容参数 / Whitespace-split compatibility arguments.
    @param display_name Telegram 显示名 / Telegram display name.
    @param chat_type Telegram chat 类型 / Telegram chat type.
    @param reply_text 可选被回复文本 / Optional replied-to text.
    """

    command: str
    target: str | None
    user_id: int
    chat_id: int
    message_id: int
    message_thread_id: int | None
    username: str | None
    argument_text: str
    arguments: tuple[str, ...]
    display_name: str = ""
    chat_type: str = "private"
    reply_text: str | None = None

    @property
    def conversation_id(self) -> ConversationId:
        """@brief 由命令 envelope 推导规范会话身份 / Derive the canonical conversation identity from the command envelope.

        @return 私聊用户或群组 Topic 的规范 Conversation ID / Canonical private-user or group-topic Conversation ID.
        @note 不能仅由 ``user_id`` 推导：群聊中的所有成员必须落在同一个
            ``group_id + topic`` 会话。/ This cannot be derived from ``user_id`` alone:
            all group members must resolve to the same ``group_id + topic`` conversation.
        """

        return TelegramConversationAddress(
            chat_type=self.chat_type,
            chat_id=self.chat_id,
            user_id=self.user_id,
            message_thread_id=self.message_thread_id,
        ).conversation_id


class TelegramCommandCooldownGuard:
    """@brief 在任何命令 handler 前作有界、重放稳定的冷却判断 / Apply bounded, replay-stable cooldown before every command handler."""

    def __init__(
        self,
        *,
        gate: ReplayAwareCooldownGate[tuple[int, str]],
        outbound: StandaloneOutboundCapability,
        bot_username: str,
        commands: Collection[str],
    ) -> None:
        """@brief 注入 P1 gate、durable outbox 与命令所有权 / Inject the P1 gate, durable outbox, and command ownership.

        @param gate 重放感知冷却门 / Replay-aware cooldown gate.
        @param outbound 通用 standalone outbox 能力 / Generic standalone-outbox capability.
        @param bot_username 当前 Bot username / Current Bot username.
        @param commands 当前进程拥有的命令名 / Command names owned by this process.
        @raise ValueError username 或 command catalog 非法 / Invalid username or command catalog.
        """

        username = bot_username.removeprefix("@").strip().casefold()
        if not username:
            raise ValueError("bot_username cannot be blank")
        normalized_commands = frozenset(
            command.strip().casefold() for command in commands
        )
        if not normalized_commands or "" in normalized_commands:
            raise ValueError("commands must contain at least one non-blank command")
        self._gate = gate
        """@brief runtime-owned P1 冷却门 / Runtime-owned P1 cooldown gate."""
        self._outbound = outbound
        """@brief durable standalone outbox 能力 / Durable standalone-outbox capability."""
        self._bot_username = username
        self._commands = normalized_commands

    @property
    def name(self) -> str:
        """@brief 返回稳定 Guard 名 / Return the stable Guard name.

        @return ``telegram-command-cooldown`` / ``telegram-command-cooldown``.
        """

        return "telegram-command-cooldown"

    async def evaluate(self, update: InboundUpdate) -> Allow | Reject:
        """@brief 对一个 durable Update 稳定决定命令冷却 / Stably decide command cooldown for one durable Update.

        @param update 已持久化 Update / Persisted Update.
        @return 非命令或获准时 Allow；冷却时 Reject + durable feedback / Allow for non-commands or admission; Reject with durable feedback on cooldown.
        """

        parsed = parse_telegram_command(update)
        if parsed is None:
            return Allow()
        if parsed.command not in self._commands:
            return Allow()
        if parsed.target is not None and parsed.target.casefold() != self._bot_username:
            return Allow()
        if self._gate.try_acquire(
            (parsed.user_id, parsed.command),
            int(update.update_id),
        ):
            return Allow()

        idempotency_key = f"update:{int(update.update_id)}:command-cooldown-feedback"
        command = StandaloneOutboundCommand(
            conversation_id=update.conversation_id,
            delivery_stream_id=delivery_stream_for_chat(
                parsed.chat_id,
                parsed.message_thread_id,
            ),
            kind=SEND_TELEGRAM_MESSAGE,
            payload={
                "chat_id": parsed.chat_id,
                "text": _COOLDOWN_TEXT,
                "message_thread_id": parsed.message_thread_id,
                "reply_to_message_id": parsed.message_id,
                "disable_web_page_preview": True,
            },
            idempotency_key=idempotency_key,
            created_at=update.received_at,
        )

        async def enqueue_feedback() -> None:
            """@brief 将确定性反馈写入 outbox / Write deterministic feedback to the outbox.

            @return None / None.
            """

            await self._outbound.enqueue(command)

        return Reject(
            reason="command_cooldown",
            feedback=RoutedOperation(
                name=f"telegram-command-cooldown-feedback:{int(update.update_id)}",
                key=conversation_aggregate_key(update.conversation_id),
                call=enqueue_feedback,
                priority=WorkPriority.CRITICAL,
            ),
        )


def parse_telegram_command(update: InboundUpdate) -> ParsedTelegramCommand | None:
    """@brief 从 PTB JSON 提取 offset-zero bot command / Extract an offset-zero bot command from PTB JSON.

    @param update durable Telegram Update / Durable Telegram Update.
    @return 命令 envelope；非命令 Update 为 None / Command envelope, or None for a non-command Update.
    @raise MalformedTelegramCommandUpdate command entity 存在但 envelope 非法 / A command entity exists but its envelope is malformed.
    """

    message = _message(update.payload)
    if message is None:
        return None
    text = message.get("text")
    entities = message.get("entities")
    if not isinstance(text, str) or not isinstance(entities, list):
        return None
    length = _command_length(entities)
    if length is None:
        return None
    payload_update_id = update.payload.get("update_id")
    if (
        isinstance(payload_update_id, bool)
        or not isinstance(payload_update_id, int)
        or payload_update_id != int(update.update_id)
    ):
        raise _malformed("payload update_id does not match durable identity")
    token = text[:length]
    matched = _COMMAND_TOKEN.fullmatch(token)
    if matched is None:
        raise _malformed("bot_command entity has an invalid token")
    sender = _object(message, "from")
    is_bot = sender.get("is_bot")
    if not isinstance(is_bot, bool):
        raise _malformed("is_bot must be a Boolean")
    if is_bot:
        return None
    user_id = _positive_int(sender, "id")
    username = _optional_string(sender, "username")
    first_name = _required_string(sender, "first_name")
    last_name = _optional_string(sender, "last_name")
    display_name = " ".join(
        part for part in (first_name.strip(), (last_name or "").strip()) if part
    )
    chat = _object(message, "chat")
    chat_id = _nonzero_int(chat, "id")
    chat_type = _required_string(chat, "type").casefold()
    message_id = _positive_int(message, "message_id")
    thread_id = _optional_positive_int(message, "message_thread_id")
    argument_text = text[length:].strip()
    parsed = ParsedTelegramCommand(
        command=matched.group(1).casefold(),
        target=matched.group(2),
        user_id=user_id,
        chat_id=chat_id,
        message_id=message_id,
        message_thread_id=thread_id,
        username=username,
        argument_text=argument_text,
        arguments=tuple(argument_text.split()),
        display_name=display_name,
        chat_type=chat_type,
        reply_text=_reply_text(message),
    )
    if update.conversation_id != parsed.conversation_id:
        raise _malformed("command address does not match durable conversation identity")
    return parsed


def _message(payload: JsonObject) -> JsonObject | None:
    """@brief 选择 message 或 edited_message / Select a message or edited_message.

    @param payload Update payload / Update payload.
    @return 单一消息对象或 None / Sole message object, or None.
    """

    candidates = tuple(
        value
        for key in ("message", "edited_message")
        if (value := payload.get(key)) is not None
    )
    if not candidates:
        return None
    if len(candidates) != 1 or not isinstance(candidates[0], dict):
        raise _malformed("command Update requires exactly one message object")
    return candidates[0]


def _command_length(entities: list[JsonValue]) -> int | None:
    """@brief 找到 offset-zero bot_command 的长度 / Find the offset-zero bot-command length.

    @param entities Telegram MessageEntity JSON / Telegram MessageEntity JSON.
    @return UTF-16 长度；无 command 时为 None / UTF-16 length, or None without a command.
    """

    for entity in entities:
        if not isinstance(entity, dict) or entity.get("type") != "bot_command":
            continue
        offset = entity.get("offset")
        length = entity.get("length")
        if isinstance(offset, bool) or not isinstance(offset, int):
            raise _malformed("bot_command entity offset must be an integer")
        if offset != 0:
            continue
        if isinstance(length, bool) or not isinstance(length, int) or length < 2:
            raise _malformed("bot_command entity length must be at least two")
        return length
    return None


def _object(parent: JsonObject, key: str) -> JsonObject:
    """@brief 读取必需 JSON object / Read a required JSON object.

    @param parent 父 object / Parent object.
    @param key 字段名 / Field name.
    @return 子 object / Child object.
    """

    value = parent.get(key)
    if not isinstance(value, dict):
        raise _malformed(f"{key} must be an object")
    return value


def _positive_int(parent: JsonObject, key: str) -> int:
    """@brief 读取正整数 / Read a positive integer.

    @param parent JSON object / JSON object.
    @param key 字段名 / Field name.
    @return 正整数 / Positive integer.
    """

    value = parent.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _malformed(f"{key} must be a positive integer")
    return value


def _nonzero_int(parent: JsonObject, key: str) -> int:
    """@brief 读取非零整数 / Read a non-zero integer.

    @param parent JSON object / JSON object.
    @param key 字段名 / Field name.
    @return 非零整数 / Non-zero integer.
    """

    value = parent.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value == 0:
        raise _malformed(f"{key} must be a non-zero integer")
    return value


def _optional_positive_int(parent: JsonObject, key: str) -> int | None:
    """@brief 读取可选正整数 / Read an optional positive integer.

    @param parent JSON object / JSON object.
    @param key 字段名 / Field name.
    @return 正整数或 None / Positive integer or None.
    """

    if key not in parent or parent[key] is None:
        return None
    return _positive_int(parent, key)


def _optional_string(parent: JsonObject, key: str) -> str | None:
    """@brief 读取可选非空字符串 / Read an optional non-empty string.

    @param parent JSON object / JSON object.
    @param key 字段名 / Field name.
    @return 字符串或 None / String or None.
    """

    value = parent.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _malformed(f"{key} must be a non-empty string when present")
    return value


def _required_string(parent: JsonObject, key: str) -> str:
    """@brief 读取必需非空字符串 / Read a required non-empty string.

    @param parent JSON object / JSON object.
    @param key 字段名 / Field name.
    @return 非空字符串 / Non-empty string.
    """

    value = _optional_string(parent, key)
    if value is None:
        raise _malformed(f"{key} must be a non-empty string")
    return value


def _reply_text(message: JsonObject) -> str | None:
    """@brief 读取 `/tl` 可复用的被回复文本 / Read replied text reusable by `/tl`.

    @param message Telegram message object / Telegram message object.
    @return 非空文本或 None / Non-empty text or None.
    """

    reply = message.get("reply_to_message")
    if reply is None:
        return None
    if not isinstance(reply, dict):
        raise _malformed("reply_to_message must be an object")
    text = reply.get("text")
    if text is None:
        return None
    if not isinstance(text, str):
        raise _malformed("reply_to_message.text must be a string")
    normalized = text.strip()
    return normalized or None


def _malformed(detail: str) -> MalformedTelegramCommandUpdate:
    """@brief 构造不泄露 payload 的永久错误 / Build a permanent error without leaking payload data.

    @param detail 安全 schema 细节 / Safe schema detail.
    @return 类型化错误 / Typed error.
    """

    return MalformedTelegramCommandUpdate(
        f"Malformed Telegram command Update: {detail}"
    )


__all__ = [
    "MalformedTelegramCommandUpdate",
    "ParsedTelegramCommand",
    "TelegramCommandCooldownGuard",
    "parse_telegram_command",
]
