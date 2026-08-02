"""@brief Telegram Assistant 流草稿与 typing 投影 / Telegram Assistant draft-stream and typing projection.

该 adapter 把 provider-neutral 当前动作映射为 Telegram 的易失 UX：默认只刷新
``typing``；显式 opt-in 时私聊才使用 ``sendMessageDraft``。答案 token delta 在进入
mailbox 前被丢弃；每 Turn 容量一 mailbox 只保留最新活动快照，并独立重试瞬时网络失败，
因此慢 Telegram 网络不会反向阻塞 LLM SSE 或工具执行。已完成过程项、最终消息和失败说明
由 transactional outbox 追加发布。/ This adapter maps the provider-neutral current action to ephemeral
Telegram UX: every active chat refreshes ``typing`` while private-chat ``sendMessageDraft`` is
strictly opt-in.
Answer-token deltas are dropped before the mailbox. A capacity-one mailbox per Turn retains only
the newest activity snapshot and independently retries transient network failures, so slow Telegram
networking never backpressures LLM SSE or tool execution. The transactional outbox append-publishes
completed progress items, final messages, and durable failure explanations.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Protocol

from telegram.constants import ChatAction
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

from fogmoe_bot.application.assistant.streaming import AssistantStreamTarget
from fogmoe_bot.domain.assistant.streaming import (
    AssistantActivityKind,
    AssistantActivityStatus,
    AssistantStreamActivity,
    AssistantStreamFrame,
    AssistantStreamKind,
)
from fogmoe_bot.domain.conversation.identity import TurnId

logger = logging.getLogger(__name__)
"""@brief Telegram 流投影诊断日志器 / Diagnostic logger for Telegram stream projection."""

_TELEGRAM_TEXT_LIMIT = 4096
"""@brief Telegram 草稿正文上限 / Telegram draft-text limit."""

_DEFAULT_DRAFT_INTERVAL_SECONDS = 0.35
"""@brief 同一 Turn 草稿写入的默认最小间隔 / Default minimum interval between draft writes for one Turn."""

_DEFAULT_TYPING_REFRESH_SECONDS = 4.0
"""@brief 小于 Telegram 五秒有效期的 typing 刷新周期 / Typing refresh cadence below Telegram's five-second lifetime."""

_DRAFT_RETRY_BASE_SECONDS = 0.5
"""@brief 草稿瞬时失败的指数退避基数 / Exponential-backoff base for transient draft failures."""

_DRAFT_RETRY_MAX_SECONDS = 8.0
"""@brief 草稿瞬时失败的最大退避 / Maximum backoff for transient draft failures."""

_DEFAULT_TERMINAL_CURSOR_CAPACITY = 8_192
"""@brief 迟到帧 high-water tombstone 的默认有界容量 / Default bounded capacity for late-frame high-water tombstones."""

_DEFAULT_TERMINAL_CURSOR_TTL_SECONDS = 1_200.0
"""@brief 覆盖默认 inference lease 的 tombstone 保留期 / Tombstone retention covering the default inference lease."""

_TELEGRAM_DRAFT_ID_MAX = 2_147_483_647
"""@brief Telegram draft ID 的正 Int32 上界 / Positive Int32 ceiling for Telegram draft IDs."""

_REVISED_PREVIEW = "收到啦，我按你的新想法重新整理～"
"""@brief steer 后新活动快照到达前的私聊预览 / Private-chat preview shown after a steer until the next activity snapshot."""

_RETRY_PREVIEW = "刚才有点卡住了，不过没关系，我接着来…"
"""@brief durable retry generation 启动后的私聊预览 / Private-chat preview after a durable retry generation starts."""

_SUSPENDED_PREVIEW = "唔，刚才暂时卡住了…我会自己接着处理的。"
"""@brief generation 暂停后的私聊恢复提示 / Private-chat recovery preview after a generation is suspended."""

_FAILED_PREVIEW = "呜，这次没能顺利做完（错误码：{code}）。你可以再叫我试一次。"
"""@brief durable 失败说明送达前的易失预览 / Ephemeral preview before the durable failure explanation arrives."""

_WORKING_PREVIEW = "我在处理这件事，稳定进展会一条条发在下面～"
"""@brief 首个稳定 Agent item 前的固定高度草稿 / Fixed-height draft before the first stable Agent item."""

_COMPLETED_PREVIEW = "最后的回答整理好了，正在接着发给你～"
"""@brief durable 最终回答送达前的固定高度草稿 / Fixed-height draft before the durable final answer arrives."""

_TOOL_ACTIVE_COPY: dict[str, str] = {
    "get_help_text": "我去看看现在能做些什么…",
    "get_current_time": "我确认一下现在的时间…",
    "list_available_stickers": "我去翻翻贴纸包…",
    "send_sticker": "我在挑合适的贴纸…",
    "google_search": "我去网上查查最新资料…",
    "fetch_url": "我在认真读这个页面…",
    "fetch_group_context": "我先看看前面的聊天线索…",
    "run_bash": "我在工作区里动手验证…",
    "generate_image": "我开始准备这张图啦…",
    "generate_voice": "我开始准备这段声音啦…",
    "search_memory": "我去回忆里找找相关线索…",
    "search_memory_by_time": "我按时间翻翻以前的记录…",
    "schedule_ai_message": "我在认真安排这件事…",
    "user_diary": "我去看看小日记…",
}
"""@brief 工具稳定名称到当前动作短文案的映射 / Mapping from stable tool names to short current-action copy."""

type _FrameCursor = tuple[int, int, int]
"""@brief ``revision, generation, sequence`` 单调游标 / Monotonic revision-generation-sequence cursor."""


@dataclass(frozen=True, slots=True)
class _TerminalCursor:
    """@brief 已退出 Turn actor 的有界 high-water tombstone / Bounded high-water tombstone for an exited Turn actor.

    @param cursor 已接受的最高流游标 / Highest accepted stream cursor.
    @param expires_at 单调时钟失效点 / Monotonic expiration instant.
    """

    cursor: _FrameCursor
    expires_at: float


class TelegramStreamBot(Protocol):
    """@brief 流投影使用的最小 Telegram Bot 端口 / Minimal Telegram Bot port used by stream projection."""

    async def send_chat_action(
        self,
        chat_id: int | str,
        action: str,
        message_thread_id: int | None = None,
    ) -> bool:
        """@brief 发送一个短生命周期 chat action / Send a short-lived chat action.

        @param chat_id Telegram chat 目标 / Telegram chat target.
        @param action Telegram action 名称 / Telegram action name.
        @param message_thread_id 可选 Topic / Optional topic.
        @return Telegram 是否确认请求 / Whether Telegram acknowledged the request.
        """

        ...

    async def send_message_draft(
        self,
        chat_id: int,
        draft_id: int,
        text: str | None = None,
        message_thread_id: int | None = None,
    ) -> bool:
        """@brief 更新一条私聊原生草稿 / Update one native private-chat draft.

        @param chat_id 私聊数值 ID / Numeric private-chat ID.
        @param draft_id 同 Turn 稳定正整数 / Stable positive integer for the Turn.
        @param text 当前累计预览 / Current cumulative preview.
        @param message_thread_id 始终为 None 的私聊 Topic / Private-chat topic, always None.
        @return Telegram 是否确认请求 / Whether Telegram acknowledged the request.
        """

        ...


@dataclass(slots=True)
class _ProjectionSession:
    """@brief 一个 Turn 的有界 Telegram 投影 actor 状态 / Bounded Telegram projection actor state for one Turn.

    @param turn_id durable Turn identity / Durable Turn identity.
    @param target 不随帧变化的 Telegram 目标 / Telegram target that does not vary between frames.
    @param mailbox 仅保留最新帧的容量一 mailbox / Capacity-one mailbox retaining only the latest frame.
    @param accepted_cursor 已接受的最新单调游标 / Latest accepted monotonic cursor.
    @param task actor 任务 / Actor task.
    @param typing_task typing 刷新任务 / Typing refresh task.
    @param latest_frame 当前最新地址与生命周期帧 / Latest address and lifecycle frame.
    @param drafts_enabled 当前 session 是否仍尝试原生草稿 / Whether native drafts remain enabled for this session.
    @param typing_error_logged 是否已压制连续 typing 错误 / Whether repeated typing errors are currently suppressed.
    @param next_draft_at 下一次允许写草稿的单调时刻 / Next monotonic instant at which a draft may be written.
    @param next_typing_at 下一次允许刷新 typing 的单调时刻 /
        Next monotonic instant at which typing may be refreshed.
    @param draft_retry_attempts 当前连续草稿瞬时失败数 / Current consecutive transient draft failures.
    @param last_draft_text 最近一次 Telegram 已确认的草稿文本 / Most recent draft text acknowledged by Telegram.
    """

    turn_id: TurnId
    target: AssistantStreamTarget
    mailbox: asyncio.Queue[AssistantStreamFrame] = field(
        default_factory=lambda: asyncio.Queue(maxsize=1)
    )
    accepted_cursor: _FrameCursor = (-1, -1, -1)
    task: asyncio.Task[None] | None = None
    typing_task: asyncio.Task[None] | None = None
    latest_frame: AssistantStreamFrame | None = None
    drafts_enabled: bool = False
    typing_error_logged: bool = False
    next_draft_at: float = 0.0
    next_typing_at: float = 0.0
    draft_retry_attempts: int = 0
    last_draft_text: str | None = None


class TelegramAssistantStreamProjection:
    """@brief 合并流帧并维护 Telegram typing 的最佳努力 adapter / Best-effort adapter coalescing stream frames and maintaining Telegram typing."""

    def __init__(
        self,
        bot: TelegramStreamBot,
        *,
        native_drafts_enabled: bool = False,
        draft_interval_seconds: float = _DEFAULT_DRAFT_INTERVAL_SECONDS,
        typing_refresh_seconds: float = _DEFAULT_TYPING_REFRESH_SECONDS,
        terminal_cursor_capacity: int = _DEFAULT_TERMINAL_CURSOR_CAPACITY,
        terminal_cursor_ttl_seconds: float = _DEFAULT_TERMINAL_CURSOR_TTL_SECONDS,
    ) -> None:
        """@brief 注入 Telegram 端口与 UX 速率边界 / Inject the Telegram port and UX rate bounds.

        @param bot Telegram 窄端口 / Narrow Telegram port.
        @param native_drafts_enabled 是否显式启用会触发客户端重绘的原生草稿 /
            Whether to explicitly enable native drafts that may trigger client-side redraw.
        @param draft_interval_seconds 同 Turn 两次草稿写入的最小间隔 /
            Minimum interval between draft writes for one Turn.
        @param typing_refresh_seconds typing 刷新周期，必须不超过五秒 /
            Typing refresh cadence, which must not exceed five seconds.
        @param terminal_cursor_capacity 最近终态 Turn 的最大 high-water 数 /
            Maximum high-water entries retained for recently terminal Turns.
        @param terminal_cursor_ttl_seconds high-water 至少覆盖旧 worker lease 的保留秒数 /
            High-water retention in seconds, which should cover an old worker lease.
        @raise ValueError 速率参数非有限或越界时抛出 /
            Raised when cadence values are non-finite or out of range.
        """

        if not isinstance(native_drafts_enabled, bool):
            raise TypeError("native_drafts_enabled must be a boolean")
        if (
            isinstance(draft_interval_seconds, bool)
            or not math.isfinite(draft_interval_seconds)
            or draft_interval_seconds < 0.0
        ):
            raise ValueError("draft_interval_seconds must be finite and non-negative")
        if (
            isinstance(typing_refresh_seconds, bool)
            or not math.isfinite(typing_refresh_seconds)
            or not 0.0 < typing_refresh_seconds <= 5.0
        ):
            raise ValueError(
                "typing_refresh_seconds must be finite and in the interval (0, 5]"
            )
        if (
            isinstance(terminal_cursor_capacity, bool)
            or not isinstance(terminal_cursor_capacity, int)
            or terminal_cursor_capacity < 1
        ):
            raise ValueError("terminal_cursor_capacity must be a positive integer")
        if (
            isinstance(terminal_cursor_ttl_seconds, bool)
            or not math.isfinite(terminal_cursor_ttl_seconds)
            or terminal_cursor_ttl_seconds <= 0.0
        ):
            raise ValueError("terminal_cursor_ttl_seconds must be finite and positive")
        self._bot = bot
        """@brief 共享 Telegram Bot 端口 / Shared Telegram Bot port."""
        self._native_drafts_enabled = native_drafts_enabled
        """@brief 是否显式启用原生草稿重绘 / Whether native draft redraw is explicitly enabled."""
        self._draft_interval_seconds = draft_interval_seconds
        """@brief 草稿节流周期 / Draft-throttling interval."""
        self._typing_refresh_seconds = typing_refresh_seconds
        """@brief typing 刷新周期 / Typing refresh cadence."""
        self._terminal_cursor_capacity = terminal_cursor_capacity
        """@brief high-water tombstone 有界容量 / Bounded high-water tombstone capacity."""
        self._terminal_cursor_ttl_seconds = terminal_cursor_ttl_seconds
        """@brief high-water tombstone TTL / High-water tombstone TTL."""
        self._sessions: dict[TurnId, _ProjectionSession] = {}
        """@brief 活跃 Turn 到有界 actor 的映射 / Active-Turn to bounded-actor mapping."""
        self._terminal_cursors: OrderedDict[TurnId, _TerminalCursor] = OrderedDict()
        """@brief 最近退出 actor 的 LRU+TTL high-water / LRU+TTL high-water for recently exited actors."""
        self._lock = asyncio.Lock()
        """@brief session 建立、替换与关闭互斥锁 / Mutex for session creation, replacement, and shutdown."""
        self._closed = False
        """@brief adapter 是否已进入终止态 / Whether the adapter has entered its terminal state."""

    async def run(self, stop_event: asyncio.Event) -> None:
        """@brief 让顶层 runtime 拥有所有易失投影任务 / Let the top-level runtime own all ephemeral projection tasks.

        @param stop_event runtime 停止信号 / Runtime stop signal.
        @return None / None.
        """

        await stop_event.wait()
        await self.aclose()

    async def project(
        self,
        target: AssistantStreamTarget,
        frame: AssistantStreamFrame,
    ) -> None:
        """@brief 非阻塞接收最新累计帧 / Accept the newest cumulative frame without network backpressure.

        @param target 已验证且与 Turn 绑定的 Telegram 目标 / Validated Telegram target bound to the Turn.
        @param frame 已验证的 Assistant 流帧 / Validated Assistant stream frame.
        @return None / None.
        @note DELTA 不进入 actor；容量一 mailbox 满时会原子替换旧活动快照。每帧都含
            完整活动列表，因此合并不会丢失已稳定的信息块。/ DELTA frames never enter the
            actor. When the capacity-one mailbox is full, its older activity snapshot is atomically
            replaced. Every frame contains the complete activity list, so coalescing loses no
            stable information block.
        """

        if frame.kind is AssistantStreamKind.DELTA:
            return
        cursor = _frame_cursor(frame)
        async with self._lock:
            if self._closed:
                return
            now = asyncio.get_running_loop().time()
            self._evict_expired_terminal_cursors(now)
            session = self._sessions.get(frame.turn_id)
            if session is None:
                terminal = self._terminal_cursors.get(frame.turn_id)
                if terminal is not None and cursor <= terminal.cursor:
                    return
                self._terminal_cursors.pop(frame.turn_id, None)
                session = _ProjectionSession(
                    frame.turn_id,
                    target,
                    drafts_enabled=self._native_drafts_enabled,
                )
                self._sessions[frame.turn_id] = session
            elif session.target != target:
                raise ValueError("Assistant stream target changed within one Turn")
            if cursor <= session.accepted_cursor:
                return
            session.accepted_cursor = cursor
            session.latest_frame = frame
            session.draft_retry_attempts = 0
            if frame.kind is AssistantStreamKind.REVISED:
                session.next_draft_at = 0.0
            _replace_mailbox_frame(session.mailbox, frame)
            if session.task is None:
                session.task = asyncio.create_task(
                    self._run_session(session),
                    name=(
                        "telegram-assistant-stream-"
                        f"{_stable_telegram_draft_id(frame.turn_id)}"
                    ),
                )

    async def aclose(self) -> None:
        """@brief 幂等取消 typing 与尚未完成的易失投影 / Idempotently cancel typing and unfinished ephemeral projections.

        @return None / None.
        @note durable outbox 不属于本 adapter，因此关闭不会删除、确认或改写任何业务数据。/
            The durable outbox is outside this adapter; shutdown never deletes, acknowledges, or
            mutates business data.
        """

        async with self._lock:
            if self._closed:
                return
            self._closed = True
            sessions = tuple(self._sessions.values())
            self._sessions.clear()
            self._terminal_cursors.clear()
        tasks = tuple(
            task
            for session in sessions
            for task in (session.task, session.typing_task)
            if task is not None
        )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_session(self, session: _ProjectionSession) -> None:
        """@brief 顺序消费一个 Turn 的合并帧 / Sequentially consume coalesced frames for one Turn.

        @param session 当前 Turn actor / Current Turn actor.
        @return None / None.
        """

        try:
            while True:
                frame = await session.mailbox.get()
                session.latest_frame = frame
                if session.typing_task is None:
                    await self._send_typing(session, frame)
                    session.typing_task = asyncio.create_task(
                        self._refresh_typing(session),
                        name=(
                            "telegram-assistant-typing-"
                            f"{_stable_telegram_draft_id(frame.turn_id)}"
                        ),
                    )
                if frame.kind not in {
                    AssistantStreamKind.SUSPENDED,
                    AssistantStreamKind.COMPLETED,
                    AssistantStreamKind.FAILED,
                }:
                    frame = await self._coalesce_until_draft_due(session, frame)
                    session.latest_frame = frame
                await self._project_frame(session, frame)
                if frame.kind is AssistantStreamKind.SUSPENDED:
                    typing_task = session.typing_task
                    session.typing_task = None
                    if typing_task is not None:
                        typing_task.cancel()
                        await asyncio.gather(
                            typing_task,
                            return_exceptions=True,
                        )
                    if await self._finish_if_current(session, frame):
                        return
                    continue
                if frame.kind in {
                    AssistantStreamKind.COMPLETED,
                    AssistantStreamKind.FAILED,
                }:
                    if await self._finish_if_current(session, frame):
                        return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Telegram Assistant stream actor failed turn_id=%s",
                session.turn_id,
            )
        finally:
            final_typing_task = session.typing_task
            if final_typing_task is not None:
                final_typing_task.cancel()
                await asyncio.gather(final_typing_task, return_exceptions=True)
            await self._forget_session(session)

    async def _coalesce_until_draft_due(
        self,
        session: _ProjectionSession,
        frame: AssistantStreamFrame,
    ) -> AssistantStreamFrame:
        """@brief 在节流窗口内继续折叠累计活动快照 / Continue folding cumulative activity snapshots inside the throttle window.

        @param session 当前 Turn actor / Current Turn actor.
        @param frame 当前候选帧 / Current candidate frame.
        @return 到期时最新帧，或无需等待的终态/修订帧 /
            Latest frame at the deadline, or an immediately deliverable terminal/revised frame.
        """

        loop = asyncio.get_running_loop()
        candidate = frame
        while True:
            remaining = session.next_draft_at - loop.time()
            if remaining <= 0.0:
                return candidate
            try:
                candidate = await asyncio.wait_for(
                    session.mailbox.get(),
                    timeout=remaining,
                )
            except TimeoutError:
                return candidate
            if candidate.kind in {
                AssistantStreamKind.SUSPENDED,
                AssistantStreamKind.COMPLETED,
                AssistantStreamKind.FAILED,
            }:
                return candidate

    async def _project_frame(
        self,
        session: _ProjectionSession,
        frame: AssistantStreamFrame,
    ) -> None:
        """@brief 把一个已合并帧映射为 Telegram 易失 UX / Map one coalesced frame to ephemeral Telegram UX.

        @param session 当前 Turn actor / Current Turn actor.
        @param frame 待投影帧 / Frame to project.
        @return None / None.
        """

        if frame.kind is AssistantStreamKind.STARTED:
            text = _RETRY_PREVIEW if frame.generation > 1 else _WORKING_PREVIEW
        elif frame.kind is AssistantStreamKind.SUSPENDED:
            text = _SUSPENDED_PREVIEW
        elif frame.kind is AssistantStreamKind.COMPLETED:
            text = _COMPLETED_PREVIEW
        elif frame.kind is AssistantStreamKind.REVISED:
            text = _REVISED_PREVIEW
            await self._send_typing(session, frame)
        elif frame.kind is AssistantStreamKind.FAILED:
            code = frame.safe_error_code
            if code is None:
                raise RuntimeError("Validated failed frame lost its safe error code")
            text = _FAILED_PREVIEW.format(code=code)
        else:
            text = _render_current_activity_draft(frame)

        target = session.target
        if target.is_group or not isinstance(target.chat_id, int):
            return
        if text is None or not session.drafts_enabled:
            return
        bounded_text = _bounded_draft_text(text)
        if bounded_text == session.last_draft_text:
            return
        try:
            acknowledged = await self._bot.send_message_draft(
                chat_id=target.chat_id,
                draft_id=_stable_telegram_draft_id(frame.turn_id),
                text=bounded_text,
                message_thread_id=None,
            )
            if not acknowledged:
                raise TelegramError("Telegram did not acknowledge sendMessageDraft")
        except RetryAfter as error:
            session.next_draft_at = asyncio.get_running_loop().time() + _retry_seconds(
                error
            )
            self._retry_latest_nonterminal_frame(session, frame)
            logger.info(
                "Telegram Assistant draft rate limited turn_id=%s",
                frame.turn_id,
            )
            return
        except BadRequest, Forbidden:
            session.drafts_enabled = False
            logger.warning(
                "Telegram Assistant native drafts disabled turn_id=%s",
                frame.turn_id,
                exc_info=True,
            )
            return
        except TelegramError:
            loop = asyncio.get_running_loop()
            if session.latest_frame is frame:
                session.draft_retry_attempts += 1
                retry_delay = min(
                    _DRAFT_RETRY_MAX_SECONDS,
                    _DRAFT_RETRY_BASE_SECONDS
                    * (2 ** min(session.draft_retry_attempts - 1, 8)),
                )
                session.next_draft_at = loop.time() + retry_delay
            else:
                session.draft_retry_attempts = 0
                session.next_draft_at = loop.time()
            self._retry_latest_nonterminal_frame(session, frame)
            logger.warning(
                "Telegram Assistant draft projection failed turn_id=%s",
                frame.turn_id,
                exc_info=True,
            )
            return
        session.draft_retry_attempts = 0
        session.last_draft_text = bounded_text
        session.next_draft_at = (
            asyncio.get_running_loop().time() + self._draft_interval_seconds
        )

    @staticmethod
    def _retry_latest_nonterminal_frame(
        session: _ProjectionSession,
        frame: AssistantStreamFrame,
    ) -> None:
        """@brief 仅重排仍为最新的非终态快照 / Requeue only a still-latest non-terminal snapshot.

        @param session 当前 Turn actor / Current Turn actor.
        @param frame 刚刚投影失败的累计帧 / Cumulative frame whose projection just failed.
        @return None / None.
        @note 重试只进入容量一 mailbox；新帧已经到达时绝不以旧帧覆盖，也不会向模型
            SSE 或工具执行施加背压。/ Retries enter only the capacity-one mailbox. A newer
            frame is never overwritten by an old one, and no backpressure reaches model SSE or
            tool execution.
        """

        if frame.kind in {
            AssistantStreamKind.SUSPENDED,
            AssistantStreamKind.COMPLETED,
            AssistantStreamKind.FAILED,
        }:
            return
        if session.latest_frame is frame and session.mailbox.empty():
            session.mailbox.put_nowait(frame)

    async def _refresh_typing(self, session: _ProjectionSession) -> None:
        """@brief 在流终结前刷新 Telegram typing / Refresh Telegram typing until the stream terminates.

        @param session 当前 Turn actor / Current Turn actor.
        @return None / None.
        """

        try:
            while True:
                await asyncio.sleep(self._typing_refresh_seconds)
                frame = session.latest_frame
                if frame is None or frame.kind in {
                    AssistantStreamKind.SUSPENDED,
                    AssistantStreamKind.COMPLETED,
                    AssistantStreamKind.FAILED,
                }:
                    return
                await self._send_typing(session, frame)
        except asyncio.CancelledError:
            raise

    async def _send_typing(
        self,
        session: _ProjectionSession,
        frame: AssistantStreamFrame,
    ) -> None:
        """@brief 最佳努力发送一次 typing / Best-effort send one typing action.

        @param session 当前 Turn actor / Current Turn actor.
        @param frame 提供日志身份的最新帧 / Latest frame providing log identity.
        @return None / None.
        """

        loop = asyncio.get_running_loop()
        if loop.time() < session.next_typing_at:
            return
        try:
            target = session.target
            acknowledged = await self._bot.send_chat_action(
                chat_id=target.chat_id,
                action=ChatAction.TYPING,
                message_thread_id=target.message_thread_id,
            )
            if not acknowledged:
                raise TelegramError("Telegram did not acknowledge sendChatAction")
        except RetryAfter as error:
            session.next_typing_at = loop.time() + _retry_seconds(error)
            if not session.typing_error_logged:
                logger.info(
                    "Telegram Assistant typing rate limited turn_id=%s",
                    frame.turn_id,
                )
                session.typing_error_logged = True
        except TelegramError:
            session.next_typing_at = loop.time() + self._typing_refresh_seconds
            if not session.typing_error_logged:
                logger.warning(
                    "Telegram Assistant typing projection failed turn_id=%s",
                    frame.turn_id,
                    exc_info=True,
                )
                session.typing_error_logged = True
        else:
            session.next_typing_at = loop.time() + self._typing_refresh_seconds
            session.typing_error_logged = False

    async def _forget_session(self, session: _ProjectionSession) -> None:
        """@brief 仅移除仍指向自己的 session / Remove a session only while the mapping still points to it.

        @param session 已终结或失败的 actor / Terminal or failed actor.
        @return None / None.
        """

        async with self._lock:
            if self._sessions.get(session.turn_id) is session:
                self._sessions.pop(session.turn_id)
                self._remember_terminal_cursor(session)

    async def _finish_if_current(
        self,
        session: _ProjectionSession,
        frame: AssistantStreamFrame,
    ) -> bool:
        """@brief 仅在没有更高 generation 已入队时原子终结 actor /
        Atomically finish an actor only when no newer generation is already queued.

        @param session 当前 Turn actor / Current Turn actor.
        @param frame 刚投影的 suspended/completed/failed 帧 /
            Suspended, completed, or failed frame just projected.
        @return 当前帧仍为最新终态时为 True；已有新 generation 时为 False /
            True when this frame remains the latest terminal state; false when a newer generation
            has already arrived.
        @note durable claim 可能在 provider 返回后、最终提交前被 steer；此锁把
            ``terminal check + session removal`` 与 ``project`` 的 session lookup 串行化，
            避免旧终态 actor 吞掉已入队的 REVISED/STARTED。/ A durable claim may be steered
            after its provider returns but before final commit. This lock serializes the terminal
            check and session removal with ``project`` session lookup, so an old terminal actor
            cannot swallow an already queued REVISED or STARTED frame.
        """

        cursor = _frame_cursor(frame)
        async with self._lock:
            if session.accepted_cursor > cursor:
                return False
            if self._sessions.get(session.turn_id) is session:
                self._sessions.pop(session.turn_id)
                self._remember_terminal_cursor(session)
            return True

    def _remember_terminal_cursor(self, session: _ProjectionSession) -> None:
        """@brief 在持锁状态记录 actor 的 high-water / Record an actor high-water while holding the lock.

        @param session 即将退出的 Turn actor / Turn actor about to exit.
        @return None / None.
        """

        now = asyncio.get_running_loop().time()
        existing = self._terminal_cursors.get(session.turn_id)
        cursor = (
            session.accepted_cursor
            if existing is None
            else max(existing.cursor, session.accepted_cursor)
        )
        self._terminal_cursors[session.turn_id] = _TerminalCursor(
            cursor=cursor,
            expires_at=now + self._terminal_cursor_ttl_seconds,
        )
        self._terminal_cursors.move_to_end(session.turn_id)
        while len(self._terminal_cursors) > self._terminal_cursor_capacity:
            self._terminal_cursors.popitem(last=False)

    def _evict_expired_terminal_cursors(self, now: float) -> None:
        """@brief 惰性删除已过 TTL 的 high-water / Lazily evict expired high-water entries.

        @param now 当前单调时刻 / Current monotonic instant.
        @return None / None.
        """

        while self._terminal_cursors:
            terminal = next(iter(self._terminal_cursors.values()))
            if terminal.expires_at > now:
                return
            self._terminal_cursors.popitem(last=False)


def _frame_cursor(frame: AssistantStreamFrame) -> _FrameCursor:
    """@brief 提取 revision 优先的单调游标 / Extract a revision-first monotonic cursor.

    @param frame Assistant 流帧 / Assistant stream frame.
    @return ``revision, generation, sequence`` / ``revision, generation, sequence``.
    """

    return (frame.revision, frame.generation, frame.sequence)


def _stable_telegram_draft_id(turn_id: TurnId) -> int:
    """@brief 从 Turn UUID 派生稳定非零 Int32 draft ID / Derive a stable non-zero Int32 draft ID from a Turn UUID.

    @param turn_id durable Turn / Durable Turn.
    @return ``1..2^31-1`` 内稳定 Telegram ID / Stable Telegram ID in ``1..2^31-1``.
    """

    return (turn_id.value.int % _TELEGRAM_DRAFT_ID_MAX) + 1


def _replace_mailbox_frame(
    mailbox: asyncio.Queue[AssistantStreamFrame],
    frame: AssistantStreamFrame,
) -> None:
    """@brief 用最新累计帧替换容量一 mailbox / Replace a capacity-one mailbox with the newest cumulative frame.

    @param mailbox 当前 Turn mailbox / Current Turn mailbox.
    @param frame 最新帧 / Newest frame.
    @return None / None.
    """

    if mailbox.full():
        mailbox.get_nowait()
    mailbox.put_nowait(frame)


def _render_current_activity_draft(frame: AssistantStreamFrame) -> str:
    """@brief 只渲染固定高度的当前动作槽位 / Render only the fixed-height current-action slot.

    @param frame 含最新 item 状态的累计帧 / Cumulative frame containing the latest item state.
    @return 不重绘历史块的短草稿 / Short draft that never redraws historical blocks.
    @note 已完成 commentary 与工具块由 durable outbox 追加为真实消息；草稿不重复累计历史，
        从而避免 Telegram 页面高度反复变化和滚动跳跃。/ Completed commentary and tool blocks
        are appended as real messages by the durable outbox. The draft never repeats cumulative
        history, avoiding repeated page-height changes and scroll jumps.
    """

    if not frame.activities:
        return _WORKING_PREVIEW
    return _render_activity_block(frame.activities[-1])


def _render_activity_block(activity: AssistantStreamActivity) -> str:
    """@brief 渲染一个不泄露内部数据的活动块 / Render one activity block without leaking internal data.

    @param activity 已验证的 Assistant 活动项 / Validated Assistant activity item.
    @return 雾萌角色一致的简短纯文本 / Short FOGMOE-consistent plain text.
    """

    if activity.kind is AssistantActivityKind.TOOL:
        if activity.status is AssistantActivityStatus.ACTIVE:
            active = _TOOL_ACTIVE_COPY.get(
                activity.label,
                "我正在用一个能力帮你处理…",
            )
            return f"✦ {active}"
        if activity.status is AssistantActivityStatus.FAILED:
            return "× 这个能力暂时没处理好，我在收束现场…"
        return "✓ 这个步骤已经稳定记录，继续处理下一步～"
    return "✓ 工作说明已经稳定记下，继续往下做～"


def _bounded_draft_text(text: str) -> str:
    """@brief 形成 Telegram 上限内、非空的预览 / Build a non-empty preview within Telegram's limit.

    @param text 累计或状态文本 / Cumulative or status text.
    @return 最多 4096 字符的文本 / Text of at most 4096 characters.
    """

    if not text:
        raise ValueError("Telegram draft text cannot be empty")
    if len(text) <= _TELEGRAM_TEXT_LIMIT:
        return text
    return text[: _TELEGRAM_TEXT_LIMIT - 1] + "…"


def _retry_seconds(error: RetryAfter) -> float:
    """@brief 规范化 PTB RetryAfter 延迟 / Normalize a PTB RetryAfter delay.

    @param error PTB rate-limit error / PTB rate-limit error.
    @return 至少一毫秒的等待秒数 / Delay of at least one millisecond.
    """

    value = error.retry_after
    seconds = value.total_seconds() if isinstance(value, timedelta) else float(value)
    return max(0.001, seconds)


__all__ = ["TelegramAssistantStreamProjection", "TelegramStreamBot"]
