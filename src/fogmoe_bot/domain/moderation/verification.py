"""@brief 成员验证纯领域聚合 / Pure member-verification domain aggregate."""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from .models import ChatId, MessageId, UserId


_TOKEN_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
"""@brief SHA-256 十六进制摘要格式 / SHA-256 hexadecimal digest format."""


class VerificationError(ValueError):
    """@brief 成员验证领域错误 / Member-verification domain error."""


class StaleVerificationVersion(VerificationError):
    """@brief 操作引用了陈旧聚合版本 / Operation referenced a stale aggregate version."""


class InvalidVerificationTransition(VerificationError):
    """@brief 当前状态不接受指定事件 / Current state does not accept the event."""


class VerificationNotFound(VerificationError):
    """@brief 指定验证聚合不存在 / Requested verification aggregate does not exist."""


class VerificationFencingError(VerificationError):
    """@brief worker 使用了过期或被取代的 claim / Worker used an expired or superseded claim."""


class VerificationStatus(StrEnum):
    """@brief 成员验证持久化生命周期 / Persisted member-verification lifecycle."""

    CREATING = "creating"
    """@brief 已提交创建意图，等待 Telegram 限制与欢迎消息 / Creation intent committed before Telegram setup."""

    PENDING = "pending"
    """@brief 等待用户验证 / Waiting for user verification."""

    PASSING = "passing"
    """@brief 通过决定已提交，等待解除限制副作用 / Pass decision committed; unrestriction is pending."""

    EXPIRING = "expiring"
    """@brief 超时决定已提交，等待移出成员副作用 / Expiry decision committed; removal is pending."""

    CANCELLING = "cancelling"
    """@brief 取消决定已提交，等待清理副作用 / Cancellation committed; cleanup is pending."""

    PASSED = "passed"
    """@brief 通过副作用已确认 / Pass side effects acknowledged."""

    EXPIRED = "expired"
    """@brief 超时副作用已确认 / Expiry side effects acknowledged."""

    CANCELLED = "cancelled"
    """@brief 取消副作用已确认 / Cancellation side effects acknowledged."""

    @property
    def needs_delivery(self) -> bool:
        """@brief 状态是否需要外部副作用 / Whether the state requires external side effects.

        @return 处于过渡投递状态时为 True / True for transitional delivery states.
        """

        return self in {
            VerificationStatus.PASSING,
            VerificationStatus.EXPIRING,
            VerificationStatus.CANCELLING,
        }

    @property
    def terminal(self) -> bool:
        """@brief 状态是否终结 / Whether the state is terminal.

        @return 终态时为 True / True for terminal states.
        """

        return self in {
            VerificationStatus.PASSED,
            VerificationStatus.EXPIRED,
            VerificationStatus.CANCELLED,
        }


class VerificationEvent(StrEnum):
    """@brief 可应用到验证聚合的领域事件 / Domain events applicable to a verification aggregate."""

    ACTIVATE = "activate"
    """@brief 欢迎消息已创建 / Welcome message was created."""

    ABORT_CREATION = "abort_creation"
    """@brief 初始 Telegram 设置失败或失联 / Initial Telegram setup failed or was abandoned."""

    PASS_REQUESTED = "pass_requested"
    """@brief 用户提交有效 token / User submitted a valid token."""

    DEADLINE_REACHED = "deadline_reached"
    """@brief deadline 已到达 / Deadline was reached."""

    MEMBER_LEFT = "member_left"
    """@brief 用户在验证前离群 / Member left before verification."""

    EFFECT_DELIVERED = "effect_delivered"
    """@brief 当前过渡态外部副作用已完成 / External effects for the transitional state completed."""


@dataclass(frozen=True, slots=True, order=True)
class VerificationVersion:
    """@brief 验证聚合乐观并发版本 / Verification aggregate optimistic-concurrency version.

    @param value 从零开始的单调版本 / Monotonic version starting at zero.
    """

    value: int
    """@brief 版本原始值 / Raw version value."""

    def __post_init__(self) -> None:
        """@brief 校验版本 / Validate the version.

        @return None / None.
        """

        if isinstance(self.value, bool) or not isinstance(self.value, int):
            raise TypeError("verification version must be an integer")
        if self.value < 0:
            raise ValueError("verification version must not be negative")

    def next(self) -> VerificationVersion:
        """@brief 返回下一版本 / Return the next version.

        @return 严格加一的新版本 / New version incremented by one.
        """

        return VerificationVersion(self.value + 1)


@dataclass(frozen=True, slots=True, order=True)
class VerificationKey:
    """@brief 群组内成员验证聚合键 / Member-verification aggregate key within a chat.

    @param chat_id Telegram 群组 ID / Telegram group ID.
    @param user_id Telegram 用户 ID / Telegram user ID.
    """

    chat_id: ChatId
    """@brief 群组 ID / Chat ID."""

    user_id: UserId
    """@brief 成员 ID / Member ID."""

    def __post_init__(self) -> None:
        """@brief 校验聚合身份 / Validate aggregate identity.

        @return None / None.
        """

        if not isinstance(self.chat_id, int):
            raise TypeError("chat_id must be an integer")
        if int(self.chat_id) == 0:
            raise ValueError("chat_id must not be zero")
        if not isinstance(self.user_id, int):
            raise TypeError("user_id must be an integer")
        if int(self.user_id) <= 0:
            raise ValueError("user_id must be positive")


@dataclass(frozen=True, slots=True)
class VerificationTask:
    """@brief 版本化成员验证聚合 / Versioned member-verification aggregate.

    @param key 群组与成员复合身份 / Composite chat/member identity.
    @param version 乐观并发版本 / Optimistic-concurrency version.
    @param token_hash 验证 token 的 SHA-256 摘要 / SHA-256 digest of the token.
    @param member_name 成员展示名称 / Member display name.
    @param expires_at 验证 deadline / Verification deadline.
    @param status 生命周期状态 / Lifecycle state.
    @param message_id 可选欢迎消息 ID / Optional welcome-message ID.
    """

    key: VerificationKey
    """@brief 聚合键 / Aggregate key."""

    version: VerificationVersion
    """@brief 聚合版本 / Aggregate version."""

    token_hash: str
    """@brief token 摘要 / Token digest."""

    member_name: str
    """@brief 成员展示名称 / Member display name."""

    expires_at: datetime
    """@brief 验证 deadline / Verification deadline."""

    status: VerificationStatus = VerificationStatus.CREATING
    """@brief 生命周期状态 / Lifecycle state."""

    message_id: MessageId | None = None
    """@brief 欢迎消息 ID / Welcome-message ID."""

    def __post_init__(self) -> None:
        """@brief 校验状态组合与时间 / Validate state combinations and time.

        @return None / None.
        @raises VerificationError 状态字段组合不可能 / If state fields form an impossible combination.
        """

        if not isinstance(self.key, VerificationKey):
            raise TypeError("key must be a VerificationKey")
        if not isinstance(self.version, VerificationVersion):
            raise TypeError("version must be a VerificationVersion")
        if _TOKEN_HASH_PATTERN.fullmatch(self.token_hash) is None:
            raise ValueError("token_hash must be a lowercase SHA-256 digest")
        normalized_name = self.member_name.strip()
        if not normalized_name or len(normalized_name) > 256:
            raise ValueError("member_name must contain 1-256 characters")
        object.__setattr__(self, "member_name", normalized_name)
        _ensure_aware(self.expires_at, "expires_at")
        if not isinstance(self.status, VerificationStatus):
            raise TypeError("status must be a VerificationStatus")
        if self.message_id is not None:
            if not isinstance(self.message_id, int):
                raise TypeError("message_id must be an integer or None")
            if int(self.message_id) <= 0:
                raise ValueError("message_id must be positive")
        if self.status not in {
            VerificationStatus.CREATING,
            VerificationStatus.CANCELLING,
            VerificationStatus.CANCELLED,
        }:
            if self.message_id is None:
                raise VerificationError(
                    f"{self.status.value} verification requires message_id"
                )

    @property
    def chat_id(self) -> ChatId:
        """@brief 返回群组 ID / Return the chat ID.

        @return 群组 ID / Chat ID.
        """

        return self.key.chat_id

    @property
    def user_id(self) -> UserId:
        """@brief 返回成员 ID / Return the member ID.

        @return 成员 ID / Member ID.
        """

        return self.key.user_id

    def accepts(self, token: str, now: datetime) -> bool:
        """@brief 判断 token 能否请求通过 / Check whether a token can request passage.

        @param token callback 明文 token / Plaintext callback token.
        @param now 当前时刻 / Current instant.
        @return token、状态与 deadline 均有效时为 True / True when token, state, and deadline are valid.
        """

        _ensure_aware(now, "now")
        return (
            self.status is VerificationStatus.PENDING
            and now < self.expires_at
            and secrets.compare_digest(self.token_hash, hash_verification_token(token))
        )

    def evolve(
        self,
        event: VerificationEvent,
        *,
        expected_version: VerificationVersion,
        now: datetime,
        message_id: MessageId | None = None,
    ) -> VerificationTask:
        """@brief 纯函数式应用一个领域事件 / Apply one domain event as a pure transition.

        @param event 领域事件 / Domain event.
        @param expected_version 调用方观察到的版本 / Version observed by the caller.
        @param now 事件时刻 / Event instant.
        @param message_id ACTIVATE 时绑定的欢迎消息 / Welcome message bound by ACTIVATE.
        @return 转移后的新聚合 / New aggregate after transition.
        @raises StaleVerificationVersion 版本不一致 / If the version is stale.
        @raises InvalidVerificationTransition 状态不接受事件 / If the state rejects the event.
        """

        if expected_version != self.version:
            raise StaleVerificationVersion(
                f"verification is version {self.version.value}, not {expected_version.value}"
            )
        _ensure_aware(now, "now")
        target: VerificationStatus
        bound_message = self.message_id
        if (
            self.status is VerificationStatus.CREATING
            and event is VerificationEvent.ACTIVATE
        ):
            if message_id is None:
                raise InvalidVerificationTransition("ACTIVATE requires message_id")
            if now >= self.expires_at:
                raise InvalidVerificationTransition(
                    "cannot activate an expired verification"
                )
            target = VerificationStatus.PENDING
            bound_message = message_id
        elif (
            self.status is VerificationStatus.CREATING
            and event is VerificationEvent.ABORT_CREATION
        ):
            target = VerificationStatus.CANCELLING
        elif (
            self.status is VerificationStatus.PENDING
            and event is VerificationEvent.PASS_REQUESTED
        ):
            if now >= self.expires_at:
                raise InvalidVerificationTransition("verification deadline has passed")
            target = VerificationStatus.PASSING
        elif (
            self.status is VerificationStatus.PENDING
            and event is VerificationEvent.DEADLINE_REACHED
        ):
            if now < self.expires_at:
                raise InvalidVerificationTransition(
                    "verification deadline has not been reached"
                )
            target = VerificationStatus.EXPIRING
        elif (
            self.status is VerificationStatus.PENDING
            and event is VerificationEvent.MEMBER_LEFT
        ):
            target = VerificationStatus.CANCELLING
        elif self.status.needs_delivery and event is VerificationEvent.EFFECT_DELIVERED:
            target = {
                VerificationStatus.PASSING: VerificationStatus.PASSED,
                VerificationStatus.EXPIRING: VerificationStatus.EXPIRED,
                VerificationStatus.CANCELLING: VerificationStatus.CANCELLED,
            }[self.status]
        else:
            raise InvalidVerificationTransition(
                f"invalid verification transition: {self.status.value} + {event.value}"
            )
        return replace(
            self,
            version=self.version.next(),
            status=target,
            message_id=bound_message,
        )


@dataclass(frozen=True, slots=True)
class VerificationClaim:
    """@brief 带 fencing token 的验证副作用领取凭证 / Verification-effect claim carrying a fencing token.

    @param task 已进入投递过渡态的聚合 / Aggregate in a delivery-transition state.
    @param token 本次领取的 UUID token / UUID token for this claim.
    @param lease_expires_at 租约截止时间 / Lease deadline.
    @param attempt_count 已领取次数 / Number of claims made so far.
    """

    task: VerificationTask
    """@brief 已领取聚合 / Claimed aggregate."""

    token: str
    """@brief fencing token / Fencing token."""

    lease_expires_at: datetime
    """@brief 租约截止 / Lease deadline."""

    attempt_count: int
    """@brief 尝试次数 / Attempt count."""

    def __post_init__(self) -> None:
        """@brief 校验 claim / Validate the claim.

        @return None / None.
        """

        if not self.task.status.needs_delivery:
            raise VerificationError("a claim requires a delivery-transition state")
        try:
            uuid.UUID(self.token)
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError("claim token must be a UUID") from error
        _ensure_aware(self.lease_expires_at, "lease_expires_at")
        if self.attempt_count < 1:
            raise ValueError("attempt_count must be positive")


def hash_verification_token(token: str) -> str:
    """@brief 计算验证 token 摘要 / Hash a verification token.

    @param token 明文 token / Plaintext token.
    @return 十六进制 SHA-256 摘要 / Hexadecimal SHA-256 digest.
    """

    if not isinstance(token, str) or not token:
        raise ValueError("verification token must not be empty")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _ensure_aware(value: datetime, field_name: str) -> None:
    """@brief 要求带时区时间 / Require a timezone-aware timestamp.

    @param value 待验证时间 / Timestamp to validate.
    @param field_name 错误字段名 / Field name for diagnostics.
    @return None / None.
    """

    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
