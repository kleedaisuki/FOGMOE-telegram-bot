"""@brief Dreaming durable job 富领域模型 / Rich domain model for durable Dreaming jobs."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Self
from uuid import UUID, uuid4

from fogmoe_bot.domain.temporal import ensure_utc

from .models import (
    DreamId,
    ProfileDocument,
    ProfileEvidence,
    ProfileMetadata,
    ProfilePatch,
    UserProfileSnapshot,
)


class DreamActivityStatus(StrEnum):
    """@brief Dream job 的稳定持久化状态 / Stable persisted states of a Dream job."""

    PENDING = "pending"
    RETRY_WAIT = "retry_wait"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED_FINAL = "failed_final"


@dataclass(frozen=True, slots=True)
class DreamLeaseToken:
    """@brief Dream worker 的强类型 fencing token / Strongly typed fencing token for a Dream worker.

    @param value 持久化 UUID / Persisted UUID.
    """

    value: UUID

    def __post_init__(self) -> None:
        """@brief 拒绝非 UUID token / Reject a non-UUID token.

        @return None / None.
        """

        if not isinstance(self.value, UUID):
            raise TypeError("Dream lease token requires a UUID")

    @classmethod
    def new(cls) -> Self:
        """@brief 生成新 fencing token / Generate a new fencing token.

        @return 新 token / New token.
        """

        return cls(uuid4())

    @classmethod
    def parse(cls, value: UUID | str) -> Self:
        """@brief 解析数据库 token / Parse a database token.

        @param value UUID 或文本 / UUID or text.
        @return token 值对象 / Token value object.
        """

        return cls(value if isinstance(value, UUID) else UUID(str(value)))

    def __str__(self) -> str:
        """@brief 返回持久化文本 / Return persistable text.

        @return UUID 文本 / UUID text.
        """

        return str(self.value)


@dataclass(frozen=True, slots=True)
class ProfileBaseline:
    """@brief Dream 冻结的 Profile 双重 CAS 基线 / Profile dual-CAS baseline frozen by a Dream.

    @param revision 当前 Profile revision，零表示尚未 materialize / Current Profile revision; zero means not materialized.
    @param observed_through_event_id 可变 Profile-head scheduler cursor；不是 immutable revision provenance watermark / Mutable Profile-head scheduler cursor; not the immutable revision-provenance watermark.
    @note NO_OP 只推进该 scheduler cursor，不会改写已存 revision provenance / A NO_OP advances only this scheduler cursor and never rewrites persisted revision provenance.
    """

    revision: int
    observed_through_event_id: int

    def __post_init__(self) -> None:
        """@brief 校验非负双基线 / Validate the non-negative dual baseline.

        @return None / None.
        """

        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 0
        ):
            raise ValueError("Profile baseline revision cannot be negative")
        if (
            isinstance(self.observed_through_event_id, bool)
            or not isinstance(self.observed_through_event_id, int)
            or self.observed_through_event_id < 0
        ):
            raise ValueError("Profile baseline watermark cannot be negative")


@dataclass(frozen=True, slots=True)
class DreamFailure:
    """@brief 可安全持久化的 Dream 失败摘要 / Safely persistable Dream failure summary.

    @param summary 去除空白且有界的摘要 / Trimmed and bounded summary.
    """

    summary: str

    def __post_init__(self) -> None:
        """@brief 规范化失败摘要 / Normalize the failure summary.

        @return None / None.
        """

        if not isinstance(self.summary, str):
            raise TypeError("Dream failure summary must be a string")
        normalized = self.summary.strip()
        if not normalized:
            raise ValueError("Dream failure cannot be empty")
        object.__setattr__(self, "summary", normalized[:1000])


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
        """

        if not isinstance(self.patch, ProfilePatch):
            raise TypeError("Dream result requires a ProfilePatch")
        if not isinstance(self.route_key, str):
            raise TypeError("Dream route_key must be a string")
        route_key = self.route_key.strip()
        if not route_key or len(route_key) > 300:
            raise ValueError("Dream route_key must contain 1-300 characters")
        if (
            isinstance(self.prompt_version, bool)
            or not isinstance(self.prompt_version, int)
            or self.prompt_version <= 0
        ):
            raise ValueError("Dream prompt_version must be positive")
        object.__setattr__(self, "route_key", route_key)


@dataclass(frozen=True, slots=True)
class PendingDream:
    """@brief 等待首次 claim / Awaiting the initial claim.

    @param claimable_at 最早领取时刻 / Earliest claim time.
    """

    claimable_at: datetime

    def __post_init__(self) -> None:
        """@brief 规范化领取时间 / Normalize claim time.

        @return None / None.
        """

        object.__setattr__(self, "claimable_at", ensure_utc(self.claimable_at))


@dataclass(frozen=True, slots=True)
class ProcessingDream:
    """@brief worker 持有有效 fencing capability / A worker owns a valid fencing capability.

    @param token 当前 claim 的唯一 fencing token / Unique fencing token of the current claim.
    @param lease_expires_at crash recovery 可回收租约的时刻 / Time at which crash recovery may reclaim the lease.
    """

    token: DreamLeaseToken
    lease_expires_at: datetime

    def __post_init__(self) -> None:
        """@brief 校验并规范化完整租约所有权 / Validate and normalize complete lease ownership.

        @return None / None.
        """

        if not isinstance(self.token, DreamLeaseToken):
            raise TypeError("Processing Dream requires DreamLeaseToken")
        object.__setattr__(
            self,
            "lease_expires_at",
            ensure_utc(self.lease_expires_at),
        )


@dataclass(frozen=True, slots=True)
class WaitingDreamRetry:
    """@brief 失败或 lease recovery 后等待重试 / Waiting for retry after failure or lease recovery.

    @param claimable_at 下次领取时刻 / Next claim time.
    @param failure 最近失败 / Latest failure.
    """

    claimable_at: datetime
    failure: DreamFailure

    def __post_init__(self) -> None:
        """@brief 校验 retry 状态 / Validate retry state.

        @return None / None.
        """

        object.__setattr__(self, "claimable_at", ensure_utc(self.claimable_at))
        if not isinstance(self.failure, DreamFailure):
            raise TypeError("Dream retry requires a DreamFailure")


@dataclass(frozen=True, slots=True)
class CompletedDream:
    """@brief Dream 与 Profile watermark 已原子提交 / Dream and Profile watermark were atomically committed.

    @param completed_at 提交时刻 / Commit time.
    @param result 已审计模型结果 / Audited model result.
    """

    completed_at: datetime
    result: DreamResult

    def __post_init__(self) -> None:
        """@brief 校验完成状态 / Validate completion state.

        @return None / None.
        """

        object.__setattr__(self, "completed_at", ensure_utc(self.completed_at))
        if not isinstance(self.result, DreamResult):
            raise TypeError("Completed Dream requires a DreamResult")


@dataclass(frozen=True, slots=True)
class FailedDreamFinal:
    """@brief 不再自动重试的 Dream 终态 / Terminal Dream excluded from automatic retries.

    @param completed_at 终结时刻 / Finalization time.
    @param failure 最终失败 / Final failure.
    """

    completed_at: datetime
    failure: DreamFailure

    def __post_init__(self) -> None:
        """@brief 校验终败状态 / Validate final-failure state.

        @return None / None.
        """

        object.__setattr__(self, "completed_at", ensure_utc(self.completed_at))
        if not isinstance(self.failure, DreamFailure):
            raise TypeError("Failed Dream requires a DreamFailure")


type DreamActivityState = (
    PendingDream
    | ProcessingDream
    | WaitingDreamRetry
    | CompletedDream
    | FailedDreamFinal
)
"""@brief Dream 生命周期穷尽状态和 / Exhaustive Dream-lifecycle state sum."""


class InvalidDreamTransition(RuntimeError):
    """@brief Dream 聚合拒绝非法转换 / Dream aggregate rejected an invalid transition."""


class StaleDreamClaimError(RuntimeError):
    """@brief Dream capability 或 Profile 双基线已过期 / Dream capability or Profile dual baseline is stale."""


class _DreamFactorySeal:
    """@brief 限制 capability/decision 工厂仅由本模块常规调用 / Restrict ordinary capability/decision factory calls to this module.

    @note Python 模块私有性是防误用边界而非安全边界；仓储仍会对 durable truth
        重新求值 / Python module privacy prevents accidental misuse rather than providing a
        security boundary; the repository still re-evaluates durable truth.
    """


_DREAM_FACTORY_SEAL = _DreamFactorySeal()
"""@brief Dream 模块私有构造密封 / Module-private Dream construction seal."""


def _require_factory_seal(seal: object) -> None:
    """@brief 拒绝绕过公开领域转换的常规构造 / Reject ordinary construction that bypasses public domain transitions.

    @param seal 候选模块密封 / Candidate module seal.
    @return None / None.
    @raise TypeError 调用者不持有本模块唯一密封 / The caller does not hold the module's unique seal.
    """

    if seal is not _DREAM_FACTORY_SEAL:
        raise TypeError("Dream factory is private to the domain module")


@dataclass(frozen=True, slots=True, init=False)
class DreamActivityDraft:
    """@brief Profile coordinator 形成的不可变 Dream 意图 / Immutable Dream intent formed by the Profile coordinator."""

    dream_id: DreamId
    owner_user_id: int
    baseline: ProfileBaseline
    through_event_id: int
    source_count: int
    metadata: ProfileMetadata
    created_at: datetime

    def __init__(
        self,
        *,
        dream_id: DreamId,
        owner_user_id: int,
        baseline: ProfileBaseline,
        through_event_id: int,
        source_count: int,
        metadata: ProfileMetadata,
        created_at: datetime,
    ) -> None:
        """@brief 创建并校验 Dream 意图 / Create and validate a Dream intent.

        @param dream_id 持久化 Dream 标识 / Persisted Dream identity.
        @param owner_user_id Profile owner 标识 / Profile-owner identity.
        @param baseline 冻结的 revision/watermark 基线 / Frozen revision-watermark baseline.
        @param through_event_id 本批次最后 evidence 标识 / Final evidence identity in this batch.
        @param source_count 冻结 evidence 数量 / Frozen evidence count.
        @param metadata 冻结用户元信息 / Frozen user metadata.
        @param created_at Dream 建立时间 / Dream creation time.
        @return None / None.
        """

        if not isinstance(dream_id, UUID):
            raise TypeError("Dream draft requires a UUID DreamId")
        if (
            isinstance(owner_user_id, bool)
            or not isinstance(owner_user_id, int)
            or owner_user_id <= 0
        ):
            raise ValueError("Dream owner_user_id must be positive")
        if not isinstance(baseline, ProfileBaseline):
            raise TypeError("Dream draft requires a ProfileBaseline")
        if (
            isinstance(through_event_id, bool)
            or not isinstance(through_event_id, int)
            or through_event_id <= baseline.observed_through_event_id
        ):
            raise ValueError("Dream through_event_id must advance the watermark")
        if (
            isinstance(source_count, bool)
            or not isinstance(source_count, int)
            or source_count <= 0
        ):
            raise ValueError("Dream source_count must be positive")
        if not isinstance(metadata, ProfileMetadata):
            raise TypeError("Dream draft requires ProfileMetadata")
        object.__setattr__(self, "dream_id", dream_id)
        object.__setattr__(self, "owner_user_id", owner_user_id)
        object.__setattr__(self, "baseline", baseline)
        object.__setattr__(self, "through_event_id", through_event_id)
        object.__setattr__(self, "source_count", source_count)
        object.__setattr__(self, "metadata", metadata)
        object.__setattr__(self, "created_at", ensure_utc(created_at))


@dataclass(frozen=True, slots=True, init=False)
class DreamActivity:
    """@brief 拥有 claim、retry、recovery 与终态不变量的 Dream 聚合根 / Dream aggregate root owning claim, retry, recovery, and terminal invariants."""

    dream_id: DreamId
    owner_user_id: int
    baseline: ProfileBaseline
    through_event_id: int
    source_count: int
    metadata: ProfileMetadata
    created_at: datetime
    state: DreamActivityState
    version: int
    attempt_count: int
    updated_at: datetime

    def __new__(cls, *args: object, **kwargs: object) -> Self:
        """@brief 拒绝绕过工厂的构造 / Reject construction that bypasses factories.

        @return 此入口永不返回 / This entry point never returns.
        """

        del args, kwargs
        raise TypeError("Use DreamActivity.enqueue() or restore()")

    @classmethod
    def _create(
        cls,
        seal: object,
        *,
        draft: DreamActivityDraft,
        state: DreamActivityState,
        version: int,
        attempt_count: int,
        updated_at: datetime,
    ) -> Self:
        """@brief 通过唯一不变量入口创建聚合 / Create an aggregate through the sole invariant gate.

        @param seal 模块私有构造密封 / Module-private construction seal.
        @param draft 不可变 Dream 意图 / Immutable Dream intent.
        @param state 穷尽生命周期状态 / Exhaustive lifecycle state.
        @param version 当前 claim fencing 版本 / Current claim-fencing version.
        @param attempt_count 已领取次数 / Number of claims issued.
        @param updated_at 当前状态生效时间 / Time at which the state became current.
        @return 通过全部不变量校验的聚合 / Aggregate satisfying all invariants.
        """

        _require_factory_seal(seal)
        if not isinstance(draft, DreamActivityDraft):
            raise TypeError("Dream activity requires a DreamActivityDraft")
        if not isinstance(
            state,
            PendingDream
            | ProcessingDream
            | WaitingDreamRetry
            | CompletedDream
            | FailedDreamFinal,
        ):
            raise TypeError("Dream activity requires a lifecycle state")
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version < 0
            or isinstance(attempt_count, bool)
            or not isinstance(attempt_count, int)
            or attempt_count < 0
        ):
            raise ValueError("Dream version and attempt_count must be non-negative")
        if version != attempt_count:
            raise ValueError(
                "Dream version and attempt_count advance together on claim"
            )
        timestamp = ensure_utc(updated_at)
        if timestamp < draft.created_at:
            raise ValueError("Dream updated_at cannot precede created_at")
        if isinstance(state, PendingDream):
            if (
                version != 0
                or timestamp != draft.created_at
                or state.claimable_at != timestamp
            ):
                raise ValueError("Pending Dream must be the untouched initial job")
        elif attempt_count < 1:
            raise ValueError("A non-pending Dream requires a prior claim")
        if isinstance(state, ProcessingDream) and state.lease_expires_at <= timestamp:
            raise ValueError("Processing Dream lease must expire after its claim time")
        if isinstance(state, WaitingDreamRetry) and state.claimable_at < timestamp:
            raise ValueError("Dream retry cannot be scheduled before its transition")
        if isinstance(state, CompletedDream | FailedDreamFinal):
            if state.completed_at != timestamp:
                raise ValueError("Terminal Dream time must match updated_at")

        activity = object.__new__(cls)
        object.__setattr__(activity, "dream_id", draft.dream_id)
        object.__setattr__(activity, "owner_user_id", draft.owner_user_id)
        object.__setattr__(activity, "baseline", draft.baseline)
        object.__setattr__(activity, "through_event_id", draft.through_event_id)
        object.__setattr__(activity, "source_count", draft.source_count)
        object.__setattr__(activity, "metadata", draft.metadata)
        object.__setattr__(activity, "created_at", draft.created_at)
        object.__setattr__(activity, "state", state)
        object.__setattr__(activity, "version", version)
        object.__setattr__(activity, "attempt_count", attempt_count)
        object.__setattr__(activity, "updated_at", timestamp)
        return activity

    @classmethod
    def enqueue(cls, draft: DreamActivityDraft) -> Self:
        """@brief 创建初始 pending job / Create the initial pending job.

        @param draft 已校验的 Dream 意图 / Validated Dream intent.
        @return 未被领取的初始聚合 / Initial unclaimed aggregate.
        """

        return cls._create(
            _DREAM_FACTORY_SEAL,
            draft=draft,
            state=PendingDream(draft.created_at),
            version=0,
            attempt_count=0,
            updated_at=draft.created_at,
        )

    @classmethod
    def restore(
        cls,
        *,
        draft: DreamActivityDraft,
        status: DreamActivityStatus,
        version: int,
        attempt_count: int,
        next_attempt_at: datetime | None,
        claim_token: DreamLeaseToken | None,
        lease_expires_at: datetime | None,
        result: DreamResult | None,
        last_error: str | None,
        updated_at: datetime,
        completed_at: datetime | None,
    ) -> Self:
        """@brief 从持久化标量严格恢复聚合 / Strictly restore an aggregate from persistence scalars.

        @param draft 持久化的不可变意图 / Persisted immutable intent.
        @param status 持久化生命周期状态 / Persisted lifecycle status.
        @param version 当前 claim fencing 版本 / Current claim-fencing version.
        @param attempt_count 已领取次数 / Number of claims issued.
        @param next_attempt_at 可领取时间 / Next claimable time.
        @param claim_token processing ownership token / Processing-ownership token.
        @param lease_expires_at processing lease 截止时间 / Processing-lease deadline.
        @param result 终态模型结果 / Terminal model result.
        @param last_error retry/final 错误摘要 / Retry or final error summary.
        @param updated_at 当前状态生效时间 / Current-state effective time.
        @param completed_at 终态提交时间 / Terminal commit time.
        @return 与持久化投影严格一致的聚合 / Aggregate strictly matching the persistence projection.
        """

        if not isinstance(status, DreamActivityStatus):
            raise TypeError("Dream restore requires DreamActivityStatus")
        next_time = ensure_utc(next_attempt_at) if next_attempt_at is not None else None
        completion_time = ensure_utc(completed_at) if completed_at is not None else None
        failure = DreamFailure(last_error) if last_error is not None else None
        lease_end = (
            ensure_utc(lease_expires_at) if lease_expires_at is not None else None
        )
        if status is DreamActivityStatus.PENDING:
            if (
                next_time is None
                or claim_token is not None
                or lease_end is not None
                or result is not None
                or failure is not None
                or completion_time is not None
            ):
                raise ValueError("Pending Dream has inconsistent persistence fields")
            state: DreamActivityState = PendingDream(next_time)
        elif status is DreamActivityStatus.PROCESSING:
            if (
                next_time is not None
                or claim_token is None
                or lease_end is None
                or result is not None
                or failure is not None
                or completion_time is not None
            ):
                raise ValueError("Processing Dream has inconsistent persistence fields")
            state = ProcessingDream(claim_token, lease_end)
        elif status is DreamActivityStatus.RETRY_WAIT:
            if (
                next_time is None
                or claim_token is not None
                or lease_end is not None
                or failure is None
                or result is not None
                or completion_time is not None
            ):
                raise ValueError("Retrying Dream has inconsistent persistence fields")
            state = WaitingDreamRetry(next_time, failure)
        elif status is DreamActivityStatus.COMPLETED:
            if (
                next_time is not None
                or claim_token is not None
                or lease_end is not None
                or result is None
                or failure is not None
                or completion_time is None
            ):
                raise ValueError("Completed Dream has inconsistent persistence fields")
            state = CompletedDream(completion_time, result)
        else:
            if (
                next_time is not None
                or claim_token is not None
                or lease_end is not None
                or result is not None
                or failure is None
                or completion_time is None
            ):
                raise ValueError("Failed Dream has inconsistent persistence fields")
            state = FailedDreamFinal(completion_time, failure)
        return cls._create(
            _DREAM_FACTORY_SEAL,
            draft=draft,
            state=state,
            version=version,
            attempt_count=attempt_count,
            updated_at=updated_at,
        )

    @property
    def status(self) -> DreamActivityStatus:
        """@brief 投影持久化状态 / Project persisted status.

        @return 稳定的持久化状态 / Stable persisted status.
        """

        if isinstance(self.state, PendingDream):
            return DreamActivityStatus.PENDING
        if isinstance(self.state, ProcessingDream):
            return DreamActivityStatus.PROCESSING
        if isinstance(self.state, WaitingDreamRetry):
            return DreamActivityStatus.RETRY_WAIT
        if isinstance(self.state, CompletedDream):
            return DreamActivityStatus.COMPLETED
        return DreamActivityStatus.FAILED_FINAL

    @property
    def next_attempt_at(self) -> datetime | None:
        """@brief 投影下次领取时刻 / Project next claim time.

        @return 可领取时间；非等待态为 None / Claimable time, or None outside waiting states.
        """

        if isinstance(self.state, PendingDream | WaitingDreamRetry):
            return self.state.claimable_at
        return None

    @property
    def result(self) -> DreamResult | None:
        """@brief 投影完成结果 / Project completion result.

        @return completed result；其他状态为 None / Completed result, or None in other states.
        """

        return self.state.result if isinstance(self.state, CompletedDream) else None

    @property
    def last_error(self) -> str | None:
        """@brief 投影失败摘要 / Project failure summary.

        @return retry/final 摘要；其他状态为 None / Retry or final summary, or None otherwise.
        """

        if isinstance(self.state, WaitingDreamRetry | FailedDreamFinal):
            return self.state.failure.summary
        return None

    @property
    def completed_at(self) -> datetime | None:
        """@brief 投影终结时刻 / Project terminal time.

        @return 终态时间；非终态为 None / Terminal time, or None before termination.
        """

        if isinstance(self.state, CompletedDream | FailedDreamFinal):
            return self.state.completed_at
        return None

    def claim(
        self,
        *,
        token: DreamLeaseToken,
        claimed_at: datetime,
        lease_expires_at: datetime,
        current_document: ProfileDocument,
        evidence: Iterable[ProfileEvidence],
    ) -> DreamClaim:
        """@brief 领取到期 job 并签发模块工厂密封 capability / Claim a due job and issue a module-factory-sealed capability.

        @param token 本次领取的唯一 fencing token / Unique fencing token for this claim.
        @param claimed_at 领取时间 / Claim time.
        @param lease_expires_at 租约截止时间 / Lease deadline.
        @param current_document 冻结的当前 Profile / Frozen current Profile.
        @param evidence 冻结且严格有序的 evidence / Frozen, strictly ordered evidence range.
        @return 持有生成输入与 ownership 的 claim / Claim owning generation inputs and ownership.
        """

        if not isinstance(self.state, PendingDream | WaitingDreamRetry):
            raise InvalidDreamTransition(
                f"Dream state {self.status.value} cannot be claimed"
            )
        timestamp = ensure_utc(claimed_at)
        lease_end = ensure_utc(lease_expires_at)
        if timestamp < self.updated_at or timestamp < self.state.claimable_at:
            raise ValueError("Dream is not claimable at claimed_at")
        if lease_end <= timestamp:
            raise ValueError("Dream lease must expire after claimed_at")
        processing = self._evolve(
            state=ProcessingDream(token, lease_end),
            version=self.version + 1,
            attempt_count=self.attempt_count + 1,
            updated_at=timestamp,
        )
        return DreamClaim._create(
            _DREAM_FACTORY_SEAL,
            processing,
            current_document=current_document,
            evidence=evidence,
        )

    def recover_expired_lease(
        self,
        lease: DreamLease,
        *,
        recovered_at: datetime,
        failure: DreamFailure,
        max_attempts: int,
    ) -> DreamLeaseRecovery:
        """@brief 按尝试预算穷尽回收过期 lease / Exhaustively recover an expired lease under an attempt budget.

        @param lease 待回收 ownership capability / Ownership capability to recover.
        @param recovered_at 回收时间 / Recovery time.
        @param failure 可审计的 recovery 原因 / Auditable recovery reason.
        @param max_attempts 包含已 crash claim 的最大尝试数 / Maximum attempts including the crashed claim.
        @return retry-wait 或 failed-final 的封闭 recovery 决定 / Closed retry-wait or failed-final recovery decision.
        """

        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or max_attempts < 1
        ):
            raise ValueError("Dream recovery max_attempts must be positive")
        if not isinstance(failure, DreamFailure):
            raise TypeError("Dream recovery requires DreamFailure")
        self._require_lease(lease)
        timestamp = self._transition_time(recovered_at)
        if timestamp < lease.lease_expires_at:
            raise InvalidDreamTransition(
                "Dream lease cannot be recovered before expiry"
            )
        decision_type = (
            DreamLeaseRecoveryFailedFinal
            if self.attempt_count >= max_attempts
            else DreamLeaseRecoveryRetry
        )
        return decision_type._create(
            _DREAM_FACTORY_SEAL,
            lease,
            recovered_at=timestamp,
            failure=failure,
        )

    def _evolve(
        self,
        *,
        state: DreamActivityState,
        version: int,
        attempt_count: int,
        updated_at: datetime,
    ) -> Self:
        """@brief 保留不可变意图并构造下一状态 / Preserve immutable intent and construct the next state.

        @param state 目标生命周期状态 / Target lifecycle state.
        @param version 目标 fencing 版本 / Target fencing version.
        @param attempt_count 目标领取次数 / Target claim count.
        @param updated_at 转换时间 / Transition time.
        @return 保持 identity 的新聚合值 / New aggregate value preserving identity.
        """

        return type(self)._create(
            _DREAM_FACTORY_SEAL,
            draft=DreamActivityDraft(
                dream_id=self.dream_id,
                owner_user_id=self.owner_user_id,
                baseline=self.baseline,
                through_event_id=self.through_event_id,
                source_count=self.source_count,
                metadata=self.metadata,
                created_at=self.created_at,
            ),
            state=state,
            version=version,
            attempt_count=attempt_count,
            updated_at=updated_at,
        )

    def _require_lease(self, lease: DreamLease) -> None:
        """@brief 验证 lease 拥有当前 processing 版本 / Verify lease ownership of the current processing version.

        @param lease 待校验 capability / Capability to validate.
        @return None / None.
        """

        if not isinstance(self.state, ProcessingDream):
            raise InvalidDreamTransition(
                f"Dream state {self.status.value} has no lease"
            )
        if not isinstance(lease, DreamLease):
            raise TypeError("Dream recovery requires DreamLease")
        if lease.activity != self or lease.expected_version != self.version:
            raise InvalidDreamTransition(
                "Dream lease does not own this activity version"
            )

    def _transition_time(self, occurred_at: datetime) -> datetime:
        """@brief 禁止聚合时钟倒退 / Reject aggregate-clock regression.

        @param occurred_at 候选转换时间 / Candidate transition time.
        @return UTC 规范化且不倒退的时间 / UTC-normalized non-regressing time.
        """

        timestamp = ensure_utc(occurred_at)
        if timestamp < self.updated_at:
            raise ValueError("Dream transition time cannot precede current version")
        return timestamp


@dataclass(frozen=True, slots=True, init=False)
class DreamLease:
    """@brief Dream version/token/lease ownership capability / Dream version-token-lease ownership capability."""

    activity: DreamActivity
    expected_version: int
    token: DreamLeaseToken
    lease_expires_at: datetime

    def __new__(cls, *args: object, **kwargs: object) -> Self:
        """@brief 拒绝公开构造 lease / Reject public lease construction.

        @return 此入口永不返回 / This entry point never returns.
        """

        del args, kwargs
        raise TypeError("Use DreamLease.restore()")

    @classmethod
    def restore(
        cls,
        activity: DreamActivity,
    ) -> Self:
        """@brief 从完整 processing 聚合恢复 lease / Restore a lease from a complete processing aggregate.

        @param activity 完整 processing 聚合 / Complete processing aggregate.
        @return 与当前版本绑定的 lease capability / Lease capability bound to the current version.
        """

        if not isinstance(activity, DreamActivity) or not isinstance(
            activity.state,
            ProcessingDream,
        ):
            raise ValueError("Dream lease requires a processing activity")
        capability = object.__new__(cls)
        object.__setattr__(capability, "activity", activity)
        object.__setattr__(capability, "expected_version", activity.version)
        object.__setattr__(capability, "token", activity.state.token)
        object.__setattr__(
            capability,
            "lease_expires_at",
            activity.state.lease_expires_at,
        )
        return capability


@dataclass(frozen=True, slots=True, init=False)
class DreamClaim:
    """@brief 携带冻结 Profile/evidence 的 Dream generation capability / Dream-generation capability carrying frozen Profile and evidence."""

    activity: DreamActivity
    expected_version: int
    token: DreamLeaseToken
    lease_expires_at: datetime
    current_document: ProfileDocument
    evidence: tuple[ProfileEvidence, ...]

    def __new__(cls, *args: object, **kwargs: object) -> Self:
        """@brief 拒绝公开构造 claim / Reject public claim construction.

        @return 此入口永不返回 / This entry point never returns.
        """

        del args, kwargs
        raise TypeError("Use DreamActivity.claim()")

    @classmethod
    def _create(
        cls,
        seal: object,
        activity: DreamActivity,
        *,
        current_document: ProfileDocument,
        evidence: Iterable[ProfileEvidence],
    ) -> Self:
        """@brief 从 processing 聚合签发 claim / Issue a claim from a processing aggregate.

        @param seal 模块私有构造密封 / Module-private construction seal.
        @param activity 已领取的 processing 聚合 / Claimed processing aggregate.
        @param current_document 冻结的当前 Profile / Frozen current Profile.
        @param evidence 冻结的有序 evidence / Frozen ordered evidence.
        @return 带 ownership 与生成输入的 claim / Claim carrying ownership and generation inputs.
        """

        _require_factory_seal(seal)
        lease = DreamLease.restore(activity)
        if not isinstance(current_document, ProfileDocument):
            raise TypeError("Dream claim requires ProfileDocument")
        if activity.baseline.revision == 0 and current_document.claims:
            raise ValueError("An unmaterialized Profile cannot have current claims")
        frozen_evidence = tuple(evidence)
        if any(not isinstance(item, ProfileEvidence) for item in frozen_evidence):
            raise TypeError("Dream claim evidence requires ProfileEvidence values")
        event_ids = tuple(item.event_id for item in frozen_evidence)
        if (
            len(frozen_evidence) != activity.source_count
            or not frozen_evidence
            or event_ids != tuple(sorted(set(event_ids)))
            or event_ids[0] <= activity.baseline.observed_through_event_id
            or event_ids[-1] != activity.through_event_id
            or any(
                item.owner_user_id != activity.owner_user_id for item in frozen_evidence
            )
            or frozen_evidence[-1].metadata != activity.metadata
        ):
            raise ValueError("Dream evidence does not match its frozen source range")
        claim = object.__new__(cls)
        object.__setattr__(claim, "activity", activity)
        object.__setattr__(claim, "expected_version", lease.expected_version)
        object.__setattr__(claim, "token", lease.token)
        object.__setattr__(claim, "lease_expires_at", lease.lease_expires_at)
        object.__setattr__(claim, "current_document", current_document)
        object.__setattr__(claim, "evidence", frozen_evidence)
        return claim

    def prepare_completion(
        self,
        result: DreamResult,
        *,
        completed_at: datetime,
    ) -> DreamCompletionPrepared:
        """@brief 验证 patch 并准备原子 Dream/Profile 提交 / Validate a patch and prepare an atomic Dream/Profile commit.

        @param result 模型 patch 与 provenance / Model patch and provenance.
        @param completed_at 完成时间 / Completion time.
        @return 已验证并赋时的 completion / Validated, timestamped completion.
        """

        return self.evaluate_result(result).prepare(completed_at=completed_at)

    def evaluate_result(self, result: DreamResult) -> DreamCompletionEvaluated:
        """@brief 在读取完成时钟前纯验证并应用模型 patch / Purely validate and apply a model patch before reading completion time.

        @param result 模型 patch 与 provenance / Model patch and provenance.
        @return 不含时间副作用的评估结果 / Evaluated result without a time side effect.
        """

        if not isinstance(result, DreamResult):
            raise TypeError("Dream completion requires DreamResult")
        return DreamCompletionEvaluated._create(
            _DREAM_FACTORY_SEAL,
            self,
            result=result,
        )

    def record_failure(
        self,
        *,
        failed_at: datetime,
        failure: DreamFailure,
    ) -> DreamFailureAttempt:
        """@brief 记录尚未分类为 retry/final 的失败 / Record a failure before retry/final classification.

        @param failed_at 失败发生时间 / Failure time.
        @param failure 可安全持久化的失败 / Safely persistable failure.
        @return 等待 retry/final 分类的失败决定 / Failure decision awaiting retry/final classification.
        """

        if not isinstance(failure, DreamFailure):
            raise TypeError("Dream failure outcome requires DreamFailure")
        return DreamFailureAttempt._create(
            _DREAM_FACTORY_SEAL,
            self,
            failed_at=failed_at,
            failure=failure,
        )


class _ClosedDecision:
    """@brief 禁止常规调用方绕过领域转换构造决定 / Prevent ordinary callers from constructing decisions outside domain transitions."""

    def __new__(cls, *args: object, **kwargs: object) -> Self:
        """@brief 拒绝公开构造 / Reject public construction.

        @return 此入口永不返回 / This entry point never returns.
        """

        del args, kwargs
        raise TypeError("Dream decisions are created by aggregate transitions")


@dataclass(frozen=True, slots=True, init=False)
class DreamCompletionEvaluated(_ClosedDecision):
    """@brief 已纯验证、尚未赋予提交时间的 Dream result / Purely validated Dream result not yet assigned a commit time."""

    claim: DreamClaim
    result: DreamResult
    document: ProfileDocument
    changed: bool

    @classmethod
    def _create(
        cls,
        seal: object,
        claim: DreamClaim,
        *,
        result: DreamResult,
    ) -> Self:
        """@brief 从 claim 的纯 patch 求值创建结果 / Create an evaluation from pure patch application.

        @param seal 模块私有构造密封 / Module-private construction seal.
        @param claim 结果所属 claim / Claim owning the result.
        @param result 已校验模型结果 / Validated model result.
        @return 不含时钟副作用的求值决定 / Evaluation decision without clock side effects.
        """

        _require_factory_seal(seal)
        if not isinstance(claim, DreamClaim):
            raise TypeError("Dream evaluation requires DreamClaim")
        if not isinstance(result, DreamResult):
            raise TypeError("Dream evaluation requires DreamResult")
        document = claim.current_document.apply(
            result.patch,
            evidence=claim.evidence,
        )
        decision = object.__new__(cls)
        object.__setattr__(decision, "claim", claim)
        object.__setattr__(decision, "result", result)
        object.__setattr__(decision, "document", document)
        object.__setattr__(decision, "changed", document != claim.current_document)
        return decision

    def prepare(self, *, completed_at: datetime) -> DreamCompletionPrepared:
        """@brief 在纯 patch 验证后赋予提交时间 / Assign commit time after pure patch validation.

        @param completed_at 完成时间 / Completion time.
        @return 等待 backlog 决策的 completion / Completion awaiting the backlog decision.
        """

        return DreamCompletionPrepared._create(
            _DREAM_FACTORY_SEAL,
            self,
            completed_at=completed_at,
        )


@dataclass(frozen=True, slots=True, init=False)
class DreamCompletionPrepared(_ClosedDecision):
    """@brief 等待 backlog 查询的 Dream completion / Dream completion awaiting a backlog observation."""

    claim: DreamClaim
    activity: DreamActivity
    document: ProfileDocument
    changed: bool

    @classmethod
    def _create(
        cls,
        seal: object,
        evaluated: DreamCompletionEvaluated,
        *,
        completed_at: datetime,
    ) -> Self:
        """@brief 由 claim 创建 completion / Create completion from a claim.

        @param seal 模块私有构造密封 / Module-private construction seal.
        @param evaluated 纯求值决定 / Pure evaluation decision.
        @param completed_at 完成时间 / Completion time.
        @return 已验证的 completion 决定 / Validated completion decision.
        """

        _require_factory_seal(seal)
        if not isinstance(evaluated, DreamCompletionEvaluated):
            raise TypeError("Dream preparation requires an evaluated result")
        claim = evaluated.claim
        timestamp = claim.activity._transition_time(completed_at)
        source = claim.activity
        activity = source._evolve(
            state=CompletedDream(timestamp, evaluated.result),
            version=source.version,
            attempt_count=source.attempt_count,
            updated_at=timestamp,
        )
        _validate_settlement(claim, activity, DreamActivityStatus.COMPLETED)
        decision = object.__new__(cls)
        object.__setattr__(decision, "claim", claim)
        object.__setattr__(decision, "activity", activity)
        object.__setattr__(decision, "document", evaluated.document)
        object.__setattr__(decision, "changed", evaluated.changed)
        return decision

    def plan_profile_commit(
        self,
        *,
        has_backlog: bool,
        refresh_after: timedelta,
    ) -> DreamCompletion:
        """@brief 根据 backlog 规划 watermark/revision/eligibility / Plan watermark, revision, and eligibility from backlog.

        @param has_backlog through 之后是否仍有 evidence / Whether evidence remains beyond through.
        @param refresh_after 无 backlog 时的刷新间隔 / Refresh interval when no backlog remains.
        @return 完整原子提交计划 / Complete atomic commit plan.
        """

        return DreamCompletion._create(
            _DREAM_FACTORY_SEAL,
            self,
            has_backlog=has_backlog,
            refresh_after=refresh_after,
        )


@dataclass(frozen=True, slots=True, init=False)
class DreamCompletion(_ClosedDecision):
    """@brief 已解析 eligibility 的 Dream/Profile 原子提交计划 / Atomic Dream/Profile commit plan with resolved eligibility."""

    prepared: DreamCompletionPrepared
    profile_revision: int
    observed_through_event_id: int
    next_eligible_at: datetime

    @classmethod
    def _create(
        cls,
        seal: object,
        prepared: DreamCompletionPrepared,
        *,
        has_backlog: bool,
        refresh_after: timedelta,
    ) -> Self:
        """@brief 由原始 backlog 事实派生完整提交计划 / Derive a complete commit plan from the raw backlog fact.

        @param seal 模块私有构造密封 / Module-private construction seal.
        @param prepared 已经验证且赋时的 completion / Validated and timestamped completion.
        @param has_backlog through 之后是否仍有 evidence / Whether evidence remains beyond through.
        @param refresh_after 无 backlog 时的刷新间隔 / Refresh interval when no backlog remains.
        @return 不接受派生字段注入的提交计划 / Commit plan that accepts no injected derived fields.
        """

        _require_factory_seal(seal)
        if not isinstance(prepared, DreamCompletionPrepared):
            raise TypeError("Dream commit plan requires prepared completion")
        if not isinstance(has_backlog, bool):
            raise TypeError("Dream backlog observation must be bool")
        if not isinstance(refresh_after, timedelta):
            raise TypeError("Profile refresh_after must be timedelta")
        if refresh_after <= timedelta():
            raise ValueError("Profile refresh_after must be positive")
        completed_at = prepared.activity.completed_at
        if completed_at is None:  # pragma: no cover - constructor proves completion.
            raise AssertionError("Dream completion lost completed_at")
        next_eligible_at = completed_at if has_backlog else completed_at + refresh_after
        completion = object.__new__(cls)
        object.__setattr__(completion, "prepared", prepared)
        object.__setattr__(
            completion,
            "profile_revision",
            prepared.claim.activity.baseline.revision + int(prepared.changed),
        )
        object.__setattr__(
            completion,
            "observed_through_event_id",
            prepared.claim.activity.through_event_id,
        )
        object.__setattr__(completion, "next_eligible_at", next_eligible_at)
        return completion

    def snapshot(self, *, profile_created_at: datetime) -> UserProfileSnapshot | None:
        """@brief 为 changed completion 构造新 snapshot / Build a new snapshot for a changed completion.

        @param profile_created_at Profile 聚合建立时间 / Profile-aggregate creation time.
        @return 新 snapshot；NO_OP 时为 None / New snapshot, or None for NO_OP.
        """

        if not self.prepared.changed:
            return None
        result = self.prepared.activity.result
        completed_at = self.prepared.activity.completed_at
        if result is None or completed_at is None:  # pragma: no cover
            raise AssertionError("Dream completion lost result metadata")
        return UserProfileSnapshot(
            user_id=self.prepared.claim.activity.owner_user_id,
            revision=self.profile_revision,
            document=self.prepared.document,
            observed_through_event_id=self.observed_through_event_id,
            created_at=profile_created_at,
            updated_at=completed_at,
            route_key=result.route_key,
            prompt_version=result.prompt_version,
        )


@dataclass(frozen=True, slots=True, init=False)
class DreamFailureAttempt(_ClosedDecision):
    """@brief 等待 retry/final 分类的 Dream 失败 / Dream failure awaiting retry/final classification."""

    claim: DreamClaim
    failed_at: datetime
    failure: DreamFailure

    @classmethod
    def _create(
        cls,
        seal: object,
        claim: DreamClaim,
        *,
        failed_at: datetime,
        failure: DreamFailure,
    ) -> Self:
        """@brief 由 claim 创建失败 outcome / Create a failure outcome from a claim.

        @param seal 模块私有构造密封 / Module-private construction seal.
        @param claim 失败所属 claim / Claim that failed.
        @param failed_at 失败时间 / Failure time.
        @param failure 可持久化失败摘要 / Persistable failure summary.
        @return 等待分类的失败 outcome / Failure outcome awaiting classification.
        """

        _require_factory_seal(seal)
        if not isinstance(claim, DreamClaim):
            raise TypeError("Dream failure outcome requires DreamClaim")
        if not isinstance(failure, DreamFailure):
            raise TypeError("Dream failure outcome requires DreamFailure")
        timestamp = claim.activity._transition_time(failed_at)
        outcome = object.__new__(cls)
        object.__setattr__(outcome, "claim", claim)
        object.__setattr__(outcome, "failed_at", timestamp)
        object.__setattr__(outcome, "failure", failure)
        return outcome

    def schedule_retry(self, *, retry_at: datetime) -> DreamRetryScheduled:
        """@brief 安排 retry，不增加 version/attempt / Schedule retry without advancing version or attempts.

        @param retry_at 下次可领取时间 / Next claimable time.
        @return 类型化 retry settlement / Typed retry settlement.
        """

        return DreamRetryScheduled._create(
            _DREAM_FACTORY_SEAL,
            self,
            retry_at=retry_at,
        )

    def fail_final(self) -> DreamFailedFinalDecision:
        """@brief 终结失败，不增加 version/attempt / Finalize failure without advancing version or attempts.

        @return 类型化 final-failure settlement / Typed final-failure settlement.
        """

        return DreamFailedFinalDecision._create(_DREAM_FACTORY_SEAL, self)


@dataclass(frozen=True, slots=True, init=False)
class DreamRetryScheduled(_ClosedDecision):
    """@brief 类型化 Dream retry settlement / Typed Dream retry settlement."""

    claim: DreamClaim
    activity: DreamActivity

    @classmethod
    def _create(
        cls,
        seal: object,
        failure_attempt: DreamFailureAttempt,
        *,
        retry_at: datetime,
    ) -> Self:
        """@brief 创建 retry settlement / Create retry settlement.

        @param seal 模块私有构造密封 / Module-private construction seal.
        @param failure_attempt 已赋时的失败事实 / Timestamped failure fact.
        @param retry_at 下次可领取时间 / Next claimable time.
        @return 已验证 retry settlement / Validated retry settlement.
        """

        _require_factory_seal(seal)
        if not isinstance(failure_attempt, DreamFailureAttempt):
            raise TypeError("Dream retry requires a failure attempt")
        retry_time = ensure_utc(retry_at)
        if retry_time <= failure_attempt.failed_at:
            raise ValueError("Dream retry_at must follow failed_at")
        claim = failure_attempt.claim
        source = claim.activity
        activity = source._evolve(
            state=WaitingDreamRetry(retry_time, failure_attempt.failure),
            version=source.version,
            attempt_count=source.attempt_count,
            updated_at=failure_attempt.failed_at,
        )
        _validate_settlement(claim, activity, DreamActivityStatus.RETRY_WAIT)
        decision = object.__new__(cls)
        object.__setattr__(decision, "claim", claim)
        object.__setattr__(decision, "activity", activity)
        return decision


@dataclass(frozen=True, slots=True, init=False)
class DreamFailedFinalDecision(_ClosedDecision):
    """@brief 类型化 Dream final-failure settlement / Typed Dream final-failure settlement."""

    claim: DreamClaim
    activity: DreamActivity

    @classmethod
    def _create(
        cls,
        seal: object,
        failure_attempt: DreamFailureAttempt,
    ) -> Self:
        """@brief 创建 final-failure settlement / Create final-failure settlement.

        @param seal 模块私有构造密封 / Module-private construction seal.
        @param failure_attempt 已赋时的失败事实 / Timestamped failure fact.
        @return 已验证 final-failure settlement / Validated final-failure settlement.
        """

        _require_factory_seal(seal)
        if not isinstance(failure_attempt, DreamFailureAttempt):
            raise TypeError("Dream final failure requires a failure attempt")
        claim = failure_attempt.claim
        source = claim.activity
        activity = source._evolve(
            state=FailedDreamFinal(
                failure_attempt.failed_at,
                failure_attempt.failure,
            ),
            version=source.version,
            attempt_count=source.attempt_count,
            updated_at=failure_attempt.failed_at,
        )
        _validate_settlement(claim, activity, DreamActivityStatus.FAILED_FINAL)
        decision = object.__new__(cls)
        object.__setattr__(decision, "claim", claim)
        object.__setattr__(decision, "activity", activity)
        return decision


@dataclass(frozen=True, slots=True, init=False)
class DreamLeaseRecoveryRetry(_ClosedDecision):
    """@brief 预算未耗尽的 lease-recovery retry 决定 / Lease-recovery retry decision with budget remaining.

    @param lease 被回收的 ownership capability / Recovered ownership capability.
    @param activity 进入 retry-wait 的目标聚合 / Target aggregate entering retry-wait.
    """

    lease: DreamLease
    activity: DreamActivity

    @classmethod
    def _create(
        cls,
        seal: object,
        lease: DreamLease,
        *,
        recovered_at: datetime,
        failure: DreamFailure,
    ) -> Self:
        """@brief 从 crash 事实派生 retry-wait 决定 / Derive a retry-wait decision from crash facts.

        @param seal 模块私有构造密封 / Module-private construction seal.
        @param lease 被回收的 ownership capability / Recovered ownership capability.
        @param recovered_at 回收时间 / Recovery time.
        @param failure 可审计 crash 原因 / Auditable crash reason.
        @return 已验证 retry recovery 决定 / Validated retry recovery decision.
        """

        _require_factory_seal(seal)
        if not isinstance(lease, DreamLease):
            raise TypeError("Dream recovery retry requires DreamLease")
        if not isinstance(failure, DreamFailure):
            raise TypeError("Dream recovery retry requires DreamFailure")
        source = lease.activity
        timestamp = source._transition_time(recovered_at)
        activity = source._evolve(
            state=WaitingDreamRetry(timestamp, failure),
            version=source.version,
            attempt_count=source.attempt_count,
            updated_at=timestamp,
        )
        _validate_identity(lease.activity, activity)
        decision = object.__new__(cls)
        object.__setattr__(decision, "lease", lease)
        object.__setattr__(decision, "activity", activity)
        return decision


@dataclass(frozen=True, slots=True, init=False)
class DreamLeaseRecoveryFailedFinal(_ClosedDecision):
    """@brief 预算已耗尽的 lease-recovery 终败决定 / Terminal lease-recovery decision after budget exhaustion.

    @param lease 被回收的 ownership capability / Recovered ownership capability.
    @param activity 进入 failed-final 的目标聚合 / Target aggregate entering failed-final.
    """

    lease: DreamLease
    activity: DreamActivity

    @classmethod
    def _create(
        cls,
        seal: object,
        lease: DreamLease,
        *,
        recovered_at: datetime,
        failure: DreamFailure,
    ) -> Self:
        """@brief 从 crash 事实派生 failed-final 决定 / Derive a failed-final decision from crash facts.

        @param seal 模块私有构造密封 / Module-private construction seal.
        @param lease 被回收的 ownership capability / Recovered ownership capability.
        @param recovered_at 终结时间 / Finalization time.
        @param failure 可审计 crash 原因 / Auditable crash reason.
        @return 已验证 final recovery 决定 / Validated final recovery decision.
        """

        _require_factory_seal(seal)
        if not isinstance(lease, DreamLease):
            raise TypeError("Dream recovery finalization requires DreamLease")
        if not isinstance(failure, DreamFailure):
            raise TypeError("Dream recovery finalization requires DreamFailure")
        source = lease.activity
        timestamp = source._transition_time(recovered_at)
        activity = source._evolve(
            state=FailedDreamFinal(timestamp, failure),
            version=source.version,
            attempt_count=source.attempt_count,
            updated_at=timestamp,
        )
        _validate_identity(lease.activity, activity)
        decision = object.__new__(cls)
        object.__setattr__(decision, "lease", lease)
        object.__setattr__(decision, "activity", activity)
        return decision


type DreamLeaseRecovery = DreamLeaseRecoveryRetry | DreamLeaseRecoveryFailedFinal
"""@brief 过期 lease recovery 的穷尽决定和 / Exhaustive decision sum for expired-lease recovery."""


def _validate_settlement(
    claim: DreamClaim,
    activity: DreamActivity,
    expected_status: DreamActivityStatus,
) -> None:
    """@brief 验证 settlement 保持 identity/version/attempt / Validate preserved identity, version, and attempt.

    @param claim settlement 的原始 claim / Original claim of the settlement.
    @param activity settlement 目标聚合 / Settlement target aggregate.
    @param expected_status 必须到达的状态 / Required target status.
    @return None / None.
    """

    if activity.status is not expected_status:
        raise ValueError(f"Dream settlement requires {expected_status.value}")
    _validate_identity(claim.activity, activity)


def _validate_identity(previous: DreamActivity, activity: DreamActivity) -> None:
    """@brief 验证转换保持 immutable intent 且不增版本 / Validate immutable intent and no version advance.

    @param previous 转换前聚合 / Aggregate before transition.
    @param activity 转换后聚合 / Aggregate after transition.
    @return None / None.
    """

    if (
        activity.dream_id != previous.dream_id
        or activity.owner_user_id != previous.owner_user_id
        or activity.baseline != previous.baseline
        or activity.through_event_id != previous.through_event_id
        or activity.source_count != previous.source_count
        or activity.metadata != previous.metadata
        or activity.created_at != previous.created_at
    ):
        raise ValueError("Dream transition cannot replace immutable intent")
    if (
        activity.version != previous.version
        or activity.attempt_count != previous.attempt_count
    ):
        raise ValueError("Dream settlement/recovery cannot advance version or attempts")


__all__ = [
    "CompletedDream",
    "DreamActivity",
    "DreamActivityDraft",
    "DreamActivityState",
    "DreamActivityStatus",
    "DreamClaim",
    "DreamCompletion",
    "DreamCompletionEvaluated",
    "DreamCompletionPrepared",
    "DreamFailedFinalDecision",
    "DreamFailure",
    "DreamFailureAttempt",
    "DreamLease",
    "DreamLeaseRecovery",
    "DreamLeaseRecoveryFailedFinal",
    "DreamLeaseRecoveryRetry",
    "DreamLeaseToken",
    "DreamResult",
    "DreamRetryScheduled",
    "FailedDreamFinal",
    "InvalidDreamTransition",
    "PendingDream",
    "ProcessingDream",
    "ProfileBaseline",
    "StaleDreamClaimError",
    "WaitingDreamRetry",
]
