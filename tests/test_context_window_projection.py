"""@brief Token-aware Context Window projection tests / Token-aware context-window projection tests."""

import asyncio
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

from fogmoe_bot.application.context_window.projection import (
    ContextWindowProjector,
    ContextWindowBounds,
    CompactionPending,
    ContextWindowRequest,
    ContextWindowReady,
    checkpoint_summary_message,
)
from fogmoe_bot.application.context_window.cache import ContextWindowCache
from fogmoe_bot.domain.conversation.payloads import JsonObject
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    ConversationMessageId,
    LeaseToken,
    MessageSequence,
    TurnId,
)
from fogmoe_bot.domain.conversation.message import (
    ConversationMessage,
    MessageDraft,
    MessageRole,
)
from fogmoe_bot.domain.context_window.budget import ContextTokenBudget, TokenCount
from fogmoe_bot.domain.context_window.compaction import (
    CompactionEnqueueResult,
    Compaction,
    CompactionPlan,
    CompactionSummary,
)


NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
"""@brief 确定性测试时刻 / Deterministic test instant."""

CONVERSATION = ConversationId("assistant-user:7")
"""@brief 测试会话 / Test conversation."""

GROUP_CONVERSATION = ConversationId("assistant-group:-1007:thread:23")
"""@brief 群 Topic 测试会话 / Group-topic test conversation."""


class _CharacterCounter:
    """@brief 可预测字符 token counter / Predictable character token counter."""

    def count_messages(self, messages: Sequence[JsonObject]) -> TokenCount:
        """@brief 每个 content 字符计一 token / Count one token per content character.

        @return token count / Token count.
        """

        return TokenCount(
            sum(max(1, len(str(message.get("content", "")))) for message in messages)
        )


class _Persistence:
    """@brief 内存 history/retention port / In-memory history and retention port."""

    def __init__(
        self,
        *,
        bounds: ContextWindowBounds,
        messages: tuple[ConversationMessage, ...],
        checkpoint: Compaction | None = None,
        active: Compaction | None = None,
    ) -> None:
        """@brief 保存固定 projection state / Store fixed projection state."""

        self.bounds = bounds
        self.messages = messages
        self.checkpoint = checkpoint
        self.active = active
        self.enqueued: list[CompactionPlan] = []
        self.pages: list[tuple[int, int, int]] = []

    async def history_bounds(
        self,
        conversation_id: ConversationId,
        *,
        through_turn_id: TurnId,
    ) -> ContextWindowBounds | None:
        """@brief 返回固定 anchor bounds / Return fixed anchor bounds."""

        assert conversation_id == self.bounds.conversation_id
        assert through_turn_id == self.bounds.through_turn_id
        return self.bounds

    async def latest_completed_compaction(
        self,
        conversation_id: ConversationId,
        *,
        epoch_floor_sequence: int,
        before_sequence: int,
    ) -> Compaction | None:
        """@brief 返回固定 checkpoint / Return a fixed checkpoint."""

        del conversation_id, epoch_floor_sequence, before_sequence
        return self.checkpoint

    async def active_compaction(
        self,
        conversation_id: ConversationId,
        *,
        epoch_floor_sequence: int,
    ) -> Compaction | None:
        """@brief 返回固定在途 Segment / Return a fixed active segment."""

        del conversation_id, epoch_floor_sequence
        return self.active

    async def read_messages_page(
        self,
        conversation_id: ConversationId,
        *,
        after_sequence: int,
        through_sequence: int,
        limit: int,
    ) -> Sequence[ConversationMessage]:
        """@brief 模拟 keyset page / Simulate a keyset page."""

        assert conversation_id == self.bounds.conversation_id
        self.pages.append((after_sequence, through_sequence, limit))
        return tuple(
            message
            for message in self.messages
            if after_sequence < int(message.sequence) <= through_sequence
        )[:limit]

    async def enqueue_compaction(
        self,
        draft: CompactionPlan,
    ) -> CompactionEnqueueResult:
        """@brief 保存并返回 PENDING Segment / Store and return a pending segment."""

        self.enqueued.append(draft)
        segment = Compaction.pending(draft)
        self.active = segment
        return CompactionEnqueueResult(segment, True)


def _message(
    sequence: int,
    turn_id: TurnId,
    text: str,
    *,
    excluded: bool = False,
    conversation_id: ConversationId = CONVERSATION,
    model_message: JsonObject | None = None,
) -> ConversationMessage:
    """@brief 构造 append-only user message / Build an append-only user message.

    @param sequence 会话内序号 / Conversation-local sequence.
    @param turn_id 来源 Turn / Source Turn.
    @param text 原始用户文本 / Raw user text.
    @param excluded 是否排除于 Assistant history / Whether to exclude from Assistant history.
    @param conversation_id 会话边界 / Conversation boundary.
    @param model_message 可选 speaker-aware 模型消息 / Optional speaker-aware model message.
    @return 已分配序号的消息 / Sequenced message.
    """

    content: JsonObject = {"text": text}
    if excluded:
        content["exclude_from_assistant"] = True
    if model_message is not None:
        content["model_message"] = model_message
    return ConversationMessage(
        MessageDraft(
            message_id=ConversationMessageId.new(),
            conversation_id=conversation_id,
            turn_id=turn_id,
            source_update_id=None,
            role=MessageRole.USER,
            content=content,
            idempotency_key=f"message:{sequence}",
            created_at=NOW + timedelta(microseconds=sequence),
        ),
        MessageSequence(sequence),
    )


def _request(
    turn_id: TurnId,
    *,
    conversation_id: ConversationId = CONVERSATION,
) -> ContextWindowRequest:
    """@brief 构造 projection request / Build a projection request.

    @param turn_id anchor Turn / Anchor Turn.
    @param conversation_id 会话边界 / Conversation boundary.
    @return 历史投影请求 / History-projection request.
    """

    return ContextWindowRequest(
        conversation_id=conversation_id,
        owner_user_id=7,
        through_turn_id=turn_id,
        base_messages=({"role": "system", "content": "S"},),
        reserved_tokens=TokenCount(0),
        requested_at=NOW + timedelta(seconds=1),
    )


def test_group_topic_projection_preserves_every_speaker_in_one_shared_context() -> None:
    """@brief 同群 Topic 的不同成员进入同一 speaker-aware Context / Different members of one group topic enter one speaker-aware Context."""

    async def scenario() -> None:
        """@brief 投影两个群成员的连续 Turn / Project consecutive Turns from two group members."""

        first_turn = TurnId.new()
        second_turn = TurnId.new()
        first_rendered = '<metadata type="supergroup" user="@alice" user_id="11" thread_id="23" />\n<message>red</message>'
        second_rendered = '<metadata type="supergroup" user="@bob" user_id="12" thread_id="23" />\n<message>blue</message>'
        messages = (
            _message(
                1,
                first_turn,
                "red",
                conversation_id=GROUP_CONVERSATION,
                model_message={"role": "user", "content": first_rendered},
            ),
            _message(
                2,
                second_turn,
                "blue",
                conversation_id=GROUP_CONVERSATION,
                model_message={"role": "user", "content": second_rendered},
            ),
        )
        persistence = _Persistence(
            bounds=ContextWindowBounds(
                GROUP_CONVERSATION,
                second_turn,
                2,
                2,
                0,
            ),
            messages=messages,
        )
        projector = ContextWindowProjector(
            persistence=persistence,
            token_counter=_CharacterCounter(),
        )

        result = await projector.project(
            _request(second_turn, conversation_id=GROUP_CONVERSATION)
        )

        assert isinstance(result, ContextWindowReady)
        assert result.messages == (
            {"role": "user", "content": first_rendered},
            {"role": "user", "content": second_rendered},
        )
        assert result.anchor_messages == (messages[1],)

    asyncio.run(scenario())


def test_more_than_128_tiny_rows_are_not_truncated() -> None:
    """@brief 129+ 小消息按 token 而非行数截断 / More than 128 tiny messages are governed by tokens, not row count."""

    async def scenario() -> None:
        """@brief 执行 tiny-history projection / Run tiny-history projection."""

        current_turn = TurnId.new()
        prior_turn = TurnId.new()
        messages = tuple(
            _message(index, current_turn if index == 130 else prior_turn, "x")
            for index in range(1, 131)
        )
        persistence = _Persistence(
            bounds=ContextWindowBounds(CONVERSATION, current_turn, 130, 130, 0),
            messages=messages,
        )
        projector = ContextWindowProjector(
            persistence=persistence,
            token_counter=_CharacterCounter(),
            budget=ContextTokenBudget(
                warning_tokens=TokenCount(1000),
                hard_tokens=TokenCount(1200),
                summary_output_tokens=TokenCount(10),
                segment_input_tokens=TokenCount(500),
            ),
            page_size=32,
        )

        result = await projector.project(_request(current_turn))

        assert isinstance(result, ContextWindowReady)
        assert len(result.messages) == 130
        assert len(persistence.pages) == 5
        assert persistence.enqueued == []

    asyncio.run(scenario())


def test_many_tiny_messages_trigger_the_count_window_before_the_token_window() -> None:
    """@brief 大量短句按条数触发压缩而非绕过 token 窗 / Many short messages trigger compaction by count instead of bypassing the token window."""

    async def scenario() -> None:
        """@brief 以很高 token 预算隔离条数阈值 / Isolate the count threshold with a high token budget."""

        current_turn = TurnId.new()
        prior_turn = TurnId.new()
        messages = tuple(
            _message(index, current_turn if index == 6 else prior_turn, "x")
            for index in range(1, 7)
        )
        persistence = _Persistence(
            bounds=ContextWindowBounds(CONVERSATION, current_turn, 6, 6, 0),
            messages=messages,
        )
        projector = ContextWindowProjector(
            persistence=persistence,
            token_counter=_CharacterCounter(),
            budget=ContextTokenBudget(
                warning_tokens=TokenCount(1_000),
                hard_tokens=TokenCount(1_200),
                warning_messages=4,
                hard_messages=8,
                summary_output_tokens=TokenCount(10),
                segment_input_tokens=TokenCount(500),
                minimum_recent_non_tool_messages=2,
            ),
        )

        result = await projector.project(_request(current_turn))

        assert isinstance(result, ContextWindowReady)
        assert len(result.messages) == 6
        assert int(result.estimated_tokens) < 1_000
        assert len(persistence.enqueued) == 1
        assert persistence.enqueued[0].through_sequence == 4

    asyncio.run(scenario())


def test_tiny_message_flood_waits_when_the_hard_count_window_is_exceeded() -> None:
    """@brief 极短消息洪泛超过条数硬上限时等待压缩 / A tiny-message flood exceeding the hard count window waits for compaction."""

    async def scenario() -> None:
        """@brief 验证硬条数门限 / Verify the hard message-count gate."""

        current_turn = TurnId.new()
        prior_turn = TurnId.new()
        messages = tuple(
            _message(index, current_turn if index == 6 else prior_turn, "x")
            for index in range(1, 7)
        )
        persistence = _Persistence(
            bounds=ContextWindowBounds(CONVERSATION, current_turn, 6, 6, 0),
            messages=messages,
        )
        projector = ContextWindowProjector(
            persistence=persistence,
            token_counter=_CharacterCounter(),
            budget=ContextTokenBudget(
                warning_tokens=TokenCount(1_000),
                hard_tokens=TokenCount(1_200),
                warning_messages=3,
                hard_messages=5,
                summary_output_tokens=TokenCount(10),
                segment_input_tokens=TokenCount(500),
                minimum_recent_non_tool_messages=2,
            ),
        )

        result = await projector.project(_request(current_turn))

        assert isinstance(result, CompactionPending)
        assert len(persistence.enqueued) == 1

    asyncio.run(scenario())


def test_history_cache_reuses_committed_prefix_and_reads_only_new_delta() -> None:
    """@brief 连续 Turn 命中缓存时只读取新增历史 / Consecutive Turns reuse the cached committed prefix and read only the new delta."""

    async def scenario() -> None:
        """@brief 先投影首回合，再投影追加回合 / Project an initial Turn then an appended Turn."""

        first_turn = TurnId.new()
        second_turn = TurnId.new()
        first = _message(1, first_turn, "first")
        second = _message(2, second_turn, "second")
        persistence = _Persistence(
            bounds=ContextWindowBounds(CONVERSATION, first_turn, 1, 1, 0),
            messages=(first,),
        )
        projector = ContextWindowProjector(
            persistence=persistence,
            token_counter=_CharacterCounter(),
            cache=ContextWindowCache(capacity=2, ttl_seconds=60),
        )

        first_result = await projector.project(_request(first_turn))
        assert isinstance(first_result, ContextWindowReady)
        assert persistence.pages == [(0, 1, 256)]

        persistence.bounds = ContextWindowBounds(CONVERSATION, second_turn, 2, 2, 0)
        persistence.messages = (first, second)
        second_result = await projector.project(_request(second_turn))

        assert isinstance(second_result, ContextWindowReady)
        assert second_result.messages == (
            {"role": "user", "content": "first"},
            {"role": "user", "content": "second"},
        )
        assert persistence.pages[-1] == (1, 2, 256)

    asyncio.run(scenario())


def test_two_large_rows_trigger_a_frozen_fenced_segment() -> None:
    """@brief 两行大历史也会超过 hard budget 并入队 snapshot / Two large rows exceed the hard budget and enqueue a frozen snapshot."""

    async def scenario() -> None:
        """@brief 执行 hard-budget scenario / Run the hard-budget scenario."""

        current_turn = TurnId.new()
        prior_turn = TurnId.new()
        messages = (
            _message(1, prior_turn, "a" * 12),
            _message(2, prior_turn, "b" * 12),
            _message(3, prior_turn, "c" * 12),
            _message(4, current_turn, "current"),
        )
        persistence = _Persistence(
            bounds=ContextWindowBounds(CONVERSATION, current_turn, 4, 4, 0),
            messages=messages,
        )
        projector = ContextWindowProjector(
            persistence=persistence,
            token_counter=_CharacterCounter(),
            budget=ContextTokenBudget(
                warning_tokens=TokenCount(20),
                hard_tokens=TokenCount(30),
                summary_output_tokens=TokenCount(2),
                segment_input_tokens=TokenCount(12),
                minimum_recent_non_tool_messages=1,
            ),
        )

        result = await projector.project(_request(current_turn))

        assert isinstance(result, CompactionPending)
        assert len(persistence.enqueued) == 1
        draft = persistence.enqueued[0]
        assert (draft.from_sequence, draft.through_sequence) == (1, 1)
        assert draft.source_snapshot == ({"role": "user", "content": "a" * 12},)
        assert draft.anchor_turn_id == current_turn

    asyncio.run(scenario())


def test_completed_checkpoint_builds_summary_plus_recent_tail() -> None:
    """@brief 已完成 checkpoint 位于 memory head，raw tail 从 watermark 后继续 / A completed checkpoint forms the memory head and raw tail resumes after its watermark."""

    async def scenario() -> None:
        """@brief 执行 checkpoint projection / Run checkpoint projection."""

        current_turn = TurnId.new()
        old_anchor = TurnId.new()
        draft = CompactionPlan.create(
            conversation_id=CONVERSATION,
            owner_user_id=7,
            epoch_floor_sequence=0,
            from_sequence=1,
            through_sequence=2,
            anchor_turn_id=old_anchor,
            predecessor_compaction_id=None,
            projection_version=1,
            source_snapshot=({"role": "user", "content": "old"},),
            source_row_count=2,
            source_token_count=TokenCount(3),
            created_at=NOW,
        )
        token = LeaseToken.new()
        checkpoint = (
            Compaction.pending(draft)
            .claim(
                token=token,
                claimed_at=NOW,
                lease_for=timedelta(seconds=30),
            )
            .complete(
                token=token,
                summary=CompactionSummary(
                    "remember old fact", TokenCount(3), "fake:model"
                ),
                completed_at=NOW + timedelta(seconds=1),
            )
        )
        messages = (
            _message(3, old_anchor, "recent"),
            _message(4, current_turn, "current"),
        )
        persistence = _Persistence(
            bounds=ContextWindowBounds(CONVERSATION, current_turn, 4, 4, 0),
            messages=messages,
            checkpoint=checkpoint,
        )
        projector = ContextWindowProjector(
            persistence=persistence,
            token_counter=_CharacterCounter(),
            budget=ContextTokenBudget(
                warning_tokens=TokenCount(300),
                hard_tokens=TokenCount(350),
                summary_output_tokens=TokenCount(10),
                segment_input_tokens=TokenCount(250),
            ),
        )

        result = await projector.project(_request(current_turn))

        assert isinstance(result, ContextWindowReady)
        assert result.checkpoint_summary == "remember old fact"
        assert [message["content"] for message in result.messages] == [
            "recent",
            "current",
        ]
        assert persistence.pages[0][0] == 2

    asyncio.run(scenario())


def test_next_compaction_snapshot_contains_the_prior_cumulative_memory() -> None:
    """@brief 后续 Segment 冻结前序摘要与新 delta，避免多段压缩遗忘 / A later segment freezes prior memory with its new delta so multi-segment compaction remains cumulative."""

    async def scenario() -> None:
        """@brief 执行第二段 compaction planning / Plan a second compaction segment."""

        current_turn = TurnId.new()
        old_anchor = TurnId.new()
        checkpoint_draft = CompactionPlan.create(
            conversation_id=CONVERSATION,
            owner_user_id=7,
            epoch_floor_sequence=0,
            from_sequence=1,
            through_sequence=1,
            anchor_turn_id=old_anchor,
            predecessor_compaction_id=None,
            projection_version=1,
            source_snapshot=({"role": "user", "content": "old"},),
            source_row_count=1,
            source_token_count=TokenCount(3),
            created_at=NOW,
        )
        token = LeaseToken.new()
        checkpoint = (
            Compaction.pending(checkpoint_draft)
            .claim(
                token=token,
                claimed_at=NOW,
                lease_for=timedelta(seconds=30),
            )
            .complete(
                token=token,
                summary=CompactionSummary(
                    "remember old fact",
                    TokenCount(3),
                    "fake:model",
                ),
                completed_at=NOW + timedelta(seconds=1),
            )
        )
        messages = (
            _message(2, old_anchor, "x" * 100),
            _message(3, old_anchor, "y" * 100),
            _message(4, current_turn, "current"),
        )
        persistence = _Persistence(
            bounds=ContextWindowBounds(CONVERSATION, current_turn, 4, 4, 0),
            messages=messages,
            checkpoint=checkpoint,
        )
        projector = ContextWindowProjector(
            persistence=persistence,
            token_counter=_CharacterCounter(),
            budget=ContextTokenBudget(
                warning_tokens=TokenCount(350),
                hard_tokens=TokenCount(500),
                summary_output_tokens=TokenCount(10),
                segment_input_tokens=TokenCount(300),
                minimum_recent_non_tool_messages=1,
            ),
        )

        result = await projector.project(_request(current_turn))

        assert isinstance(result, ContextWindowReady)
        assert len(persistence.enqueued) == 1
        draft = persistence.enqueued[0]
        assert draft.predecessor_compaction_id == checkpoint.compaction_id
        assert draft.source_snapshot[0] == checkpoint_summary_message(
            "remember old fact"
        )
        assert draft.source_snapshot[1:] == ({"role": "user", "content": "x" * 100},)

    asyncio.run(scenario())


def test_history_isolation_reads_only_the_anchor_turn_and_never_compacts() -> None:
    """@brief 翻译等隔离任务只投影当前 Turn，不因普通历史触发压缩 / Isolated tasks project only the anchor Turn and never compact ordinary history."""

    async def scenario() -> None:
        """@brief 执行 history-isolated projection / Run a history-isolated projection."""

        current_turn = TurnId.new()
        prior_turn = TurnId.new()
        current = _message(2, current_turn, "translate me", excluded=True)
        persistence = _Persistence(
            bounds=ContextWindowBounds(CONVERSATION, current_turn, 2, 2, 0),
            messages=(
                _message(1, prior_turn, "old" * 10_000),
                current,
            ),
        )
        projector = ContextWindowProjector(
            persistence=persistence,
            token_counter=_CharacterCounter(),
            budget=ContextTokenBudget(
                warning_tokens=TokenCount(100),
                hard_tokens=TokenCount(120),
                summary_output_tokens=TokenCount(10),
                segment_input_tokens=TokenCount(50),
            ),
        )
        request = ContextWindowRequest(
            conversation_id=CONVERSATION,
            owner_user_id=7,
            through_turn_id=current_turn,
            base_messages=(
                {"role": "system", "content": "translate"},
                {"role": "user", "content": "translate me"},
            ),
            reserved_tokens=TokenCount(0),
            requested_at=NOW,
            include_history=False,
        )

        result = await projector.project(request)

        assert isinstance(result, ContextWindowReady)
        assert result.messages == ()
        assert result.anchor_messages == (current,)
        assert persistence.pages == [(1, 2, 256)]
        assert persistence.enqueued == []

    asyncio.run(scenario())


def test_excluded_translation_payload_is_removed_before_token_budgeting() -> None:
    """@brief 翻译隔离标记在 token 预算前生效 / Translation-isolation markers apply before token budgeting."""

    async def scenario() -> None:
        """@brief 执行 exclusion scenario / Run the exclusion scenario."""

        current_turn = TurnId.new()
        prior_turn = TurnId.new()
        messages = (
            _message(1, prior_turn, "secret" * 100, excluded=True),
            _message(2, current_turn, "current"),
        )
        persistence = _Persistence(
            bounds=ContextWindowBounds(CONVERSATION, current_turn, 2, 2, 0),
            messages=messages,
        )
        projector = ContextWindowProjector(
            persistence=persistence,
            token_counter=_CharacterCounter(),
            budget=ContextTokenBudget(
                warning_tokens=TokenCount(50),
                hard_tokens=TokenCount(60),
                summary_output_tokens=TokenCount(5),
                segment_input_tokens=TokenCount(25),
            ),
        )

        result = await projector.project(_request(current_turn))

        assert isinstance(result, ContextWindowReady)
        assert result.messages == ({"role": "user", "content": "current"},)
        assert persistence.enqueued == []

    asyncio.run(scenario())
