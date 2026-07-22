"""@brief 助手定时回合的领域模型与纯 recurrence 算法 / Assistant-turn scheduling domain model and pure recurrence algorithms."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from uuid import UUID

from fogmoe_bot.domain.conversation.identity import ConversationId, DeliveryStreamId
from fogmoe_bot.domain.temporal import TimeZoneId, ensure_utc

_MAX_TRIGGER_REASON_LENGTH = 200
"""@brief 触发原因的领域长度上限 / Domain length limit for trigger reasons."""

_MAX_INSTRUCTION_LENGTH = 2_000
"""@brief 助手指令的领域长度上限 / Domain length limit for assistant instructions."""

_MAX_CONTEXT_SNAPSHOT_LENGTH = 1_000
"""@brief 创建时上下文快照的领域长度上限 / Domain length limit for creation-time context snapshots."""

_MAX_ERROR_LENGTH = 4_000
"""@brief 查询快照错误摘要的领域长度上限 / Domain length limit for query-snapshot error summaries."""


class StaleScheduleClaimError(RuntimeError):
    """@brief 调度 claim 已被回收或替换 / A schedule claim was recovered or superseded.

    @note Repository 与 Unit of Work 必须用此异常阻止陈旧 worker 提交。/
        Repositories and Units of Work must use this exception to fence stale workers from committing.
    """


@dataclass(frozen=True, slots=True)
class ScheduleTarget:
    """@brief 助手回合的会话与投递目标 / Conversation and delivery target of an assistant turn.

    @param conversation_id 内部会话聚合键 / Internal conversation aggregate key.
    @param delivery_stream_id 外部有序投递流 / External ordered-delivery stream.
    @param chat_id Telegram chat 标识 / Telegram chat identifier.
    @param is_group 目标是否为群聊 / Whether the target is a group chat.
    @param message_thread_id 可选 Telegram forum topic 标识 / Optional Telegram forum-topic identifier.
    """

    conversation_id: ConversationId
    delivery_stream_id: DeliveryStreamId
    chat_id: int
    is_group: bool
    message_thread_id: int | None = None

    def __post_init__(self) -> None:
        """@brief 校验目标身份及 thread 不变量 / Validate target identity and thread invariants.

        @return None / None.
        @raise TypeError 值对象或标量类型错误时抛出 / Raised for invalid value-object or scalar types.
        @raise ValueError chat 或 thread 标识超出领域范围时抛出 / Raised when chat or thread identifiers are outside domain bounds.
        """

        if not isinstance(self.conversation_id, ConversationId):
            raise TypeError("conversation_id must be a ConversationId")
        if not isinstance(self.delivery_stream_id, DeliveryStreamId):
            raise TypeError("delivery_stream_id must be a DeliveryStreamId")
        if not _is_int(self.chat_id):
            raise TypeError("chat_id must be an integer")
        if self.chat_id == 0:
            raise ValueError("chat_id cannot be zero")
        if not isinstance(self.is_group, bool):
            raise TypeError("is_group must be a boolean")
        if self.is_group and self.chat_id > 0:
            raise ValueError("Group chat_id must be negative")
        if not self.is_group and self.chat_id < 0:
            raise ValueError("Private chat_id must be positive")
        if self.message_thread_id is not None:
            if not _is_int(self.message_thread_id):
                raise TypeError("message_thread_id must be an integer or None")
            if self.message_thread_id < 1:
                raise ValueError("message_thread_id must be positive")
            if not self.is_group:
                raise ValueError("message_thread_id is only valid for group targets")

    @property
    def group_id(self) -> int | None:
        """@brief 返回群聊标识，私聊返回 None / Return the group identifier, or None for a private chat.

        @return 群聊的 chat_id 或 None / The group's chat_id or None.
        """

        return self.chat_id if self.is_group else None


@dataclass(frozen=True, slots=True)
class OneShot:
    """@brief 只发生一次的 cadence / Cadence that occurs exactly once."""

    def next_after(self, *, current: datetime, after: datetime) -> datetime | None:
        """@brief 查找严格晚于基准的唯一发生项 / Find the sole occurrence strictly after a reference instant.

        @param current 尚未消费的当前发生项 / Current unconsumed occurrence.
        @param after 严格下界 / Exclusive lower bound.
        @return current 尚在未来时返回规范 UTC，否则返回 None / Normalized UTC current when still future, otherwise None.
        """

        current_utc, after_utc = _normalize_occurrence_inputs(current, after)
        return current_utc if current_utc > after_utc else None


@dataclass(frozen=True, slots=True)
class FixedInterval:
    """@brief 按绝对时长重复的 cadence / Cadence recurring by an absolute duration.

    @param every 相邻发生项的固定时长 / Fixed duration between adjacent occurrences.
    @note 固定 24 小时不是本地“每天同一时刻” / A fixed 24 hours is not the same local wall time every day.
    """

    every: timedelta

    def __post_init__(self) -> None:
        """@brief 强制固定间隔为正 / Require a positive fixed interval.

        @return None / None.
        @raise TypeError every 不是 timedelta 时抛出 / Raised when every is not a timedelta.
        @raise ValueError every 非正时抛出 / Raised when every is not positive.
        """

        if not isinstance(self.every, timedelta):
            raise TypeError("Fixed interval must be a timedelta")
        if self.every <= timedelta():
            raise ValueError("Fixed interval must be positive")

    def next_after(self, *, current: datetime, after: datetime) -> datetime:
        """@brief 以 O(1) 算术跳到严格晚于基准的发生项 / Jump in O(1) arithmetic to the occurrence strictly after a reference instant.

        @param current 当前计划发生项 / Current scheduled occurrence.
        @param after 严格下界 / Exclusive lower bound.
        @return 首个严格晚于 after 的 UTC aware 发生项 / First UTC-aware occurrence strictly later than after.
        """

        current_utc, after_utc = _normalize_occurrence_inputs(current, after)
        if current_utc > after_utc:
            return current_utc
        elapsed = after_utc - current_utc
        skipped_intervals = elapsed // self.every + 1
        return current_utc + self.every * skipped_intervals


@dataclass(frozen=True, slots=True)
class CalendarDaily:
    """@brief 在 IANA 时区中按本地日期重复的 daily cadence / Daily cadence recurring by local dates in an IANA zone.

    @param local_time 每个 active date 的本地墙钟时间 / Local wall-clock time on each active date.
    @param time_zone 决定日期与 DST 规则的 IANA 时区 / IANA zone determining dates and DST rules.
    @param interval 相邻 active date 的天数 / Number of days between active dates.
    """

    local_time: time
    time_zone: TimeZoneId
    interval: int = 1

    def __post_init__(self) -> None:
        """@brief 校验 daily cadence / Validate the daily cadence.

        @return None / None.
        @raise TypeError 字段类型错误时抛出 / Raised for invalid field types.
        @raise ValueError local_time 带时区或 interval 非正时抛出 / Raised for an aware local_time or non-positive interval.
        """

        _validate_calendar_rule(self.local_time, self.time_zone, self.interval)
        object.__setattr__(self, "local_time", self.local_time.replace(fold=0))

    def next_after(self, *, current: datetime, after: datetime) -> datetime:
        """@brief 查找严格晚于基准的下一个本地日期发生项 / Find the next local-date occurrence strictly after a reference instant.

        @param current 当前计划发生项，也是 interval 的日期锚点 / Current scheduled occurrence and date anchor for interval.
        @param after 严格下界 / Exclusive lower bound.
        @return 首个严格晚于 after 的 UTC aware 发生项 / First UTC-aware occurrence strictly later than after.
        @note DST overlap 选较早瞬间，gap 按 offset 跳变量前移 / DST overlaps choose the earlier instant and gaps shift by the offset transition.
        """

        current_utc, after_utc = _normalize_occurrence_inputs(current, after)
        anchor_date = _require_calendar_cursor(
            local_time=self.local_time,
            time_zone=self.time_zone,
            current=current_utc,
        )
        if current_utc > after_utc:
            return current_utc

        reference_date = self.time_zone.localize(after_utc).date()
        elapsed_days = max(0, (reference_date - anchor_date).days)
        skipped_intervals = elapsed_days // self.interval
        candidate_date = anchor_date + timedelta(days=skipped_intervals * self.interval)
        candidate = _resolve_calendar_date(
            candidate_date, self.local_time, self.time_zone
        )
        if candidate <= after_utc:
            candidate_date += timedelta(days=self.interval)
            candidate = _resolve_calendar_date(
                candidate_date, self.local_time, self.time_zone
            )
        return candidate


@dataclass(frozen=True, slots=True)
class CalendarWeekly:
    """@brief 在 IANA 时区中按 active week 与 ISO weekday 重复的 cadence / Cadence recurring by active weeks and ISO weekdays in an IANA zone.

    @param local_time active weekday 的本地墙钟时间 / Local wall-clock time on each active weekday.
    @param time_zone 决定周与 DST 规则的 IANA 时区 / IANA zone determining weeks and DST rules.
    @param weekdays ISO weekday 集合，1 为周一、7 为周日 / ISO weekday set, where Monday is 1 and Sunday is 7.
    @param interval 相邻 active week 的周数 / Number of weeks between active weeks.
    """

    local_time: time
    time_zone: TimeZoneId
    weekdays: frozenset[int]
    interval: int = 1

    def __post_init__(self) -> None:
        """@brief 校验 weekly cadence / Validate the weekly cadence.

        @return None / None.
        @raise TypeError 字段类型错误时抛出 / Raised for invalid field types.
        @raise ValueError local_time、weekdays 或 interval 无效时抛出 / Raised for an invalid local_time, weekdays, or interval.
        """

        _validate_calendar_rule(self.local_time, self.time_zone, self.interval)
        if not isinstance(self.weekdays, frozenset):
            raise TypeError("Calendar weekdays must be a frozenset")
        if not self.weekdays:
            raise ValueError("Calendar weekdays cannot be empty")
        if any(not _is_int(day) for day in self.weekdays):
            raise TypeError("Calendar weekdays must contain only integers")
        if any(day < 1 or day > 7 for day in self.weekdays):
            raise ValueError("Calendar weekdays must use ISO values from 1 through 7")
        object.__setattr__(self, "local_time", self.local_time.replace(fold=0))

    def next_after(self, *, current: datetime, after: datetime) -> datetime:
        """@brief 以有界候选集查找下一个 active-week 发生项 / Find the next active-week occurrence from a bounded candidate set.

        @param current 当前计划发生项，也是 active-week 的锚点 / Current occurrence and active-week anchor.
        @param after 严格下界 / Exclusive lower bound.
        @return 首个严格晚于 after 的 UTC aware 发生项 / First UTC-aware occurrence strictly later than after.
        @note 算法只检查当前或下一个 active week，不随漏过的周数循环 / The algorithm checks only the current or next active week and never loops over missed weeks.
        """

        current_utc, after_utc = _normalize_occurrence_inputs(current, after)
        anchor_date = _require_calendar_cursor(
            local_time=self.local_time,
            time_zone=self.time_zone,
            current=current_utc,
        )
        if anchor_date.isoweekday() not in self.weekdays:
            raise ValueError(
                "Current calendar occurrence is outside the configured weekdays"
            )
        if current_utc > after_utc:
            return current_utc

        anchor_week = anchor_date - timedelta(days=anchor_date.isoweekday() - 1)
        reference_date = self.time_zone.localize(after_utc).date()
        reference_week = reference_date - timedelta(
            days=reference_date.isoweekday() - 1
        )
        elapsed_weeks = max(0, (reference_week - anchor_week).days // 7)
        skipped_cycles = elapsed_weeks // self.interval
        active_week = anchor_week + timedelta(weeks=skipped_cycles * self.interval)

        candidate = self._first_in_week_after(active_week, after_utc)
        if candidate is not None:
            return candidate
        next_active_week = active_week + timedelta(weeks=self.interval)
        candidate = self._first_in_week_after(next_active_week, after_utc)
        if (
            candidate is None
        ):  # pragma: no cover - a non-empty weekday set always yields a future item
            raise AssertionError(
                "A non-empty weekly cadence must produce an occurrence"
            )
        return candidate

    def _first_in_week_after(
        self, week_start: date, after: datetime
    ) -> datetime | None:
        """@brief 从一个 active week 的至多七个候选中选择发生项 / Select an occurrence from at most seven candidates in one active week.

        @param week_start active week 的本地周一 / Local Monday of the active week.
        @param after 严格 UTC 下界 / Exclusive UTC lower bound.
        @return 最早合格发生项；若该周均已过去则为 None / Earliest eligible occurrence, or None when the week has passed.
        """

        candidates = (
            _resolve_calendar_date(
                week_start + timedelta(days=weekday - 1),
                self.local_time,
                self.time_zone,
            )
            for weekday in self.weekdays
        )
        return min(
            (candidate for candidate in candidates if candidate > after), default=None
        )


type Cadence = OneShot | FixedInterval | CalendarDaily | CalendarWeekly
"""@brief 助手定时回合支持的 recurrence 类型并集 / Union of supported assistant-turn recurrence types."""


class MisfirePolicy(StrEnum):
    """@brief worker 发现过期发生项时的策略 / Policy applied when a worker observes a late occurrence."""

    FIRE_ONCE = "fire_once"
    """@brief 合并错过的发生项并立即接受一次 / Coalesce missed occurrences and accept once immediately."""

    SKIP = "skip"
    """@brief 跳过超出 grace 的发生项 / Skip an occurrence outside its grace window."""


class ScheduleStatus(StrEnum):
    """@brief 助手定时回合的持久化生命周期状态 / Persisted lifecycle state of an assistant-turn schedule."""

    PENDING = "pending"
    """@brief 等待到期 / Waiting to become due."""

    PROCESSING = "processing"
    """@brief 已被带 fencing token 的 worker 领取 / Claimed by a worker carrying a fencing token."""

    RETRY_WAIT = "retry_wait"
    """@brief 可重试失败后的退避等待 / Waiting in backoff after a retryable failure."""

    COMPLETED = "completed"
    """@brief 一次性计划成功完成 / One-shot schedule completed successfully."""

    CANCELLED = "cancelled"
    """@brief 被用户或系统显式取消 / Explicitly cancelled by a user or the system."""

    EXPIRED = "expired"
    """@brief 按 misfire policy 终结为过期 / Terminally expired under the misfire policy."""

    FAILED_FINAL = "failed_final"
    """@brief 不可重试或耗尽重试的最终失败 / Final failure after a permanent error or exhausted retries."""


@dataclass(frozen=True, slots=True)
class ScheduledAssistantTurn:
    """@brief 可持久化的助手定时回合聚合 / Persistable assistant-turn schedule aggregate.

    @param schedule_id 计划标识 / Schedule identifier.
    @param creator_user_id 创建者 Telegram user 标识 / Creator Telegram user identifier.
    @param target 会话与投递目标 / Conversation and delivery target.
    @param trigger_reason 注入回合的稳定触发原因 / Stable trigger reason injected into the turn.
    @param instruction 到期时交给 assistant 的指令 / Instruction passed to the assistant when due.
    @param cadence recurrence 规则 / Recurrence rule.
    @param next_run_at 当前未消费发生项 / Current unconsumed occurrence.
    @param created_at 计划创建瞬间 / Schedule creation instant.
    @param time_zone 用户解释与展示所用 IANA 时区 / IANA zone used for user interpretation and display.
    @param context_snapshot 创建计划时捕获的可选上下文 / Optional context captured when the schedule was created.
    @param misfire_policy 过期发生项策略 / Late-occurrence policy.
    @param misfire_grace 允许迟到的可选宽限时长 / Optional lateness grace duration.
    """

    schedule_id: int
    creator_user_id: int
    target: ScheduleTarget
    trigger_reason: str
    instruction: str
    cadence: Cadence
    next_run_at: datetime
    created_at: datetime
    time_zone: TimeZoneId
    context_snapshot: str | None = None
    misfire_policy: MisfirePolicy = MisfirePolicy.FIRE_ONCE
    misfire_grace: timedelta | None = None

    def __post_init__(self) -> None:
        """@brief 规范文本与 UTC 时间并维护聚合不变量 / Normalize text and UTC timestamps and enforce aggregate invariants.

        @return None / None.
        @raise TypeError 字段类型错误时抛出 / Raised for invalid field types.
        @raise ValueError 标识、文本、时间顺序或 cadence 时区无效时抛出 / Raised for invalid identifiers, text, chronology, or cadence zone.
        """

        _require_positive_int(self.schedule_id, "schedule_id")
        _require_positive_int(self.creator_user_id, "creator_user_id")
        if not isinstance(self.target, ScheduleTarget):
            raise TypeError("target must be a ScheduleTarget")
        if not isinstance(
            self.cadence, (OneShot, FixedInterval, CalendarDaily, CalendarWeekly)
        ):
            raise TypeError("cadence must be a supported assistant schedule cadence")
        if not isinstance(self.time_zone, TimeZoneId):
            raise TypeError("time_zone must be a TimeZoneId")
        if not isinstance(self.misfire_policy, MisfirePolicy):
            raise TypeError("misfire_policy must be a MisfirePolicy")

        trigger_reason = _normalize_required_text(
            self.trigger_reason,
            field="trigger_reason",
            maximum=_MAX_TRIGGER_REASON_LENGTH,
        )
        instruction = _normalize_required_text(
            self.instruction,
            field="instruction",
            maximum=_MAX_INSTRUCTION_LENGTH,
        )
        context_snapshot = _normalize_optional_text(
            self.context_snapshot,
            field="context_snapshot",
            maximum=_MAX_CONTEXT_SNAPSHOT_LENGTH,
        )
        next_run_at = ensure_utc(self.next_run_at)
        created_at = ensure_utc(self.created_at)
        if next_run_at < created_at:
            raise ValueError("next_run_at cannot be earlier than created_at")

        if isinstance(self.cadence, (CalendarDaily, CalendarWeekly)):
            if self.cadence.time_zone != self.time_zone:
                raise ValueError(
                    "Calendar cadence and schedule must use the same time zone"
                )
            occurrence_date = _require_calendar_cursor(
                local_time=self.cadence.local_time,
                time_zone=self.cadence.time_zone,
                current=next_run_at,
            )
            if (
                isinstance(self.cadence, CalendarWeekly)
                and occurrence_date.isoweekday() not in self.cadence.weekdays
            ):
                raise ValueError("next_run_at is outside the configured weekdays")

        if self.misfire_grace is not None:
            if not isinstance(self.misfire_grace, timedelta):
                raise TypeError("misfire_grace must be a timedelta or None")
            if self.misfire_grace <= timedelta():
                raise ValueError("misfire_grace must be positive")
        if self.misfire_policy is MisfirePolicy.SKIP and self.misfire_grace is None:
            raise ValueError("SKIP misfire policy requires misfire_grace")

        object.__setattr__(self, "trigger_reason", trigger_reason)
        object.__setattr__(self, "instruction", instruction)
        object.__setattr__(self, "context_snapshot", context_snapshot)
        object.__setattr__(self, "next_run_at", next_run_at)
        object.__setattr__(self, "created_at", created_at)

    def next_occurrence(self, *, after: datetime) -> datetime | None:
        """@brief 计算当前发生项之后的下一发生项 / Compute the occurrence following the current one.

        @param after 严格 UTC 下界，通常为 worker 当前时刻 / Exclusive UTC lower bound, normally the worker's current instant.
        @return 首个严格晚于 after 的发生项；一次性任务耗尽后为 None / First occurrence strictly later than after, or None after a one-shot is exhausted.
        """

        return next_occurrence(self.cadence, current=self.next_run_at, after=after)


@dataclass(frozen=True, slots=True)
class ScheduleClaim:
    """@brief 带 fencing token 与租约的已领取助手计划 / Claimed assistant schedule carrying a fencing token and lease.

    @param schedule 已领取计划 / Claimed schedule.
    @param attempt_count 当前 occurrence 的执行尝试序号 / Execution-attempt ordinal for the current occurrence.
    @param token 本次 claim 的 UUID fencing token / UUID fencing token unique to this claim.
    @param claimed_at 原子领取瞬间 / Instant of atomic claim.
    @param lease_expires_at 独占租约到期瞬间 / Exclusive lease-expiration instant.
    """

    schedule: ScheduledAssistantTurn
    attempt_count: int
    token: UUID
    claimed_at: datetime
    lease_expires_at: datetime

    def __post_init__(self) -> None:
        """@brief 规范 UTC 时间并校验 claim 不变量 / Normalize UTC timestamps and validate claim invariants.

        @return None / None.
        @raise TypeError schedule 或 token 类型错误时抛出 / Raised for invalid schedule or token types.
        @raise ValueError token、到期条件或租约顺序无效时抛出 / Raised for an invalid token, due condition, or lease chronology.
        """

        if not isinstance(self.schedule, ScheduledAssistantTurn):
            raise TypeError("schedule must be a ScheduledAssistantTurn")
        _require_positive_int(self.attempt_count, "attempt_count")
        if not isinstance(self.token, UUID):
            raise TypeError("token must be a UUID")
        if self.token.int == 0:
            raise ValueError("Schedule claim token cannot be the nil UUID")
        claimed_at = ensure_utc(self.claimed_at)
        lease_expires_at = ensure_utc(self.lease_expires_at)
        if claimed_at < self.schedule.next_run_at:
            raise ValueError("A schedule cannot be claimed before it is due")
        if lease_expires_at <= claimed_at:
            raise ValueError("Schedule claim lease must expire after claimed_at")
        object.__setattr__(self, "claimed_at", claimed_at)
        object.__setattr__(self, "lease_expires_at", lease_expires_at)


_TERMINAL_SCHEDULE_STATUSES = frozenset(
    {
        ScheduleStatus.COMPLETED,
        ScheduleStatus.CANCELLED,
        ScheduleStatus.EXPIRED,
        ScheduleStatus.FAILED_FINAL,
    }
)
"""@brief 必须携带 terminal_at 的生命周期终态 / Lifecycle terminal states that require terminal_at."""


@dataclass(frozen=True, slots=True)
class ScheduleSnapshot:
    """@brief 面向查询的助手计划生命周期快照 / Query-facing lifecycle snapshot of an assistant schedule.

    @param schedule 已校验计划定义 / Validated schedule definition.
    @param status 当前持久化状态 / Current persisted state.
    @param attempt_count 已开始的执行尝试数 / Number of execution attempts started.
    @param last_accepted_for 最近被接受的计划发生项 / Most recent scheduled occurrence accepted for execution.
    @param last_accepted_at 最近一次原子接受瞬间 / Most recent atomic acceptance instant.
    @param last_error 最近错误摘要 / Latest error summary.
    @param terminal_at 进入终态的瞬间 / Instant at which the schedule entered a terminal state.
    """

    schedule: ScheduledAssistantTurn
    status: ScheduleStatus
    attempt_count: int
    last_accepted_for: datetime | None
    last_accepted_at: datetime | None
    last_error: str | None
    terminal_at: datetime | None

    def __post_init__(self) -> None:
        """@brief 规范快照并拒绝不可能的生命周期组合 / Normalize the snapshot and reject impossible lifecycle combinations.

        @return None / None.
        @raise TypeError 字段类型错误时抛出 / Raised for invalid field types.
        @raise ValueError attempt、acceptance 或 terminal 状态不一致时抛出 / Raised for inconsistent attempt, acceptance, or terminal state.
        """

        if not isinstance(self.schedule, ScheduledAssistantTurn):
            raise TypeError("schedule must be a ScheduledAssistantTurn")
        if not isinstance(self.status, ScheduleStatus):
            raise TypeError("status must be a ScheduleStatus")
        if not _is_int(self.attempt_count):
            raise TypeError("attempt_count must be an integer")
        if self.attempt_count < 0:
            raise ValueError("attempt_count cannot be negative")

        accepted_for = _normalize_optional_instant(self.last_accepted_for)
        accepted_at = _normalize_optional_instant(self.last_accepted_at)
        if (accepted_for is None) != (accepted_at is None):
            raise ValueError(
                "last_accepted_for and last_accepted_at must be present together"
            )
        if accepted_for is not None and accepted_at is not None:
            if accepted_for < self.schedule.created_at:
                raise ValueError("last_accepted_for cannot predate schedule creation")
            if accepted_at < accepted_for:
                raise ValueError(
                    "last_accepted_at cannot predate its scheduled occurrence"
                )

        terminal_at = _normalize_optional_instant(self.terminal_at)
        is_terminal = self.status in _TERMINAL_SCHEDULE_STATUSES
        if is_terminal != (terminal_at is not None):
            raise ValueError(
                "terminal_at must be present exactly for terminal statuses"
            )
        if terminal_at is not None:
            if terminal_at < self.schedule.created_at:
                raise ValueError("terminal_at cannot predate schedule creation")
            if accepted_at is not None and terminal_at < accepted_at:
                raise ValueError("terminal_at cannot predate the latest acceptance")

        last_error = _normalize_optional_text(
            self.last_error,
            field="last_error",
            maximum=_MAX_ERROR_LENGTH,
        )
        object.__setattr__(self, "last_accepted_for", accepted_for)
        object.__setattr__(self, "last_accepted_at", accepted_at)
        object.__setattr__(self, "last_error", last_error)
        object.__setattr__(self, "terminal_at", terminal_at)


def next_occurrence(
    cadence: Cadence,
    *,
    current: datetime,
    after: datetime,
) -> datetime | None:
    """@brief 以 cadence 纯算法查找严格晚于基准的发生项 / Find an occurrence strictly after a reference instant using a pure cadence algorithm.

    @param cadence recurrence 规则 / Recurrence rule.
    @param current 当前未消费发生项 / Current unconsumed occurrence.
    @param after 严格下界 / Exclusive lower bound.
    @return 首个合格 UTC aware 发生项；耗尽时为 None / First eligible UTC-aware occurrence, or None when exhausted.
    @raise TypeError cadence 不受支持时抛出 / Raised for an unsupported cadence.
    """

    if not isinstance(cadence, (OneShot, FixedInterval, CalendarDaily, CalendarWeekly)):
        raise TypeError("Unsupported assistant schedule cadence")
    return cadence.next_after(current=current, after=after)


def _normalize_occurrence_inputs(
    current: datetime, after: datetime
) -> tuple[datetime, datetime]:
    """@brief 规范 recurrence 算法的两个 UTC 输入 / Normalize both UTC inputs of a recurrence algorithm.

    @param current 当前计划发生项 / Current scheduled occurrence.
    @param after 严格下界 / Exclusive lower bound.
    @return ``(current_utc, after_utc)`` / ``(current_utc, after_utc)``.
    """

    return ensure_utc(current), ensure_utc(after)


def _validate_calendar_rule(
    local_time: time, time_zone: TimeZoneId, interval: int
) -> None:
    """@brief 校验 calendar cadence 的公共字段 / Validate fields shared by calendar cadences.

    @param local_time naive 本地墙钟时间 / Naive local wall-clock time.
    @param time_zone IANA 时区 / IANA time zone.
    @param interval 正整数周期 / Positive integer interval.
    @return None / None.
    @raise TypeError 字段类型错误时抛出 / Raised for invalid field types.
    @raise ValueError local_time 带时区或 interval 非正时抛出 / Raised for an aware local_time or non-positive interval.
    """

    if not isinstance(local_time, time):
        raise TypeError("Calendar local_time must be a datetime.time")
    if local_time.tzinfo is not None:
        raise ValueError("Calendar local_time must be timezone-naive")
    if not isinstance(time_zone, TimeZoneId):
        raise TypeError("Calendar time_zone must be a TimeZoneId")
    _require_positive_int(interval, "interval")


def _require_calendar_cursor(
    *,
    local_time: time,
    time_zone: TimeZoneId,
    current: datetime,
) -> date:
    """@brief 验证 calendar cursor 恰为规则生成的发生项 / Verify that a calendar cursor is an occurrence generated by its rule.

    @param local_time 规则的本地墙钟时间 / Rule's local wall-clock time.
    @param time_zone 规则的 IANA 时区 / Rule's IANA time zone.
    @param current 已规范为 UTC 的 cursor / Cursor already normalized to UTC.
    @return cursor 的本地日期 / Cursor's local date.
    @raise ValueError cursor 与 calendar 规则不对齐时抛出 / Raised when the cursor is not aligned with the calendar rule.
    """

    local_date = time_zone.localize(current).date()
    resolved = _resolve_calendar_date(local_date, local_time, time_zone)
    if resolved != current:
        raise ValueError("Current occurrence is not aligned with the calendar rule")
    return local_date


def _resolve_calendar_date(
    local_date: date, local_time: time, time_zone: TimeZoneId
) -> datetime:
    """@brief 以统一 DST policy 解析日期与墙钟时间 / Resolve a date and wall-clock time with the shared DST policy.

    @param local_date 本地日期 / Local date.
    @param local_time naive 本地墙钟时间 / Naive local wall-clock time.
    @param time_zone IANA 时区 / IANA time zone.
    @return 规范 UTC aware 瞬间 / Normalized UTC-aware instant.
    """

    local_value = datetime.combine(local_date, local_time)
    return time_zone.resolve_calendar_occurrence(local_value)


def _normalize_optional_instant(value: datetime | None) -> datetime | None:
    """@brief 规范可选 UTC 瞬间 / Normalize an optional UTC instant.

    @param value 可选 aware datetime / Optional aware datetime.
    @return 规范 UTC datetime 或 None / Normalized UTC datetime or None.
    """

    return None if value is None else ensure_utc(value)


def _normalize_required_text(value: str, *, field: str, maximum: int) -> str:
    """@brief 规范有界必填文本 / Normalize bounded required text.

    @param value 输入文本 / Input text.
    @param field 错误消息字段名 / Field name for error messages.
    @param maximum 最大字符数 / Maximum character count.
    @return 去除两端空白的文本 / Text stripped of surrounding whitespace.
    @raise TypeError value 不是 str 时抛出 / Raised when value is not a string.
    @raise ValueError 文本为空或过长时抛出 / Raised when text is empty or oversized.
    """

    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} cannot be empty")
    if len(normalized) > maximum:
        raise ValueError(f"{field} cannot exceed {maximum} characters")
    return normalized


def _normalize_optional_text(
    value: str | None, *, field: str, maximum: int
) -> str | None:
    """@brief 规范有界可选文本 / Normalize bounded optional text.

    @param value 输入文本或 None / Input text or None.
    @param field 错误消息字段名 / Field name for error messages.
    @param maximum 最大字符数 / Maximum character count.
    @return 去除两端空白的文本；空白输入折叠为 None / Stripped text, with blank input collapsed to None.
    @raise TypeError value 既非 str 也非 None 时抛出 / Raised when value is neither a string nor None.
    @raise ValueError 文本过长时抛出 / Raised when text is oversized.
    """

    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string or None")
    normalized = value.strip()
    if len(normalized) > maximum:
        raise ValueError(f"{field} cannot exceed {maximum} characters")
    return normalized or None


def _require_positive_int(value: int, field: str) -> None:
    """@brief 强制领域整数严格为正且排除 bool / Require a strictly positive domain integer while excluding bool.

    @param value 输入值 / Input value.
    @param field 错误消息字段名 / Field name for error messages.
    @return None / None.
    @raise TypeError value 不是纯 integer 时抛出 / Raised when value is not a plain integer.
    @raise ValueError value 非正时抛出 / Raised when value is not positive.
    """

    if not _is_int(value):
        raise TypeError(f"{field} must be an integer")
    if value < 1:
        raise ValueError(f"{field} must be positive")


def _is_int(value: object) -> bool:
    """@brief 判断值是否为非 bool 整数 / Test whether a value is an integer but not a boolean.

    @param value 任意值 / Arbitrary value.
    @return 非 bool int 时为 True / True for an int that is not bool.
    """

    return isinstance(value, int) and not isinstance(value, bool)


__all__ = [
    "Cadence",
    "CalendarDaily",
    "CalendarWeekly",
    "FixedInterval",
    "MisfirePolicy",
    "OneShot",
    "ScheduleClaim",
    "ScheduleSnapshot",
    "ScheduleStatus",
    "ScheduleTarget",
    "ScheduledAssistantTurn",
    "StaleScheduleClaimError",
    "next_occurrence",
]
