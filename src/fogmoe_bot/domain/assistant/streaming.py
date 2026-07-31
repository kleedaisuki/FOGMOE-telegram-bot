"""@brief Provider-neutral Assistant 流领域状态机 / Provider-neutral Assistant stream domain state machine.

流帧是最佳努力的用户体验投影，不是最终投递事实。结构化活动快照只描述模型公开的
commentary 与工具生命周期；provider 文本 delta 只保留协议与恢复语义。领域帧不携带
任何投影目标或供应商标识，最终消息仍必须由 durable outbox 发送。/
Stream frames are best-effort UX projections, not final-delivery facts. Structured activity
snapshots describe only model-authored public commentary and tool lifecycles, while provider text deltas
remain only for protocol and recovery semantics. Domain frames carry neither projection targets nor
provider identifiers, and the durable outbox remains the sole publisher of the final message.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Self

from fogmoe_bot.domain.conversation.identity import TurnId
from fogmoe_bot.domain.temporal import ensure_utc

_SAFE_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,99}$")
"""@brief 可进入用户投影的稳定错误代码语法 / Grammar for stable error codes safe for user projections."""

_MAX_ACTIVITY_ITEMS = 64
"""@brief 单 Turn 易失活动项上限 / Maximum ephemeral activity items retained for one Turn."""


class AssistantActivityKind(StrEnum):
    """@brief 用户可见的高层活动类别 / High-level user-visible activity category."""

    COMMENTARY = "commentary"
    TOOL = "tool"


class AssistantActivityStatus(StrEnum):
    """@brief 活动项生命周期 / Activity-item lifecycle."""

    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AssistantStreamActivity:
    """@brief 一个稳定、可累计展示的 Assistant 活动项 / One stable, cumulatively displayable Assistant activity item.

    @param key Turn 内稳定活动键 / Stable activity key inside the Turn.
    @param kind 活动类别 / Activity category.
    @param label checkpoint commentary 或工具稳定名称 / Checkpoint commentary or stable tool name.
    @param status 当前状态 / Current status.
    @note commentary 是模型明确发给用户的公开工作说明；工具项只携带稳定名称。两者都
        不得携带参数、原始结果、日志或私有推理。/ Commentary is an explicit public work
        note from the model, while tool items carry only a stable name. Neither may carry
        arguments, raw results, logs, or private reasoning.
    """

    key: str
    kind: AssistantActivityKind
    label: str
    status: AssistantActivityStatus

    def __post_init__(self) -> None:
        """@brief 校验活动项的有界稳定字段 / Validate bounded stable activity fields.

        @return None / None.
        @raise ValueError key 或 label 为空、过长或含控制字符时抛出 /
            Raised when key or label is blank, oversized, or contains control characters.
        """

        for field_name, value in (("key", self.key), ("label", self.label)):
            maximum = (
                4_096
                if (
                    field_name == "label"
                    and self.kind is AssistantActivityKind.COMMENTARY
                )
                else 160
            )
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > maximum
                or any(
                    ord(character) < 32 and character not in {"\n", "\t"}
                    for character in value
                )
            ):
                raise ValueError(
                    f"Assistant stream activity {field_name} must contain "
                    f"1..{maximum} safe characters"
                )


class AssistantStreamKind(StrEnum):
    """@brief Assistant 流生命周期事件 / Assistant stream lifecycle event."""

    STARTED = "started"
    DELTA = "delta"
    ACTIVITY = "activity"
    REVISED = "revised"
    SUSPENDED = "suspended"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AssistantStreamFrame:
    """@brief 一帧 provider-neutral 累计流状态 / One cumulative provider-neutral stream frame.

    @param turn_id durable Turn identity / Durable Turn identity.
    @param generation provider 尝试序号 / Provider-attempt ordinal.
    @param revision 用户 steer 输入版本 / User-steer input revision.
    @param sequence 当前 generation 内单调帧序号 / Monotonic frame sequence inside the current generation.
    @param kind 生命周期事件 / Lifecycle event.
    @param delta_text 本帧新增文本 / Text added by this frame.
    @param cumulative_text 当前 revision 的完整累计文本 / Complete accumulated text for the current revision.
    @param safe_error_code 可进入用户 UX 的稳定失败代码 / Stable failure code safe for user UX.
    @param activities 当前 Turn 的有序高层活动快照 / Ordered high-level activity snapshot for the Turn.
    @param emitted_at 事件观察时刻 / Event observation instant.
    @note ``cumulative_text`` 是预览，不得直接写入最终 outbox 或 Conversation history。/
        ``cumulative_text`` is a preview and must not be written directly to the final outbox or
        Conversation history.
    """

    turn_id: TurnId
    generation: int
    revision: int
    sequence: int
    kind: AssistantStreamKind
    delta_text: str
    cumulative_text: str
    safe_error_code: str | None
    activities: tuple[AssistantStreamActivity, ...]
    emitted_at: datetime

    def __post_init__(self) -> None:
        """@brief 校验帧身份、顺序与安全错误边界 / Validate frame identity, ordering, and safe-error boundaries.

        @return None / None.
        @raise ValueError 帧字段不满足生命周期不变量时抛出 / Raised when fields violate lifecycle invariants.
        """

        if self.generation < 1:
            raise ValueError("Assistant stream generation must be positive")
        if self.revision < 0 or self.sequence < 0:
            raise ValueError(
                "Assistant stream revision and sequence cannot be negative"
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
        if len(self.activities) > _MAX_ACTIVITY_ITEMS:
            raise ValueError("Assistant stream activity snapshot exceeds its bound")
        activity_keys = [activity.key for activity in self.activities]
        if len(set(activity_keys)) != len(activity_keys):
            raise ValueError("Assistant stream activity keys must be unique")
        object.__setattr__(self, "emitted_at", ensure_utc(self.emitted_at))


class AssistantStreamState:
    """@brief 单一 Turn 的易失流状态机 / Ephemeral stream state machine for one Turn.

    @note 状态只能经 ``begin`` 建立；所有可变集合均封闭在聚合内部，读取只返回不可变视图。/
        State can only be established through ``begin``; mutable collections remain inside the
        aggregate and reads expose immutable views.
    """

    _turn_id: TurnId
    """@brief durable Turn 身份 / Durable Turn identity."""
    _generation: int
    """@brief 当前 provider 尝试 / Current provider attempt."""
    _revision: int
    """@brief 当前 steer revision / Current steer revision."""
    _sequence: int
    """@brief 当前 generation 帧序号 / Current-generation frame sequence."""
    _cumulative_text: str
    """@brief 当前 revision 累计预览 / Current-revision accumulated preview."""
    _activities: list[AssistantStreamActivity]
    """@brief 聚合内部可变活动状态 / Aggregate-internal mutable activity state."""
    _current_frame: AssistantStreamFrame
    """@brief 最近形成的不可变帧 / Most recently formed immutable frame."""
    _terminal: bool
    """@brief 聚合是否终结 / Whether the aggregate is terminal."""

    __slots__ = (
        "_activities",
        "_cumulative_text",
        "_current_frame",
        "_generation",
        "_revision",
        "_sequence",
        "_terminal",
        "_turn_id",
    )
    """@brief 聚合私有状态槽 / Private aggregate-state slots."""

    def __init__(self) -> None:
        """@brief 拒绝绕过命名构造器 / Reject bypassing the named constructor.

        @return 永不返回 / Never returns.
        @raise TypeError 必须调用 ``begin`` / ``begin`` must be used.
        """

        raise TypeError("Use AssistantStreamState.begin()")

    @property
    def turn_id(self) -> TurnId:
        """@brief 返回 durable Turn 身份 / Return the durable Turn identity.

        @return Turn 身份 / Turn identity.
        """

        return self._turn_id

    @property
    def generation(self) -> int:
        """@brief 返回当前 provider 尝试 / Return the current provider attempt.

        @return 正整数 generation / Positive generation.
        """

        return self._generation

    @property
    def revision(self) -> int:
        """@brief 返回当前 steer revision / Return the current steer revision.

        @return 非负 revision / Non-negative revision.
        """

        return self._revision

    @property
    def sequence(self) -> int:
        """@brief 返回当前 generation 的帧序号 / Return the frame sequence in the current generation.

        @return 非负序号 / Non-negative sequence.
        """

        return self._sequence

    @property
    def cumulative_text(self) -> str:
        """@brief 返回当前 revision 累计预览 / Return the current revision's accumulated preview.

        @return 累计文本 / Accumulated text.
        """

        return self._cumulative_text

    @property
    def activities(self) -> tuple[AssistantStreamActivity, ...]:
        """@brief 返回不可变活动快照 / Return an immutable activity snapshot.

        @return 有序活动元组 / Ordered activity tuple.
        """

        return tuple(self._activities)

    @property
    def current_frame(self) -> AssistantStreamFrame:
        """@brief 返回最近形成的领域帧 / Return the most recently formed domain frame.

        @return 当前帧 / Current frame.
        """

        return self._current_frame

    @property
    def terminal(self) -> bool:
        """@brief 判断聚合是否终结 / Tell whether the aggregate is terminal.

        @return 已终结时为 True / True when terminal.
        """

        return self._terminal

    @classmethod
    def begin(
        cls,
        *,
        turn_id: TurnId,
        generation: int,
        revision: int,
        emitted_at: datetime,
        revised: bool = False,
    ) -> Self:
        """@brief 建立 started 状态 / Establish a started state.

        @param turn_id durable Turn / Durable Turn.
        @param generation provider 尝试 / Provider attempt.
        @param revision steer revision / Steer revision.
        @param emitted_at 观察时刻 / Observation instant.
        @param revised generation 是否由 steer 触发 / Whether steering triggered this generation.
        @return 新流状态 / New stream state.
        """

        if revised and revision < 1:
            raise ValueError("A revised generation requires a positive revision")
        activities: list[AssistantStreamActivity] = []
        frame = AssistantStreamFrame(
            turn_id=turn_id,
            generation=generation,
            revision=revision,
            sequence=0,
            kind=(
                AssistantStreamKind.REVISED if revised else AssistantStreamKind.STARTED
            ),
            delta_text="",
            cumulative_text="",
            safe_error_code=None,
            activities=tuple(activities),
            emitted_at=emitted_at,
        )
        state = object.__new__(cls)
        state._turn_id = turn_id
        state._generation = generation
        state._revision = revision
        state._sequence = 0
        state._cumulative_text = ""
        state._activities = activities
        state._current_frame = frame
        state._terminal = False
        return state

    def append(self, delta_text: str, *, emitted_at: datetime) -> AssistantStreamFrame:
        """@brief 追加文本并形成累计 delta 帧 / Append text and form a cumulative delta frame.

        @param delta_text provider 新增文本 / Text newly emitted by the provider.
        @param emitted_at 观察时刻 / Observation instant.
        @return 新累计帧 / New cumulative frame.
        """

        self._require_open()
        if not delta_text:
            raise ValueError("Assistant stream delta_text cannot be empty")
        self._cumulative_text += delta_text
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
        if generation <= self._generation:
            raise ValueError("A revised stream requires a newer generation")
        if revision <= self._revision:
            raise ValueError("A revised stream requires a newer revision")
        self._generation = generation
        self._revision = revision
        self._sequence = -1
        self._cumulative_text = ""
        self._activities = []
        return self._advance(AssistantStreamKind.REVISED, emitted_at=emitted_at)

    def commentary(
        self,
        item_id: str,
        text: str,
        *,
        emitted_at: datetime,
    ) -> AssistantStreamFrame:
        """@brief 记录 checkpoint 后稳定 commentary / Record stable commentary after checkpointing.

        @param item_id 当前 revision 内稳定 item ID / Stable item ID in the current revision.
        @param text 模型明确给用户的自然工作说明 / Natural work note explicitly addressed to the user.
        @param emitted_at 观察时刻 / Observation instant.
        @return 累计活动帧 / Cumulative activity frame.
        """

        self._require_open()
        self._complete_active_activities()
        key = f"commentary:{item_id}"
        existing = self._activity_index(key)
        activity = AssistantStreamActivity(
            key=key,
            kind=AssistantActivityKind.COMMENTARY,
            label=text,
            status=AssistantActivityStatus.COMPLETED,
        )
        if existing is None:
            self._append_activity(activity)
        else:
            if self._activities[existing] != activity:
                raise ValueError("Assistant stream commentary identity conflict")
        return self._advance(AssistantStreamKind.ACTIVITY, emitted_at=emitted_at)

    def start_tool(
        self,
        invocation_id: str,
        tool_name: str,
        *,
        emitted_at: datetime,
    ) -> AssistantStreamFrame:
        """@brief 记录一个已 checkpoint 的工具调用开始 / Record the start of a checkpointed tool call.

        @param invocation_id Turn 内稳定调用身份 / Stable invocation identity inside the Turn.
        @param tool_name 目录工具稳定名称 / Stable catalog tool name.
        @param emitted_at 观察时刻 / Observation instant.
        @return 累计活动帧 / Cumulative activity frame.
        @note 参数和结果不会进入活动投影 / Arguments and results never enter the activity projection.
        """

        self._require_open()
        self._complete_active_activities()
        key = f"tool:{invocation_id}"
        existing = self._activity_index(key)
        activity = AssistantStreamActivity(
            key=key,
            kind=AssistantActivityKind.TOOL,
            label=tool_name,
            status=AssistantActivityStatus.ACTIVE,
        )
        if existing is None:
            self._append_activity(activity)
        else:
            prior = self._activities[existing]
            if prior.kind is not AssistantActivityKind.TOOL or prior.label != tool_name:
                raise ValueError("Assistant stream tool activity identity conflict")
            self._activities[existing] = activity
        return self._advance(AssistantStreamKind.ACTIVITY, emitted_at=emitted_at)

    def finish_tool(
        self,
        invocation_id: str,
        tool_name: str,
        *,
        succeeded: bool,
        emitted_at: datetime,
    ) -> AssistantStreamFrame:
        """@brief 记录工具完成或失败 / Record tool completion or failure.

        @param invocation_id Turn 内稳定调用身份 / Stable invocation identity inside the Turn.
        @param tool_name 目录工具稳定名称 / Stable catalog tool name.
        @param succeeded 工具是否形成可用结果 / Whether the tool produced a usable result.
        @param emitted_at 观察时刻 / Observation instant.
        @return 累计活动帧 / Cumulative activity frame.
        """

        self._require_open()
        key = f"tool:{invocation_id}"
        existing = self._activity_index(key)
        activity = AssistantStreamActivity(
            key=key,
            kind=AssistantActivityKind.TOOL,
            label=tool_name,
            status=(
                AssistantActivityStatus.COMPLETED
                if succeeded
                else AssistantActivityStatus.FAILED
            ),
        )
        if existing is None:
            self._append_activity(activity)
        else:
            prior = self._activities[existing]
            if prior.kind is not AssistantActivityKind.TOOL or prior.label != tool_name:
                raise ValueError("Assistant stream tool activity identity conflict")
            self._activities[existing] = activity
        return self._advance(AssistantStreamKind.ACTIVITY, emitted_at=emitted_at)

    def complete(self, *, emitted_at: datetime) -> AssistantStreamFrame:
        """@brief 终结为 completed / Terminalize as completed.

        @param emitted_at 观察时刻 / Observation instant.
        @return completed 帧 / Completed frame.
        """

        self._require_open()
        self._complete_active_activities()
        self._terminal = True
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
        self._terminal = True
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
        self._terminal = True
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

        self._sequence += 1
        frame = AssistantStreamFrame(
            turn_id=self._turn_id,
            generation=self._generation,
            revision=self._revision,
            sequence=self._sequence,
            kind=kind,
            delta_text=delta_text,
            cumulative_text=self._cumulative_text,
            safe_error_code=safe_error_code,
            activities=tuple(self._activities),
            emitted_at=emitted_at,
        )
        self._current_frame = frame
        return frame

    def _activity_index(self, key: str) -> int | None:
        """@brief 查找活动键的位置 / Find the position of an activity key.

        @param key 稳定活动键 / Stable activity key.
        @return 索引或 None / Index or None.
        """

        return next(
            (
                index
                for index, activity in enumerate(self._activities)
                if activity.key == key
            ),
            None,
        )

    def _append_activity(self, activity: AssistantStreamActivity) -> None:
        """@brief 有界追加活动项 / Append an activity item within the hard bound.

        @param activity 新活动项 / New activity item.
        @return None / None.
        @note 超限时只淘汰最旧的非 active 项，避免丢失仍在执行的状态。/
            At the bound, only the oldest non-active item is evicted so an in-flight state is
            never hidden.
        """

        if len(self._activities) >= _MAX_ACTIVITY_ITEMS:
            removable = next(
                (
                    index
                    for index, existing in enumerate(self._activities)
                    if existing.status is not AssistantActivityStatus.ACTIVE
                ),
                None,
            )
            if removable is None:
                raise RuntimeError("Assistant stream has too many active activities")
            self._activities.pop(removable)
        self._activities.append(activity)

    def _complete_active_activities(self) -> None:
        """@brief 把所有 active 高层活动收束为完成 / Converge all active high-level activities to completed.

        @return None / None.
        """

        self._activities = [
            (
                AssistantStreamActivity(
                    key=activity.key,
                    kind=activity.kind,
                    label=activity.label,
                    status=AssistantActivityStatus.COMPLETED,
                )
                if activity.status is AssistantActivityStatus.ACTIVE
                else activity
            )
            for activity in self._activities
        ]

    def _require_open(self) -> None:
        """@brief 拒绝终态后的事件 / Reject events after a terminal state.

        @return None / None.
        @raise RuntimeError 流已终结时抛出 / Raised when the stream is terminal.
        """

        if self._terminal:
            raise RuntimeError("Assistant stream is already terminal")


__all__ = [
    "AssistantActivityKind",
    "AssistantActivityStatus",
    "AssistantStreamActivity",
    "AssistantStreamFrame",
    "AssistantStreamKind",
    "AssistantStreamState",
]
