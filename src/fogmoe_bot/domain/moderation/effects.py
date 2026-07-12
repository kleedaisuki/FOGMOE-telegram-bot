"""@brief 可持久化治理副作用意图 / Persistable moderation-effect intents."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Self
from uuid import UUID, uuid5

from .models import (
    ChatId,
    EnforcementFailureMode,
    MessageId,
    RuleKind,
    UserId,
)


_MODERATION_EFFECT_NAMESPACE = UUID("85fd59e6-3151-54e5-87aa-64c292a2900d")
"""@brief Update 到治理副作用的 UUIDv5 命名空间 / UUIDv5 namespace for Update-to-moderation effects."""


class ModerationEffectKind(StrEnum):
    """@brief 治理副作用类别 / Moderation-effect kind."""

    SPAM_ENFORCEMENT = "spam_enforcement"
    """@brief 删除垃圾消息并警告 / Delete spam and warn."""

    KEYWORD_REPLY = "keyword_reply"
    """@brief 发送关键词自动回复 / Send a keyword auto-reply."""


class ModerationEffectStatus(StrEnum):
    """@brief 治理副作用执行状态 / Moderation-effect execution status."""

    PENDING = "pending"
    """@brief 已持久化但未执行 / Persisted but not executed."""

    MESSAGE_DELETED = "message_deleted"
    """@brief 垃圾消息已删除、警告待发送 / Spam deleted, warning pending."""

    DELIVERED = "delivered"
    """@brief 全部外部效果已完成 / All external effects completed."""

    FAILED = "failed"
    """@brief 当前尝试失败、可由 inbox 重放 / Current attempt failed and may be replayed by the inbox."""


@dataclass(frozen=True, slots=True, order=True)
class ModerationEffectId:
    """@brief 稳定治理副作用 ID / Stable moderation-effect identifier.

    @param value UUID 值 / UUID value.
    """

    value: UUID

    @classmethod
    def for_update(
        cls,
        update_id: int,
        kind: ModerationEffectKind,
    ) -> Self:
        """@brief 从 Update 与效果类别推导 ID / Derive an ID from an Update and effect kind.

        @param update_id Telegram Update ID / Telegram Update identifier.
        @param kind 副作用类别 / Effect kind.
        @return 稳定 UUIDv5 ID / Stable UUIDv5 identifier.
        @raises ValueError Update ID 为负数 / If the Update ID is negative.
        """

        if update_id < 0:
            raise ValueError("Update ID cannot be negative")
        return cls(uuid5(_MODERATION_EFFECT_NAMESPACE, f"{update_id}:{kind.value}"))

    @classmethod
    def parse(cls, value: UUID | str) -> Self:
        """@brief 解析持久化 ID / Parse a persisted identifier.

        @param value UUID 或规范文本 / UUID or canonical text.
        @return 解析后的 ID / Parsed identifier.
        """

        return cls(value if isinstance(value, UUID) else UUID(str(value)))

    def __str__(self) -> str:
        """@brief 返回规范 UUID 文本 / Return canonical UUID text.

        @return UUID 文本 / UUID text.
        """

        return str(self.value)


@dataclass(frozen=True, slots=True)
class SpamEnforcementPlan:
    """@brief 垃圾消息处置意图 / Spam-message enforcement intent.

    @param effect_id 幂等副作用 ID / Idempotent effect identifier.
    @param update_id 来源 Update ID / Source Update identifier.
    @param chat_id 群组 ID / Group identifier.
    @param user_id 发送者 ID / Author identifier.
    @param message_id 消息 ID / Message identifier.
    @param matched_text 命中文本 / Matched text.
    @param rule_kind 命中规则类别 / Matched rule kind.
    @param failure_mode 删除失败传播策略 / Deletion-failure propagation policy.
    """

    effect_id: ModerationEffectId
    update_id: int
    chat_id: ChatId
    user_id: UserId
    message_id: MessageId
    matched_text: str
    rule_kind: RuleKind
    failure_mode: EnforcementFailureMode


@dataclass(frozen=True, slots=True)
class KeywordReplyPlan:
    """@brief 关键词自动回复意图 / Keyword auto-reply intent.

    @param effect_id 幂等副作用 ID / Idempotent effect identifier.
    @param update_id 来源 Update ID / Source Update identifier.
    @param chat_id 群组 ID / Group identifier.
    @param user_id 发送者 ID / Author identifier.
    @param message_id 触发消息 ID / Trigger-message identifier.
    @param keyword 命中关键词 / Matched keyword.
    @param response 回复文本 / Response text.
    """

    effect_id: ModerationEffectId
    update_id: int
    chat_id: ChatId
    user_id: UserId
    message_id: MessageId
    keyword: str
    response: str


type ModerationEffectPlan = SpamEnforcementPlan | KeywordReplyPlan
"""@brief 可持久化治理副作用意图联合 / Persistable moderation-effect intent union."""


@dataclass(frozen=True, slots=True)
class ModerationEffect:
    """@brief 治理副作用聚合 / Moderation-effect aggregate.

    @param plan 不可变效果意图 / Immutable effect intent.
    @param status 执行状态 / Execution status.
    @param version OCC 版本 / OCC version.
    @param warning_count 本次处置对应的一小时警告序号 / One-hour warning ordinal for this enforcement.
    @param last_error 最近错误摘要 / Latest error summary.
    @param updated_at 最近更新时间 / Latest update instant.
    """

    plan: ModerationEffectPlan
    status: ModerationEffectStatus
    version: int
    warning_count: int | None
    last_error: str | None
    updated_at: datetime

    def __post_init__(self) -> None:
        """@brief 验证效果聚合 / Validate the effect aggregate.

        @return None / None.
        @raises ValueError 版本、计数或时间无效 / For invalid version, count, or timestamp.
        """

        if self.version < 0:
            raise ValueError("Effect version cannot be negative")
        if self.warning_count is not None and self.warning_count < 1:
            raise ValueError("Warning count must be positive")
        if isinstance(self.plan, SpamEnforcementPlan) and self.warning_count is None:
            raise ValueError("Spam enforcement requires a warning count")
        if isinstance(self.plan, KeywordReplyPlan) and self.warning_count is not None:
            raise ValueError("Keyword replies cannot carry a warning count")
        object.__setattr__(self, "updated_at", _utc(self.updated_at))

    @property
    def effect_id(self) -> ModerationEffectId:
        """@brief 返回聚合键 / Return the aggregate key.

        @return 副作用 ID / Effect identifier.
        """

        return self.plan.effect_id

    def deleted(self, *, now: datetime) -> Self:
        """@brief 记录垃圾消息删除成功 / Record successful spam deletion.

        @param now 事件时刻 / Event instant.
        @return 下一版本聚合 / Next-version aggregate.
        @raises ValueError 非垃圾效果或已终结 / For a non-spam or terminal effect.
        """

        if not isinstance(self.plan, SpamEnforcementPlan):
            raise ValueError("Only spam enforcement has a deletion stage")
        if self.status is ModerationEffectStatus.DELIVERED:
            return self
        if self.status not in {
            ModerationEffectStatus.PENDING,
            ModerationEffectStatus.FAILED,
        }:
            raise ValueError(f"Cannot delete effect in {self.status.value}")
        return replace(
            self,
            status=ModerationEffectStatus.MESSAGE_DELETED,
            version=self.version + 1,
            last_error=None,
            updated_at=_utc(now),
        )

    def delivered(self, *, now: datetime) -> Self:
        """@brief 记录效果全部投递 / Record complete effect delivery.

        @param now 事件时刻 / Event instant.
        @return 下一版本聚合 / Next-version aggregate.
        @raises ValueError 垃圾效果尚未完成删除 / If spam has not reached its deletion stage.
        """

        if self.status is ModerationEffectStatus.DELIVERED:
            return self
        if isinstance(self.plan, SpamEnforcementPlan):
            allowed = {ModerationEffectStatus.MESSAGE_DELETED}
        else:
            allowed = {
                ModerationEffectStatus.PENDING,
                ModerationEffectStatus.FAILED,
            }
        if self.status not in allowed:
            raise ValueError(f"Cannot deliver effect in {self.status.value}")
        return replace(
            self,
            status=ModerationEffectStatus.DELIVERED,
            version=self.version + 1,
            last_error=None,
            updated_at=_utc(now),
        )

    def failed(self, error: str, *, now: datetime) -> Self:
        """@brief 记录可恢复执行失败 / Record a recoverable execution failure.

        @param error 错误摘要 / Error summary.
        @param now 事件时刻 / Event instant.
        @return 下一版本聚合 / Next-version aggregate.
        """

        if self.status is ModerationEffectStatus.DELIVERED:
            return self
        summary = error.strip()[:1000] or "unknown moderation effect failure"
        failed_status = (
            ModerationEffectStatus.MESSAGE_DELETED
            if self.status is ModerationEffectStatus.MESSAGE_DELETED
            else ModerationEffectStatus.FAILED
        )
        return replace(
            self,
            status=failed_status,
            version=self.version + 1,
            last_error=summary,
            updated_at=_utc(now),
        )


def _utc(value: datetime) -> datetime:
    """@brief 规范为 UTC 时间 / Normalize an instant to UTC.

    @param value 输入时间 / Input instant.
    @return UTC aware 时间 / UTC-aware instant.
    @raises ValueError 时间无时区 / If the instant is naive.
    """

    if value.tzinfo is None:
        raise ValueError("Moderation effect timestamps must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "KeywordReplyPlan",
    "ModerationEffect",
    "ModerationEffectId",
    "ModerationEffectKind",
    "ModerationEffectPlan",
    "ModerationEffectStatus",
    "SpamEnforcementPlan",
]
