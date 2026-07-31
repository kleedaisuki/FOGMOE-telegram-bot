"""@brief 生成媒体制品的 durable 领域生命周期 / Durable domain lifecycle for generated-media artifacts.

领域层表达 ``available``/``claimed`` 状态以及过期、完成和恢复决策；文件路径、
原子 rename 与 fsync 仍属于基础设施。/ The domain layer expresses the
``available``/``claimed`` states and expiry, completion, and recovery decisions;
filesystem paths, atomic renames, and fsync remain infrastructure concerns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Self

from fogmoe_bot.domain.temporal import ensure_utc

from .identifiers import ArtifactId

_CLAIM_TOKEN_PATTERN = re.compile(r"^[0-9a-f]{32}$")
"""@brief artifact claim/completion fencing token grammar / Artifact claim/completion fencing-token grammar."""


class ArtifactKind(StrEnum):
    """@brief 生成媒体制品类型 / Generated-media artifact kind."""

    IMAGE = "image"
    AUDIO = "audio"


@dataclass(frozen=True, slots=True, order=True)
class ArtifactClaimToken:
    """@brief 唯一领取或完成 fencing token / Unique claim or completion fencing token.

    @param value 32 位小写十六进制值 / A 32-character lowercase hexadecimal value.
    """

    value: str
    """@brief 文件名中的规范 token / Canonical token embedded in a filename."""

    def __post_init__(self) -> None:
        """@brief 校验 fencing token / Validate the fencing token.

        @return None / None.
        @raise TypeError 值不是字符串时抛出 / Raised when the value is not a string.
        @raise ValueError 值不符合 grammar 时抛出 / Raised when the value violates the grammar.
        """

        if not isinstance(self.value, str):
            raise TypeError("artifact claim token must be a string")
        if _CLAIM_TOKEN_PATTERN.fullmatch(self.value) is None:
            raise ValueError(
                "artifact claim token must be 32 lowercase hexadecimal characters"
            )

    def __str__(self) -> str:
        """@brief 返回文件名值 / Return the filename value.

        @return 32 位小写十六进制值 / The 32-character lowercase hexadecimal value.
        """

        return self.value


@dataclass(frozen=True, slots=True, init=False)
class ArtifactRecord:
    """@brief 制品的不可变持久化元数据 / Immutable persisted artifact metadata."""

    artifact_id: ArtifactId
    """@brief 制品聚合身份 / Artifact aggregate identity."""
    kind: ArtifactKind
    """@brief 生成媒体类型 / Generated-media kind."""
    filename: str
    """@brief 用户可见文件名 / User-visible filename."""
    mime_type: str
    """@brief 媒体 MIME 类型 / Media MIME type."""
    size_bytes: int
    """@brief 预期 payload 字节数 / Expected payload size in bytes."""
    created_at: datetime
    """@brief 规范 UTC 创建时刻 / Canonical UTC creation instant."""
    expires_at: datetime
    """@brief 规范 UTC 过期时刻 / Canonical UTC expiry instant."""

    def __init__(self) -> None:
        """@brief 禁止绕过 ``create``/``restore`` 不变量门 / Prevent bypassing the ``create``/``restore`` invariant gate.

        @raise TypeError 始终抛出 / Always raised.
        """

        raise TypeError("ArtifactRecord must be created or restored through a factory")

    @classmethod
    def create(
        cls,
        *,
        artifact_id: ArtifactId,
        kind: ArtifactKind,
        filename: str,
        mime_type: str,
        size_bytes: int,
        created_at: datetime,
        ttl: timedelta,
    ) -> Self:
        """@brief 创建新的 available 制品记录 / Create a new available-artifact record.

        @param artifact_id 新制品标识 / New artifact identifier.
        @param kind 媒体类型 / Media kind.
        @param filename 用户可见文件名 / User-visible filename.
        @param mime_type MIME 类型 / MIME type.
        @param size_bytes payload 字节数 / Payload size in bytes.
        @param created_at 创建时刻 / Creation instant.
        @param ttl 可领取存活时间 / Claimable time to live.
        @return 已规范的记录 / Canonical record.
        """

        if not isinstance(ttl, timedelta):
            raise TypeError("artifact TTL must be a timedelta")
        if ttl <= timedelta(0):
            raise ValueError("artifact TTL must be positive")
        created = ensure_utc(created_at)
        return cls._create(
            artifact_id=artifact_id,
            kind=kind,
            filename=filename,
            mime_type=mime_type,
            size_bytes=size_bytes,
            created_at=created,
            expires_at=created + ttl,
        )

    @classmethod
    def restore(
        cls,
        *,
        artifact_id: ArtifactId,
        kind: ArtifactKind,
        filename: str,
        mime_type: str,
        size_bytes: int,
        created_at: datetime,
        expires_at: datetime,
    ) -> Self:
        """@brief 经统一不变量门恢复 manifest 记录 / Restore a manifest record through one invariant gate.

        @param artifact_id 持久化标识 / Persisted identifier.
        @param kind 持久化媒体类型 / Persisted media kind.
        @param filename 持久化文件名 / Persisted filename.
        @param mime_type 持久化 MIME / Persisted MIME type.
        @param size_bytes 持久化字节数 / Persisted byte count.
        @param created_at 持久化创建时刻 / Persisted creation instant.
        @param expires_at 持久化过期时刻 / Persisted expiry instant.
        @return 已规范记录 / Canonical record.
        """

        return cls._create(
            artifact_id=artifact_id,
            kind=kind,
            filename=filename,
            mime_type=mime_type,
            size_bytes=size_bytes,
            created_at=created_at,
            expires_at=expires_at,
        )

    @classmethod
    def _create(
        cls,
        *,
        artifact_id: ArtifactId,
        kind: ArtifactKind,
        filename: str,
        mime_type: str,
        size_bytes: int,
        created_at: datetime,
        expires_at: datetime,
    ) -> Self:
        """@brief 应用记录的唯一不变量门 / Apply the record's sole invariant gate.

        @return 已验证记录 / Validated record.
        @raise TypeError 字段类型不正确时抛出 / Raised for invalid field types.
        @raise ValueError 元数据或时间顺序不合法时抛出 /
            Raised for invalid metadata or temporal ordering.
        """

        if not isinstance(artifact_id, ArtifactId):
            raise TypeError("artifact record requires an ArtifactId")
        if not isinstance(kind, ArtifactKind):
            raise TypeError("artifact record requires an ArtifactKind")
        if not isinstance(filename, str) or not isinstance(mime_type, str):
            raise TypeError("artifact filename and MIME type must be strings")
        if (
            not filename.strip()
            or len(filename) > 200
            or "\x00" in filename
            or not mime_type.strip()
        ):
            raise ValueError("artifact filename and MIME type are invalid")
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int):
            raise TypeError("artifact size_bytes must be an integer")
        if size_bytes <= 0:
            raise ValueError("artifact size_bytes must be positive")
        created = ensure_utc(created_at)
        expires = ensure_utc(expires_at)
        if expires <= created:
            raise ValueError("artifact expires_at must be after created_at")
        instance = object.__new__(cls)
        object.__setattr__(instance, "artifact_id", artifact_id)
        object.__setattr__(instance, "kind", kind)
        object.__setattr__(instance, "filename", filename)
        object.__setattr__(instance, "mime_type", mime_type)
        object.__setattr__(instance, "size_bytes", size_bytes)
        object.__setattr__(instance, "created_at", created)
        object.__setattr__(instance, "expires_at", expires)
        return instance

    def is_expired_at(self, instant: datetime) -> bool:
        """@brief 判断制品在指定时刻是否过期 / Determine whether the artifact is expired at an instant.

        @param instant 待判断时刻 / Instant to test.
        @return ``instant >= expires_at`` 时为 True / True when ``instant >= expires_at``.
        """

        return ensure_utc(instant) >= self.expires_at

    def available(self) -> ArtifactAvailable:
        """@brief 恢复 available 状态 / Restore the available state.

        @return 可领取制品 / Claimable artifact state.
        """

        return ArtifactAvailable(self)


@dataclass(frozen=True, slots=True, init=False)
class ArtifactClaimCapability:
    """@brief 绑定制品、token 与租约的领取 capability / Claim capability binding artifact, token, and lease."""

    artifact_id: ArtifactId
    """@brief capability 所属制品 / Artifact owned by the capability."""
    token: ArtifactClaimToken
    """@brief 唯一 fencing token / Unique fencing token."""
    lease_expires_at: datetime
    """@brief 规范 UTC 租约截止 / Canonical UTC lease deadline."""

    def __init__(self) -> None:
        """@brief 禁止绕过 ``issue``/``restore`` capability 门 / Prevent bypassing the ``issue``/``restore`` gate.

        @raise TypeError 始终抛出 / Always raised.
        """

        raise TypeError(
            "ArtifactClaimCapability must be issued or restored through a factory"
        )

    @classmethod
    def issue(
        cls,
        *,
        artifact_id: ArtifactId,
        token: ArtifactClaimToken,
        claimed_at: datetime,
        lease_duration: timedelta,
    ) -> Self:
        """@brief 签发新领取 capability / Issue a new claim capability.

        @param artifact_id 目标制品 / Target artifact.
        @param token 新 fencing token / New fencing token.
        @param claimed_at 领取时刻 / Claim instant.
        @param lease_duration 故障恢复租期 / Crash-recovery lease duration.
        @return 新 capability / New capability.
        """

        if not isinstance(lease_duration, timedelta):
            raise TypeError("artifact claim lease must be a timedelta")
        if lease_duration <= timedelta(0):
            raise ValueError("artifact claim lease must be positive")
        current = ensure_utc(claimed_at)
        return cls.restore(
            artifact_id=artifact_id,
            token=token,
            lease_expires_at=current + lease_duration,
        )

    @classmethod
    def restore(
        cls,
        *,
        artifact_id: ArtifactId,
        token: ArtifactClaimToken,
        lease_expires_at: datetime,
    ) -> Self:
        """@brief 从 claim 文件名恢复 capability / Restore a capability from a claim filename.

        @param artifact_id 文件名制品标识 / Filename artifact identifier.
        @param token 文件名 fencing token / Filename fencing token.
        @param lease_expires_at 文件名租约截止 / Filename lease deadline.
        @return 已验证 capability / Validated capability.
        """

        if not isinstance(artifact_id, ArtifactId):
            raise TypeError("artifact claim capability requires an ArtifactId")
        if not isinstance(token, ArtifactClaimToken):
            raise TypeError("artifact claim capability requires an ArtifactClaimToken")
        expiry = ensure_utc(lease_expires_at)
        instance = object.__new__(cls)
        object.__setattr__(instance, "artifact_id", artifact_id)
        object.__setattr__(instance, "token", token)
        object.__setattr__(instance, "lease_expires_at", expiry)
        return instance


@dataclass(frozen=True, slots=True)
class ArtifactAvailable:
    """@brief durable manifest 已发布且可领取 / Durable manifest is published and claimable.

    @param record 制品元数据 / Artifact metadata.
    """

    record: ArtifactRecord

    def __post_init__(self) -> None:
        """@brief 校验 available 状态 / Validate the available state."""

        if not isinstance(self.record, ArtifactRecord):
            raise TypeError("available artifact requires an ArtifactRecord")

    def claim(
        self,
        *,
        capability: ArtifactClaimCapability,
        claimed_at: datetime,
    ) -> ArtifactClaimed | ArtifactClaimExpiredDecision:
        """@brief 在 manifest rename 获胜后应用领取转换 / Apply a claim after the manifest rename wins.

        @param capability 与 claim 文件名一致的 capability / Capability matching the claim filename.
        @param claimed_at 领取时刻 / Claim instant.
        @return claimed 状态，或已过期终态 / Claimed state or expired terminal state.
        @raise ValueError capability 属于其他制品或租约已经截止时抛出 /
            Raised when the capability belongs to another artifact or its lease is already over.
        """

        if not isinstance(capability, ArtifactClaimCapability):
            raise TypeError("artifact claim requires a claim capability")
        if capability.artifact_id != self.record.artifact_id:
            raise ValueError("artifact claim capability identity does not match")
        current = ensure_utc(claimed_at)
        if current < self.record.created_at:
            raise ValueError("artifact cannot be claimed before its creation")
        if self.record.is_expired_at(current):
            return ArtifactClaimExpiredDecision(self.record, current)
        if capability.lease_expires_at <= current:
            raise ValueError(
                "artifact claim capability lease must end after claim time"
            )
        return ArtifactClaimed(self.record, capability)


@dataclass(frozen=True, slots=True)
class ArtifactClaimed:
    """@brief 一个 fencing capability 独占的制品 / Artifact exclusively held by a fencing capability.

    @param record 制品元数据 / Artifact metadata.
    @param capability 独占 capability / Exclusive capability.
    """

    record: ArtifactRecord
    capability: ArtifactClaimCapability

    def __post_init__(self) -> None:
        """@brief 校验 claimed 状态身份 / Validate claimed-state identity."""

        if not isinstance(self.record, ArtifactRecord):
            raise TypeError("claimed artifact requires an ArtifactRecord")
        if not isinstance(self.capability, ArtifactClaimCapability):
            raise TypeError("claimed artifact requires a claim capability")
        if self.capability.artifact_id != self.record.artifact_id:
            raise ValueError("claimed artifact capability identity does not match")
        if self.capability.lease_expires_at <= self.record.created_at:
            raise ValueError("artifact claim lease must end after artifact creation")

    def release(self) -> ArtifactAvailable:
        """@brief 计划可重试释放 / Plan a retryable release.

        @return 同一制品的 available 状态 / Available state for the same artifact.
        """

        return ArtifactAvailable(self.record)

    def complete(self, token: ArtifactClaimToken) -> ArtifactCompletionDecision:
        """@brief 计划带 tombstone token 的完成 / Plan completion with a tombstone token.

        @param token 完成 tombstone token / Completion-tombstone token.
        @return 持久化完成 tombstone 的决策 / Decision to persist a completion tombstone.
        """

        return ArtifactCompletionDecision(self.record, token)

    def recover(
        self,
        instant: datetime,
    ) -> ArtifactRecoveryDecision:
        """@brief 根据租约计划 kill-9 恢复 / Plan kill-9 recovery according to the lease.

        @param instant 恢复检查时刻 / Recovery-check instant.
        @return 仍活跃或恢复到 available / Still active or recovered to available.
        """

        current = ensure_utc(instant)
        if self.capability.lease_expires_at > current:
            return ArtifactRecoveryBlockedDecision(self, current)
        return ArtifactRecoveryReadyDecision(self, current)


@dataclass(frozen=True, slots=True)
class ArtifactRecoveryBlockedDecision:
    """@brief 恢复被活跃租约阻止的决策 / Decision that recovery is blocked by an active lease.

    @param claim 仍活跃的 claimed 状态 / Still-active claimed state.
    @param observed_at 规范 UTC 检查时刻 / Canonical UTC observation instant.
    """

    claim: ArtifactClaimed
    observed_at: datetime

    def __post_init__(self) -> None:
        """@brief 校验活跃 claim 决策 / Validate the active-claim decision."""

        if not isinstance(self.claim, ArtifactClaimed):
            raise TypeError("blocked recovery decision requires ArtifactClaimed")
        observed = ensure_utc(self.observed_at)
        if observed >= self.claim.capability.lease_expires_at:
            raise ValueError("recovery cannot be blocked after the claim lease")
        object.__setattr__(self, "observed_at", observed)


@dataclass(frozen=True, slots=True)
class ArtifactRecoveryReadyDecision:
    """@brief 租约过期 claim 可恢复为 available / A lease-expired claim is ready to recover to available.

    @param claim 原 claimed 状态 / Previous claimed state.
    @param observed_at 规范 UTC 检查时刻 / Canonical UTC observation instant.
    """

    claim: ArtifactClaimed
    observed_at: datetime

    def __post_init__(self) -> None:
        """@brief 校验可恢复决策 / Validate the recovery-ready decision."""

        if not isinstance(self.claim, ArtifactClaimed):
            raise TypeError("ready recovery decision requires ArtifactClaimed")
        observed = ensure_utc(self.observed_at)
        if observed < self.claim.capability.lease_expires_at:
            raise ValueError("artifact recovery cannot precede its claim lease")
        object.__setattr__(self, "observed_at", observed)


@dataclass(frozen=True, slots=True)
class ArtifactCompletionDecision:
    """@brief 将 claimed 线性化为 durable completion tombstone 的决策 / Decision to linearize a claim as a durable completion tombstone.

    @param record 已投递制品的元数据 / Metadata of the delivered artifact.
    @param token 完成 fencing token / Completion fencing token.
    """

    record: ArtifactRecord
    token: ArtifactClaimToken

    def __post_init__(self) -> None:
        """@brief 校验完成决策 / Validate the completion decision."""

        if not isinstance(self.record, ArtifactRecord):
            raise TypeError("completion decision requires an ArtifactRecord")
        if not isinstance(self.token, ArtifactClaimToken):
            raise TypeError("completion decision requires an ArtifactClaimToken")


@dataclass(frozen=True, slots=True)
class ArtifactClaimExpiredDecision:
    """@brief 领取线性化后判定制品已过期 / Decision that an artifact is expired after claim linearization.

    @param record 已过期制品 / Expired artifact record.
    @param observed_at 规范 UTC 过期观测时刻 / Canonical UTC expiry-observation instant.
    """

    record: ArtifactRecord
    observed_at: datetime

    def __post_init__(self) -> None:
        """@brief 校验过期领取决策 / Validate the expired-claim decision."""

        if not isinstance(self.record, ArtifactRecord):
            raise TypeError("expired artifact requires an ArtifactRecord")
        observed = ensure_utc(self.observed_at)
        if not self.record.is_expired_at(observed):
            raise ValueError("artifact cannot expire before expires_at")
        object.__setattr__(self, "observed_at", observed)


type ArtifactRecoveryDecision = (
    ArtifactRecoveryBlockedDecision | ArtifactRecoveryReadyDecision
)
"""@brief claim 恢复的穷尽决策和 / Exhaustive claim-recovery decision sum."""
