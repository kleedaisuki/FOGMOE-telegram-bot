"""@brief Workspace 附件 receipt 可见性与 ContextWindow 缓存 CTest / CTest for Workspace-attachment receipt visibility and ContextWindow caching."""

from __future__ import annotations

import asyncio
import unittest
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from fogmoe_bot.application.context_window.cache import ContextWindowCache
from fogmoe_bot.application.context_window.projection import (
    ContextWindowBounds,
    ContextWindowProjector,
    ContextWindowReady,
    ContextWindowRequest,
    project_conversation_message,
)
from fogmoe_bot.domain.assistant.messages import CanonicalMessage, text_message
from fogmoe_bot.domain.context_window.budget import ContextTokenBudget, TokenCount
from fogmoe_bot.domain.context_window.compaction import (
    Compaction,
    CompactionEnqueueResult,
    CompactionPlan,
)
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    ConversationMessageId,
    MessageSequence,
    TurnId,
)
from fogmoe_bot.domain.conversation.message import (
    ConversationMessage,
    MessageDraft,
    MessageRole,
)
from fogmoe_bot.domain.conversation.payloads import JsonObject
from fogmoe_bot.domain.workspace.attachment import (
    WorkspaceAttachmentImportState,
    pending_workspace_attachment_marker,
)

_NOW = datetime(2030, 1, 1, tzinfo=UTC)
"""@brief 测试使用的固定 UTC 时刻 / Stable UTC instant used by tests."""

_CONVERSATION = ConversationId("assistant-user:42")
"""@brief 测试使用的个人会话 / Personal conversation used by tests."""


class _CharacterCounter:
    """@brief 以文本字符数近似 token 的稳定替身 / Stable double approximating tokens by text characters."""

    def count_messages(self, messages: Sequence[CanonicalMessage]) -> TokenCount:
        """@brief 统计每条 canonical message 的最少一字符 token / Count at least one character token per canonical message.

        @param messages 有序 canonical V2 消息 / Ordered canonical V2 messages.
        @return 稳定的近似 token 数 / Stable approximate token count.
        """

        return TokenCount(sum(max(1, len(message.text)) for message in messages))


class _Persistence:
    """@brief 仅供 ContextWindow 投影使用的内存 persistence 替身 / In-memory persistence double used only by ContextWindow projection."""

    def __init__(
        self,
        *,
        bounds: ContextWindowBounds,
        messages: tuple[ConversationMessage, ...],
    ) -> None:
        """@brief 保存可替换的 append-only 行 / Store replaceable append-only rows.

        @param bounds 当前 anchor 边界 / Current anchor bounds.
        @param messages 当前数据库读取应返回的行 / Rows returned by the current database read.
        @return None / None.
        """

        self.bounds = bounds
        self.messages = messages
        self.pages: list[tuple[int, int]] = []
        self.enqueued: list[CompactionPlan] = []

    async def history_bounds(
        self,
        conversation_id: ConversationId,
        *,
        through_turn_id: TurnId,
    ) -> ContextWindowBounds | None:
        """@brief 返回固定 anchor 边界 / Return fixed anchor bounds.

        @param conversation_id 请求会话 / Requested conversation.
        @param through_turn_id 请求锚点 / Requested anchor.
        @return 匹配的固定边界 / Matching fixed bounds.
        """

        if (
            conversation_id != self.bounds.conversation_id
            or through_turn_id != self.bounds.through_turn_id
        ):
            raise AssertionError("ContextWindow request crossed its test bounds")
        return self.bounds

    async def latest_completed_compaction(
        self,
        conversation_id: ConversationId,
        *,
        epoch_floor_sequence: int,
        before_sequence: int,
    ) -> Compaction | None:
        """@brief 不提供 checkpoint / Provide no checkpoint.

        @param conversation_id 请求会话 / Requested conversation.
        @param epoch_floor_sequence reset epoch 起点 / Reset epoch floor.
        @param before_sequence anchor 前边界 / Boundary before the anchor.
        @return 恒为 None / Always None.
        """

        del conversation_id, epoch_floor_sequence, before_sequence
        return None

    async def active_compaction(
        self,
        conversation_id: ConversationId,
        *,
        epoch_floor_sequence: int,
    ) -> Compaction | None:
        """@brief 不提供既有 compaction / Provide no existing compaction.

        @param conversation_id 请求会话 / Requested conversation.
        @param epoch_floor_sequence reset epoch 起点 / Reset epoch floor.
        @return 恒为 None / Always None.
        """

        del conversation_id, epoch_floor_sequence
        return None

    async def read_messages_page(
        self,
        conversation_id: ConversationId,
        *,
        after_sequence: int,
        through_sequence: int,
        limit: int,
    ) -> Sequence[ConversationMessage]:
        """@brief 按 sequence 返回一个完整 keyset page / Return one complete keyset page by sequence.

        @param conversation_id 请求会话 / Requested conversation.
        @param after_sequence 排他游标 / Exclusive cursor.
        @param through_sequence 包含上界 / Inclusive upper bound.
        @param limit 页面限制 / Page limit.
        @return 符合边界的测试行 / Test rows satisfying the bounds.
        """

        if conversation_id != _CONVERSATION:
            raise AssertionError("ContextWindow read crossed its conversation")
        self.pages.append((after_sequence, through_sequence))
        return tuple(
            message
            for message in self.messages
            if after_sequence < int(message.sequence) <= through_sequence
        )[:limit]

    async def enqueue_compaction(
        self,
        draft: CompactionPlan,
    ) -> CompactionEnqueueResult:
        """@brief 记录 compaction plan 并返回 pending 聚合 / Record a compaction plan and return a pending aggregate.

        @param draft 待持久化的 plan / Plan to persist.
        @return 对应的 pending receipt / Corresponding pending receipt.
        """

        self.enqueued.append(draft)
        return CompactionEnqueueResult(Compaction.pending(draft), True)


def _message(
    *,
    sequence: int,
    turn_id: TurnId,
    text: str,
    attachment_state: WorkspaceAttachmentImportState | None = None,
) -> ConversationMessage:
    """@brief 构造带可选附件 visibility marker 的 user 行 / Build a user row with an optional attachment-visibility marker.

    @param sequence 会话 sequence / Conversation sequence.
    @param turn_id 所属 Turn / Owning Turn.
    @param text canonical placeholder 或普通文本 / Canonical placeholder or ordinary text.
    @param attachment_state 可选受控附件状态 / Optional controlled attachment state.
    @return 有序 durable ConversationMessage / Sequenced durable ConversationMessage.
    """

    content: JsonObject = {
        "text": text,
        "model_message": text_message(MessageRole.USER, text).to_json(),
    }
    if attachment_state is not None:
        marker = pending_workspace_attachment_marker()
        marker["state"] = attachment_state.value
        content["workspace_attachment"] = marker
    return ConversationMessage(
        draft=MessageDraft(
            message_id=ConversationMessageId.new(),
            conversation_id=_CONVERSATION,
            turn_id=turn_id,
            source_update_id=None,
            role=MessageRole.USER,
            content=content,
            idempotency_key=f"workspace-attachment-visibility:{sequence}:{turn_id}",
            created_at=_NOW + timedelta(microseconds=sequence),
        ),
        sequence=MessageSequence(sequence),
    )


def _request(turn_id: TurnId) -> ContextWindowRequest:
    """@brief 构造普通历史投影请求 / Build an ordinary-history projection request.

    @param turn_id 当前 anchor Turn / Current anchor Turn.
    @return 有效的 ContextWindow request / Valid ContextWindow request.
    """

    return ContextWindowRequest(
        conversation_id=_CONVERSATION,
        owner_user_id=42,
        through_turn_id=turn_id,
        base_messages=(text_message(MessageRole.SYSTEM, "system"),),
        reserved_tokens=TokenCount(0),
        requested_at=_NOW,
    )


class WorkspaceAttachmentVisibilityTests(unittest.TestCase):
    """@brief pending/imported/unavailable 三态在所有普通投影面的回归测试 / Regression tests for pending/imported/unavailable across ordinary projection surfaces."""

    def test_only_imported_marker_projects_a_workspace_path(self) -> None:
        """@brief 只有 receipt 已见证的 imported 行能进入普通 canonical 投影 / Only a receipt-witnessed imported row enters ordinary canonical projection.

        @return None / None.
        """

        turn = TurnId.new()
        path_text = '<workspace_file path="/workspace/uploads/attachment-a/payload" />'
        pending = _message(
            sequence=1,
            turn_id=turn,
            text=path_text,
            attachment_state=WorkspaceAttachmentImportState.PENDING,
        )
        imported = _message(
            sequence=2,
            turn_id=turn,
            text=path_text,
            attachment_state=WorkspaceAttachmentImportState.IMPORTED,
        )
        unavailable = _message(
            sequence=3,
            turn_id=turn,
            text=path_text,
            attachment_state=WorkspaceAttachmentImportState.UNAVAILABLE,
        )

        self.assertEqual(project_conversation_message(pending), [])
        self.assertEqual(
            project_conversation_message(imported),
            [text_message(MessageRole.USER, path_text)],
        )
        self.assertEqual(project_conversation_message(unavailable), [])

    def test_pending_row_never_enters_cache_and_a_later_import_is_re_read(self) -> None:
        """@brief pending 读取不缓存，receipt 提交后的下一次投影重新读取并显示 imported / A pending read is not cached, so a later receipt commit is re-read and shows imported.

        @return None / None.
        """

        async def scenario() -> None:
            """@brief 模拟两个进程之间无法共享本地 invalidation 的 receipt 提交 / Simulate a receipt commit that cannot share local invalidation across processes.

            @return None / None.
            """

            prior_turn = TurnId.new()
            current_turn = TurnId.new()
            path_text = (
                '<workspace_file path="/workspace/uploads/attachment-a/payload" />'
            )
            pending = _message(
                sequence=1,
                turn_id=prior_turn,
                text=path_text,
                attachment_state=WorkspaceAttachmentImportState.PENDING,
            )
            current = _message(sequence=2, turn_id=current_turn, text="current")
            persistence = _Persistence(
                bounds=ContextWindowBounds(_CONVERSATION, current_turn, 2, 2, 0),
                messages=(pending, current),
            )
            cache = ContextWindowCache(capacity=2, ttl_seconds=60)
            projector = ContextWindowProjector(
                persistence=persistence,
                token_counter=_CharacterCounter(),
                cache=cache,
            )

            first = await projector.project(_request(current_turn))
            self.assertIsInstance(first, ContextWindowReady)
            assert isinstance(first, ContextWindowReady)
            self.assertEqual([message.text for message in first.messages], ["current"])
            self.assertIsNone(
                cache.get(
                    conversation_id=_CONVERSATION,
                    epoch_floor_sequence=0,
                    start_sequence=0,
                    checkpoint_id=None,
                    include_history=True,
                    through_sequence=2,
                )
            )

            imported = _message(
                sequence=1,
                turn_id=prior_turn,
                text=path_text,
                attachment_state=WorkspaceAttachmentImportState.IMPORTED,
            )
            persistence.messages = (imported, current)
            second = await projector.project(_request(current_turn))
            self.assertIsInstance(second, ContextWindowReady)
            assert isinstance(second, ContextWindowReady)
            self.assertEqual(
                [message.text for message in second.messages],
                [path_text, "current"],
            )
            self.assertEqual(persistence.pages, [(0, 2), (0, 2)])

        asyncio.run(scenario())

    def test_pending_row_blocks_compaction_from_crossing_its_future_receipt(
        self,
    ) -> None:
        """@brief pending 行可暂时隐藏但不能被压缩为空摘要 / A pending row may be hidden temporarily but cannot be compacted into an empty summary.

        @return None / None.
        """

        async def scenario() -> None:
            """@brief 构造一个需要压缩、且 pending 位于可压缩前缀中的窗口 / Construct a window requiring compaction with pending inside its compactable prefix.

            @return None / None.
            """

            first_turn = TurnId.new()
            pending_turn = TurnId.new()
            current_turn = TurnId.new()
            first = _message(sequence=1, turn_id=first_turn, text="a" * 10)
            pending = _message(
                sequence=2,
                turn_id=pending_turn,
                text='<workspace_file path="/workspace/uploads/attachment-a/payload" />',
                attachment_state=WorkspaceAttachmentImportState.PENDING,
            )
            current = _message(sequence=3, turn_id=current_turn, text="c" * 10)
            persistence = _Persistence(
                bounds=ContextWindowBounds(_CONVERSATION, current_turn, 3, 3, 0),
                messages=(first, pending, current),
            )
            projector = ContextWindowProjector(
                persistence=persistence,
                token_counter=_CharacterCounter(),
                budget=ContextTokenBudget(
                    warning_tokens=TokenCount(15),
                    hard_tokens=TokenCount(30),
                    warning_messages=2,
                    hard_messages=4,
                    summary_output_tokens=TokenCount(1),
                    segment_input_tokens=TokenCount(10),
                    minimum_recent_non_tool_messages=1,
                ),
            )

            result = await projector.project(_request(current_turn))
            self.assertIsInstance(result, ContextWindowReady)
            self.assertEqual(len(persistence.enqueued), 1)
            plan = persistence.enqueued[0]
            self.assertEqual((plan.from_sequence, plan.through_sequence), (1, 1))
            self.assertNotIn("workspace_file", str(plan.source_snapshot))

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
