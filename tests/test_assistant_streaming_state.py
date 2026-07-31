"""@brief Assistant 流投影状态机测试 / Tests for the Assistant stream-projection state machine."""

import asyncio
from datetime import UTC, datetime

import pytest

from fogmoe_bot.application.assistant.streaming import (
    AssistantStreamSession,
    AssistantStreamTarget,
)
from fogmoe_bot.domain.assistant.streaming import (
    AssistantActivityKind,
    AssistantActivityStatus,
    AssistantStreamFrame,
    AssistantStreamKind,
    AssistantStreamState,
)
from fogmoe_bot.domain.conversation.identity import TurnId

NOW = datetime(2030, 1, 1, tzinfo=UTC)
"""@brief 确定性测试时刻 / Deterministic test instant."""


class _BlockingTerminalProjection:
    """@brief 阻塞终态入队以复现 task cancellation 窗口 / Block terminal enqueue to reproduce a task-cancellation window."""

    def __init__(self) -> None:
        """@brief 初始化同步点与 frame 日志 / Initialize synchronization points and the frame log."""

        self.entered = asyncio.Event()
        """@brief 终态 projection 已进入 / Terminal projection has been entered."""
        self.release = asyncio.Event()
        """@brief 允许终态 projection 返回 / Permit terminal projection to return."""
        self.frames: list[AssistantStreamFrame] = []
        """@brief 已完成入队的 frames / Frames whose enqueue completed."""

    async def project(
        self,
        target: AssistantStreamTarget,
        frame: AssistantStreamFrame,
    ) -> None:
        """@brief 记录普通帧并阻塞终态帧 / Record ordinary frames and block a terminal frame.

        @param target 显式投影目标 / Explicit projection target.
        @param frame 当前流帧 / Current stream frame.
        @return None / None.
        """

        assert target.chat_id == 42
        if frame.kind in {
            AssistantStreamKind.SUSPENDED,
            AssistantStreamKind.COMPLETED,
            AssistantStreamKind.FAILED,
        }:
            self.entered.set()
            await self.release.wait()
        self.frames.append(frame)


def test_stream_frames_are_cumulative_and_monotonic() -> None:
    """@brief 文本流形成单调累计帧 / Text streaming forms monotonic cumulative frames."""

    turn_id = TurnId.parse("00000000-0000-4000-8000-000000000042")
    state = AssistantStreamState.begin(
        turn_id=turn_id,
        generation=3,
        revision=0,
        emitted_at=NOW,
    )

    started = state.current_frame
    first = state.append("Hel", emitted_at=NOW)
    second = state.append("lo", emitted_at=NOW)
    completed = state.complete(emitted_at=NOW)

    assert [frame.kind for frame in (started, first, second, completed)] == [
        AssistantStreamKind.STARTED,
        AssistantStreamKind.DELTA,
        AssistantStreamKind.DELTA,
        AssistantStreamKind.COMPLETED,
    ]
    assert [frame.sequence for frame in (started, first, second, completed)] == [
        0,
        1,
        2,
        3,
    ]
    assert first.delta_text == "Hel"
    assert second.delta_text == "lo"
    assert completed.cumulative_text == "Hello"
    assert not hasattr(completed, "chat_id")
    assert not hasattr(completed, "draft_id")


def test_stream_aggregate_closes_construction_and_mutable_activity_state() -> None:
    """@brief 聚合拒绝裸构造且不泄漏可变活动集合 /
    The aggregate rejects bare construction and does not leak its mutable activity collection.

    @return None / None.
    """

    with pytest.raises(TypeError, match=r"AssistantStreamState\.begin"):
        AssistantStreamState()

    state = AssistantStreamState.begin(
        turn_id=TurnId.new(),
        generation=1,
        revision=0,
        emitted_at=NOW,
    )
    state.commentary("checkpoint", "正在检查。", emitted_at=NOW)

    assert isinstance(state.activities, tuple)
    with pytest.raises(AttributeError):
        state.activities = ()  # type: ignore[misc]


def test_steer_revision_resets_provider_neutral_preview() -> None:
    """@brief steer 提升 revision 并重置 provider-neutral 预览 / A steer advances the revision and resets the provider-neutral preview."""

    turn_id = TurnId.parse("00000000-0000-4000-8000-000000000043")
    state = AssistantStreamState.begin(
        turn_id=turn_id,
        generation=1,
        revision=0,
        emitted_at=NOW,
    )
    state.append("stale preview", emitted_at=NOW)

    revised = state.revise(
        generation=2,
        revision=1,
        emitted_at=NOW,
    )
    fresh = state.append("fresh", emitted_at=NOW)

    assert revised.kind is AssistantStreamKind.REVISED
    assert revised.revision == 1
    assert revised.generation == 2
    assert revised.cumulative_text == ""
    assert fresh.cumulative_text == "fresh"


def test_activity_frames_keep_model_commentary_and_tool_identity_without_payloads() -> (
    None
):
    """@brief 活动帧只保存模型公开 commentary 与工具名 /
    Activity frames retain only model-authored public commentary and the tool name.
    """

    state = AssistantStreamState.begin(
        turn_id=TurnId.new(),
        generation=1,
        revision=0,
        emitted_at=NOW,
    )
    commentary = state.commentary(
        "step:0:commentary",
        "我先确认一下现在的时间，再回答你。",
        emitted_at=NOW,
    )
    started = state.start_tool(
        "step:0:call:0",
        "google_search",
        emitted_at=NOW,
    )
    finished = state.finish_tool(
        "step:0:call:0",
        "google_search",
        succeeded=True,
        emitted_at=NOW,
    )
    completed = state.complete(emitted_at=NOW)

    assert all(
        frame.kind is AssistantStreamKind.ACTIVITY
        for frame in (commentary, started, finished)
    )
    note = commentary.activities[-1]
    assert note.kind is AssistantActivityKind.COMMENTARY
    assert note.label == "我先确认一下现在的时间，再回答你。"
    assert note.status is AssistantActivityStatus.COMPLETED
    tool = next(
        activity
        for activity in completed.activities
        if activity.kind is AssistantActivityKind.TOOL
    )
    assert tool.label == "google_search"
    assert tool.status is AssistantActivityStatus.COMPLETED
    assert "arguments" not in repr(completed.activities)
    assert {activity.kind for activity in completed.activities} == {
        AssistantActivityKind.COMMENTARY,
        AssistantActivityKind.TOOL,
    }


def test_stream_failure_exposes_only_a_stable_safe_code() -> None:
    """@brief failure frame 只接受稳定安全代码且状态进入终态 / A failure frame accepts only a stable safe code and terminalizes the stream."""

    state = AssistantStreamState.begin(
        turn_id=TurnId.new(),
        generation=1,
        revision=0,
        emitted_at=NOW,
    )
    failed = state.fail("provider_unavailable", emitted_at=NOW)

    assert failed.kind is AssistantStreamKind.FAILED
    assert failed.safe_error_code == "provider_unavailable"
    with pytest.raises(RuntimeError, match="terminal"):
        state.append("must not leak", emitted_at=NOW)

    fresh = AssistantStreamState.begin(
        turn_id=TurnId.new(),
        generation=1,
        revision=0,
        emitted_at=NOW,
    )
    with pytest.raises(ValueError, match="safe_error_code"):
        fresh.fail("/srv/secret key=abc", emitted_at=NOW)


def test_private_and_group_target_invariants_are_explicit() -> None:
    """@brief 私聊拒绝 Topic，群聊要求数值 chat ID / Private chats reject topics and groups require numeric chat IDs."""

    with pytest.raises(ValueError, match="Private"):
        AssistantStreamTarget(chat_id=42, is_group=False, message_thread_id=7)
    with pytest.raises(ValueError, match="integer"):
        AssistantStreamTarget(
            chat_id="@channel",
            is_group=True,
            message_thread_id=None,
        )


def test_terminal_projection_finishes_enqueue_before_cancellation_propagates() -> None:
    """@brief durable 终态投影先完成短入队，再传播调用方取消 /
    A durable terminal projection completes its short enqueue before caller cancellation propagates.
    """

    async def scenario() -> None:
        """@brief 在 COMPLETED 等待 projection lock 时取消 task / Cancel while COMPLETED waits on the projection lock."""

        projection = _BlockingTerminalProjection()
        session = AssistantStreamSession(
            target=AssistantStreamTarget(
                chat_id=42,
                is_group=False,
                message_thread_id=None,
            ),
            state=AssistantStreamState.begin(
                turn_id=TurnId.new(),
                generation=1,
                revision=0,
                emitted_at=NOW,
            ),
            projection=projection,
        )
        await session.start()
        await session.append("answer", emitted_at=NOW)
        terminal = asyncio.create_task(session.complete(emitted_at=NOW))
        await asyncio.wait_for(projection.entered.wait(), timeout=1.0)

        terminal.cancel()
        await asyncio.sleep(0)
        assert not terminal.done()
        projection.release.set()
        with pytest.raises(asyncio.CancelledError):
            await terminal

        assert projection.frames[-1].kind is AssistantStreamKind.COMPLETED
        assert projection.frames[-1].cumulative_text == "answer"

    asyncio.run(scenario())
