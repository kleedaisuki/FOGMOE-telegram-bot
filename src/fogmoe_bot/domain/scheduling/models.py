"""@brief 后台调度领域模型 / Background-scheduling domain models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Generic, TypeVar


class ScheduleStatus(str, Enum):
    """@brief 调度任务持久化状态 / Persisted schedule status."""

    PENDING = "pending"
    EXECUTING = "executing"
    EXECUTED = "executed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class ScheduleCreationBlockReason(str, Enum):
    """@brief 创建任务受阻原因 / Reasons schedule creation can be blocked."""

    PENDING_LIMIT = "pending_limit"
    TOTAL_LIMIT = "total_limit"


class RecurrenceUnit(str, Enum):
    """@brief 支持的重复周期单位 / Supported recurrence units."""

    NONE = "none"
    MINUTE = "minute"
    HOUR = "hour"
    DAY = "day"


@dataclass(frozen=True, slots=True)
class JobKind:
    """@brief 可横向扩展的任务类型标识 / Extensible scheduled-job kind.

    @param value 稳定、可持久化的任务类型名 / Stable persistable job-kind name.
    """

    value: str

    def __post_init__(self) -> None:
        """@brief 校验任务类型名 / Validate the job-kind name.

        @return None / None.
        @raise ValueError 类型名为空时抛出 / Raised when the kind is empty.
        """

        normalized = self.value.strip().lower()
        if not normalized:
            raise ValueError("Job kind cannot be empty")
        object.__setattr__(self, "value", normalized)


PROMPT_JOB_KIND = JobKind("prompt.turn")
"""@brief 定时 Prompt 回合任务类型 / Scheduled prompt-turn job kind."""


@dataclass(frozen=True, slots=True)
class MaintenanceTask:
    """@brief 进程内周期维护任务定义 / In-process periodic maintenance-task definition.

    @param kind 稳定的维护任务类型 / Stable maintenance-task kind.
    @param interval 周期执行间隔 / Recurring execution interval.
    @param initial_delay 进程启动后的首次延迟 / First-run delay after process startup.
    """

    kind: JobKind
    interval: timedelta
    initial_delay: timedelta = timedelta()

    def __post_init__(self) -> None:
        """@brief 校验维护任务时间边界 / Validate maintenance-task timing boundaries.

        @return None / None.
        @raise ValueError 周期非正或首次延迟为负时抛出 / Raised for non-positive intervals or negative initial delays.
        """

        if self.interval <= timedelta():
            raise ValueError("Maintenance interval must be positive")
        if self.initial_delay < timedelta():
            raise ValueError("Maintenance initial delay cannot be negative")


@dataclass(frozen=True, slots=True)
class PromptJobPayload:
    """@brief 定时 Prompt 回合输入 / Scheduled prompt-turn input.

    @param trigger_reason 触发原因 / Trigger reason.
    @param context_text 创建任务时保存的上下文 / Context captured when scheduled.
    @param instruction 执行指令 / Execution instruction.
    """

    trigger_reason: str
    context_text: str | None
    instruction: str


@dataclass(frozen=True, slots=True)
class Recurrence:
    """@brief 调度重复规则 / Schedule recurrence rule.

    @param unit 重复单位 / Recurrence unit.
    @param interval 每次跨越的单位数量 / Number of units per occurrence.
    """

    unit: RecurrenceUnit = RecurrenceUnit.NONE
    interval: int = 1

    def __post_init__(self) -> None:
        """@brief 维护重复规则不变量 / Enforce recurrence invariants.

        @return None / None.
        @raise ValueError interval 小于 1 时抛出 / Raised when interval is below one.
        """

        if self.interval < 1:
            raise ValueError("Recurrence interval must be at least one")
        if self.unit is RecurrenceUnit.NONE and self.interval != 1:
            object.__setattr__(self, "interval", 1)

    @classmethod
    def from_storage(cls, unit: object, interval: object) -> "Recurrence":
        """@brief 从持久化值恢复重复规则 / Restore a recurrence from storage values.

        @param unit 数据库存储的周期单位 / Stored recurrence unit.
        @param interval 数据库存储的周期间隔 / Stored recurrence interval.
        @return 已校验的重复规则 / Validated recurrence rule.
        """

        if isinstance(unit, bytes):
            unit = unit.decode("utf-8", errors="strict")
        normalized_unit = str(unit or RecurrenceUnit.NONE.value).strip().lower()
        try:
            recurrence_unit = RecurrenceUnit(normalized_unit)
        except ValueError as exc:
            raise ValueError(f"Unsupported recurrence unit: {normalized_unit}") from exc
        try:
            normalized_interval = int(interval or 1)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid recurrence interval: {interval}") from exc
        return cls(recurrence_unit, normalized_interval)

    def duration(self) -> timedelta | None:
        """@brief 将规则转换为固定时长 / Convert the rule to a fixed duration.

        @return 一次性任务返回 None，否则返回固定时长 / None for one-shot jobs, otherwise a duration.
        """

        seconds_per_unit = {
            RecurrenceUnit.MINUTE: 60,
            RecurrenceUnit.HOUR: 60 * 60,
            RecurrenceUnit.DAY: 24 * 60 * 60,
        }
        seconds = seconds_per_unit.get(self.unit)
        return None if seconds is None else timedelta(seconds=seconds * self.interval)

    def next_after(self, previous_run_at: datetime, now: datetime) -> datetime | None:
        """@brief 计算严格晚于 now 的下一次运行时刻 / Compute the next run strictly after now.

        @param previous_run_at 当前周期的计划运行时刻 / Scheduled time of the current occurrence.
        @param now 计算基准时刻 / Reference time.
        @return 一次性任务返回 None，否则返回下一次 UTC 时刻 / None for one-shot jobs, otherwise the next UTC time.
        """

        duration = self.duration()
        if duration is None:
            return None
        previous = ensure_utc(previous_run_at)
        current = ensure_utc(now)
        if previous > current:
            return previous
        elapsed = current - previous
        skipped_intervals = elapsed // duration + 1
        return previous + duration * skipped_intervals


PayloadT = TypeVar("PayloadT")
"""@brief 调度载荷泛型 / Scheduled payload generic type."""


@dataclass(frozen=True, slots=True)
class ScheduledJob(Generic[PayloadT]):
    """@brief 已领取的类型化调度任务 / Typed claimed scheduled job.

    @param schedule_id 任务 ID / Schedule identifier.
    @param owner_id 任务所有者 ID / Job owner identifier.
    @param kind 可扩展任务类型 / Extensible job kind.
    @param run_at 本次计划运行时刻 / Scheduled occurrence time.
    @param created_at 任务创建时刻 / Schedule creation time.
    @param recurrence 重复规则 / Recurrence rule.
    @param payload 业务处理器载荷 / Business-handler payload.
    """

    schedule_id: int
    owner_id: int
    kind: JobKind
    run_at: datetime
    created_at: datetime | None
    recurrence: Recurrence
    payload: PayloadT


@dataclass(frozen=True, slots=True)
class ScheduleClaim(Generic[PayloadT]):
    """@brief 带防陈旧 token 的任务领取凭证 / Claimed job with a stale-worker fencing token.

    @param job 已领取任务 / Claimed scheduled job.
    @param token 本次领取的不可复用 token / Non-reusable token for this claim.
    @param lease_expires_at 租约失效时刻 / Lease expiration time.
    """

    job: ScheduledJob[PayloadT]
    token: str
    lease_expires_at: datetime

    def __post_init__(self) -> None:
        """@brief 校验领取凭证 / Validate the claim.

        @return None / None.
        @raise ValueError token 为空时抛出 / Raised when the token is empty.
        """

        if not self.token.strip():
            raise ValueError("Schedule claim token cannot be empty")
        object.__setattr__(self, "lease_expires_at", ensure_utc(self.lease_expires_at))


@dataclass(frozen=True, slots=True)
class ScheduleSnapshot(Generic[PayloadT]):
    """@brief 面向查询的调度任务快照 / Query-facing schedule snapshot.

    @param job 任务定义 / Scheduled job definition.
    @param status 当前状态 / Current status.
    @param executed_at 最近完成时刻 / Most recent completion time.
    @param last_run_at 最近计划运行时刻 / Most recent scheduled occurrence.
    @param error 最近错误 / Most recent error.
    """

    job: ScheduledJob[PayloadT]
    status: ScheduleStatus
    executed_at: datetime | None
    last_run_at: datetime | None
    error: str | None


@dataclass(frozen=True, slots=True)
class ScheduleCreationResult:
    """@brief 创建或替换任务的领域结果 / Domain result of creating or replacing a schedule.

    @param schedule_id 创建或替换的任务 ID / Created or replaced schedule identifier.
    @param created_at 创建时刻 / Creation time.
    @param replaced 是否替换历史任务 / Whether a historical job was replaced.
    @param blocked_reason 受阻原因 / Blocking reason.
    """

    schedule_id: int | None
    created_at: datetime | None
    replaced: bool
    blocked_reason: ScheduleCreationBlockReason | None


def ensure_utc(value: datetime) -> datetime:
    """@brief 将 datetime 规范为 UTC aware 值 / Normalize a datetime to UTC-aware form.

    @param value 输入时间；naive 值按既有数据库约定解释为 UTC / Input time; naive values follow the legacy UTC convention.
    @return UTC aware datetime / UTC-aware datetime.
    """

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def to_storage_datetime(value: datetime) -> datetime:
    """@brief 转换为兼容旧 TIMESTAMP 列的 UTC naive 值 / Convert to legacy UTC-naive storage form.

    @param value UTC aware 或 naive 时间 / UTC-aware or naive datetime.
    @return UTC naive datetime / UTC-naive datetime.
    """

    return ensure_utc(value).replace(tzinfo=None)
