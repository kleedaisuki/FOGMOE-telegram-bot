"""@brief 零费用 `/tl` 翻译入口测试 / Tests for zero-cost `/tl` translation ingress."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from fogmoe_bot.application.conversation.assistant_ingress import (
    AssistantTurnAccepted,
    AssistantTurnRequest,
    AssistantUserNotRegistered,
)
from fogmoe_bot.application.conversation.translation_ingress import (
    TranslationFeedbackReason,
    TranslationIngressCoordinator,
    TranslationRejected,
    TranslationReplyTarget,
    TranslationTurnRequest,
)
from fogmoe_bot.application.conversation.telegram_identity import (
    TelegramConversationAddress,
)
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    UpdateId,
)


NOW = datetime(2030, 1, 1, tzinfo=UTC)
"""@brief 固定测试时刻 / Fixed test instant."""


class FakeAcceptance:
    """@brief 捕获翻译转换出的 Assistant 请求 / Double capturing Assistant requests converted from translation."""

    def __init__(
        self, result: AssistantTurnAccepted | AssistantUserNotRegistered
    ) -> None:
        """@brief 注入 acceptance 结果 / Inject an acceptance result.

        @param result 预设结果 / Predefined result.
        """

        self.result = result
        """@brief 预设 acceptance 结果 / Predefined acceptance result."""
        self.requests: list[AssistantTurnRequest] = []
        """@brief 捕获的请求 / Captured requests."""

    async def accept(
        self,
        request: AssistantTurnRequest,
        *,
        accepted_at: datetime,
    ) -> AssistantTurnAccepted | AssistantUserNotRegistered:
        """@brief 捕获请求并返回预设结果 / Capture a request and return the predefined result.

        @param request 转换后的 Assistant 请求 / Converted Assistant request.
        @param accepted_at acceptance 时刻 / Acceptance instant.
        @return 预设结果 / Predefined result.
        """

        del accepted_at
        self.requests.append(request)
        return self.result


class FakeFeedback:
    """@brief 捕获 durable standalone 反馈 / Double capturing durable standalone feedback."""

    def __init__(self) -> None:
        """@brief 初始化命令日志 / Initialize the command log."""

        self.commands: list[Any] = []
        """@brief 捕获的 outbox 命令 / Captured outbox commands."""

    async def enqueue(self, command: Any) -> None:
        """@brief 记录一个反馈命令 / Record one feedback command.

        @param command 反馈命令 / Feedback command.
        @return None / None.
        """

        self.commands.append(command)


def _target(*, chat_type: str = "private") -> TranslationReplyTarget:
    """@brief 构造规范私聊或群聊翻译目标 / Build a canonical private or group translation target.

    @param chat_type Telegram chat 类型 / Telegram chat type.
    @return 翻译目标 / Translation target.
    """

    is_group = chat_type != "private"
    chat_id = -1001 if is_group else 42
    thread_id = 23 if is_group else None
    conversation_id = TelegramConversationAddress(
        chat_type=chat_type,
        chat_id=chat_id,
        user_id=42,
        message_thread_id=thread_id,
    ).conversation_id
    return TranslationReplyTarget(
        update_id=UpdateId(100),
        conversation_id=conversation_id,
        received_at=NOW,
        chat_id=chat_id,
        chat_type=chat_type,
        message_id=7,
        message_thread_id=thread_id,
        delivery_stream_id=DeliveryStreamId(
            f"telegram:primary:chat:{chat_id}:thread:{thread_id or 0}"
        ),
    )


def _request(
    *, chat_type: str = "private", text: str = "hello"
) -> TranslationTurnRequest:
    """@brief 构造翻译请求 / Build a translation request.

    @param chat_type Telegram chat 类型 / Telegram chat type.
    @param text 待翻译文本 / Text to translate.
    @return 翻译请求 / Translation request.
    """

    return TranslationTurnRequest(
        target=_target(chat_type=chat_type),
        user_id=42,
        username="klee",
        display_name="Klee",
        is_group=chat_type != "private",
        text=text,
    )


def test_translation_builds_a_no_charge_history_isolated_assistant_request() -> None:
    """@brief `/tl` 构建无计费且隔离历史的请求 / `/tl` builds a no-charge request isolated from history."""

    request = _request().to_assistant_request()

    assert not hasattr(request, "coin_cost")
    assert "coin_cost" not in request.user_content
    assert request.task_kind == "translation"
    assert request.translation_input == "hello"
    assert request.user_content["exclude_from_assistant"] is True


def test_translation_supports_private_and_group_topic_addresses() -> None:
    """@brief 翻译地址遵从私聊与群 Topic 边界 / Translation addresses honor private and group-topic boundaries."""

    private = _request().to_assistant_request()
    group = _request(chat_type="supergroup").to_assistant_request()

    assert private.conversation_id == ConversationId("assistant-user:42")
    assert not private.is_group
    assert group.conversation_id == ConversationId("assistant-group:-1001:thread:23")
    assert group.is_group
    assert group.message_thread_id == 23


def test_too_long_translation_never_reaches_acceptance() -> None:
    """@brief 超长文本只写稳定反馈，不触发 acceptance / Overlong text writes stable feedback only and never reaches acceptance."""

    async def scenario() -> None:
        """@brief 执行超长文本场景 / Execute the overlong-text scenario.

        @return None / None.
        """

        acceptance = FakeAcceptance(
            AssistantTurnAccepted(acceptance=None, replayed=True)
        )
        feedback = FakeFeedback()
        coordinator = TranslationIngressCoordinator(
            acceptance=acceptance,
            feedback=feedback,
        )
        result = await coordinator.handle(_request(text="x" * 3001))

        assert result == TranslationRejected(TranslationFeedbackReason.TEXT_TOO_LONG)
        assert acceptance.requests == []
        assert len(feedback.commands) == 1
        assert "too long" in feedback.commands[0].payload["text"].casefold()

    asyncio.run(scenario())


def test_unregistered_translation_user_receives_registration_feedback() -> None:
    """@brief 未注册用户得到注册反馈，不存在金币不足分支 / Unregistered users receive registration feedback; no insufficient-coins branch exists."""

    async def scenario() -> None:
        """@brief 执行未注册场景 / Execute the unregistered-user scenario.

        @return None / None.
        """

        acceptance = FakeAcceptance(AssistantUserNotRegistered())
        feedback = FakeFeedback()
        coordinator = TranslationIngressCoordinator(
            acceptance=acceptance,
            feedback=feedback,
        )
        result = await coordinator.handle(_request())

        assert isinstance(result, AssistantUserNotRegistered)
        assert len(acceptance.requests) == 1
        assert len(feedback.commands) == 1
        assert "/me" in feedback.commands[0].payload["text"]

    asyncio.run(scenario())
