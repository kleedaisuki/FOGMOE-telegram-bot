"""@brief Dreaming 应用端口与 claim 值对象 / Dreaming application ports and claim values."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from fogmoe_bot.domain.user_profile.models import (
    DreamId,
    ProfileDocument,
    ProfileEvidence,
    ProfileMetadata,
    ProfilePatch,
    UserProfileSnapshot,
)


@dataclass(frozen=True, slots=True)
class DreamClaim:
    """@brief 带 lease/fencing 的冻结 Dreaming 输入 / Frozen Dreaming input with lease and fencing.

    @param dream_id 工作项 ID / Work-item identifier.
    @param owner_user_id Profile owner / Profile owner.
    @param base_revision 形成工作项时的 Profile revision，0 表示空 / Profile revision at scheduling; zero means empty.
    @param base_observed_through_event_id 形成工作项时的 evidence watermark / Evidence watermark at scheduling.
    @param through_event_id 本批上界 / Batch upper bound.
    @param current_document 当前 Profile 文档 / Current Profile document.
    @param evidence 冻结且按 event_id 排序的证据 / Frozen evidence ordered by event ID.
    @param metadata 本批最新 acceptance 元信息 / Latest acceptance metadata in the batch.
    @param claim_token fencing token / Fencing token.
    @param attempt_count 已开始尝试次数 / Number of started attempts.
    """

    dream_id: DreamId
    owner_user_id: int
    base_revision: int
    base_observed_through_event_id: int
    through_event_id: int
    current_document: ProfileDocument
    evidence: tuple[ProfileEvidence, ...]
    metadata: ProfileMetadata
    claim_token: UUID
    attempt_count: int

    def __post_init__(self) -> None:
        """@brief 校验 claim 的用户、range 与顺序 / Validate claim owner, range, and ordering.

        @return None / None.
        @raise ValueError claim 不一致 / Inconsistent claim.
        """

        if isinstance(self.owner_user_id, bool) or self.owner_user_id <= 0:
            raise ValueError("Dream owner_user_id must be positive")
        if isinstance(self.base_revision, bool) or self.base_revision < 0:
            raise ValueError("Dream base_revision cannot be negative")
        if (
            isinstance(self.base_observed_through_event_id, bool)
            or self.base_observed_through_event_id < 0
        ):
            raise ValueError("Dream base watermark cannot be negative")
        if self.through_event_id <= self.base_observed_through_event_id:
            raise ValueError("Dream through_event_id must advance the watermark")
        if isinstance(self.attempt_count, bool) or self.attempt_count < 1:
            raise ValueError("Dream attempt_count must be positive")
        if not self.evidence:
            raise ValueError("Dream claim requires evidence")
        event_ids = tuple(item.event_id for item in self.evidence)
        if event_ids != tuple(sorted(event_ids)) or len(set(event_ids)) != len(
            event_ids
        ):
            raise ValueError("Dream evidence must be strictly event ordered")
        if event_ids[-1] != self.through_event_id:
            raise ValueError("Dream evidence does not reach its upper bound")
        if any(item.owner_user_id != self.owner_user_id for item in self.evidence):
            raise ValueError("Dream evidence crossed a user boundary")


@dataclass(frozen=True, slots=True)
class DreamResult:
    """@brief 模型产生的 patch 与 route provenance / Model patch with route provenance.

    @param patch 结构化 Profile patch / Structured Profile patch.
    @param route_key 实际 provider/model route / Actual provider/model route.
    @param prompt_version Dreaming prompt 版本 / Dreaming-prompt version.
    """

    patch: ProfilePatch
    route_key: str
    prompt_version: int

    def __post_init__(self) -> None:
        """@brief 校验生成 provenance / Validate generation provenance.

        @return None / None.
        @raise ValueError route 或版本非法 / Invalid route or version.
        """

        if not self.route_key.strip() or len(self.route_key) > 300:
            raise ValueError("Dream route_key must contain 1-300 characters")
        if isinstance(self.prompt_version, bool) or self.prompt_version <= 0:
            raise ValueError("Dream prompt_version must be positive")


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


class StaleDreamClaimError(RuntimeError):
    """@brief Dream claim 已被回收或 Profile 已变更 / Dream claim was recovered or its Profile changed."""


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
        """@brief 幂等投影一条来源证据 / Idempotently project one source evidence item."""

        ...

    async def enqueue_eligible(
        self,
        *,
        now: datetime,
        limit: int,
        max_events_per_dream: int,
        max_evidence_chars: int,
    ) -> int:
        """@brief 为到期且存在新证据的 Profile 建立有界冻结 job / Enqueue bounded frozen jobs for due Profiles with new evidence."""

        ...

    async def claim_dreams(
        self,
        *,
        now: datetime,
        limit: int,
        lease_for: timedelta,
    ) -> Sequence[DreamClaim]:
        """@brief 领取 ready jobs / Claim ready jobs."""

        ...

    async def complete_dream(
        self,
        claim: DreamClaim,
        result: DreamResult,
        *,
        document: ProfileDocument,
        completed_at: datetime,
        refresh_after: timedelta,
    ) -> UserProfileSnapshot | None:
        """@brief fenced 提交 reducer 结果并推进 evidence watermark / Fenced commit of the reducer result and evidence watermark."""

        ...

    async def retry_dream(
        self,
        claim: DreamClaim,
        *,
        failed_at: datetime,
        retry_at: datetime,
        error: str,
    ) -> None:
        """@brief fenced 安排重试 / Schedule a fenced retry."""

        ...

    async def fail_dream(
        self,
        claim: DreamClaim,
        *,
        failed_at: datetime,
        error: str,
    ) -> None:
        """@brief fenced 终结损坏 job / Finally fail a corrupt job."""

        ...

    async def recover_expired_dream_leases(self, *, now: datetime) -> int:
        """@brief 回收 crash/cancellation 遗留 lease / Recover leases left by crashes or cancellation."""

        ...


class DreamingModel(Protocol):
    """@brief 无工具、无 mutation 的 Profile patch 模型 / Tool-free, mutation-free Profile patch model."""

    async def dream(self, claim: DreamClaim) -> DreamResult:
        """@brief 从当前 Profile 与新证据提出 patch / Propose a patch from the current Profile and new evidence."""

        ...


__all__ = [
    "DreamClaim",
    "DreamResult",
    "DreamingModel",
    "ProfileEvidenceSource",
    "ProfileReader",
    "ProfileStore",
    "RetryableDreamingError",
    "StaleDreamClaimError",
]
