"""@brief Telegram Assistant 流投影测试 / Tests for Telegram Assistant stream projection."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from telegram.error import BadRequest, TelegramError

from fogmoe_bot.application.assistant.streaming import AssistantStreamTarget
from fogmoe_bot.domain.assistant.streaming import (
    AssistantStreamFrame,
    AssistantStreamState,
)
from fogmoe_bot.domain.conversation.identity import TurnId
from fogmoe_bot.infrastructure.telegram.assistant_streaming import (
    TelegramAssistantStreamProjection,
)


class _RecordingBot:
    """@brief 记录草稿与 typing 调用的 Telegram 替身 / Telegram double recording draft and typing calls."""

    def __init__(self, *, reject_first_draft: bool = False) -> None:
        """@brief 创建可选拒绝首个草稿的替身 / Create a double optionally rejecting its first draft.

        @param reject_first_draft 是否模拟原生草稿不可用 / Whether to simulate unavailable native drafts.
        """

        self.reject_first_draft = reject_first_draft
        """@brief 是否拒绝首个草稿 / Whether the first draft is rejected."""
        self.typing_calls: list[tuple[int | str, str, int | None]] = []
        """@brief 已观察 typing 调用 / Observed typing calls."""
        self.draft_calls: list[tuple[int, int, str | None, int | None]] = []
        """@brief 已观察草稿调用 / Observed draft calls."""
        self.typing_event = asyncio.Event()
        """@brief 首个 typing 调用同步点 / Synchronization point for the first typing call."""
        self.draft_event = asyncio.Event()
        """@brief 首个草稿调用同步点 / Synchronization point for the first draft call."""

    async def send_chat_action(
        self,
        chat_id: int | str,
        action: str,
        message_thread_id: int | None = None,
    ) -> bool:
        """@brief 记录 typing / Record typing.

        @param chat_id Telegram chat / Telegram chat.
        @param action action 名称 / Action name.
        @param message_thread_id 可选 Topic / Optional topic.
        @return 始终确认 / Always acknowledged.
        """

        self.typing_calls.append((chat_id, action, message_thread_id))
        self.typing_event.set()
        return True

    async def send_message_draft(
        self,
        chat_id: int,
        draft_id: int,
        text: str | None = None,
        message_thread_id: int | None = None,
    ) -> bool:
        """@brief 记录或拒绝草稿 / Record or reject a draft.

        @param chat_id 私聊 ID / Private-chat ID.
        @param draft_id 稳定草稿 ID / Stable draft ID.
        @param text 累计预览 / Cumulative preview.
        @param message_thread_id 私聊无 Topic / No topic in private chats.
        @return 未拒绝时确认 / Acknowledged unless rejected.
        @raise BadRequest 配置为拒绝首个草稿时抛出 /
            Raised when configured to reject the first draft.
        """

        self.draft_calls.append((chat_id, draft_id, text, message_thread_id))
        self.draft_event.set()
        if self.reject_first_draft and len(self.draft_calls) == 1:
            raise BadRequest("sendMessageDraft unavailable")
        return True


class _BlockingTerminalBot(_RecordingBot):
    """@brief 在指定草稿写入处阻塞以稳定复现终态竞态 / Block one selected draft write to deterministically reproduce a terminal race."""

    def __init__(self) -> None:
        """@brief 初始化终态同步点 / Initialize terminal synchronization points."""

        super().__init__()
        self.block_next_draft = False
        """@brief 下一次草稿调用是否阻塞 / Whether the next draft call blocks."""
        self.terminal_entered = asyncio.Event()
        """@brief actor 已进入阻塞调用 / Actor has entered the blocked call."""
        self.release_terminal = asyncio.Event()
        """@brief 允许阻塞调用返回 / Permit the blocked call to return."""

    async def send_message_draft(
        self,
        chat_id: int,
        draft_id: int,
        text: str | None = None,
        message_thread_id: int | None = None,
    ) -> bool:
        """@brief 记录草稿并按需阻塞 / Record a draft and block when requested.

        @param chat_id 私聊 ID / Private-chat ID.
        @param draft_id 稳定草稿 ID / Stable draft ID.
        @param text 累计预览 / Cumulative preview.
        @param message_thread_id 私聊无 Topic / No topic in private chats.
        @return Telegram 确认 / Telegram acknowledgement.
        """

        acknowledged = await super().send_message_draft(
            chat_id,
            draft_id,
            text,
            message_thread_id,
        )
        if self.block_next_draft:
            self.block_next_draft = False
            self.terminal_entered.set()
            await self.release_terminal.wait()
        return acknowledged


class _TransientDraftBot(_RecordingBot):
    """@brief 首次草稿瞬时失败、随后恢复的网络替身 / Network double with one transient draft failure followed by recovery."""

    async def send_message_draft(
        self,
        chat_id: int,
        draft_id: int,
        text: str | None = None,
        message_thread_id: int | None = None,
    ) -> bool:
        """@brief 首次调用抛出瞬时 TelegramError / Raise a transient TelegramError on the first call.

        @param chat_id 私聊 ID / Private-chat ID.
        @param draft_id 稳定草稿 ID / Stable draft ID.
        @param text 累计活动快照 / Cumulative activity snapshot.
        @param message_thread_id 私聊无 Topic / No topic in private chats.
        @return 第二次起确认 / Acknowledged from the second call onward.
        @raise TelegramError 首次模拟抖动 / Simulated jitter on the first call.
        """

        self.draft_calls.append((chat_id, draft_id, text, message_thread_id))
        self.draft_event.set()
        if len(self.draft_calls) == 1:
            raise TelegramError("transient network jitter")
        return True


_TARGETS: dict[TurnId, AssistantStreamTarget] = {}
"""@brief 测试 Turn 到显式投影目标的映射 / Test-Turn to explicit-projection-target mapping."""


def _state(
    *,
    chat_id: int = 42,
    is_group: bool = False,
    message_thread_id: int | None = None,
) -> AssistantStreamState:
    """@brief 构造确定性测试流 / Build a deterministic test stream.

    @param chat_id Telegram chat / Telegram chat.
    @param is_group 是否群聊 / Whether this is a group chat.
    @param message_thread_id 可选 Topic / Optional topic.
    @return started 流状态 / Started stream state.
    """

    state = AssistantStreamState.begin(
        turn_id=TurnId.parse("5c55082f-0904-4b9d-839a-c20718fd3729"),
        generation=1,
        revision=0,
        emitted_at=datetime(2026, 7, 31, tzinfo=UTC),
    )
    _TARGETS[state.turn_id] = AssistantStreamTarget(
        chat_id=chat_id,
        is_group=is_group,
        message_thread_id=message_thread_id,
    )
    return state


async def _project(
    projection: TelegramAssistantStreamProjection,
    frame: AssistantStreamFrame,
) -> None:
    """@brief 向测试 Turn 的显式目标投影一帧 / Project a frame to its test Turn's explicit target.

    @param projection Telegram 投影 adapter / Telegram projection adapter.
    @param frame provider-neutral 流帧 / Provider-neutral stream frame.
    @return None / None.
    """

    await projection.project(_TARGETS[frame.turn_id], frame)


def test_private_stream_coalesces_deltas_and_stops_typing_at_completion() -> None:
    """@brief 私聊累计草稿有界合并且终态停止 typing / Private cumulative drafts coalesce boundedly and completion stops typing."""

    async def scenario() -> None:
        """@brief 执行私聊流生命周期 / Exercise the private stream lifecycle.

        @return None / None.
        """

        bot = _RecordingBot()
        projection = TelegramAssistantStreamProjection(
            bot,
            native_drafts_enabled=True,
            draft_interval_seconds=0.05,
            typing_refresh_seconds=0.01,
        )
        state = _state()
        await _project(projection, state.current_frame)
        await asyncio.wait_for(bot.typing_event.wait(), timeout=1.0)

        for delta in ("流", "式", "回", "答"):
            await _project(
                projection, state.append(delta, emitted_at=datetime.now(UTC))
            )
        await _project(projection, state.complete(emitted_at=datetime.now(UTC)))
        await asyncio.wait_for(bot.draft_event.wait(), timeout=1.0)
        await asyncio.sleep(0.03)

        assert bot.draft_calls[-1][2] == "最后的回答整理好了，正在接着发给你～"
        assert all("流式回答" not in str(call[2]) for call in bot.draft_calls)
        assert len({call[1] for call in bot.draft_calls}) == 1
        typing_count = len(bot.typing_calls)
        await asyncio.sleep(0.03)
        assert len(bot.typing_calls) == typing_count
        await projection.aclose()

    asyncio.run(scenario())


def test_group_stream_uses_topic_typing_without_native_drafts() -> None:
    """@brief 群聊只投影 Topic typing、不调用私聊草稿 / Group streams project topic typing without private drafts."""

    async def scenario() -> None:
        """@brief 执行群聊投影 / Exercise group projection.

        @return None / None.
        """

        bot = _RecordingBot()
        projection = TelegramAssistantStreamProjection(
            bot,
            native_drafts_enabled=True,
            typing_refresh_seconds=0.02,
        )
        state = _state(chat_id=-100123, is_group=True, message_thread_id=77)
        await _project(projection, state.current_frame)
        await _project(
            projection, state.append("群聊回答", emitted_at=datetime.now(UTC))
        )
        await asyncio.wait_for(bot.typing_event.wait(), timeout=1.0)
        await _project(projection, state.complete(emitted_at=datetime.now(UTC)))
        await asyncio.sleep(0.03)

        assert bot.typing_calls[0] == (-100123, "typing", 77)
        assert bot.draft_calls == []
        await projection.aclose()

    asyncio.run(scenario())


def test_native_drafts_are_opt_in_to_avoid_client_side_redraw() -> None:
    """@brief 默认只发 typing，避免原生草稿重绘导致视口跳动 /
    The default sends only typing to avoid viewport jumps caused by native-draft redraw.
    """

    async def scenario() -> None:
        """@brief 投影完整私聊生命周期但不启用 draft / Project a full private lifecycle without enabling drafts."""

        bot = _RecordingBot()
        projection = TelegramAssistantStreamProjection(
            bot,
            typing_refresh_seconds=0.01,
        )
        state = _state()
        await _project(projection, state.current_frame)
        await _project(
            projection,
            state.commentary(
                "step:0:commentary",
                "我先确认一下资料。",
                emitted_at=datetime.now(UTC),
            ),
        )
        await _project(
            projection,
            state.start_tool(
                "step:0:call:0",
                "google_search",
                emitted_at=datetime.now(UTC),
            ),
        )
        await asyncio.wait_for(bot.typing_event.wait(), timeout=1.0)
        await _project(projection, state.complete(emitted_at=datetime.now(UTC)))
        await asyncio.sleep(0.03)

        assert bot.typing_calls
        assert bot.draft_calls == []
        await projection.aclose()

    asyncio.run(scenario())


def test_steer_reuses_draft_identity_and_discards_a_stale_generation_frame() -> None:
    """@brief steer 复用草稿身份且丢弃旧代际迟到帧 / A steer reuses draft identity and discards a late stale-generation frame."""

    async def scenario() -> None:
        """@brief 执行 revision 切换 / Exercise a revision switch.

        @return None / None.
        """

        bot = _RecordingBot()
        projection = TelegramAssistantStreamProjection(
            bot,
            native_drafts_enabled=True,
            draft_interval_seconds=0.0,
            typing_refresh_seconds=0.05,
        )
        state = _state()
        await _project(projection, state.current_frame)
        stale = state.append("旧答案", emitted_at=datetime.now(UTC))
        await _project(projection, stale)
        await _project(
            projection,
            state.revise(
                generation=2,
                revision=1,
                emitted_at=datetime.now(UTC),
            ),
        )
        await _project(projection, state.append("新答案", emitted_at=datetime.now(UTC)))
        await _project(projection, stale)
        await _project(projection, state.complete(emitted_at=datetime.now(UTC)))
        await asyncio.wait_for(bot.draft_event.wait(), timeout=1.0)
        await asyncio.sleep(0.03)

        assert bot.draft_calls[-1][2] == "最后的回答整理好了，正在接着发给你～"
        assert len({call[1] for call in bot.draft_calls}) == 1
        await projection.aclose()

    asyncio.run(scenario())


def test_native_draft_rejection_degrades_to_typing_without_failing_projection() -> None:
    """@brief 原生草稿拒绝后降级为 typing 且不污染推理 / Native-draft rejection degrades to typing without failing inference."""

    async def scenario() -> None:
        """@brief 执行草稿降级路径 / Exercise the draft fallback.

        @return None / None.
        """

        bot = _RecordingBot(reject_first_draft=True)
        projection = TelegramAssistantStreamProjection(
            bot,
            native_drafts_enabled=True,
            draft_interval_seconds=0.0,
            typing_refresh_seconds=0.02,
        )
        state = _state()
        await _project(projection, state.current_frame)
        await _project(
            projection, state.append("不会中断推理", emitted_at=datetime.now(UTC))
        )
        await asyncio.wait_for(bot.draft_event.wait(), timeout=1.0)
        await _project(
            projection, state.append("，只降级", emitted_at=datetime.now(UTC))
        )
        await _project(projection, state.complete(emitted_at=datetime.now(UTC)))
        await asyncio.sleep(0.03)

        assert len(bot.draft_calls) == 1
        assert bot.typing_calls
        await projection.aclose()

    asyncio.run(scenario())


def test_suspended_actor_hands_off_the_next_generation_without_losing_its_start() -> (
    None
):
    """@brief SUSPENDED 停止 typing 并以原子 handoff 保住下一代 STARTED /
    SUSPENDED stops typing and atomically hands off a racing next-generation STARTED.
    """

    async def scenario() -> None:
        """@brief 在 suspension 清理期间立即提交下一 generation / Submit the next generation while suspension cleanup is in flight.

        @return None / None.
        """

        bot = _RecordingBot()
        projection = TelegramAssistantStreamProjection(
            bot,
            native_drafts_enabled=True,
            draft_interval_seconds=0.0,
            typing_refresh_seconds=0.01,
        )
        first = _state()
        await _project(projection, first.current_frame)
        await asyncio.wait_for(bot.typing_event.wait(), timeout=1.0)
        await _project(projection, first.suspend(emitted_at=datetime.now(UTC)))

        retry = AssistantStreamState.begin(
            turn_id=first.turn_id,
            generation=2,
            revision=0,
            emitted_at=datetime.now(UTC),
        )
        await _project(projection, retry.current_frame)
        await _project(
            projection, retry.append("重试后的回答", emitted_at=datetime.now(UTC))
        )
        await _project(projection, retry.complete(emitted_at=datetime.now(UTC)))
        await asyncio.wait_for(bot.draft_event.wait(), timeout=1.0)
        await asyncio.sleep(0.03)

        assert bot.draft_calls[-1][2] == "最后的回答整理好了，正在接着发给你～"
        assert len({call[1] for call in bot.draft_calls}) == 1
        await projection.aclose()

    asyncio.run(scenario())


def test_committed_retry_replaces_partial_text_with_an_explicit_status() -> None:
    """@brief retry 事务提交后用明确状态替换不完整草稿并停止 typing /
    A committed retry replaces an incomplete draft with an explicit status and stops typing.
    """

    async def scenario() -> None:
        """@brief 投影 partial 后的 SUSPENDED / Project SUSPENDED after a partial draft."""

        bot = _RecordingBot()
        projection = TelegramAssistantStreamProjection(
            bot,
            native_drafts_enabled=True,
            draft_interval_seconds=0.0,
            typing_refresh_seconds=0.01,
        )
        state = _state()
        await _project(projection, state.current_frame)
        await _project(
            projection, state.append("不完整回答", emitted_at=datetime.now(UTC))
        )
        await asyncio.wait_for(bot.draft_event.wait(), timeout=1.0)
        await _project(projection, state.suspend(emitted_at=datetime.now(UTC)))

        async with asyncio.timeout(1.0):
            while (
                not bot.draft_calls
                or bot.draft_calls[-1][2] != "唔，刚才暂时卡住了…我会自己接着处理的。"
            ):
                await asyncio.sleep(0)

        typing_count = len(bot.typing_calls)
        await asyncio.sleep(0.03)
        assert len(bot.typing_calls) == typing_count
        await projection.aclose()

    asyncio.run(scenario())


def test_terminal_actor_does_not_drop_a_racing_new_generation() -> None:
    """@brief 旧终态网络调用期间到达的新 generation 仍由同 actor 消费 /
    A new generation arriving during the old terminal network call remains owned by the actor.
    """

    async def scenario() -> None:
        """@brief 用显式同步点覆盖 terminal-check/session-removal 竞态 / Cover the terminal-check/session-removal race with explicit synchronization."""

        bot = _BlockingTerminalBot()
        projection = TelegramAssistantStreamProjection(
            bot,
            native_drafts_enabled=True,
            draft_interval_seconds=0.0,
            typing_refresh_seconds=0.05,
        )
        old = _state()
        await _project(projection, old.current_frame)
        await asyncio.wait_for(bot.typing_event.wait(), timeout=1.0)
        await _project(projection, old.append("旧答案", emitted_at=datetime.now(UTC)))
        await asyncio.wait_for(bot.draft_event.wait(), timeout=1.0)

        bot.block_next_draft = True
        await _project(projection, old.complete(emitted_at=datetime.now(UTC)))
        await asyncio.wait_for(bot.terminal_entered.wait(), timeout=1.0)

        new = AssistantStreamState.begin(
            turn_id=old.turn_id,
            generation=2,
            revision=1,
            revised=True,
            emitted_at=datetime.now(UTC),
        )
        await _project(projection, new.current_frame)
        await _project(projection, new.append("新答案", emitted_at=datetime.now(UTC)))
        await _project(projection, new.complete(emitted_at=datetime.now(UTC)))
        bot.release_terminal.set()

        async with asyncio.timeout(1.0):
            while (
                not bot.draft_calls
                or bot.draft_calls[-1][2] != "最后的回答整理好了，正在接着发给你～"
            ):
                await asyncio.sleep(0)

        assert bot.draft_calls[-1][2] == "最后的回答整理好了，正在接着发给你～"
        assert len({call[1] for call in bot.draft_calls}) == 1
        await projection.aclose()

    asyncio.run(scenario())


def test_terminal_high_water_rejects_an_old_generation_after_actor_exit() -> None:
    """@brief 新 generation actor 退出后 high-water 仍拒绝旧 SUSPENDED /
    The high-water rejects an old SUSPENDED frame after the newer actor has exited.
    """

    async def scenario() -> None:
        """@brief 先完成 generation 2，再投影 generation 1 迟到帧 / Complete generation 2 before projecting late generation 1 frames."""

        bot = _RecordingBot()
        projection = TelegramAssistantStreamProjection(
            bot,
            native_drafts_enabled=True,
            draft_interval_seconds=0.0,
            typing_refresh_seconds=0.02,
            terminal_cursor_capacity=4,
            terminal_cursor_ttl_seconds=60.0,
        )
        old = _state()
        stale_delta = old.append("旧答案", emitted_at=datetime.now(UTC))
        stale_suspended = old.suspend(emitted_at=datetime.now(UTC))
        new = AssistantStreamState.begin(
            turn_id=old.turn_id,
            generation=2,
            revision=0,
            emitted_at=datetime.now(UTC),
        )
        await _project(projection, new.current_frame)
        await _project(projection, new.append("新答案", emitted_at=datetime.now(UTC)))
        await _project(projection, new.complete(emitted_at=datetime.now(UTC)))

        async with asyncio.timeout(1.0):
            while (
                not bot.draft_calls
                or bot.draft_calls[-1][2] != "最后的回答整理好了，正在接着发给你～"
            ):
                await asyncio.sleep(0)
        await asyncio.sleep(0)
        draft_count = len(bot.draft_calls)
        typing_count = len(bot.typing_calls)

        await _project(projection, stale_delta)
        await _project(projection, stale_suspended)
        await asyncio.sleep(0.03)

        assert len(bot.draft_calls) == draft_count
        assert len(bot.typing_calls) == typing_count
        assert bot.draft_calls[-1][2] == "最后的回答整理好了，正在接着发给你～"
        await projection.aclose()

    asyncio.run(scenario())


def test_failed_stream_exposes_only_the_stable_safe_code_and_stops_typing() -> None:
    """@brief 失败预览仅暴露稳定安全码并停止 typing / A failed preview exposes only its stable safe code and stops typing."""

    async def scenario() -> None:
        """@brief 执行失败终态 / Exercise the failed terminal state.

        @return None / None.
        """

        bot = _RecordingBot()
        projection = TelegramAssistantStreamProjection(
            bot,
            native_drafts_enabled=True,
            typing_refresh_seconds=0.01,
        )
        state = _state()
        await _project(projection, state.current_frame)
        await _project(
            projection, state.fail("provider_unavailable", emitted_at=datetime.now(UTC))
        )
        await asyncio.wait_for(bot.draft_event.wait(), timeout=1.0)
        await asyncio.sleep(0.02)

        assert bot.draft_calls[-1][2] == (
            "呜，这次没能顺利做完（错误码：provider_unavailable）。你可以再叫我试一次。"
        )
        typing_count = len(bot.typing_calls)
        await asyncio.sleep(0.02)
        assert len(bot.typing_calls) == typing_count
        await projection.aclose()

    asyncio.run(scenario())


def test_transient_draft_failure_retries_latest_snapshot_without_a_new_model_frame() -> (
    None
):
    """@brief 网络抖动后 actor 自行重试最新快照且不要求新模型 delta /
    The actor retries its latest snapshot after network jitter without requiring a new model delta.
    """

    async def scenario() -> None:
        """@brief 只投影 STARTED 并等待独立展示 actor 恢复 / Project only STARTED and wait for the independent presentation actor to recover."""

        bot = _TransientDraftBot()
        projection = TelegramAssistantStreamProjection(
            bot,
            native_drafts_enabled=True,
            draft_interval_seconds=0.0,
            typing_refresh_seconds=0.05,
        )
        state = _state()
        await _project(projection, state.current_frame)

        async with asyncio.timeout(1.5):
            while len(bot.draft_calls) < 2:
                await asyncio.sleep(0.01)

        assert bot.draft_calls[0][2] == bot.draft_calls[1][2]
        assert bot.draft_calls[-1][2] == ("我在处理这件事，稳定进展会一条条发在下面～")
        await projection.aclose()

    asyncio.run(scenario())


def test_current_action_draft_is_fixed_height_deduplicated_and_hides_answer_tokens() -> (
    None
):
    """@brief 当前动作草稿定高去重且不累计历史或答案 token /
    The current-action draft is fixed-height, deduplicated, and excludes history and answer tokens.
    """

    async def scenario() -> None:
        """@brief 投影 commentary、工具开始与完成 / Project commentary, tool start, and completion."""

        bot = _RecordingBot()
        projection = TelegramAssistantStreamProjection(
            bot,
            native_drafts_enabled=True,
            draft_interval_seconds=0.0,
            typing_refresh_seconds=0.05,
        )

        async def wait_for_text(text: str) -> None:
            """@brief 等待指定草稿被确认 / Wait for the specified draft.

            @param text 目标草稿文本 / Target draft text.
            @return None / None.
            """

            async with asyncio.timeout(1.0):
                while not bot.draft_calls or bot.draft_calls[-1][2] != text:
                    await asyncio.sleep(0)

        state = _state()
        await _project(projection, state.current_frame)
        await wait_for_text("我在处理这件事，稳定进展会一条条发在下面～")
        await _project(
            projection,
            state.commentary(
                "step:0:commentary",
                "我先查一下最新资料，再回来给你一个完整答案。",
                emitted_at=datetime.now(UTC),
            ),
        )
        await wait_for_text("✓ 工作说明已经稳定记下，继续往下做～")
        await _project(
            projection,
            state.start_tool(
                "step:0:call:0",
                "google_search",
                emitted_at=datetime.now(UTC),
            ),
        )
        await wait_for_text("✦ 我去网上查查最新资料…\n  能力：google_search")
        await _project(
            projection,
            state.append("不应该出现在状态卡片里的答案", emitted_at=datetime.now(UTC)),
        )
        await _project(
            projection,
            state.finish_tool(
                "step:0:call:0",
                "google_search",
                succeeded=True,
                emitted_at=datetime.now(UTC),
            ),
        )
        await wait_for_text("✓ 这个步骤已经稳定记录，继续处理下一步～")
        before_duplicate = len(bot.draft_calls)
        await _project(
            projection,
            state.finish_tool(
                "step:0:call:0",
                "google_search",
                succeeded=True,
                emitted_at=datetime.now(UTC),
            ),
        )
        await asyncio.sleep(0.01)
        assert len(bot.draft_calls) == before_duplicate
        await _project(projection, state.complete(emitted_at=datetime.now(UTC)))

        await wait_for_text("最后的回答整理好了，正在接着发给你～")

        draft_texts = [str(call[2]) for call in bot.draft_calls]
        assert "✦ 我去网上查查最新资料…\n  能力：google_search" in draft_texts
        assert draft_texts.count("✓ 这个步骤已经稳定记录，继续处理下一步～") == 1
        assert all(
            "我先查一下最新资料" not in text
            and "不应该出现在状态卡片里的答案" not in text
            and "google_search\n✓" not in text
            for text in draft_texts
        )
        await projection.aclose()

    asyncio.run(scenario())
