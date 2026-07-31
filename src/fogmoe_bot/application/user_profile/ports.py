"""@brief Dreaming 应用端口 / Dreaming application ports."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from fogmoe_bot.domain.user_profile import (
    DreamClaim,
    DreamCompletionPrepared,
    DreamFailedFinalDecision,
    DreamResult,
    DreamRetryScheduled,
    ProfileEvidence,
    UserProfileSnapshot,
)


@dataclass(frozen=True, slots=True)
class DreamProfileUpdated:
    """@brief Dream 提交建立新 Profile revision 的显式回执 / Explicit receipt for a Dream commit that created a Profile revision.

    @param snapshot 已从持久化后态映射并校验的新 revision / New revision mapped and validated from persisted post-state.
    """

    snapshot: UserProfileSnapshot

    def __post_init__(self) -> None:
        """@brief 校验 changed 回执 / Validate the changed receipt.

        @return None / None.
        @raise TypeError snapshot 类型非法 / Invalid snapshot type.
        """

        if not isinstance(self.snapshot, UserProfileSnapshot):
            raise TypeError("Updated Dream receipt requires a UserProfileSnapshot")


@dataclass(frozen=True, slots=True)
class DreamProfileUnchanged:
    """@brief Dream 提交仅推进调度 head cursor 的显式 NO_OP 回执 / Explicit NO_OP receipt for a Dream commit that advanced only the scheduling head cursor.

    @param owner_user_id Profile owner / Profile owner.
    @param retained_revision 保留的 immutable Profile revision；零表示尚无 revision /
        Retained immutable Profile revision; zero means no revision exists yet.
    @param scheduler_head_event_id 提交后 scheduler head cursor / Scheduler head cursor after commit.
    @note ``UserProfileSnapshot.observed_through_event_id`` 属于当前 immutable revision 的
        provenance watermark，并不是此处可继续前进的 scheduler head cursor。/
        ``UserProfileSnapshot.observed_through_event_id`` is provenance for the current
        immutable revision, not the independently advancing scheduler head cursor recorded here.
    """

    owner_user_id: int
    retained_revision: int
    scheduler_head_event_id: int

    def __post_init__(self) -> None:
        """@brief 校验 NO_OP 回执 / Validate the NO_OP receipt.

        @return None / None.
        @raise ValueError owner、revision 或 cursor 非法 / Invalid owner, revision, or cursor.
        """

        if (
            isinstance(self.owner_user_id, bool)
            or not isinstance(self.owner_user_id, int)
            or self.owner_user_id <= 0
        ):
            raise ValueError("NO_OP Dream receipt owner_user_id must be positive")
        if (
            isinstance(self.retained_revision, bool)
            or not isinstance(self.retained_revision, int)
            or self.retained_revision < 0
        ):
            raise ValueError("NO_OP Dream receipt revision cannot be negative")
        if (
            isinstance(self.scheduler_head_event_id, bool)
            or not isinstance(self.scheduler_head_event_id, int)
            or self.scheduler_head_event_id <= 0
        ):
            raise ValueError("NO_OP Dream receipt cursor must be positive")


type DreamCommitReceipt = DreamProfileUpdated | DreamProfileUnchanged
"""@brief Dream/Profile 原子提交的穷尽回执和 / Exhaustive receipt sum for an atomic Dream/Profile commit."""


class RetryableDreamingError(RuntimeError):
    """@brief 可重试的模型或网络失败 / Retryable model or network failure.

    @param retry_after provider 建议的最小等待 / Provider-suggested minimum delay.
    """

    retry_after: timedelta | None

    def __init__(self, message: str, *, retry_after: timedelta | None = None) -> None:
        """@brief 创建可重试错误 / Create a retryable error.

        @param message 安全错误文本 / Safe error text.
        @param retry_after 可选最小等待 / Optional minimum delay.
        @return None / None.
        """

        if retry_after is not None and retry_after <= timedelta():
            raise ValueError("Dream retry_after must be positive")
        super().__init__(message)
        self.retry_after = retry_after


class ProfileEvidenceSource(Protocol):
    """@brief 未投影 Conversation Turn 来源 / Source of unprojected Conversation turns."""

    async def read_unprojected(self, *, limit: int) -> Sequence[ProfileEvidence]:
        """@brief 读取尚未进入 Profile evidence log 的 Turn / Read turns absent from the Profile evidence log.

        @param limit 最大 Turn 数 / Maximum turns.
        @return event_id 为 0 的来源证据 / Source evidence with event_id zero.
        """

        ...


class ProfileReader(Protocol):
    """@brief acceptance 所需的窄 Profile 读取端口 / Narrow Profile read port needed at acceptance."""

    async def read_profile(self, user_id: int) -> UserProfileSnapshot | None:
        """@brief 读取最新 committed Profile snapshot / Read the latest committed Profile snapshot.

        @param user_id Profile owner / Profile owner.
        @return snapshot；尚未形成则 None / Snapshot, or None before materialization.
        @note snapshot watermark 是当前 immutable revision 的 provenance；NO_OP Dream 可在
            不建立 revision 的前提下推进内部 scheduler head cursor。/
            The snapshot watermark is provenance for the current immutable revision; a NO_OP
            Dream may advance the internal scheduler head cursor without creating a revision.
        """

        ...


class ProfileStore(ProfileReader, Protocol):
    """@brief Profile evidence、job 与 revision 持久化 / Persistence for Profile evidence, jobs, and revisions."""

    async def project_evidence(
        self,
        evidence: ProfileEvidence,
        *,
        projected_at: datetime,
    ) -> None:
        """@brief 幂等投影一条来源证据 / Idempotently project one source evidence item.

        @param evidence 待投影来源证据 / Source evidence to project.
        @param projected_at 投影时间 / Projection time.
        @return None / None.
        """

        ...

    async def enqueue_eligible(
        self,
        *,
        now: datetime,
        limit: int,
        max_events_per_dream: int,
        max_evidence_chars: int,
    ) -> int:
        """@brief 为到期且存在新证据的 Profile 建立有界冻结 job / Enqueue bounded frozen jobs for due Profiles with new evidence.

        @param now 调度时间 / Scheduling time.
        @param limit 单轮最大 job 数 / Maximum jobs per pass.
        @param max_events_per_dream 单个 Dream 最大 evidence 数 / Maximum evidence items per Dream.
        @param max_evidence_chars 单个 Dream 最大文本字符数 / Maximum text characters per Dream.
        @return 实际建立的 job 数 / Number of jobs actually enqueued.
        """

        ...

    async def claim_dreams(
        self,
        *,
        now: datetime,
        limit: int,
        lease_for: timedelta,
    ) -> Sequence[DreamClaim]:
        """@brief 领取 ready jobs / Claim ready jobs.

        @param now 领取时间 / Claim time.
        @param limit 单轮最大领取数 / Maximum claims per pass.
        @param lease_for ownership 租约长度 / Ownership lease duration.
        @return 带冻结生成输入的 claims / Claims carrying frozen generation inputs.
        """

        ...

    async def complete_dream(
        self,
        decision: DreamCompletionPrepared,
        *,
        refresh_after: timedelta,
    ) -> DreamCommitReceipt:
        """@brief fenced 提交已验证 Dream/Profile 决定 / Commit a validated Dream/Profile decision under fencing.

        @param decision 已纯验证且赋时的 completion / Purely validated, timestamped completion.
        @param refresh_after 无 backlog 时的刷新间隔 / Refresh interval when no backlog remains.
        @return 显式 updated 或 NO_OP durable 回执 / Explicit updated or NO_OP durable receipt.
        """

        ...

    async def retry_dream(
        self,
        decision: DreamRetryScheduled,
    ) -> None:
        """@brief fenced 持久化类型化重试 / Persist a typed retry under fencing.

        @param decision 已验证 retry 决定 / Validated retry decision.
        @return None / None.
        """

        ...

    async def fail_dream(
        self,
        decision: DreamFailedFinalDecision,
    ) -> None:
        """@brief fenced 持久化类型化终败 / Persist a typed final failure under fencing.

        @param decision 已验证 final-failure 决定 / Validated final-failure decision.
        @return None / None.
        """

        ...

    async def recover_expired_dream_leases(
        self,
        *,
        now: datetime,
        max_attempts: int,
        limit: int,
    ) -> int:
        """@brief 回收 crash/cancellation 遗留 lease / Recover leases left by crashes or cancellation.

        @param now recovery 截止时间 / Recovery cutoff time.
        @param max_attempts 包含已 crash claim 的最大尝试数 / Maximum attempts including the crashed claim.
        @param limit 单轮有界回收数 / Bounded recoveries per pass.
        @return 实际回收的 lease 数 / Number of leases actually recovered.
        """

        ...


class DreamingModel(Protocol):
    """@brief 无工具、无 mutation 的 Profile patch 模型 / Tool-free, mutation-free Profile patch model."""

    async def dream(self, claim: DreamClaim) -> DreamResult:
        """@brief 从当前 Profile 与新证据提出 patch / Propose a patch from the current Profile and new evidence.

        @param claim 带冻结 Profile/evidence 的生成 capability / Generation capability with frozen Profile and evidence.
        @return 结构化 patch 与 provenance / Structured patch and provenance.
        """

        ...


__all__ = [
    "DreamCommitReceipt",
    "DreamProfileUnchanged",
    "DreamProfileUpdated",
    "DreamingModel",
    "ProfileEvidenceSource",
    "ProfileReader",
    "ProfileStore",
    "RetryableDreamingError",
]
