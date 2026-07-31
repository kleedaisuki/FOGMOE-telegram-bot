"""@brief Passage 向量任务领域生命周期 / Passage-vector job domain lifecycle."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Self
from uuid import UUID

from fogmoe_bot.domain.temporal import ensure_utc

from .models import EmbeddingSpace, EmbeddingVector, RetrievalPassage

_SPACE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,99}$")
"""@brief 持久化 embedding space 标识语法 / Persisted embedding-space identifier syntax."""

RECOVERED_PASSAGE_VECTOR_LEASE_ERROR = "recovered expired embedding lease"
"""@brief crash 后恢复过期向量租约的稳定原因 / Stable reason for recovering an expired vector lease."""


class PassageVectorStatus(StrEnum):
    """@brief Passage 向量任务的稳定持久化状态 / Stable persisted passage-vector status."""

    PENDING = "pending"
    RETRY_WAIT = "retry_wait"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED_FINAL = "failed_final"


@dataclass(frozen=True, slots=True)
class PassageVectorJobKey:
    """@brief Passage 与 embedding space 组成的向量任务身份 / Vector-job identity formed by a passage and embedding space.

    @param passage_id Passage 身份 / Passage identity.
    @param space_id Embedding space 身份 / Embedding-space identity.
    """

    passage_id: UUID
    space_id: str

    def __post_init__(self) -> None:
        """@brief 校验复合身份 / Validate the composite identity.

        @return None / None.
        @raise TypeError Passage ID 或 space ID 类型错误 / Invalid passage-ID or space-ID type.
        @raise ValueError Space ID 不符合持久化语法 / Space ID violates the persisted syntax.
        """

        if not isinstance(self.passage_id, UUID):
            raise TypeError("Passage vector job requires a UUID passage_id")
        if not isinstance(self.space_id, str):
            raise TypeError("Passage vector job space_id must be a string")
        space_id = self.space_id.strip()
        if _SPACE_ID_PATTERN.fullmatch(space_id) is None:
            raise ValueError("Passage vector job space_id has invalid syntax")
        object.__setattr__(self, "space_id", space_id)


@dataclass(frozen=True, slots=True)
class PassageVectorFailure:
    """@brief 可安全持久化的向量失败摘要 / Safely persistable vector-failure summary.

    @param summary 去除空白且有界的失败摘要 / Trimmed and bounded failure summary.
    """

    summary: str

    def __post_init__(self) -> None:
        """@brief 规范化失败摘要 / Normalize the failure summary.

        @return None / None.
        @raise TypeError 摘要不是字符串 / Summary is not a string.
        @raise ValueError 摘要为空 / Summary is blank.
        """

        if not isinstance(self.summary, str):
            raise TypeError("Passage vector failure summary must be a string")
        summary = self.summary.strip()[:1_000]
        if not summary:
            raise ValueError("Passage vector failure summary cannot be blank")
        object.__setattr__(self, "summary", summary)


@dataclass(frozen=True, slots=True)
class AwaitingPassageVector:
    """@brief 等待首次 embedding 领取 / Awaiting the first embedding claim.

    @param next_attempt_at 最早领取时刻 / Earliest claim instant.
    """

    next_attempt_at: datetime

    def __post_init__(self) -> None:
        """@brief 规范化领取时刻 / Normalize the claim instant.

        @return None / None.
        """

        object.__setattr__(self, "next_attempt_at", ensure_utc(self.next_attempt_at))


@dataclass(frozen=True, slots=True)
class WaitingPassageVectorRetry:
    """@brief 失败后等待再次 embedding / Waiting for another embedding attempt after failure.

    @param next_attempt_at 最早重试时刻 / Earliest retry instant.
    @param failure 最近失败 / Most recent failure.
    """

    next_attempt_at: datetime
    failure: PassageVectorFailure

    def __post_init__(self) -> None:
        """@brief 校验重试等待状态 / Validate the retry-wait state.

        @return None / None.
        @raise TypeError Failure 类型错误 / Invalid failure type.
        """

        object.__setattr__(self, "next_attempt_at", ensure_utc(self.next_attempt_at))
        if not isinstance(self.failure, PassageVectorFailure):
            raise TypeError("Passage vector retry requires a PassageVectorFailure")


@dataclass(frozen=True, slots=True)
class ProcessingPassageVector:
    """@brief 带 fencing token 的处理中状态 / Processing state carrying a fencing token.

    @param claim_token 当前 claim capability token / Current claim-capability token.
    @param lease_expires_at crash recovery 截止时刻 / Crash-recovery lease deadline.
    """

    claim_token: UUID
    lease_expires_at: datetime

    def __post_init__(self) -> None:
        """@brief 校验租约所有权 / Validate lease ownership.

        @return None / None.
        @raise TypeError Claim token 不是 UUID / Claim token is not a UUID.
        """

        if not isinstance(self.claim_token, UUID):
            raise TypeError("Processing passage vector requires a UUID claim token")
        object.__setattr__(
            self,
            "lease_expires_at",
            ensure_utc(self.lease_expires_at),
        )


@dataclass(frozen=True, slots=True)
class CompletedPassageVector:
    """@brief 已持久化 embedding 的成功终态 / Successful terminal state with a persisted embedding.

    @param vector 已完成向量 / Completed vector.
    @param completed_at 完成时刻 / Completion instant.
    """

    vector: EmbeddingVector
    completed_at: datetime

    def __post_init__(self) -> None:
        """@brief 校验完成结果 / Validate the completed result.

        @return None / None.
        @raise TypeError Vector 类型错误 / Invalid vector type.
        """

        if not isinstance(self.vector, EmbeddingVector):
            raise TypeError("Completed passage vector requires an EmbeddingVector")
        object.__setattr__(self, "completed_at", ensure_utc(self.completed_at))


@dataclass(frozen=True, slots=True)
class FailedPassageVector:
    """@brief 不再自动重试的失败终态 / Failure terminal state excluded from automatic retries.

    @param failure 最终失败 / Final failure.
    """

    failure: PassageVectorFailure

    def __post_init__(self) -> None:
        """@brief 校验最终失败 / Validate the terminal failure.

        @return None / None.
        @raise TypeError Failure 类型错误 / Invalid failure type.
        """

        if not isinstance(self.failure, PassageVectorFailure):
            raise TypeError("Failed passage vector requires a PassageVectorFailure")


type PassageVectorState = (
    AwaitingPassageVector
    | WaitingPassageVectorRetry
    | ProcessingPassageVector
    | CompletedPassageVector
    | FailedPassageVector
)
"""@brief Passage 向量任务的穷尽状态和 / Exhaustive passage-vector job state sum."""


class InvalidPassageVectorTransition(RuntimeError):
    """@brief Passage 向量聚合拒绝非法生命周期转换 / Passage-vector aggregate rejected an invalid lifecycle transition."""


class _SealedDomainObject:
    """@brief 只允许领域工厂分配的内部基类 / Internal base allowing allocation only through domain factories."""

    def __new__(cls) -> Self:
        """@brief 拒绝调用公开构造器 / Reject public constructor calls.

        @return 永不返回 / Never returns.
        @raise TypeError 必须使用领域工厂 / A domain factory must be used.
        """

        raise TypeError(f"{cls.__name__} must be created by a domain factory")


@dataclass(frozen=True, slots=True, init=False)
class PassageVectorJob(_SealedDomainObject):
    """@brief 拥有状态、版本与 attempt 的 Passage 向量聚合 / Passage-vector aggregate owning state, version, and attempts.

    @param key Passage 与 space 的复合身份 / Composite passage-and-space identity.
    @param state 穷尽生命周期状态 / Exhaustive lifecycle state.
    @param version 乐观并发版本 / Optimistic-concurrency version.
    @param attempt_count 已经开始的 embedding 次数 / Number of embedding attempts started.
    @param created_at 创建时刻 / Creation instant.
    @param updated_at 最近领域转换时刻 / Most recent domain-transition instant.
    @note 新任务只能经 ``create_pending``，持久化 hydration 必须经 ``restore``。/
        New jobs go through ``create_pending`` and persistence hydration goes through ``restore``.
    """

    key: PassageVectorJobKey
    state: PassageVectorState
    version: int
    attempt_count: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def _create(
        cls,
        *,
        key: PassageVectorJobKey,
        state: PassageVectorState,
        version: int,
        attempt_count: int,
        created_at: datetime,
        updated_at: datetime,
    ) -> Self:
        """@brief 通过统一不变量门构造聚合 / Construct an aggregate through one invariant gate.

        @param key 聚合身份 / Aggregate identity.
        @param state 生命周期状态 / Lifecycle state.
        @param version 乐观并发版本 / Optimistic-concurrency version.
        @param attempt_count 已开始尝试数 / Started-attempt count.
        @param created_at 创建时刻 / Creation instant.
        @param updated_at 最近转换时刻 / Most recent transition instant.
        @return 已验证聚合 / Validated aggregate.
        @raise TypeError 字段类型错误 / Invalid field type.
        @raise ValueError 持久化不变量不成立 / Persisted invariants do not hold.
        """

        if not isinstance(key, PassageVectorJobKey):
            raise TypeError("Passage vector job requires a PassageVectorJobKey")
        if not isinstance(
            state,
            AwaitingPassageVector
            | WaitingPassageVectorRetry
            | ProcessingPassageVector
            | CompletedPassageVector
            | FailedPassageVector,
        ):
            raise TypeError("Passage vector job has an unknown state")
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            raise ValueError("Passage vector version must be a non-negative integer")
        if (
            isinstance(attempt_count, bool)
            or not isinstance(attempt_count, int)
            or attempt_count < 0
        ):
            raise ValueError(
                "Passage vector attempt_count must be a non-negative integer"
            )
        if version < attempt_count:
            raise ValueError("Passage vector version cannot trail attempt_count")

        created = ensure_utc(created_at)
        updated = ensure_utc(updated_at)
        if created > updated:
            raise ValueError("Passage vector updated_at cannot precede created_at")

        if isinstance(state, AwaitingPassageVector):
            if attempt_count != 0:
                raise ValueError("Pending passage vector must have zero attempts")
            if state.next_attempt_at != created or updated != created:
                raise ValueError(
                    "Pending passage vector timestamps must equal created_at"
                )
        else:
            if attempt_count < 1:
                raise ValueError("Claimed passage vector must have a positive attempt")
            if (
                isinstance(state, ProcessingPassageVector)
                and state.lease_expires_at <= updated
            ):
                raise ValueError(
                    "Processing passage vector lease must follow updated_at"
                )
            if (
                isinstance(state, WaitingPassageVectorRetry)
                and state.next_attempt_at < updated
            ):
                raise ValueError("Passage vector retry cannot precede updated_at")
            if (
                isinstance(state, CompletedPassageVector)
                and state.completed_at != updated
            ):
                raise ValueError(
                    "Completed passage vector timestamp must equal updated_at"
                )

        job = object.__new__(cls)
        object.__setattr__(job, "key", key)
        object.__setattr__(job, "state", state)
        object.__setattr__(job, "version", version)
        object.__setattr__(job, "attempt_count", attempt_count)
        object.__setattr__(job, "created_at", created)
        object.__setattr__(job, "updated_at", updated)
        return job

    @classmethod
    def create_pending(
        cls,
        key: PassageVectorJobKey,
        *,
        created_at: datetime,
    ) -> Self:
        """@brief 创建立即可领取的初始向量任务 / Create an initially claimable vector job.

        @param key 聚合身份 / Aggregate identity.
        @param created_at 创建与首次领取时刻 / Creation and first-claim instant.
        @return 初始 pending 聚合 / Initial pending aggregate.
        """

        timestamp = ensure_utc(created_at)
        return cls._create(
            key=key,
            state=AwaitingPassageVector(timestamp),
            version=0,
            attempt_count=0,
            created_at=timestamp,
            updated_at=timestamp,
        )

    @classmethod
    def restore(
        cls,
        *,
        key: PassageVectorJobKey,
        status: PassageVectorStatus,
        version: int,
        attempt_count: int,
        next_attempt_at: datetime | None,
        claim_token: UUID | None,
        lease_expires_at: datetime | None,
        vector: EmbeddingVector | None,
        last_error: str | None,
        created_at: datetime,
        updated_at: datetime,
        completed_at: datetime | None,
    ) -> Self:
        """@brief 从完整持久化列恢复聚合 / Restore an aggregate from the complete persisted row.

        @param key 聚合身份 / Aggregate identity.
        @param status 持久化状态 / Persisted status.
        @param version 乐观并发版本 / Optimistic-concurrency version.
        @param attempt_count 已开始尝试数 / Started-attempt count.
        @param next_attempt_at 可选下次领取时刻 / Optional next claim instant.
        @param claim_token 可选 fencing token / Optional fencing token.
        @param lease_expires_at 可选租约截止 / Optional lease deadline.
        @param vector 可选完成向量 / Optional completed vector.
        @param last_error 可选失败摘要 / Optional failure summary.
        @param created_at 创建时刻 / Creation instant.
        @param updated_at 最近转换时刻 / Most recent transition instant.
        @param completed_at 可选完成时刻 / Optional completion instant.
        @return 完整恢复的聚合 / Fully restored aggregate.
        @raise ValueError Nullable 列不符合穷尽状态矩阵 / Nullable columns violate the exhaustive state matrix.
        """

        if not isinstance(status, PassageVectorStatus):
            raise TypeError("Passage vector restore requires a PassageVectorStatus")
        failure = PassageVectorFailure(last_error) if last_error is not None else None

        if status is PassageVectorStatus.PENDING:
            if (
                next_attempt_at is None
                or claim_token is not None
                or lease_expires_at is not None
                or vector is not None
                or completed_at is not None
                or failure is not None
            ):
                raise ValueError("Pending passage vector has inconsistent fields")
            state: PassageVectorState = AwaitingPassageVector(next_attempt_at)
        elif status is PassageVectorStatus.RETRY_WAIT:
            if (
                next_attempt_at is None
                or claim_token is not None
                or lease_expires_at is not None
                or vector is not None
                or completed_at is not None
                or failure is None
            ):
                raise ValueError("Retrying passage vector has inconsistent fields")
            state = WaitingPassageVectorRetry(next_attempt_at, failure)
        elif status is PassageVectorStatus.PROCESSING:
            if (
                next_attempt_at is not None
                or claim_token is None
                or lease_expires_at is None
                or vector is not None
                or completed_at is not None
                or failure is not None
            ):
                raise ValueError("Processing passage vector has inconsistent fields")
            state = ProcessingPassageVector(claim_token, lease_expires_at)
        elif status is PassageVectorStatus.COMPLETED:
            if (
                next_attempt_at is not None
                or claim_token is not None
                or lease_expires_at is not None
                or vector is None
                or completed_at is None
                or failure is not None
            ):
                raise ValueError("Completed passage vector has inconsistent fields")
            state = CompletedPassageVector(vector, completed_at)
        else:
            if (
                next_attempt_at is not None
                or claim_token is not None
                or lease_expires_at is not None
                or vector is not None
                or completed_at is not None
                or failure is None
            ):
                raise ValueError("Failed passage vector has inconsistent fields")
            state = FailedPassageVector(failure)

        return cls._create(
            key=key,
            state=state,
            version=version,
            attempt_count=attempt_count,
            created_at=created_at,
            updated_at=updated_at,
        )

    def claim(
        self,
        *,
        passage: RetrievalPassage,
        space: EmbeddingSpace,
        claim_token: UUID,
        claimed_at: datetime,
        lease_for: timedelta,
    ) -> PassageVectorClaimed:
        """@brief 领取一个到期向量任务 / Claim one due vector job.

        @param passage 待 embedding Passage / Passage to embed.
        @param space 目标 embedding space / Target embedding space.
        @param claim_token 新 fencing token / New fencing token.
        @param claimed_at 领取时刻 / Claim instant.
        @param lease_for crash recovery 租期 / Crash-recovery lease duration.
        @return sealed claim 决策 / Sealed claim decision.
        @raise InvalidPassageVectorTransition 状态未到期或不可领取 / State is not due or claimable.
        """

        if not isinstance(
            self.state,
            AwaitingPassageVector | WaitingPassageVectorRetry,
        ):
            raise InvalidPassageVectorTransition(
                "Only pending or retrying passage vectors can be claimed"
            )
        timestamp = ensure_utc(claimed_at)
        if self.state.next_attempt_at > timestamp:
            raise InvalidPassageVectorTransition("Passage vector is not due")
        if not isinstance(lease_for, timedelta) or lease_for <= timedelta():
            raise ValueError("Passage vector lease_for must be positive")
        processing = PassageVectorJob._create(
            key=self.key,
            state=ProcessingPassageVector(claim_token, timestamp + lease_for),
            version=self.version + 1,
            attempt_count=self.attempt_count + 1,
            created_at=self.created_at,
            updated_at=timestamp,
        )
        claim = PassageVectorClaim._create(
            job=processing,
            passage=passage,
            space=space,
        )
        return PassageVectorClaimed._create(previous=self, claim=claim)

    def complete(
        self,
        claim: PassageVectorClaim,
        vector: EmbeddingVector,
        *,
        completed_at: datetime,
    ) -> PassageVectorCompleted:
        """@brief 使用当前 capability 完成向量 / Complete the vector with the current capability.

        @param claim 当前 sealed claim / Current sealed claim.
        @param vector Provider 返回向量 / Provider-produced vector.
        @param completed_at 完成时刻 / Completion instant.
        @return sealed 完成决策 / Sealed completion decision.
        """

        self._require_claim(claim)
        vector.require_space(claim.space)
        timestamp = ensure_utc(completed_at)
        self._require_nonregressing_time(timestamp)
        completed = PassageVectorJob._create(
            key=self.key,
            state=CompletedPassageVector(vector, timestamp),
            version=self.version + 1,
            attempt_count=self.attempt_count,
            created_at=self.created_at,
            updated_at=timestamp,
        )
        return PassageVectorCompleted._create(claim=claim, job=completed)

    def schedule_retry(
        self,
        claim: PassageVectorClaim,
        *,
        retry_at: datetime,
        failure: PassageVectorFailure,
        failed_at: datetime,
    ) -> PassageVectorRetryScheduled:
        """@brief 安排下一次 embedding 尝试 / Schedule the next embedding attempt.

        @param claim 当前 sealed claim / Current sealed claim.
        @param retry_at 最早重试时刻 / Earliest retry instant.
        @param failure 安全失败摘要 / Safe failure summary.
        @param failed_at 本次失败时刻 / Current failure instant.
        @return sealed 重试决策 / Sealed retry decision.
        """

        self._require_claim(claim)
        failure_time = ensure_utc(failed_at)
        self._require_nonregressing_time(failure_time)
        retry_time = ensure_utc(retry_at)
        if retry_time <= failure_time:
            raise ValueError("Passage vector retry_at must follow failed_at")
        retrying = PassageVectorJob._create(
            key=self.key,
            state=WaitingPassageVectorRetry(retry_time, failure),
            version=self.version + 1,
            attempt_count=self.attempt_count,
            created_at=self.created_at,
            updated_at=failure_time,
        )
        return PassageVectorRetryScheduled._create(claim=claim, job=retrying)

    def fail(
        self,
        claim: PassageVectorClaim,
        *,
        failure: PassageVectorFailure,
        failed_at: datetime,
    ) -> PassageVectorFailed:
        """@brief 终结不可恢复的向量任务 / Finally fail an unrecoverable vector job.

        @param claim 当前 sealed claim / Current sealed claim.
        @param failure 安全最终失败 / Safe terminal failure.
        @param failed_at 失败时刻 / Failure instant.
        @return sealed 最终失败决策 / Sealed terminal-failure decision.
        """

        self._require_claim(claim)
        timestamp = ensure_utc(failed_at)
        self._require_nonregressing_time(timestamp)
        failed = PassageVectorJob._create(
            key=self.key,
            state=FailedPassageVector(failure),
            version=self.version + 1,
            attempt_count=self.attempt_count,
            created_at=self.created_at,
            updated_at=timestamp,
        )
        return PassageVectorFailed._create(claim=claim, job=failed)

    def recover_expired(self, *, recovered_at: datetime) -> PassageVectorLeaseRecovered:
        """@brief 把已过期 processing lease 恢复为立即重试 / Recover an expired processing lease for immediate retry.

        @param recovered_at 恢复判定与提交时刻 / Recovery decision and commit instant.
        @return sealed lease 恢复决策 / Sealed lease-recovery decision.
        @raise InvalidPassageVectorTransition 状态非 processing 或 lease 仍存活 /
            State is not processing or its lease remains live.
        """

        if not isinstance(self.state, ProcessingPassageVector):
            raise InvalidPassageVectorTransition(
                "Only processing passage vectors have recoverable leases"
            )
        timestamp = ensure_utc(recovered_at)
        if self.state.lease_expires_at > timestamp:
            raise InvalidPassageVectorTransition(
                "Live passage vector lease cannot be recovered"
            )
        retrying = PassageVectorJob._create(
            key=self.key,
            state=WaitingPassageVectorRetry(
                timestamp,
                PassageVectorFailure(RECOVERED_PASSAGE_VECTOR_LEASE_ERROR),
            ),
            version=self.version + 1,
            attempt_count=self.attempt_count,
            created_at=self.created_at,
            updated_at=timestamp,
        )
        return PassageVectorLeaseRecovered._create(previous=self, job=retrying)

    def _require_claim(self, claim: PassageVectorClaim) -> None:
        """@brief 要求 capability 精确绑定当前 processing 快照 / Require a capability bound to this exact processing snapshot.

        @param claim 待验证 capability / Capability to validate.
        @return None / None.
        @raise InvalidPassageVectorTransition Claim 不匹配当前聚合 / Claim does not match the aggregate.
        """

        if not isinstance(claim, PassageVectorClaim) or claim.job != self:
            raise InvalidPassageVectorTransition(
                "Passage vector claim does not match the processing snapshot"
            )
        if not isinstance(self.state, ProcessingPassageVector):
            raise InvalidPassageVectorTransition(
                "Passage vector claim requires a processing job"
            )

    def _require_nonregressing_time(self, occurred_at: datetime) -> None:
        """@brief 禁止 settlement 时间倒退 / Prevent settlement-time regression.

        @param occurred_at 已规范的转换时刻 / Normalized transition instant.
        @return None / None.
        @raise ValueError 转换早于当前 processing 版本 / Transition predates the current processing version.
        """

        if occurred_at < self.updated_at:
            raise ValueError(
                "Passage vector transition cannot precede the current version"
            )


@dataclass(frozen=True, slots=True, init=False)
class PassageVectorClaim(_SealedDomainObject):
    """@brief 绑定领域工作与 processing 快照的 sealed capability / Sealed capability binding work to a processing snapshot.

    @param job 已领取的完整聚合快照 / Complete claimed aggregate snapshot.
    @param passage 待 embedding Passage / Passage to embed.
    @param space 目标 embedding space / Target embedding space.
    """

    job: PassageVectorJob
    passage: RetrievalPassage
    space: EmbeddingSpace

    @classmethod
    def _create(
        cls,
        *,
        job: PassageVectorJob,
        passage: RetrievalPassage,
        space: EmbeddingSpace,
    ) -> Self:
        """@brief 从领域 claim 决策创建 capability / Create a capability from a domain claim decision.

        @param job 领取后的 processing 聚合 / Claimed processing aggregate.
        @param passage 待 embedding Passage / Passage to embed.
        @param space 目标 embedding space / Target embedding space.
        @return 已验证 sealed capability / Validated sealed capability.
        """

        if not isinstance(job, PassageVectorJob) or not isinstance(
            job.state, ProcessingPassageVector
        ):
            raise TypeError("Passage vector claim requires a processing job")
        if not isinstance(passage, RetrievalPassage):
            raise TypeError("Passage vector claim requires a RetrievalPassage")
        if not isinstance(space, EmbeddingSpace):
            raise TypeError("Passage vector claim requires an EmbeddingSpace")
        if job.key.passage_id != passage.passage_id:
            raise ValueError("Passage vector claim passage does not match its job key")
        if job.key.space_id != space.space_id:
            raise ValueError("Passage vector claim space does not match its job key")
        if passage.format_version != space.passage_format_version:
            raise ValueError("Passage vector claim format does not match its space")
        claim = object.__new__(cls)
        object.__setattr__(claim, "job", job)
        object.__setattr__(claim, "passage", passage)
        object.__setattr__(claim, "space", space)
        return claim


@dataclass(frozen=True, slots=True, init=False)
class PassageVectorClaimed(_SealedDomainObject):
    """@brief sealed 领取决策 / Sealed claim decision.

    @param previous 领取前聚合 / Aggregate before claiming.
    @param job 领取后聚合 / Aggregate after claiming.
    @param claim 新 capability / New capability.
    """

    previous: PassageVectorJob
    job: PassageVectorJob
    claim: PassageVectorClaim

    @classmethod
    def _create(
        cls,
        *,
        previous: PassageVectorJob,
        claim: PassageVectorClaim,
    ) -> Self:
        """@brief 创建 sealed 领取决策 / Create a sealed claim decision.

        @param previous 领取前聚合 / Pre-claim aggregate.
        @param claim 新 capability / New capability.
        @return 领取决策 / Claim decision.
        """

        decision = object.__new__(cls)
        object.__setattr__(decision, "previous", previous)
        object.__setattr__(decision, "job", claim.job)
        object.__setattr__(decision, "claim", claim)
        return decision


@dataclass(frozen=True, slots=True, init=False)
class PassageVectorCompleted(_SealedDomainObject):
    """@brief sealed 完成决策 / Sealed completion decision."""

    claim: PassageVectorClaim
    job: PassageVectorJob

    @classmethod
    def _create(
        cls,
        *,
        claim: PassageVectorClaim,
        job: PassageVectorJob,
    ) -> Self:
        """@brief 创建 sealed 完成决策 / Create a sealed completion decision.

        @return 完成决策 / Completion decision.
        """

        decision = object.__new__(cls)
        object.__setattr__(decision, "claim", claim)
        object.__setattr__(decision, "job", job)
        return decision


@dataclass(frozen=True, slots=True, init=False)
class PassageVectorRetryScheduled(_SealedDomainObject):
    """@brief sealed 重试决策 / Sealed retry decision."""

    claim: PassageVectorClaim
    job: PassageVectorJob

    @classmethod
    def _create(
        cls,
        *,
        claim: PassageVectorClaim,
        job: PassageVectorJob,
    ) -> Self:
        """@brief 创建 sealed 重试决策 / Create a sealed retry decision.

        @return 重试决策 / Retry decision.
        """

        decision = object.__new__(cls)
        object.__setattr__(decision, "claim", claim)
        object.__setattr__(decision, "job", job)
        return decision


@dataclass(frozen=True, slots=True, init=False)
class PassageVectorFailed(_SealedDomainObject):
    """@brief sealed 最终失败决策 / Sealed terminal-failure decision."""

    claim: PassageVectorClaim
    job: PassageVectorJob

    @classmethod
    def _create(
        cls,
        *,
        claim: PassageVectorClaim,
        job: PassageVectorJob,
    ) -> Self:
        """@brief 创建 sealed 最终失败决策 / Create a sealed terminal-failure decision.

        @return 最终失败决策 / Terminal-failure decision.
        """

        decision = object.__new__(cls)
        object.__setattr__(decision, "claim", claim)
        object.__setattr__(decision, "job", job)
        return decision


@dataclass(frozen=True, slots=True, init=False)
class PassageVectorLeaseRecovered(_SealedDomainObject):
    """@brief sealed 过期 lease 恢复决策 / Sealed expired-lease recovery decision."""

    previous: PassageVectorJob
    job: PassageVectorJob

    @classmethod
    def _create(
        cls,
        *,
        previous: PassageVectorJob,
        job: PassageVectorJob,
    ) -> Self:
        """@brief 创建 sealed lease 恢复决策 / Create a sealed lease-recovery decision.

        @return Lease 恢复决策 / Lease-recovery decision.
        """

        decision = object.__new__(cls)
        object.__setattr__(decision, "previous", previous)
        object.__setattr__(decision, "job", job)
        return decision


__all__ = [
    "AwaitingPassageVector",
    "CompletedPassageVector",
    "FailedPassageVector",
    "InvalidPassageVectorTransition",
    "PassageVectorClaim",
    "PassageVectorClaimed",
    "PassageVectorCompleted",
    "PassageVectorFailed",
    "PassageVectorFailure",
    "PassageVectorJob",
    "PassageVectorJobKey",
    "PassageVectorLeaseRecovered",
    "PassageVectorRetryScheduled",
    "PassageVectorState",
    "PassageVectorStatus",
    "ProcessingPassageVector",
    "RECOVERED_PASSAGE_VECTOR_LEASE_ERROR",
    "WaitingPassageVectorRetry",
]
