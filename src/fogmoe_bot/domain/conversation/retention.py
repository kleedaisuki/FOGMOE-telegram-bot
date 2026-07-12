"""@brief 会话保留、压缩与永久记忆领域模型 / Conversation retention, compaction, and permanent-memory domain models."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Self, cast
from uuid import UUID, uuid5

from .payloads import (
    JsonObject,
    JsonValue,
)
from .identity import (
    ConversationId,
    LeaseToken,
    TurnId,
)
from .temporal import ensure_utc


_RETENTION_SEGMENT_NAMESPACE = UUID("f21610f9-072f-50ed-98e7-354ee460c530")
"""@brief Retention Segment 稳定 UUIDv5 命名空间 / Stable UUIDv5 namespace for retention segments."""

_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
"""@brief SHA-256 小写十六进制格式 / Lowercase hexadecimal SHA-256 format."""


@dataclass(frozen=True, slots=True, order=True)
class RetentionSegmentId:
    """@brief 永久记忆 Segment 标识 / Permanent-memory segment identifier.

    @param value 不透明 UUID / Opaque UUID.
    """

    value: UUID

    @classmethod
    def parse(cls, value: UUID | str) -> Self:
        """@brief 解析持久化 UUID / Parse a persisted UUID.

        @param value UUID 对象或文本 / UUID object or text.
        @return Segment 标识 / Segment identifier.
        """

        return cls(value if isinstance(value, UUID) else UUID(str(value)))

    @classmethod
    def for_compaction(
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
        return cls(uuid5(_RETENTION_SEGMENT_NAMESPACE, identity))

    @classmethod
    def for_legacy_record(cls, legacy_record_id: int) -> Self:
        """@brief 为旧永久记录推导稳定 ID / Derive a stable ID for a legacy permanent record.

        @param legacy_record_id 旧表主键 / Legacy-table primary key.
        @return 确定性 UUIDv5 / Deterministic UUIDv5.
        @raise ValueError 主键非正时抛出 / Raised when the key is not positive.
        """

        if isinstance(legacy_record_id, bool) or legacy_record_id <= 0:
            raise ValueError("legacy_record_id must be positive")
        legacy_digest = hashlib.md5(  # noqa: S324 - non-security stable migration identity
            f"legacy:{legacy_record_id}".encode(),
            usedforsecurity=False,
        ).hexdigest()
        return cls(UUID(legacy_digest))

    def __str__(self) -> str:
        """@brief 返回规范 UUID 文本 / Return canonical UUID text.

        @return UUID 文本 / UUID text.
        """

        return str(self.value)


@dataclass(frozen=True, slots=True, order=True)
class TokenCount:
    """@brief 非负 token 数值对象 / Non-negative token-count value object.

    @param value token 数 / Token count.
    """

    value: int

    def __post_init__(self) -> None:
        """@brief 校验 token 数 / Validate the token count.

        @return None / None.
        @raise ValueError 布尔值或负数非法 / Booleans and negative values are invalid.
        """

        if isinstance(self.value, bool) or self.value < 0:
            raise ValueError("Token count cannot be negative")

    def __int__(self) -> int:
        """@brief 返回整数 token 数 / Return the integer token count.

        @return token 数 / Token count.
        """

        return self.value


@dataclass(frozen=True, slots=True)
class ContextTokenBudget:
    """@brief 会话投影与摘要的显式 token 预算 / Explicit token budget for history projection and summarization.

    @param warning_tokens 后台压缩触发点 / Background-compaction trigger.
    @param hard_tokens 模型输入硬上限 / Hard model-input limit.
    @param summary_output_tokens 摘要输出上限 / Summary-output limit.
    @param segment_input_tokens 单次摘要输入上限 / Per-summary input limit.
    @param minimum_recent_non_tool_messages 至少保留的近期非工具消息数 / Minimum recent non-tool messages to retain.
    @param guard_ratio 启发式 token 保护系数 / Heuristic token guard ratio.
    """

    warning_tokens: TokenCount = TokenCount(114_000)
    hard_tokens: TokenCount = TokenCount(120_000)
    summary_output_tokens: TokenCount = TokenCount(2_500)
    segment_input_tokens: TokenCount = TokenCount(64_000)
    minimum_recent_non_tool_messages: int = 10
    guard_ratio: float = 1.15

    def __post_init__(self) -> None:
        """@brief 校验预算严格次序 / Validate strict budget ordering.

        @return None / None.
        @raise ValueError 预算或保护系数非法 / Raised for invalid budgets or guard ratios.
        """

        summary = int(self.summary_output_tokens)
        segment = int(self.segment_input_tokens)
        warning = int(self.warning_tokens)
        hard = int(self.hard_tokens)
        if not 0 < summary < warning < hard:
            raise ValueError("Token budgets must satisfy 0 < summary < warning < hard")
        if not summary < segment <= warning:
            raise ValueError(
                "Segment input budget must be above summary output and at most warning"
            )
        if (
            isinstance(self.minimum_recent_non_tool_messages, bool)
            or self.minimum_recent_non_tool_messages < 1
        ):
            raise ValueError("minimum_recent_non_tool_messages must be positive")
        if not math.isfinite(self.guard_ratio) or self.guard_ratio < 1.0:
            raise ValueError("guard_ratio must be finite and at least one")


class RetentionKind(StrEnum):
    """@brief Segment 的穷尽业务类别 / Exhaustive business kinds for retention segments."""

    COMPACTION = "compaction"
    LEGACY_ARCHIVE = "legacy_archive"


class RetentionStatus(StrEnum):
    """@brief durable compaction activity 状态 / Durable compaction-activity status."""

    PENDING = "pending"
    PROCESSING = "processing"
    RETRY_WAIT = "retry_wait"
    COMPLETED = "completed"
    FAILED_FINAL = "failed_final"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class RetentionSummary:
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
            raise ValueError("Retention summary cannot be blank")
        if not route_key or len(route_key) > 512:
            raise ValueError(
                "Retention summary route key must contain 1-512 characters"
            )
        if int(self.token_count) < 1:
            raise ValueError("Retention summary token count must be positive")
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "route_key", route_key)


@dataclass(frozen=True, slots=True)
class RetentionSegmentDraft:
    """@brief 不可变 Segment 来源与幂等 identity / Immutable segment source and idempotency identity.

    @param segment_id 稳定 Segment ID / Stable segment ID.
    @param kind compaction 或 legacy archive / Compaction or legacy archive.
    @param conversation_id 所属长期会话 / Owning long-lived conversation.
    @param owner_user_id 永久工具隔离使用的用户 ID / User ID used to isolate permanent-memory tools.
    @param epoch_floor_sequence 当前 reset epoch 下界 / Current reset-epoch floor.
    @param from_sequence 本次 delta 首序号 / First sequence in this delta.
    @param through_sequence 本次 delta 末序号 / Last sequence in this delta.
    @param anchor_turn_id 固定 epoch 语义的 Turn / Turn anchoring epoch semantics.
    @param predecessor_segment_id 上一个累计 checkpoint / Previous cumulative checkpoint.
    @param projection_version source snapshot 投影版本 / Source-snapshot projection version.
    @param source_digest snapshot SHA-256 / Snapshot SHA-256.
    @param source_snapshot 冻结的 provider-neutral 消息 / Frozen provider-neutral messages.
    @param source_row_count snapshot 来源数据库行数 / Number of source database rows.
    @param source_token_count snapshot token 数 / Snapshot token count.
    @param legacy_record_id 可选旧表主键 / Optional legacy-table primary key.
    @param created_at Segment 创建时间 / Segment creation time.
    """

    segment_id: RetentionSegmentId
    kind: RetentionKind
    conversation_id: ConversationId
    owner_user_id: int
    epoch_floor_sequence: int | None
    from_sequence: int | None
    through_sequence: int | None
    anchor_turn_id: TurnId | None
    predecessor_segment_id: RetentionSegmentId | None
    projection_version: int
    source_digest: str
    source_snapshot: tuple[JsonObject, ...]
    source_row_count: int
    source_token_count: TokenCount
    legacy_record_id: int | None
    created_at: datetime

    def __post_init__(self) -> None:
        """@brief 校验 kind-specific shape、stable ID 与 digest / Validate kind-specific shape, stable ID, and digest.

        @return None / None.
        @raise ValueError 任一不可变语义非法 / Raised when any immutable semantic is invalid.
        """

        if isinstance(self.owner_user_id, bool) or self.owner_user_id <= 0:
            raise ValueError("Retention owner_user_id must be positive")
        if isinstance(self.projection_version, bool) or self.projection_version < 0:
            raise ValueError("Retention projection_version cannot be negative")
        if isinstance(self.source_row_count, bool) or self.source_row_count < 0:
            raise ValueError("Retention source_row_count cannot be negative")
        if _DIGEST_PATTERN.fullmatch(self.source_digest) is None:
            raise ValueError("Retention source digest must be lowercase SHA-256")
        snapshot = _copy_snapshot(self.source_snapshot)
        digest = retention_source_digest(snapshot)
        if self.source_digest != digest:
            raise ValueError("Retention source digest does not match its snapshot")

        if self.kind is RetentionKind.COMPACTION:
            self._validate_compaction_shape()
        elif self.kind is RetentionKind.LEGACY_ARCHIVE:
            self._validate_legacy_shape()
        else:  # pragma: no cover - StrEnum construction already fences this branch.
            raise ValueError(f"Unsupported retention kind {self.kind!r}")
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
        if floor is None or start is None or end is None or self.anchor_turn_id is None:
            raise ValueError(
                "Compaction segments require epoch, range, and anchor Turn"
            )
        if floor < 0 or start <= floor or end < start:
            raise ValueError("Compaction segment range is outside its reset epoch")
        if self.projection_version < 1:
            raise ValueError("Compaction projection_version must be positive")
        if self.legacy_record_id is not None:
            raise ValueError("Compaction segments cannot carry a legacy record ID")
        if self.source_row_count < 1 or not self.source_snapshot:
            raise ValueError("Compaction source cannot be empty")
        expected = RetentionSegmentId.for_compaction(
            conversation_id=self.conversation_id,
            epoch_floor_sequence=floor,
            from_sequence=start,
            through_sequence=end,
            projection_version=self.projection_version,
        )
        if self.segment_id != expected:
            raise ValueError("Compaction segment ID does not match its immutable range")

    def _validate_legacy_shape(self) -> None:
        """@brief 校验 legacy archive provenance / Validate legacy-archive provenance.

        @return None / None.
        @raise ValueError 旧记录形状或 ID 非法 / Raised for an invalid legacy shape or identifier.
        """

        if any(
            value is not None
            for value in (
                self.epoch_floor_sequence,
                self.from_sequence,
                self.through_sequence,
                self.anchor_turn_id,
                self.predecessor_segment_id,
            )
        ):
            raise ValueError(
                "Legacy archive segments cannot invent conversation ranges"
            )
        if self.projection_version != 0:
            raise ValueError("Legacy archives require projection_version zero")
        if self.legacy_record_id is None:
            raise ValueError("Legacy archives require their original record ID")
        if self.source_row_count != len(self.source_snapshot):
            raise ValueError("Legacy archive row count must match its snapshot")
        expected = RetentionSegmentId.for_legacy_record(self.legacy_record_id)
        if self.segment_id != expected:
            raise ValueError("Legacy archive segment ID does not match its record ID")

    @classmethod
    def compaction(
        cls,
        *,
        conversation_id: ConversationId,
        owner_user_id: int,
        epoch_floor_sequence: int,
        from_sequence: int,
        through_sequence: int,
        anchor_turn_id: TurnId,
        predecessor_segment_id: RetentionSegmentId | None,
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
            segment_id=RetentionSegmentId.for_compaction(
                conversation_id=conversation_id,
                epoch_floor_sequence=epoch_floor_sequence,
                from_sequence=from_sequence,
                through_sequence=through_sequence,
                projection_version=projection_version,
            ),
            kind=RetentionKind.COMPACTION,
            conversation_id=conversation_id,
            owner_user_id=owner_user_id,
            epoch_floor_sequence=epoch_floor_sequence,
            from_sequence=from_sequence,
            through_sequence=through_sequence,
            anchor_turn_id=anchor_turn_id,
            predecessor_segment_id=predecessor_segment_id,
            projection_version=projection_version,
            source_digest=retention_source_digest(snapshot),
            source_snapshot=snapshot,
            source_row_count=source_row_count,
            source_token_count=source_token_count,
            legacy_record_id=None,
            created_at=created_at,
        )

    @classmethod
    def legacy_archive(
        cls,
        *,
        legacy_record_id: int,
        conversation_id: ConversationId,
        owner_user_id: int,
        source_snapshot: tuple[JsonObject, ...],
        source_token_count: TokenCount,
        created_at: datetime,
    ) -> Self:
        """@brief 创建旧永久记录 archive draft / Create a legacy permanent-record archive draft.

        @return 已校验 legacy draft / Validated legacy draft.
        """

        snapshot = _copy_snapshot(source_snapshot)
        return cls(
            segment_id=RetentionSegmentId.for_legacy_record(legacy_record_id),
            kind=RetentionKind.LEGACY_ARCHIVE,
            conversation_id=conversation_id,
            owner_user_id=owner_user_id,
            epoch_floor_sequence=None,
            from_sequence=None,
            through_sequence=None,
            anchor_turn_id=None,
            predecessor_segment_id=None,
            projection_version=0,
            source_digest=retention_source_digest(snapshot),
            source_snapshot=snapshot,
            source_row_count=len(snapshot),
            source_token_count=source_token_count,
            legacy_record_id=legacy_record_id,
            created_at=created_at,
        )


@dataclass(frozen=True, slots=True)
class RetentionSegment:
    """@brief Segment 输入、activity ownership 与完成 artifact 的统一聚合 / Unified aggregate for segment input, activity ownership, and completed artifact.

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

    draft: RetentionSegmentDraft
    status: RetentionStatus
    version: int
    attempt_count: int
    next_attempt_at: datetime | None
    claim_token: LeaseToken | None
    lease_expires_at: datetime | None
    completion_token: LeaseToken | None
    summary: RetentionSummary | None
    updated_at: datetime
    completed_at: datetime | None = None
    last_error: str | None = None

    def __post_init__(self) -> None:
        """@brief 校验状态形状与时间不变量 / Validate status shape and temporal invariants.

        @return None / None.
        @raise ValueError 状态字段组合非法 / Raised for an invalid status-field combination.
        """

        if self.version < 0 or self.attempt_count < 0:
            raise ValueError("Retention version and attempt_count cannot be negative")
        updated_at = ensure_utc(self.updated_at)
        if updated_at < self.draft.created_at:
            raise ValueError("Retention updated_at cannot precede created_at")
        next_attempt_at = (
            ensure_utc(self.next_attempt_at) if self.next_attempt_at else None
        )
        lease_expires_at = (
            ensure_utc(self.lease_expires_at) if self.lease_expires_at else None
        )
        completed_at = ensure_utc(self.completed_at) if self.completed_at else None
        claim_pair = self.claim_token is not None and lease_expires_at is not None
        if (self.claim_token is None) != (lease_expires_at is None):
            raise ValueError("Retention claim token and lease must appear together")
        if self.status in {RetentionStatus.PENDING, RetentionStatus.RETRY_WAIT}:
            if next_attempt_at is None or claim_pair or completed_at is not None:
                raise ValueError(
                    "Claimable retention segment has an invalid state shape"
                )
            if self.completion_token is not None or self.summary is not None:
                raise ValueError(
                    "Claimable retention segment cannot carry completion data"
                )
        elif self.status is RetentionStatus.PROCESSING:
            if (
                next_attempt_at is not None
                or not claim_pair
                or completed_at is not None
            ):
                raise ValueError(
                    "Processing retention segment requires exactly one lease"
                )
            if lease_expires_at is not None and lease_expires_at <= updated_at:
                raise ValueError("Retention lease must expire after claim time")
            if self.completion_token is not None or self.summary is not None:
                raise ValueError("Processing retention segment cannot be completed")
        elif self.status is RetentionStatus.COMPLETED:
            if next_attempt_at is not None or claim_pair or completed_at is None:
                raise ValueError(
                    "Completed retention segment has an invalid state shape"
                )
            if self.completion_token is None:
                raise ValueError(
                    "Completed retention segment requires a completion token"
                )
            if self.draft.kind is RetentionKind.COMPACTION and self.summary is None:
                raise ValueError("Completed compaction segment requires a summary")
        elif self.status in {
            RetentionStatus.FAILED_FINAL,
            RetentionStatus.CANCELLED,
        }:
            if (
                next_attempt_at is not None
                or claim_pair
                or completed_at is None
                or self.completion_token is not None
                or self.summary is not None
            ):
                raise ValueError(
                    "Terminal retention segment has an invalid state shape"
                )
        else:  # pragma: no cover - StrEnum construction already fences this branch.
            raise ValueError(f"Unsupported retention status {self.status!r}")
        if self.summary is not None and completed_at is not None:
            if int(self.summary.token_count) > 2_500:
                raise ValueError("Retention summary exceeds the product token limit")
        object.__setattr__(self, "updated_at", updated_at)
        object.__setattr__(self, "next_attempt_at", next_attempt_at)
        object.__setattr__(self, "lease_expires_at", lease_expires_at)
        object.__setattr__(self, "completed_at", completed_at)

    @property
    def segment_id(self) -> RetentionSegmentId:
        return self.draft.segment_id

    @classmethod
    def pending(cls, draft: RetentionSegmentDraft) -> Self:
        """@brief 建立初始待领取 activity / Create an initial claimable activity.

        @param draft 不可变 Segment / Immutable segment.
        @return PENDING 聚合 / Pending aggregate.
        @raise ValueError legacy archive 不能执行 compaction / Legacy archives cannot run compaction.
        """

        if draft.kind is not RetentionKind.COMPACTION:
            raise ValueError("Only compaction segments can enter the work queue")
        return cls(
            draft=draft,
            status=RetentionStatus.PENDING,
            version=0,
            attempt_count=0,
            next_attempt_at=draft.created_at,
            claim_token=None,
            lease_expires_at=None,
            completion_token=None,
            summary=None,
            updated_at=draft.created_at,
        )

    @classmethod
    def imported(
        cls,
        draft: RetentionSegmentDraft,
        *,
        summary: RetentionSummary | None,
    ) -> Self:
        """@brief 把旧永久记录表示成已完成 archive / Represent a legacy record as a completed archive.

        @param draft legacy archive draft / Legacy archive draft.
        @param summary 可选旧摘要 / Optional legacy summary.
        @return COMPLETED archive / Completed archive.
        """

        if draft.kind is not RetentionKind.LEGACY_ARCHIVE:
            raise ValueError("Only legacy archives may be imported directly")
        token = LeaseToken.parse(draft.segment_id.value)
        return cls(
            draft=draft,
            status=RetentionStatus.COMPLETED,
            version=0,
            attempt_count=0,
            next_attempt_at=None,
            claim_token=None,
            lease_expires_at=None,
            completion_token=token,
            summary=summary,
            updated_at=draft.created_at,
            completed_at=draft.created_at,
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
        @raise RetentionStateError 状态或时间不可领取 / Status or time is not claimable.
        """

        timestamp = ensure_utc(claimed_at)
        if self.status not in {RetentionStatus.PENDING, RetentionStatus.RETRY_WAIT}:
            raise RetentionStateError("Retention segment is not claimable")
        if self.next_attempt_at is None or self.next_attempt_at > timestamp:
            raise RetentionStateError("Retention segment is not ready yet")
        if lease_for <= timedelta():
            raise ValueError("Retention lease_for must be positive")
        return replace(
            self,
            status=RetentionStatus.PROCESSING,
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
        summary: RetentionSummary,
        completed_at: datetime,
    ) -> Self:
        """@brief 用 fencing token 完成摘要 / Complete a summary using a fencing token.

        @return COMPLETED 聚合 / Completed aggregate.
        @raise StaleRetentionClaimError token 或状态已失效 / Claim token or status is stale.
        """

        timestamp = ensure_utc(completed_at)
        self._require_current_claim(token)
        if timestamp < self.updated_at:
            raise ValueError("Retention completion cannot precede claim time")
        return replace(
            self,
            status=RetentionStatus.COMPLETED,
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
            raise ValueError("Retention retry times are invalid")
        return replace(
            self,
            status=RetentionStatus.RETRY_WAIT,
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
            raise ValueError("Retention failure cannot precede claim time")
        return replace(
            self,
            status=RetentionStatus.FAILED_FINAL,
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
        @raise RetentionStateError lease 尚未过期 / Lease is not expired.
        """

        timestamp = ensure_utc(now)
        if (
            self.status is not RetentionStatus.PROCESSING
            or self.lease_expires_at is None
            or self.lease_expires_at > timestamp
        ):
            raise RetentionStateError("Retention lease is not expired")
        return replace(
            self,
            status=RetentionStatus.RETRY_WAIT,
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
        @raise StaleRetentionClaimError 状态或 token 不匹配 / Status or token does not match.
        """

        if self.status is not RetentionStatus.PROCESSING or self.claim_token != token:
            raise StaleRetentionClaimError(
                f"Stale retention claim for {self.segment_id}"
            )


@dataclass(frozen=True, slots=True)
class RetentionEnqueueResult:
    """@brief 幂等 enqueue 结果 / Idempotent enqueue result.

    @param segment 规范聚合 / Canonical aggregate.
    @param inserted 是否本次插入 / Whether inserted by this call.
    """

    segment: RetentionSegment
    inserted: bool


class RetentionStateError(RuntimeError):
    """@brief 非法 retention 状态转移 / Illegal retention-state transition."""


class RetentionIdempotencyConflictError(RuntimeError):
    """@brief 相同 Segment ID 被不同来源语义复用 / Same segment ID reused for different source semantics."""


class StaleRetentionClaimError(RuntimeError):
    """@brief worker 使用了已被替换的 fencing token / Worker used a superseded fencing token."""


def retention_source_digest(snapshot: tuple[JsonObject, ...]) -> str:
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
        raise TypeError("Retention source snapshot must contain only JSON objects")
    return tuple(cast(JsonObject, item) for item in decoded)


def _required_error(error: str) -> str:
    """@brief 规范化有界错误摘要 / Normalize a bounded error summary.

    @param error 原错误文本 / Raw error text.
    @return 1..2000 字符错误 / Error containing 1..2000 characters.
    """

    normalized = error.strip()
    if not normalized:
        raise ValueError("Retention error cannot be blank")
    return normalized[:2000]


__all__ = [
    "ContextTokenBudget",
    "RetentionEnqueueResult",
    "RetentionIdempotencyConflictError",
    "RetentionKind",
    "RetentionSegment",
    "RetentionSegmentDraft",
    "RetentionSegmentId",
    "RetentionStateError",
    "RetentionStatus",
    "RetentionSummary",
    "StaleRetentionClaimError",
    "TokenCount",
    "retention_source_digest",
]
