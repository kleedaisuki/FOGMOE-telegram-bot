"""@brief Durable Conversation acceptance 工作流测试 / Tests for durable Conversation acceptance."""

import asyncio
from datetime import datetime, timedelta, timezone

from fogmoe_bot.application.conversation.workflow import (
    AcceptConversationTurn,
    ConversationWorkflow,
)
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    InferenceActivityId,
    TurnId,
    TurnSource,
    UpdateId,
)
from fogmoe_bot.domain.conversation.turn import ConversationTurn
from fogmoe_bot.domain.conversation.inference import InferenceActivityDraft
from fogmoe_bot.domain.conversation.message import MessageDraft


NOW = datetime(2026, 7, 11, 10, tzinfo=timezone.utc)
"""@brief 工作流测试基准时间 / Workflow test reference time."""


class _Persistence:
    """@brief 记录 acceptance UoW 的测试替身 / Test double recording acceptance units of work."""

    def __init__(self) -> None:
        """@brief 初始化调用记录 / Initialize call records."""

        self.acceptances: list[
            tuple[ConversationTurn, MessageDraft, InferenceActivityDraft, datetime]
        ] = []

    async def create_and_accept_turn(
        self,
        turn: ConversationTurn,
        *,
        message: MessageDraft,
        activity: InferenceActivityDraft,
        accepted_at: datetime,
    ) -> object:
        """@brief 记录 acceptance UoW / Record the acceptance unit of work.

        @param turn 初始回合 / Initial turn.
        @param message 用户消息 / User message.
        @param activity 推理活动意图 / Inference activity intent.
        @param accepted_at 接受时间 / Acceptance time.
        @return 固定回执 / Fixed receipt.
        """

        self.acceptances.append((turn, message, activity, accepted_at))
        return "accepted"


def test_accept_builds_stable_turn_message_and_activity_identities() -> None:
    """@brief Update 重放构造相同 Turn、Message 与 Activity / Update replay builds stable Turn, Message, and Activity identities."""

    async def scenario() -> None:
        """@brief 两次执行相同 acceptance / Execute identical acceptance twice.

        @return None / None.
        """

        persistence = _Persistence()
        workflow = ConversationWorkflow(persistence)  # type: ignore[arg-type]
        source = TurnSource.telegram(UpdateId(91))
        command = AcceptConversationTurn(
            source=source,
            conversation_id=ConversationId("assistant-user:7"),
            user_content={"text": "hello", "chat_id": 7},
            inference_request={"prompt": "hello", "profile": "assistant"},
            received_at=NOW,
            accepted_at=NOW + timedelta(milliseconds=1),
        )

        assert await workflow.accept(command) == "accepted"
        assert await workflow.accept(command) == "accepted"
        first = persistence.acceptances[0]
        second = persistence.acceptances[1]
        turn_id = TurnId.for_source(source)
        assert first[0].turn_id == second[0].turn_id == turn_id
        assert first[0].version == second[0].version == 0
        assert first[1].message_id == second[1].message_id
        assert first[2].activity_id == second[2].activity_id
        assert first[2].activity_id == InferenceActivityId.for_turn(turn_id)
        assert first[2].request == {"prompt": "hello", "profile": "assistant"}

    asyncio.run(scenario())


def test_accept_rejects_time_travel_before_repository_call() -> None:
    """@brief 接受时间不能早于 listener 接收时间 / Acceptance cannot precede listener receipt."""

    try:
        AcceptConversationTurn(
            source=TurnSource.telegram(UpdateId(1)),
            conversation_id=ConversationId("assistant-user:7"),
            user_content={"text": "hello"},
            inference_request={"prompt": "hello"},
            received_at=NOW,
            accepted_at=NOW - timedelta(seconds=1),
        )
    except ValueError as error:
        assert "cannot precede" in str(error)
    else:
        raise AssertionError("time-travel acceptance was not rejected")
