"""@brief Provider-neutral Assistant 流投影状态机 / Provider-neutral Assistant stream-projection state machine.

流帧是最佳努力的用户体验投影，不是最终投递事实。私聊 adapter 可把同一
``draft_id`` 映射到 Telegram ``sendMessageDraft``；群聊 adapter 可仅维持 typing。
无论投影成功与否，最终消息仍必须由 durable outbox 发送。/
Stream frames are best-effort UX projections, not final-delivery facts. A private-chat adapter
may map one stable ``draft_id`` to Telegram ``sendMessageDraft`` while a group adapter may keep
only a typing action alive. The durable outbox remains the sole publisher of the final message.
"""

from __future__ import annotations

import asyncio
import re
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, Self

from fogmoe_bot.domain.conversation.identity import TurnId
from fogmoe_bot.domain.temporal import ensure_utc

_SAFE_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,99}$")
"""@brief 可进入用户投影的稳定错误代码语法 / Grammar for stable error codes safe for user projections."""

_TELEGRAM_DRAFT_ID_MAX = 2_147_483_647
"""@brief Telegram draft ID 的正 Int32 上界 / Positive Int32 ceiling for Telegram draft IDs."""


class AssistantStreamKind(StrEnum):
    """@brief Assistant 流生命周期事件 / Assistant stream lifecycle event."""

    STARTED = "started"
    DELTA = "delta"
    REVISED = "revised"
    SUSPENDED = "suspended"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AssistantStreamAddress:
    """@brief 一条 Assistant 流的 Telegram 地址 / Telegram address for one Assistant stream.

    @param chat_id Telegram 数值 chat ID 或私聊兼容目标 / Numeric Telegram chat ID or a private-chat compatible target.
    @param is_group 是否属于群聊 / Whether the stream belongs to a group chat.
    @param message_thread_id 可选 Telegram Topic ID / Optional Telegram topic identifier.
    """

    chat_id: int | str
    is_group: bool
    message_thread_id: int | None

    def __post_init__(self) -> None:
        """@brief 校验私聊、群聊与 Topic 闭集 / Validate the private/group/topic sum type.

        @return None / None.
        @raise ValueError 地址组合非法时抛出 / Raised for an invalid address combination.
        """

        if isinstance(self.chat_id, int):
            if isinstance(self.chat_id, bool) or self.chat_id == 0:
                raise ValueError("Assistant stream chat_id cannot be zero")
        elif not self.chat_id.strip():
            raise ValueError("Assistant stream chat_id cannot be blank")
        if self.is_group and not isinstance(self.chat_id, int):
            raise ValueError("Group Assistant streams require an integer chat_id")
        if not self.is_group and self.message_thread_id is not None:
            raise ValueError("Private Assistant streams cannot have a message thread")
        if self.message_thread_id is not None and (
            isinstance(self.message_thread_id, bool) or self.message_thread_id <= 0
        ):
            raise ValueError("Assistant stream message_thread_id must be positive")


@dataclass(frozen=True, slots=True)
class AssistantStreamFrame:
    """@brief 可由 Telegram adapter 投影的一帧累计流状态 / One cumulative stream frame projectable by a Telegram adapter.

    @param turn_id durable Turn identity / Durable Turn identity.
    @param generation provider 尝试序号 / Provider-attempt ordinal.
    @param revision 用户 steer 输入版本 / User-steer input revision.
    @param sequence 当前 generation 内单调帧序号 / Monotonic frame sequence inside the current generation.
    @param draft_id 同一 Turn 始终稳定的非零 Telegram draft ID /
        Stable non-zero Telegram draft ID shared by the whole Turn.
    @param chat_id 外部 chat 目标 / External chat target.
    @param is_group 是否群聊 / Whether this is a group chat.
    @param message_thread_id 可选 Topic / Optional topic.
    @param kind 生命周期事件 / Lifecycle event.
    @param delta_text 本帧新增文本 / Text added by this frame.
    @param cumulative_text 当前 revision 的完整累计文本 / Complete accumulated text for the current revision.
    @param safe_error_code 可进入用户 UX 的稳定失败代码 / Stable failure code safe for user UX.
    @param emitted_at 事件观察时刻 / Event observation instant.
    @note ``cumulative_text`` 是预览，不得直接写入最终 outbox 或 Conversation history。/
        ``cumulative_text`` is a preview and must not be written directly to the final outbox or
        Conversation history.
    """

    turn_id: TurnId
    generation: int
    revision: int
    sequence: int
    draft_id: int
    chat_id: int | str
    is_group: bool
    message_thread_id: int | None
    kind: AssistantStreamKind
    delta_text: str
    cumulative_text: str
    safe_error_code: str | None
    emitted_at: datetime

    def __post_init__(self) -> None:
        """@brief 校验帧身份、顺序与安全错误边界 / Validate frame identity, ordering, and safe-error boundaries.

        @return None / None.
        @raise ValueError 帧字段不满足生命周期不变量时抛出 / Raised when fields violate lifecycle invariants.
        """

        if self.generation < 1:
            raise ValueError("Assistant stream generation must be positive")
        if self.revision < 0 or self.sequence < 0:
            raise ValueError("Assistant stream revision and sequence cannot be negative")
        if not 1 <= self.draft_id <= _TELEGRAM_DRAFT_ID_MAX:
            raise ValueError("Assistant stream draft_id must be a positive Int32")
        AssistantStreamAddress(
            self.chat_id,
            self.is_group,
            self.message_thread_id,
        )
        if self.kind is AssistantStreamKind.DELTA and not self.delta_text:
            raise ValueError("A stream delta frame requires delta_text")
        if self.kind is not AssistantStreamKind.DELTA and self.delta_text:
            raise ValueError("Only stream delta frames may carry delta_text")
        if self.kind is AssistantStreamKind.FAILED:
            if (
                self.safe_error_code is None
                or _SAFE_ERROR_CODE.fullmatch(self.safe_error_code) is None
            ):
                raise ValueError("A failed stream requires a safe_error_code")
        elif self.safe_error_code is not None:
            raise ValueError("Only a failed stream may carry safe_error_code")
        object.__setattr__(self, "emitted_at", ensure_utc(self.emitted_at))


class AssistantStreamProjection(Protocol):
    """@brief Assistant 流帧的最佳努力投影端口 / Best-effort projection port for Assistant stream frames."""

    async def project(self, frame: AssistantStreamFrame) -> None:
        """@brief 投影最新累计帧 / Project the latest cumulative frame.

        @param frame 已验证的累计帧 / Validated cumulative frame.
        @return None / None.
        @note 调用方必须隔离 adapter 失败；预览失败不能改变 durable inference 结果。/
            Callers must isolate adapter failures; preview failures cannot alter durable
            inference outcomes.
        """

        ...


@dataclass(slots=True)
class AssistantStreamState:
    """@brief 单一 Turn 的易失流状态机 / Ephemeral stream state machine for one Turn.

    @param turn_id durable Turn / Durable Turn.
    @param address Telegram 投影地址 / Telegram projection address.
    @param generation 当前 provider 尝试 / Current provider attempt.
    @param revision 当前 steer revision / Current steer revision.
    @param sequence 当前 generation 的最近序号 / Latest sequence in the current generation.
    @param cumulative_text 当前 revision 累计预览 / Accumulated preview for the current revision.
    @param current_frame 最近形成的帧 / Most recently formed frame.
    @param terminal 是否已终结 / Whether the stream is terminal.
    """

    turn_id: TurnId
    address: AssistantStreamAddress
    generation: int
    revision: int
    sequence: int
    cumulative_text: str
    current_frame: AssistantStreamFrame
    terminal: bool = False

    @classmethod
    def begin(
        cls,
        *,
        turn_id: TurnId,
        address: AssistantStreamAddress,
        generation: int,
        revision: int,
        emitted_at: datetime,
        revised: bool = False,
    ) -> Self:
        """@brief 建立 started 状态 / Establish a started state.

        @param turn_id durable Turn / Durable Turn.
        @param address Telegram 地址 / Telegram address.
        @param generation provider 尝试 / Provider attempt.
        @param revision steer revision / Steer revision.
        @param emitted_at 观察时刻 / Observation instant.
        @param revised generation 是否由 steer 触发 / Whether steering triggered this generation.
        @return 新流状态 / New stream state.
        """

        draft_id = stable_telegram_draft_id(turn_id)
        if revised and revision < 1:
            raise ValueError("A revised generation requires a positive revision")
        frame = AssistantStreamFrame(
            turn_id=turn_id,
            generation=generation,
            revision=revision,
            sequence=0,
            draft_id=draft_id,
            chat_id=address.chat_id,
            is_group=address.is_group,
            message_thread_id=address.message_thread_id,
            kind=(
                AssistantStreamKind.REVISED
                if revised
                else AssistantStreamKind.STARTED
            ),
            delta_text="",
            cumulative_text="",
            safe_error_code=None,
            emitted_at=emitted_at,
        )
        return cls(
            turn_id=turn_id,
            address=address,
            generation=generation,
            revision=revision,
            sequence=0,
            cumulative_text="",
            current_frame=frame,
        )

    def append(self, delta_text: str, *, emitted_at: datetime) -> AssistantStreamFrame:
        """@brief 追加文本并形成累计 delta 帧 / Append text and form a cumulative delta frame.

        @param delta_text provider 新增文本 / Text newly emitted by the provider.
        @param emitted_at 观察时刻 / Observation instant.
        @return 新累计帧 / New cumulative frame.
        """

        self._require_open()
        if not delta_text:
            raise ValueError("Assistant stream delta_text cannot be empty")
        self.cumulative_text += delta_text
        return self._advance(
            AssistantStreamKind.DELTA,
            emitted_at=emitted_at,
            delta_text=delta_text,
        )

    def revise(
        self,
        *,
        generation: int,
        revision: int,
        emitted_at: datetime,
    ) -> AssistantStreamFrame:
        """@brief 接受 steer 后切换 generation 并清空预览 / Switch generation and clear the preview after a steer.

        @param generation 新 provider 尝试 / New provider attempt.
        @param revision 严格递增 steer revision / Strictly increasing steer revision.
        @param emitted_at 观察时刻 / Observation instant.
        @return revised 帧 / Revised frame.
        """

        self._require_open()
        if generation <= self.generation:
            raise ValueError("A revised stream requires a newer generation")
        if revision <= self.revision:
            raise ValueError("A revised stream requires a newer revision")
        self.generation = generation
        self.revision = revision
        self.sequence = -1
        self.cumulative_text = ""
        return self._advance(AssistantStreamKind.REVISED, emitted_at=emitted_at)

    def complete(self, *, emitted_at: datetime) -> AssistantStreamFrame:
        """@brief 终结为 completed / Terminalize as completed.

        @param emitted_at 观察时刻 / Observation instant.
        @return completed 帧 / Completed frame.
        """

        self._require_open()
        self.terminal = True
        return self._advance(AssistantStreamKind.COMPLETED, emitted_at=emitted_at)

    def suspend(self, *, emitted_at: datetime) -> AssistantStreamFrame:
        """@brief 终止本次易失 generation 并等待 durable retry / End this ephemeral generation while awaiting durable retry.

        @param emitted_at 观察时刻 / Observation instant.
        @return suspended 帧 / Suspended frame.
        @note suspended 不承诺 Turn 最终失败；下一 claim 会用更高 generation 重新 STARTED。/
            Suspended does not declare final Turn failure; the next claim restarts with a higher
            generation.
        """

        self._require_open()
        self.terminal = True
        return self._advance(AssistantStreamKind.SUSPENDED, emitted_at=emitted_at)

    def fail(
        self,
        safe_error_code: str,
        *,
        emitted_at: datetime,
    ) -> AssistantStreamFrame:
        """@brief 以安全稳定代码终结为 failed / Terminalize as failed with a safe stable code.

        @param safe_error_code 不含诊断详情的稳定代码 / Stable code containing no diagnostics.
        @param emitted_at 观察时刻 / Observation instant.
        @return failed 帧 / Failed frame.
        """

        self._require_open()
        if _SAFE_ERROR_CODE.fullmatch(safe_error_code) is None:
            raise ValueError("Assistant stream safe_error_code has invalid syntax")
        self.terminal = True
        return self._advance(
            AssistantStreamKind.FAILED,
            emitted_at=emitted_at,
            safe_error_code=safe_error_code,
        )

    def _advance(
        self,
        kind: AssistantStreamKind,
        *,
        emitted_at: datetime,
        delta_text: str = "",
        safe_error_code: str | None = None,
    ) -> AssistantStreamFrame:
        """@brief 形成下一个已验证帧 / Form the next validated frame.

        @param kind 生命周期 kind / Lifecycle kind.
        @param emitted_at 观察时刻 / Observation instant.
        @param delta_text 可选增量 / Optional delta.
        @param safe_error_code 可选安全错误代码 / Optional safe error code.
        @return 新帧 / New frame.
        """

        self.sequence += 1
        frame = AssistantStreamFrame(
            turn_id=self.turn_id,
            generation=self.generation,
            revision=self.revision,
            sequence=self.sequence,
            draft_id=stable_telegram_draft_id(self.turn_id),
            chat_id=self.address.chat_id,
            is_group=self.address.is_group,
            message_thread_id=self.address.message_thread_id,
            kind=kind,
            delta_text=delta_text,
            cumulative_text=self.cumulative_text,
            safe_error_code=safe_error_code,
            emitted_at=emitted_at,
        )
        self.current_frame = frame
        return frame

    def _require_open(self) -> None:
        """@brief 拒绝终态后的事件 / Reject events after a terminal state.

        @return None / None.
        @raise RuntimeError 流已终结时抛出 / Raised when the stream is terminal.
        """

        if self.terminal:
            raise RuntimeError("Assistant stream is already terminal")


def stable_telegram_draft_id(turn_id: TurnId) -> int:
    """@brief 从 Turn UUID 派生稳定非零 Int32 draft ID / Derive a stable non-zero Int32 draft ID from a Turn UUID.

    @param turn_id durable Turn / Durable Turn.
    @return ``1..2^31-1`` 内稳定 ID / Stable ID in ``1..2^31-1``.
    """

    return (turn_id.value.int % _TELEGRAM_DRAFT_ID_MAX) + 1


_LOGGER = logging.getLogger(__name__)
"""@brief Assistant 流投影失败日志器 / Logger for Assistant stream-projection failures."""


@dataclass(slots=True)
class AssistantStreamSession:
    """@brief 隔离 adapter 失败的一次 generation 流会话 / One generation stream session isolating adapter failures.

    @param state 易失流状态 / Ephemeral stream state.
    @param projection 最佳努力投影端口 / Best-effort projection port.
    """

    state: AssistantStreamState
    projection: AssistantStreamProjection

    async def start(self) -> None:
        """@brief 投影 STARTED 或 REVISED / Project STARTED or REVISED.

        @return None / None.
        """

        await self._project(self.state.current_frame)

    async def append(self, text: str, *, emitted_at: datetime) -> None:
        """@brief 投影一个文本增量 / Project one text delta.

        @param text provider 新增文本 / Provider text delta.
        @param emitted_at 观察时刻 / Observation instant.
        @return None / None.
        """

        if not text or self.state.terminal:
            return
        await self._project(self.state.append(text, emitted_at=emitted_at))

    async def complete(self, *, emitted_at: datetime) -> None:
        """@brief 最佳努力投影成功终态 / Best-effort project the successful terminal state.

        @param emitted_at 观察时刻 / Observation instant.
        @return None / None.
        """

        if self.state.terminal:
            return
        await self._project_terminal(self.state.complete(emitted_at=emitted_at))

    async def suspend(self, *, emitted_at: datetime) -> None:
        """@brief 最佳努力停止 typing、等待 retry / Best-effort stop typing while awaiting retry.

        @param emitted_at 观察时刻 / Observation instant.
        @return None / None.
        """

        if self.state.terminal:
            return
        await self._project_terminal(self.state.suspend(emitted_at=emitted_at))

    async def fail(self, code: str, *, emitted_at: datetime) -> None:
        """@brief 最佳努力投影安全失败码 / Best-effort project a safe failure code.

        @param code 稳定低基数错误码 / Stable low-cardinality error code.
        @param emitted_at 观察时刻 / Observation instant.
        @return None / None.
        """

        if self.state.terminal:
            return
        await self._project_terminal(self.state.fail(code, emitted_at=emitted_at))

    async def _project_terminal(self, frame: AssistantStreamFrame) -> None:
        """@brief 让终态入队相对 task cancellation 原子化 / Make terminal enqueue atomic with respect to task cancellation.

        @param frame 已形成且使 session terminal 的帧 / Frame that has made the session terminal.
        @return None / None.
        @note durable commit 后若调用方被取消，必须先等待短小的 projection 入队结束再传播
            ``CancelledError``，否则 state 已 terminal 而 Telegram actor 永远看不到终态。/
            If the caller is cancelled after a durable commit, the short projection enqueue must
            finish before ``CancelledError`` propagates; otherwise the state is terminal while
            the Telegram actor never observes its terminal frame.
        """

        projection_task = asyncio.create_task(
            self._project(frame),
            name=f"assistant-stream-terminal-{frame.draft_id}-{frame.sequence}",
        )
        try:
            await asyncio.shield(projection_task)
        except asyncio.CancelledError:
            await projection_task
            raise

    async def _project(self, frame: AssistantStreamFrame) -> None:
        """@brief 隔离任意 connector 异常 / Isolate every connector exception.

        @param frame 待投影帧 / Frame to project.
        @return None / None.
        """

        try:
            await self.projection.project(frame)
        except Exception:
            _LOGGER.warning(
                "Assistant stream projection failed turn_id=%s kind=%s",
                frame.turn_id,
                frame.kind.value,
                exc_info=True,
            )


__all__ = [
    "AssistantStreamAddress",
    "AssistantStreamFrame",
    "AssistantStreamKind",
    "AssistantStreamProjection",
    "AssistantStreamSession",
    "AssistantStreamState",
    "stable_telegram_draft_id",
]
