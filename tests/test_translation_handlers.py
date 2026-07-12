"""@brief Durable `/tl` Telegram adapter 测试 / Durable `/tl` Telegram-adapter tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fogmoe_bot.application.conversation.translation_ingress import (
    TranslationFeedbackReason,
    TranslationReplyTarget,
    TranslationTurnRequest,
)
from fogmoe_bot.domain.conversation.payloads import JsonObject
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    UpdateId,
)
from fogmoe_bot.domain.conversation.inbox import InboundUpdate
from fogmoe_bot.presentation.telegram.command_cooldown_guard import (
    parse_telegram_command,
)
from fogmoe_bot.presentation.telegram.translation_handlers import (
    TranslationTelegramCommandHandler,
)


NOW = datetime(2030, 1, 1, tzinfo=UTC)
"""@brief 固定接收时刻 / Fixed receipt instant."""


class _Coordinator:
    """@brief 记录 typed translation API 调用 / Record typed translation API calls."""

    def __init__(self) -> None:
        """@brief 初始化记录 / Initialize recordings."""

        self.requests: list[TranslationTurnRequest] = []
        """@brief 接受请求 / Accepted requests."""
        self.rejections: list[
            tuple[TranslationReplyTarget, TranslationFeedbackReason]
        ] = []
        """@brief 拒绝请求 / Rejections."""

    async def handle(self, request: TranslationTurnRequest) -> object:
        """@brief 记录有效请求 / Record a valid request.

        @param request typed request / Typed request.
        @return 占位结果 / Placeholder result.
        """

        self.requests.append(request)
        return object()

    async def reject(
        self,
        target: TranslationReplyTarget,
        reason: TranslationFeedbackReason,
        *,
        required: int = 0,
    ) -> None:
        """@brief 记录拒绝 / Record a rejection.

        @param target 回复目标 / Reply target.
        @param reason 拒绝原因 / Rejection reason.
        @param required 未使用费用 / Unused required charge.
        @return None / None.
        """

        del required
        self.rejections.append((target, reason))


def _inbound(
    *,
    argument: str = "hello",
    reply_text: str | None = None,
) -> InboundUpdate:
    """@brief 构造 `/tl` durable Update / Build a durable `/tl` Update.

    @param argument 命令参数文本 / Command argument text.
    @param reply_text 可选被回复文本 / Optional replied text.
    @return inbound Update / Inbound Update.
    """

    text = f"/tl {argument}" if argument else "/tl"
    message: JsonObject = {
        "message_id": 77,
        "date": 1_893_456_000,
        "message_thread_id": 8,
        "chat": {"id": -100, "type": "supergroup"},
        "from": {
            "id": 42,
            "is_bot": False,
            "username": "klee",
            "first_name": "Klee",
            "last_name": "Moe",
        },
        "text": text,
        "entities": [{"type": "bot_command", "offset": 0, "length": 3}],
    }
    if reply_text is not None:
        message["reply_to_message"] = {"message_id": 70, "text": reply_text}
    return InboundUpdate.pending(
        update_id=UpdateId(9),
        conversation_id=ConversationId("assistant-user:42"),
        payload={"update_id": 9, "message": message},
        received_at=NOW,
    )


def test_handler_owns_tl_and_prefers_replied_text() -> None:
    """@brief `/tl` 独占且回复文本优先于参数 / `/tl` is exclusively owned and replied text takes precedence over arguments."""

    async def scenario() -> None:
        """@brief 执行回复翻译 / Execute a replied-text translation."""

        inbound = _inbound(argument="ignored", reply_text="你好")
        command = parse_telegram_command(inbound)
        assert command is not None
        coordinator = _Coordinator()
        handler = TranslationTelegramCommandHandler(  # type: ignore[arg-type]
            coordinator
        )

        assert handler.commands == frozenset({"tl"})
        await handler.handle(inbound, command)

        assert coordinator.rejections == []
        assert len(coordinator.requests) == 1
        request = coordinator.requests[0]
        assert request.text == "你好"
        assert request.display_name == "Klee Moe"
        assert request.is_group is True
        assert request.target.message_thread_id == 8

    asyncio.run(scenario())


def test_handler_routes_missing_input_to_durable_usage_feedback() -> None:
    """@brief 缺少输入只产生 durable 用法反馈 / Missing input produces only durable usage feedback."""

    async def scenario() -> None:
        """@brief 执行空参数命令 / Execute an argument-less command."""

        inbound = _inbound(argument="")
        command = parse_telegram_command(inbound)
        assert command is not None
        coordinator = _Coordinator()
        handler = TranslationTelegramCommandHandler(  # type: ignore[arg-type]
            coordinator
        )

        await handler.handle(inbound, command)

        assert coordinator.requests == []
        assert len(coordinator.rejections) == 1
        target, reason = coordinator.rejections[0]
        assert reason is TranslationFeedbackReason.USAGE
        assert target.update_id == UpdateId(9)
        assert target.chat_id == -100

    asyncio.run(scenario())
