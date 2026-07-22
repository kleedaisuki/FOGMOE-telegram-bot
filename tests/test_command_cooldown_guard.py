"""@brief Durable Telegram 命令冷却 Guard 测试 / Tests for the durable Telegram command-cooldown Guard."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from fogmoe_bot.application.conversation.router import (
    Allow,
    Reject,
)
from fogmoe_bot.application.conversation.standalone_outbound import (
    StandaloneOutboundCommand,
)
from fogmoe_bot.application.conversation.telegram_identity import (
    TelegramConversationAddress,
)
from fogmoe_bot.application.runtime import ReplayAwareCooldownGate
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    UpdateId,
)
from fogmoe_bot.domain.conversation.inbox import InboundUpdate
from fogmoe_bot.domain.conversation.payloads import (
    JsonObject,
    JsonValue,
)
from fogmoe_bot.presentation.telegram.command_cooldown_guard import (
    MalformedTelegramCommandUpdate,
    TelegramCommandCooldownGuard,
    parse_telegram_command,
)

NOW = datetime(2030, 1, 1, tzinfo=UTC)
"""@brief 固定接收时刻 / Fixed receipt time."""


class ManualMonotonic:
    """@brief 可控 monotonic clock / Controllable monotonic clock."""

    def __init__(self) -> None:
        """@brief 从零初始化 / Initialize at zero."""

        self.value = 0.0
        """@brief 当前秒数 / Current seconds."""

    def __call__(self) -> float:
        """@brief 返回当前秒数 / Return current seconds.

        @return monotonic 秒 / Monotonic seconds.
        """

        return self.value


class RecordingOutbound:
    """@brief 记录 standalone outbox 命令 / Record standalone-outbox commands."""

    def __init__(self) -> None:
        """@brief 初始化空记录 / Initialize an empty recording."""

        self.commands: list[StandaloneOutboundCommand] = []
        """@brief 收到的 commands / Received commands."""

    async def enqueue(self, command: StandaloneOutboundCommand) -> None:
        """@brief 记录命令 / Record a command.

        @param command 出站命令 / Outbound command.
        @return None / None.
        """

        self.commands.append(command)


def _payload(
    update_id: int,
    *,
    token: str = "/help",
    user_id: int = 42,
    chat_id: int = -100,
    thread_id: int | None = 9,
    command_entity: bool = True,
) -> JsonObject:
    """@brief 构造 PTB JSON 命令 payload / Build a PTB-JSON command payload.

    @param update_id Telegram Update ID / Telegram Update identifier.
    @param token 命令 token / Command token.
    @param user_id 发送者 ID / Sender identifier.
    @param chat_id Chat ID / Chat identifier.
    @param thread_id 可选 topic ID / Optional topic identifier.
    @param command_entity 是否声明 bot_command entity / Whether to declare a bot-command entity.
    @return durable payload / Durable payload.
    """

    message: JsonObject = {
        "message_id": update_id + 100,
        "date": 1_893_456_000,
        "chat": {"id": chat_id, "type": "supergroup"},
        "from": {"id": user_id, "is_bot": False, "first_name": "Klee"},
        "text": f"{token} trailing arguments",
        "message_thread_id": thread_id,
    }
    if command_entity:
        entities: list[JsonValue] = [
            {"type": "bot_command", "offset": 0, "length": len(token)}
        ]
        """@brief Telegram entity JSON / Telegram entity JSON."""
        message["entities"] = entities
    return {"update_id": update_id, "message": message}


def _inbound(
    update_id: int,
    *,
    token: str = "/help",
    payload_update_id: int | None = None,
) -> InboundUpdate:
    """@brief 构造 durable Update / Build a durable Update.

    @param update_id durable identity / Durable identity.
    @param token 命令 token / Command token.
    @param payload_update_id 可选不一致 payload identity / Optional mismatching payload identity.
    @return pending Update / Pending Update.
    """

    payload_id = update_id if payload_update_id is None else payload_update_id
    return InboundUpdate.pending(
        update_id=UpdateId(update_id),
        conversation_id=TelegramConversationAddress(
            chat_type="supergroup",
            chat_id=-100,
            user_id=42,
            message_thread_id=9,
        ).conversation_id,
        payload=_payload(payload_id, token=token),
        received_at=NOW + timedelta(seconds=update_id),
    )


def _guard(
    clock: ManualMonotonic,
    outbound: RecordingOutbound,
) -> TelegramCommandCooldownGuard:
    """@brief 构造 Guard / Build a Guard.

    @param clock 可控 monotonic clock / Controllable monotonic clock.
    @param outbound outbox 替身 / Outbox double.
    @return 测试 Guard / Test Guard.
    """

    return TelegramCommandCooldownGuard(
        gate=ReplayAwareCooldownGate(
            cooldown_seconds=1.0,
            max_entries=32,
            retention_seconds=60.0,
            monotonic=clock,
        ),
        outbound=outbound,
        bot_username="FogMoeBot",
    )


def test_parses_owned_command_and_ignores_plain_slash_text() -> None:
    """@brief 只接受 offset-zero command entity / Only an offset-zero command entity is parsed."""

    parsed = parse_telegram_command(_inbound(1, token="/HeLp@FogMoeBot"))
    assert parsed is not None
    assert parsed.command == "help"
    assert parsed.target == "FogMoeBot"
    assert parsed.user_id == 42
    assert parsed.chat_id == -100
    assert parsed.message_thread_id == 9

    plain = InboundUpdate.pending(
        update_id=UpdateId(2),
        conversation_id=TelegramConversationAddress(
            chat_type="supergroup",
            chat_id=-100,
            user_id=42,
            message_thread_id=9,
        ).conversation_id,
        payload=_payload(2, command_entity=False),
        received_at=NOW,
    )
    assert parse_telegram_command(plain) is None


def test_rejection_uses_deterministic_outbox_and_stable_replay() -> None:
    """@brief 冷却拒绝不直接 I/O，且反馈失败重试仍拒绝 / Cooldown rejection performs no direct I/O and remains rejected on feedback retry."""

    clock = ManualMonotonic()
    outbound = RecordingOutbound()
    guard = _guard(clock, outbound)

    assert isinstance(asyncio.run(guard.evaluate(_inbound(10))), Allow)
    clock.value = 0.1
    rejected = asyncio.run(guard.evaluate(_inbound(11)))
    assert isinstance(rejected, Reject)
    assert rejected.reason == "command_cooldown"
    assert outbound.commands == []
    assert rejected.feedback is not None
    asyncio.run(rejected.feedback.call())

    assert len(outbound.commands) == 1
    command = outbound.commands[0]
    assert command.idempotency_key == "update:11:command-cooldown-feedback"
    assert command.delivery_stream_id.value == "telegram:primary:chat:-100:thread:9"
    assert command.payload["reply_to_message_id"] == 111
    assert command.created_at == NOW + timedelta(seconds=11)

    clock.value = 5.0
    replay = asyncio.run(guard.evaluate(_inbound(11)))
    assert isinstance(replay, Reject)


def test_old_admission_replays_after_a_newer_admission() -> None:
    """@brief 新 Update 不覆盖旧 Update 的已获准决定 / A newer Update does not overwrite an older Update's admitted decision."""

    clock = ManualMonotonic()
    outbound = RecordingOutbound()
    guard = _guard(clock, outbound)
    old = _inbound(20)

    assert isinstance(asyncio.run(guard.evaluate(old)), Allow)
    clock.value = 2.0
    assert isinstance(asyncio.run(guard.evaluate(_inbound(21))), Allow)
    assert isinstance(asyncio.run(guard.evaluate(old)), Allow)


@pytest.mark.parametrize(
    ("token", "expected"),
    (("/unknown", Reject), ("/help@AnotherBot", Allow)),
)
def test_unknown_commands_are_cooled_but_other_bot_commands_are_not(
    token: str,
    expected: type[Allow] | type[Reject],
) -> None:
    """@brief 未知命令也限流，其他 Bot 的命令仍直接放行 / Unknown commands are cooled while commands for another Bot pass through.

    @param token command token / Command token.
    @param expected 期望 guard 结果类型 / Expected guard-result type.
    """

    clock = ManualMonotonic()
    outbound = RecordingOutbound()
    guard = _guard(clock, outbound)

    assert isinstance(asyncio.run(guard.evaluate(_inbound(30, token=token))), Allow)
    assert isinstance(asyncio.run(guard.evaluate(_inbound(31, token=token))), expected)


def test_command_payload_identity_mismatch_is_permanent_ingress_error() -> None:
    """@brief payload/durable Update identity 不一致不会进入 handler / A payload/durable Update identity mismatch never reaches a handler."""

    with pytest.raises(MalformedTelegramCommandUpdate, match="does not match"):
        parse_telegram_command(_inbound(40, payload_update_id=41))


def test_command_address_identity_supports_private_group_and_topic() -> None:
    """@brief 命令地址以私聊用户或群组 Topic 验证 / Commands validate private-user or group-topic identities."""

    cases = (
        ("private", 42, None, "assistant-user:42"),
        ("group", -100, None, "assistant-group:-100:thread:0"),
        ("supergroup", -100, 9, "assistant-group:-100:thread:9"),
    )
    for chat_type, chat_id, thread_id, expected in cases:
        payload = _payload(
            50,
            chat_id=chat_id,
            thread_id=thread_id,
        )
        message = payload["message"]
        assert isinstance(message, dict)
        chat = message["chat"]
        assert isinstance(chat, dict)
        chat["type"] = chat_type
        inbound = InboundUpdate.pending(
            update_id=UpdateId(50),
            conversation_id=ConversationId(expected),
            payload=payload,
            received_at=NOW,
        )
        parsed = parse_telegram_command(inbound)
        assert parsed is not None
        assert parsed.conversation_id == ConversationId(expected)


def test_command_address_identity_mismatch_is_permanent_ingress_error() -> None:
    """@brief 命令 payload 与 durable 会话不一致会被隔离 / A command payload and durable conversation mismatch is quarantined."""

    malformed = InboundUpdate.pending(
        update_id=UpdateId(51),
        conversation_id=ConversationId("assistant-user:42"),
        payload=_payload(51),
        received_at=NOW,
    )

    with pytest.raises(MalformedTelegramCommandUpdate, match="address"):
        parse_telegram_command(malformed)
