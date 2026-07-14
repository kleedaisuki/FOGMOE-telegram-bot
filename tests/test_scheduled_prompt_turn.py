"""@brief Durable 定时 Prompt Turn 测试 / Durable scheduled-prompt Turn tests."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from fogmoe_bot.application.assistant.inference_command import (
    DurableAssistantInferenceCommand,
    DurableAssistantUser,
)
from fogmoe_bot.application.conversation.workflow import ConversationWorkflow
from fogmoe_bot.application.scheduling.prompt_turn import PromptTurnHandler
from fogmoe_bot.domain.conversation.turn import ConversationTurn
from fogmoe_bot.domain.conversation.inference import InferenceActivityDraft
from fogmoe_bot.domain.conversation.message import MessageDraft
from fogmoe_bot.domain.scheduling import (
    PROMPT_JOB_KIND,
    PromptJobPayload,
    Recurrence,
    ScheduledJob,
)


NOW = datetime(2026, 7, 11, 12, tzinfo=timezone.utc)
"""@brief 测试 acceptance 时间 / Test acceptance time."""

RUN_AT = datetime(2026, 7, 11, 11, 30, tzinfo=timezone.utc)
"""@brief 测试计划发生时间 / Test scheduled occurrence time."""


class _FixedClock:
    """@brief 固定 UTC 时钟 / Fixed UTC clock."""

    def now(self) -> datetime:
        """@brief 返回固定时刻 / Return the fixed instant.

        @return NOW / NOW.
        """

        return NOW


class _Profiles:
    """@brief 可控用户快照端口 / Controllable user-snapshot port."""

    def __init__(self, profile: DurableAssistantUser | None) -> None:
        """@brief 注入返回快照 / Inject the returned profile.

        @param profile 返回值 / Returned value.
        """

        self.profile = profile
        self.user_ids: list[int] = []

    async def read(self, user_id: int) -> DurableAssistantUser | None:
        """@brief 记录并返回快照 / Record the lookup and return the profile.

        @param user_id 用户 ID / User identifier.
        @return 注入快照 / Injected profile.
        """

        self.user_ids.append(user_id)
        return self.profile


class _Persistence:
    """@brief 记录 Conversation acceptance 的持久化替身 / Persistence double recording Conversation acceptances."""

    def __init__(self) -> None:
        """@brief 初始化 acceptance 列表 / Initialize the acceptance list."""

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
        """@brief 记录原子 acceptance 参数 / Record atomic-acceptance arguments.

        @param turn 初始 Turn / Initial Turn.
        @param message 用户消息 / User message.
        @param activity 推理意图 / Inference intent.
        @param accepted_at 接受时间 / Acceptance time.
        @return 固定回执 / Fixed receipt.
        """

        self.acceptances.append((turn, message, activity, accepted_at))
        return "accepted"


def _profile() -> DurableAssistantUser:
    """@brief 构造严格用户快照 / Build a strict user snapshot.

    @return 测试用户快照 / Test user snapshot.
    """

    return DurableAssistantUser(
        user_id=42,
        username="klee",
        display_name="Klee",
        coins=19,
        plan="paid",
        permission=1,
        profile=None,
        personal_info="",
        diary_exists=True,
    )


def _job(*, run_at: datetime = RUN_AT) -> ScheduledJob[PromptJobPayload]:
    """@brief 构造 Prompt 调度发生项 / Build a prompt schedule occurrence.

    @param run_at 本次计划时间 / Occurrence time.
    @return 类型化调度任务 / Typed scheduled job.
    """

    return ScheduledJob(
        schedule_id=7,
        owner_id=42,
        kind=PROMPT_JOB_KIND,
        run_at=run_at,
        created_at=RUN_AT - timedelta(days=1),
        recurrence=Recurrence(),
        payload=PromptJobPayload(
            trigger_reason="timer",
            context_text="previous context",
            instruction="Send the reminder",
        ),
    )


def test_retry_converges_on_one_occurrence_identity_and_durable_command() -> None:
    """@brief 同一发生项重放得到相同 Turn/Message/Activity / Replaying an occurrence yields identical Turn, message, and activity identities."""

    async def scenario() -> None:
        """@brief 执行同一任务两次 / Execute the same job twice.

        @return None / None.
        """

        persistence = _Persistence()
        handler = PromptTurnHandler(
            workflow=ConversationWorkflow(persistence),  # type: ignore[arg-type]
            profiles=_Profiles(_profile()),
            clock=_FixedClock(),
        )

        await handler.handle(_job())
        await handler.handle(_job())

        first, replay = persistence.acceptances
        assert first[0].turn_id == replay[0].turn_id
        assert first[1].message_id == replay[1].message_id
        assert first[2].activity_id == replay[2].activity_id
        assert first[0].source.kind == "schedule.prompt"
        assert first[0].source.key == "7:2026-07-11T11:30:00.000000Z"
        assert first[0].source.update_id is None
        assert first[0].conversation_id.value == "assistant-user:42"
        assert first[1].content["content_kind"] == "scheduled_prompt"
        assert "Send the reminder" in str(first[1].content["text"])
        command = DurableAssistantInferenceCommand.from_json(first[2].request)
        assert command.typed_turn_id == first[0].turn_id
        assert command.user == _profile()
        assert command.chat_id == 42
        assert command.reply_to_message_id is None
        assert command.delivery_stream_id == "telegram:primary:chat:42:thread:0"

    asyncio.run(scenario())


def test_recurrence_occurrences_have_distinct_turn_identities() -> None:
    """@brief 同一 schedule 的不同发生项得到不同 Turn / Different occurrences of one schedule receive different Turns."""

    async def scenario() -> None:
        """@brief 接受两个周期发生项 / Accept two recurring occurrences.

        @return None / None.
        """

        persistence = _Persistence()
        handler = PromptTurnHandler(
            workflow=ConversationWorkflow(persistence),  # type: ignore[arg-type]
            profiles=_Profiles(_profile()),
            clock=_FixedClock(),
        )

        await handler.handle(_job())
        await handler.handle(_job(run_at=RUN_AT + timedelta(hours=1)))

        assert (
            persistence.acceptances[0][0].turn_id
            != persistence.acceptances[1][0].turn_id
        )

    asyncio.run(scenario())


def test_missing_owner_fails_before_conversation_acceptance() -> None:
    """@brief 所有者不存在时不产生 Conversation 写入 / A missing owner causes no Conversation write."""

    async def scenario() -> None:
        """@brief 执行无所有者任务 / Execute a job whose owner is absent.

        @return None / None.
        """

        persistence = _Persistence()
        handler = PromptTurnHandler(
            workflow=ConversationWorkflow(persistence),  # type: ignore[arg-type]
            profiles=_Profiles(None),
            clock=_FixedClock(),
        )

        with pytest.raises(LookupError, match="owner not found"):
            await handler.handle(_job())
        assert persistence.acceptances == []

    asyncio.run(scenario())
