"""@brief Context Window 压缩领域模型 / Context-window compaction domain models."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Self, cast
from uuid import UUID, uuid5

from fogmoe_bot.domain.context_window.budget import TokenCount
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    LeaseToken,
    TurnId,
)
from fogmoe_bot.domain.conversation.payloads import (
    JsonObject,
    JsonValue,
)
from fogmoe_bot.domain.temporal import ensure_utc

_COMPACTION_NAMESPACE = UUID("f21610f9-072f-50ed-98e7-354ee460c530")
"""@brief Compaction 稳定 UUIDv5 命名空间 / Stable UUIDv5 namespace for compactions."""

_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
"""@brief SHA-256 小写十六进制格式 / Lowercase hexadecimal SHA-256 format."""


@dataclass(frozen=True, slots=True, order=True)
class CompactionId:
    """@brief Context Window Compaction 标识 / Context-window compaction identifier.

    @param value 不透明 UUID / Opaque UUID.
    """

    value: UUID

    @classmethod
    def parse(cls, value: UUID | str) -> Self:
        """@brief 解析持久化 UUID / Parse a persisted UUID.

        @param value UUID 对象或文本 / UUID object or text.
        @return Compaction 标识 / Compaction identifier.
        """

        return cls(value if isinstance(value, UUID) else UUID(str(value)))

    @classmethod
    def for_range(
        cls,
        *,
        conversation_id: ConversationId,
        epoch_floor_sequence: int,
        from_sequence: int,
        through_sequence: int,
        projection_version: int,
    ) -> Self:
        """@brief 从不可变压缩范围推导稳定 ID / Derive a stable ID from an immutable compaction range.

        @param conversation_id 会话 ID / Conversation identifier.
        @param epoch_floor_sequence reset epoch 下界 / Reset-epoch floor.
        @param from_sequence delta 首序号 / First delta sequence.
        @param through_sequence delta 末序号 / Last delta sequence.
        @param projection_version 投影算法版本 / Projection algorithm version.
        @return 确定性 UUIDv5 / Deterministic UUIDv5.
        """

        identity = (
            f"compaction:{conversation_id}:{epoch_floor_sequence}:"
            f"{from_sequence}:{through_sequence}:v{projection_version}"
        )
        return cls(uuid5(_COMPACTION_NAMESPACE, identity))

    def __str__(self) -> str:
        """@brief 返回规范 UUID 文本 / Return canonical UUID text.

        @return UUID 文本 / UUID text.
        """

        return str(self.value)


class CompactionStatus(StrEnum):
    """@brief durable compaction activity 状态 / Durable compaction-activity status."""

    PENDING = "pending"
    PROCESSING = "processing"
    RETRY_WAIT = "retry_wait"
    COMPLETED = "completed"
    FAILED_FINAL = "failed_final"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class CompactionSummary:
    """@brief 一个 provider-neutral 累计会话摘要 / One provider-neutral cumulative conversation summary.

    @param text 摘要正文 / Summary text.
    @param token_count 摘要 token 数 / Summary token count.
    @param route_key 生成摘要的 provider/model 或 deterministic route / Provider/model or deterministic route that produced the summary.
    """

    text: str
    token_count: TokenCount
    route_key: str

    def __post_init__(self) -> None:
        """@brief 规范化并校验摘要 / Normalize and validate the summary.

        @return None / None.
        @raise ValueError 摘要、route 或 token 数为空 / Raised for blank text, route, or zero tokens.
        """

        text = self.text.strip()
        route_key = self.route_key.strip()
        if not text:
            raise ValueError("Compaction summary cannot be blank")
        if not route_key or len(route_key) > 512:
            raise ValueError(
                "Compaction summary route key must contain 1-512 characters"
            )
        if int(self.token_count) < 1:
            raise ValueError("Compaction summary token count must be positive")
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "route_key", route_key)


@dataclass(frozen=True, slots=True)
class CompactionPlan:
    """@brief 不可变 Compaction 来源与幂等 identity / Immutable compaction source and idempotency identity.

    @param compaction_id 稳定 Compaction ID / Stable compaction ID.
    @param conversation_id 所属长期会话 / Owning long-lived conversation.
    @param owner_user_id Context State 所有者 / Owner of the Context State.
    @param epoch_floor_sequence 当前 reset epoch 下界 / Current reset-epoch floor.
    @param from_sequence 本次 delta 首序号 / First sequence in this delta.
    @param through_sequence 本次 delta 末序号 / Last sequence in this delta.
    @param anchor_turn_id 固定 epoch 语义的 Turn / Turn anchoring epoch semantics.
    @param predecessor_compaction_id 上一个累计 checkpoint / Previous cumulative checkpoint.
    @param projection_version source snapshot 投影版本 / Source-snapshot projection version.
    @param source_digest snapshot SHA-256 / Snapshot SHA-256.
    @param source_snapshot 冻结的 provider-neutral 消息 / Frozen provider-neutral messages.
    @param source_row_count snapshot 来源数据库行数 / Number of source database rows.
    @param source_token_count snapshot token 数 / Snapshot token count.
    @param created_at Compaction 创建时间 / Compaction creation time.
    """

    compaction_id: CompactionId
    conversation_id: ConversationId
    owner_user_id: int
    epoch_floor_sequence: int
    from_sequence: int
    through_sequence: int
    anchor_turn_id: TurnId
    predecessor_compaction_id: CompactionId | None
    projection_version: int
    source_digest: str
    source_snapshot: tuple[JsonObject, ...]
    source_row_count: int
    source_token_count: TokenCount
    created_at: datetime

    def __post_init__(self) -> None:
        """@brief 校验 range、stable ID 与 digest / Validate the range, stable ID, and digest.

        @return None / None.
        @raise ValueError 任一不可变语义非法 / Raised when any immutable semantic is invalid.
        """

        if isinstance(self.owner_user_id, bool) or self.owner_user_id <= 0:
            raise ValueError("Compaction owner_user_id must be positive")
        if isinstance(self.projection_version, bool) or self.projection_version < 1:
            raise ValueError("Compaction projection_version must be positive")
        if isinstance(self.source_row_count, bool) or self.source_row_count < 1:
            raise ValueError("Compaction source_row_count must be positive")
        if _DIGEST_PATTERN.fullmatch(self.source_digest) is None:
            raise ValueError("Compaction source digest must be lowercase SHA-256")
        snapshot = _copy_snapshot(self.source_snapshot)
        digest = compaction_source_digest(snapshot)
        if self.source_digest != digest:
            raise ValueError("Compaction source digest does not match its snapshot")

        self._validate_compaction_shape()
        object.__setattr__(self, "source_snapshot", snapshot)
        object.__setattr__(self, "created_at", ensure_utc(self.created_at))

    def _validate_compaction_shape(self) -> None:
        """@brief 校验 compaction range 与稳定 ID / Validate a compaction range and stable ID.

        @return None / None.
        @raise ValueError range、anchor、版本或 ID 非法 / Raised for an invalid range, anchor, version, or ID.
        """

        floor = self.epoch_floor_sequence
        start = self.from_sequence
        end = self.through_sequence
        if floor < 0 or start <= floor or end < start:
            raise ValueError("Compaction range is outside its reset epoch")
        if not self.source_snapshot:
            raise ValueError("Compaction source cannot be empty")
        expected = CompactionId.for_range(
            conversation_id=self.conversation_id,
            epoch_floor_sequence=floor,
            from_sequence=start,
            through_sequence=end,
            projection_version=self.projection_version,
        )
        if self.compaction_id != expected:
            raise ValueError("Compaction ID does not match its immutable range")

    @classmethod
    def create(
        cls,
        *,
        conversation_id: ConversationId,
        owner_user_id: int,
        epoch_floor_sequence: int,
        from_sequence: int,
        through_sequence: int,
        anchor_turn_id: TurnId,
        predecessor_compaction_id: CompactionId | None,
        projection_version: int,
        source_snapshot: tuple[JsonObject, ...],
        source_row_count: int,
        source_token_count: TokenCount,
        created_at: datetime,
    ) -> Self:
        """@brief 创建确定性 compaction draft / Create a deterministic compaction draft.

        @return 已校验 draft / Validated draft.
        """

        snapshot = _copy_snapshot(source_snapshot)
        return cls(
            compaction_id=CompactionId.for_range(
                conversation_id=conversation_id,
                epoch_floor_sequence=epoch_floor_sequence,
                from_sequence=from_sequence,
                through_sequence=through_sequence,
                projection_version=projection_version,
            ),
            conversation_id=conversation_id,
            owner_user_id=owner_user_id,
            epoch_floor_sequence=epoch_floor_sequence,
            from_sequence=from_sequence,
            through_sequence=through_sequence,
            anchor_turn_id=anchor_turn_id,
            predecessor_compaction_id=predecessor_compaction_id,
            projection_version=projection_version,
            source_digest=compaction_source_digest(snapshot),
            source_snapshot=snapshot,
            source_row_count=source_row_count,
            source_token_count=source_token_count,
            created_at=created_at,
        )


@dataclass(frozen=True, slots=True)
class Compaction:
    """@brief Compaction 输入、activity ownership 与完成 artifact 的统一聚合 / Unified aggregate for segment input, activity ownership, and completed artifact.

    @param draft 不可变来源 / Immutable source.
    @param status durable 状态 / Durable status.
    @param version 乐观并发版本 / Optimistic-concurrency version.
    @param attempt_count 领取次数 / Claim count.
    @param next_attempt_at 下次可领取时间 / Next claimable time.
    @param claim_token 当前 fencing token / Current fencing token.
    @param lease_expires_at 当前租约截止 / Current lease expiry.
    @param completion_token 成功 claim 的持久回执 / Durable receipt for the successful claim.
    @param summary 可选摘要 / Optional summary.
    @param updated_at 最近更新时间 / Last update time.
    @param completed_at 终态时间 / Terminal time.
    @param last_error 最近错误 / Latest error.
    """

    draft: CompactionPlan
    status: CompactionStatus
    version: int
    attempt_count: int
    next_attempt_at: datetime | None
    claim_token: LeaseToken | None
    lease_expires_at: datetime | None
    completion_token: LeaseToken | None
    summary: CompactionSummary | None
    updated_at: datetime
    completed_at: datetime | None = None
    last_error: str | None = None

    def __post_init__(self) -> None:
        """@brief 校验状态形状与时间不变量 / Validate status shape and temporal invariants.

        @return None / None.
        @raise ValueError 状态字段组合非法 / Raised for an invalid status-field combination.
        """

        if self.version < 0 or self.attempt_count < 0:
            raise ValueError("Compaction version and attempt_count cannot be negative")
        updated_at = ensure_utc(self.updated_at)
        if updated_at < self.draft.created_at:
            raise ValueError("Compaction updated_at cannot precede created_at")
        next_attempt_at = (
            ensure_utc(self.next_attempt_at) if self.next_attempt_at else None
        )
        lease_expires_at = (
            ensure_utc(self.lease_expires_at) if self.lease_expires_at else None
        )
        completed_at = ensure_utc(self.completed_at) if self.completed_at else None
        claim_pair = self.claim_token is not None and lease_expires_at is not None
        if (self.claim_token is None) != (lease_expires_at is None):
            raise ValueError("Compaction claim token and lease must appear together")
        if self.status in {CompactionStatus.PENDING, CompactionStatus.RETRY_WAIT}:
            if next_attempt_at is None or claim_pair or completed_at is not None:
                raise ValueError(
                    "Claimable compaction segment has an invalid state shape"
                )
            if self.completion_token is not None or self.summary is not None:
                raise ValueError(
                    "Claimable compaction segment cannot carry completion data"
                )
        elif self.status is CompactionStatus.PROCESSING:
            if (
                next_attempt_at is not None
                or not claim_pair
                or completed_at is not None
            ):
                raise ValueError(
                    "Processing compaction segment requires exactly one lease"
                )
            if lease_expires_at is not None and lease_expires_at <= updated_at:
                raise ValueError("Compaction lease must expire after claim time")
            if self.completion_token is not None or self.summary is not None:
                raise ValueError("Processing compaction segment cannot be completed")
        elif self.status is CompactionStatus.COMPLETED:
            if next_attempt_at is not None or claim_pair or completed_at is None:
                raise ValueError(
                    "Completed compaction segment has an invalid state shape"
                )
            if self.completion_token is None:
                raise ValueError(
                    "Completed compaction segment requires a completion token"
                )
            if self.summary is None:
                raise ValueError("Completed compaction segment requires a summary")
        elif self.status in {
            CompactionStatus.FAILED_FINAL,
            CompactionStatus.CANCELLED,
        }:
            if (
                next_attempt_at is not None
                or claim_pair
                or completed_at is None
                or self.completion_token is not None
                or self.summary is not None
            ):
                raise ValueError(
                    "Terminal compaction segment has an invalid state shape"
                )
        else:  # pragma: no cover - StrEnum construction already fences this branch.
            raise ValueError(f"Unsupported compaction status {self.status!r}")
        if self.summary is not None and completed_at is not None:
            if int(self.summary.token_count) > 2_500:
                raise ValueError("Compaction summary exceeds the product token limit")
        object.__setattr__(self, "updated_at", updated_at)
        object.__setattr__(self, "next_attempt_at", next_attempt_at)
        object.__setattr__(self, "lease_expires_at", lease_expires_at)
        object.__setattr__(self, "completed_at", completed_at)

    @property
    def compaction_id(self) -> CompactionId:
        return self.draft.compaction_id

    @classmethod
    def pending(cls, draft: CompactionPlan) -> Self:
        """@brief 建立初始待领取 activity / Create an initial claimable activity.

        @param draft 不可变 Compaction / Immutable segment.
        @return PENDING 聚合 / Pending aggregate.
        """

        return cls(
            draft=draft,
            status=CompactionStatus.PENDING,
            version=0,
            attempt_count=0,
            next_attempt_at=draft.created_at,
            claim_token=None,
            lease_expires_at=None,
            completion_token=None,
            summary=None,
            updated_at=draft.created_at,
        )

    def claim(
        self,
        *,
        token: LeaseToken,
        claimed_at: datetime,
        lease_for: timedelta,
    ) -> Self:
        """@brief 纯函数领取 activity / Purely claim the activity.

        @param token 新 fencing token / New fencing token.
        @param claimed_at 领取时刻 / Claim time.
        @param lease_for 租约长度 / Lease duration.
        @return PROCESSING 聚合 / Processing aggregate.
        @raise CompactionStateError 状态或时间不可领取 / Status or time is not claimable.
        """

        timestamp = ensure_utc(claimed_at)
        if self.status not in {CompactionStatus.PENDING, CompactionStatus.RETRY_WAIT}:
            raise CompactionStateError("Compaction segment is not claimable")
        if self.next_attempt_at is None or self.next_attempt_at > timestamp:
            raise CompactionStateError("Compaction segment is not ready yet")
        if lease_for <= timedelta():
            raise ValueError("Compaction lease_for must be positive")
        return replace(
            self,
            status=CompactionStatus.PROCESSING,
            version=self.version + 1,
            attempt_count=self.attempt_count + 1,
            next_attempt_at=None,
            claim_token=token,
            lease_expires_at=timestamp + lease_for,
            updated_at=timestamp,
            last_error=None,
        )

    def complete(
        self,
        *,
        token: LeaseToken,
        summary: CompactionSummary,
        completed_at: datetime,
    ) -> Self:
        """@brief 用 fencing token 完成摘要 / Complete a summary using a fencing token.

        @return COMPLETED 聚合 / Completed aggregate.
        @raise StaleCompactionClaimError token 或状态已失效 / Claim token or status is stale.
        """

        timestamp = ensure_utc(completed_at)
        self._require_current_claim(token)
        if timestamp < self.updated_at:
            raise ValueError("Compaction completion cannot precede claim time")
        return replace(
            self,
            status=CompactionStatus.COMPLETED,
            version=self.version + 1,
            claim_token=None,
            lease_expires_at=None,
            completion_token=token,
            summary=summary,
            updated_at=timestamp,
            completed_at=timestamp,
            last_error=None,
        )

    def retry(
        self,
        *,
        token: LeaseToken,
        failed_at: datetime,
        retry_at: datetime,
        error: str,
    ) -> Self:
        """@brief 安排 fenced retry / Schedule a fenced retry.

        @return RETRY_WAIT 聚合 / Retry-wait aggregate.
        """

        failure_time = ensure_utc(failed_at)
        retry_time = ensure_utc(retry_at)
        self._require_current_claim(token)
        if failure_time < self.updated_at or retry_time <= failure_time:
            raise ValueError("Compaction retry times are invalid")
        return replace(
            self,
            status=CompactionStatus.RETRY_WAIT,
            version=self.version + 1,
            next_attempt_at=retry_time,
            claim_token=None,
            lease_expires_at=None,
            updated_at=failure_time,
            last_error=_required_error(error),
        )

    def fail_final(
        self,
        *,
        token: LeaseToken,
        failed_at: datetime,
        error: str,
    ) -> Self:
        """@brief 以当前 token 终结损坏 activity / Finally fail a corrupt activity with the current token.

        @return FAILED_FINAL 聚合 / Finally failed aggregate.
        """

        timestamp = ensure_utc(failed_at)
        self._require_current_claim(token)
        if timestamp < self.updated_at:
            raise ValueError("Compaction failure cannot precede claim time")
        return replace(
            self,
            status=CompactionStatus.FAILED_FINAL,
            version=self.version + 1,
            claim_token=None,
            lease_expires_at=None,
            updated_at=timestamp,
            completed_at=timestamp,
            last_error=_required_error(error),
        )

    def recover_expired(self, *, now: datetime) -> Self:
        """@brief 回收已过期 processing lease / Recover an expired processing lease.

        @param now 当前 UTC 时间 / Current UTC time.
        @return RETRY_WAIT 聚合 / Retry-wait aggregate.
        @raise CompactionStateError lease 尚未过期 / Lease is not expired.
        """

        timestamp = ensure_utc(now)
        if (
            self.status is not CompactionStatus.PROCESSING
            or self.lease_expires_at is None
            or self.lease_expires_at > timestamp
        ):
            raise CompactionStateError("Compaction lease is not expired")
        return replace(
            self,
            status=CompactionStatus.RETRY_WAIT,
            version=self.version + 1,
            next_attempt_at=timestamp + timedelta(microseconds=1),
            claim_token=None,
            lease_expires_at=None,
            updated_at=timestamp,
            last_error="recovered expired compaction lease",
        )

    def _require_current_claim(self, token: LeaseToken) -> None:
        """@brief 验证 processing token / Validate the processing token.

        @param token 调用方 token / Caller token.
        @return None / None.
        @raise StaleCompactionClaimError 状态或 token 不匹配 / Status or token does not match.
        """

        if self.status is not CompactionStatus.PROCESSING or self.claim_token != token:
            raise StaleCompactionClaimError(
                f"Stale compaction claim for {self.compaction_id}"
            )


@dataclass(frozen=True, slots=True)
class CompactionEnqueueResult:
    """@brief 幂等 enqueue 结果 / Idempotent enqueue result.

    @param compaction 规范聚合 / Canonical aggregate.
    @param inserted 是否本次插入 / Whether inserted by this call.
    """

    compaction: Compaction
    inserted: bool


class CompactionStateError(RuntimeError):
    """@brief 非法 compaction 状态转移 / Illegal compaction-state transition."""


class CompactionIdempotencyConflictError(RuntimeError):
    """@brief 相同 Compaction ID 被不同来源语义复用 / Same segment ID reused for different source semantics."""


class StaleCompactionClaimError(RuntimeError):
    """@brief worker 使用了已被替换的 fencing token / Worker used a superseded fencing token."""


def compaction_source_digest(snapshot: tuple[JsonObject, ...]) -> str:
    """@brief 计算 canonical snapshot SHA-256 / Compute a canonical snapshot SHA-256.

    @param snapshot provider-neutral JSON messages / Provider-neutral JSON messages.
    @return 64 字符小写十六进制摘要 / 64-character lowercase hexadecimal digest.
    """

    canonical = json.dumps(
        snapshot,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _copy_snapshot(snapshot: tuple[JsonObject, ...]) -> tuple[JsonObject, ...]:
    """@brief 深拷贝 JSON snapshot 以隔离可变输入 / Deep-copy a JSON snapshot to isolate mutable inputs.

    @return 隔离后的 snapshot / Isolated snapshot.
    """

    encoded = json.dumps(
        snapshot,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    decoded = cast(JsonValue, json.loads(encoded))
    if not isinstance(decoded, list) or not all(
        isinstance(item, dict) for item in decoded
    ):
        raise TypeError("Compaction source snapshot must contain only JSON objects")
    return tuple(cast(JsonObject, item) for item in decoded)


def _required_error(error: str) -> str:
    """@brief 规范化有界错误摘要 / Normalize a bounded error summary.

    @param error 原错误文本 / Raw error text.
    @return 1..2000 字符错误 / Error containing 1..2000 characters.
    """

    normalized = error.strip()
    if not normalized:
        raise ValueError("Compaction error cannot be blank")
    return normalized[:2000]


__all__ = [
    "CompactionEnqueueResult",
    "CompactionIdempotencyConflictError",
    "Compaction",
    "CompactionPlan",
    "CompactionId",
    "CompactionStateError",
    "CompactionStatus",
    "CompactionSummary",
    "StaleCompactionClaimError",
    "compaction_source_digest",
]
