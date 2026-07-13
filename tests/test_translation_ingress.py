"""@brief Durable translation ingress 测试 / Durable translation-ingress tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from fogmoe_bot.application.conversation.assistant_ingress import (
    AssistantAccountContext,
    AssistantInsufficientCoins,
    AssistantTurnAccepted,
    AssistantTurnRequest,
    AssistantUserNotRegistered,
)
from fogmoe_bot.application.conversation.standalone_outbound import (
    StandaloneOutboundCommand,
)
from fogmoe_bot.application.conversation.translation_ingress import (
    TranslationFeedbackReason,
    TranslationIngressCoordinator,
    TranslationRejected,
    TranslationReplyTarget,
    TranslationTurnRequest,
    translation_text_cost,
)
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    UpdateId,
)


NOW = datetime(2030, 1, 1, tzinfo=UTC)
"""@brief 固定时刻 / Fixed instant."""


class _Clock:
    """@brief 固定 UTC 时钟 / Fixed UTC clock."""

    def now(self) -> datetime:
        """@brief 返回固定时刻 / Return the fixed instant.

        @return NOW / NOW.
        """

        return NOW


class _Acceptance:
    """@brief 记录共享 acceptance 调用 / Record shared-acceptance calls."""

    def __init__(self, result: object) -> None:
        """@brief 保存固定结果 / Store a fixed result.

        @param result acceptance 结果 / Acceptance result.
        """

        self.result = result
        """@brief 固定结果 / Fixed result."""
        self.calls: list[tuple[AssistantTurnRequest, datetime]] = []
        """@brief 收到的请求 / Received requests."""

    async def accept(
        self,
        request: AssistantTurnRequest,
        *,
        accepted_at: datetime,
    ) -> object:
        """@brief 记录并返回 / Record and return.

        @param request acceptance 请求 / Acceptance request.
        @param accepted_at 接受时刻 / Acceptance instant.
        @return 固定结果 / Fixed result.
        """

        self.calls.append((request, accepted_at))
        return self.result


class _Outbound:
    """@brief 记录 standalone outbox / Record standalone outbox commands."""

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


def _target(update_id: int = 9) -> TranslationReplyTarget:
    """@brief 构造回复目标 / Build a reply target.

    @param update_id Update ID / Update identifier.
    @return target / Target.
    """

    return TranslationReplyTarget(
        update_id=UpdateId(update_id),
        conversation_id=ConversationId("assistant-user:42"),
        received_at=NOW,
        chat_id=-100,
        message_id=77,
        message_thread_id=8,
        delivery_stream_id=DeliveryStreamId("telegram:primary:chat:-100:thread:8"),
    )


def _request(text: str) -> TranslationTurnRequest:
    """@brief 构造翻译请求 / Build a translation request.

    @param text 输入文本 / Input text.
    @return request / Request.
    """

    return TranslationTurnRequest(
        target=_target(),
        user_id=42,
        username="klee",
        display_name="Klee",
        is_group=True,
        text=text,
    )


@pytest.mark.parametrize(
    ("length", "cost"),
    ((1, 0), (500, 0), (501, 1), (1000, 1), (1001, 2), (2000, 2), (2001, 3), (3000, 3)),
)
def test_translation_cost_preserves_zero_to_three_boundaries(
    length: int,
    cost: int,
) -> None:
    """@brief 费用边界保持旧产品语义 / Cost boundaries preserve legacy product semantics.

    @param length 输入长度 / Input length.
    @param cost 预期费用 / Expected charge.
    """

    assert translation_text_cost("x" * length) == cost


def test_valid_translation_uses_shared_acceptance_and_marks_history_isolation() -> None:
    """@brief 有效翻译进入共享 acceptance 且永久隔离历史 / A valid translation enters shared acceptance and is permanently isolated from history."""

    async def scenario() -> None:
        """@brief 执行有效请求 / Execute a valid request."""

        acceptance = _Acceptance(AssistantTurnAccepted(None, True))
        outbound = _Outbound()
        coordinator = TranslationIngressCoordinator(
            acceptance=acceptance,  # type: ignore[arg-type]
            feedback=outbound,
            clock=_Clock(),
        )
        result = await coordinator.handle(_request("x" * 500))

        assert isinstance(result, AssistantTurnAccepted)
        assert len(acceptance.calls) == 1
        command, accepted_at = acceptance.calls[0]
        assert accepted_at == NOW
        assert command.coin_cost == 0
        assert command.task_kind == "translation"
        assert command.translation_input == "x" * 500
        assert command.user_content["exclude_from_assistant"] is True
        assert command.user_content["coin_cost"] == 0
        durable = command.to_accept_turn(
            AssistantAccountContext(
                coins=7,
                plan="free",
                permission=0,
                profile=None,
                personal_info="",
                diary_exists=False,
            ),
            accepted_at=NOW,
        )
        assert durable.inference_request["task_kind"] == "translation"
        assert durable.inference_request["translation_input"] == "x" * 500
        assert outbound.commands == []

    asyncio.run(scenario())


def test_too_long_translation_never_reaches_acceptance_and_feedback_is_stable() -> None:
    """@brief 超长输入不扣费/建 Turn，反馈 identity 可重放 / Oversized input neither charges nor creates a Turn, and feedback identity is replay-stable."""

    async def scenario() -> None:
        """@brief 执行两次相同拒绝 / Execute the same rejection twice."""

        acceptance = _Acceptance(AssistantTurnAccepted(None, True))
        outbound = _Outbound()
        coordinator = TranslationIngressCoordinator(
            acceptance=acceptance,  # type: ignore[arg-type]
            feedback=outbound,
            clock=_Clock(),
        )
        first = await coordinator.handle(_request("x" * 3001))
        second = await coordinator.handle(_request("x" * 3001))

        assert (
            first
            == second
            == TranslationRejected(TranslationFeedbackReason.TEXT_TOO_LONG)
        )
        assert acceptance.calls == []
        assert len(outbound.commands) == 2
        assert (
            outbound.commands[0].idempotency_key
            == outbound.commands[1].idempotency_key
            == "update:9:translation-feedback:text_too_long"
        )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("acceptance_result", "reason"),
    (
        (AssistantUserNotRegistered(), TranslationFeedbackReason.USER_NOT_REGISTERED),
        (
            AssistantInsufficientCoins(available=1, required=3),
            TranslationFeedbackReason.INSUFFICIENT_COINS,
        ),
    ),
)
def test_business_rejections_use_durable_translation_feedback(
    acceptance_result: object,
    reason: TranslationFeedbackReason,
) -> None:
    """@brief 注册/余额拒绝通过 outbox 发布 / Registration and balance rejections publish through the outbox.

    @param acceptance_result UoW 结果 / UoW result.
    @param reason 预期反馈 / Expected feedback.
    """

    async def scenario() -> None:
        """@brief 执行业务拒绝 / Execute a business rejection."""

        acceptance = _Acceptance(acceptance_result)
        outbound = _Outbound()
        coordinator = TranslationIngressCoordinator(
            acceptance=acceptance,  # type: ignore[arg-type]
            feedback=outbound,
            clock=_Clock(),
        )
        await coordinator.handle(_request("x" * 2500))

        assert len(outbound.commands) == 1
        assert outbound.commands[0].idempotency_key.endswith(reason.value)
        if reason is TranslationFeedbackReason.INSUFFICIENT_COINS:
            assert "need 3" in str(outbound.commands[0].payload["text"])

    asyncio.run(scenario())
