"""@brief Billing 权益授予与订阅状态机 / Billing entitlement grants and subscription state machine."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from ._validation import (
    normalize_code,
    normalize_instant,
    normalize_text,
    require_positive_identity,
)


class EntitlementScope(StrEnum):
    """@brief 权益归属范围 / Scope that owns an entitlement."""

    USER = "user"
    """@brief 归属于单个用户 / Owned by one user."""

    GROUP = "group"
    """@brief 归属于群组或小镇 / Owned by a group or town."""


class EntitlementStatus(StrEnum):
    """@brief 已授予权益的生命周期状态 / Lifecycle status of a granted entitlement."""

    ACTIVE = "active"
    """@brief 当前可以使用 / Currently usable."""

    EXPIRED = "expired"
    """@brief 到达自然到期时刻 / Reached its natural expiration instant."""

    REVOKED = "revoked"
    """@brief 因退款、争议或后台决定被撤销 / Revoked due to refund, dispute, or back-office decision."""


class SubscriptionStatus(StrEnum):
    """@brief 订阅的生命周期状态 / Lifecycle status of a subscription."""

    ACTIVE = "active"
    """@brief 当前周期有效 / Current period is effective."""

    CANCELLED = "cancelled"
    """@brief 已按期末取消 / Cancelled at the end of its period."""

    EXPIRED = "expired"
    """@brief 未续费而自然结束 / Naturally ended without renewal."""

    REVOKED = "revoked"
    """@brief 因退款、争议或后台决定立即撤销 / Immediately revoked due to refund, dispute, or back-office decision."""


@dataclass(frozen=True, slots=True)
class EntitlementGrant:
    """@brief 从已履约订单派生的权益授予 / Entitlement grant derived from a fulfilled order.

    @param grant_id 权益授予稳定标识 / Stable entitlement-grant identity.
    @param code 权益代码 / Entitlement code.
    @param scope 权益归属范围 / Entitlement owner scope.
    @param subject_id 范围内拥有者标识 / Owner identity within scope.
    @param source_order_id 来源订单标识 / Source order identity.
    @param starts_at 开始生效时刻 / Effective-start instant.
    @param expires_at 可选自然到期时刻 / Optional natural-expiration instant.
    @param status 权益状态 / Entitlement status.
    @param ended_at 可选终止时刻 / Optional terminal instant.
    @param revocation_reason 可选撤销原因 / Optional revocation reason.
    @param version 乐观并发版本 / Optimistic-concurrency version.
    """

    grant_id: UUID
    """@brief 权益授予稳定标识 / Stable entitlement-grant identity."""

    code: str
    """@brief 规范化权益代码 / Normalized entitlement code."""

    scope: EntitlementScope
    """@brief 权益归属范围 / Entitlement owner scope."""

    subject_id: int
    """@brief 范围内拥有者标识 / Owner identity within scope."""

    source_order_id: UUID
    """@brief 来源订单标识 / Source order identity."""

    starts_at: datetime
    """@brief 开始生效时刻 / Effective-start instant."""

    expires_at: datetime | None = None
    """@brief 可选自然到期时刻 / Optional natural-expiration instant."""

    status: EntitlementStatus = EntitlementStatus.ACTIVE
    """@brief 当前权益状态 / Current entitlement status."""

    ended_at: datetime | None = None
    """@brief 可选终止时刻 / Optional terminal instant."""

    revocation_reason: str | None = None
    """@brief 可选撤销原因 / Optional revocation reason."""

    version: int = 0
    """@brief 乐观并发版本 / Optimistic-concurrency version."""

    def __post_init__(self) -> None:
        """@brief 验证权益授予状态形状 / Validate entitlement-grant state shape.

        @return None / None.
        @raise TypeError 范围、状态或版本类型非法时抛出 /
            Raised for invalid scope, status, or version types.
        @raise ValueError 拥有者、时刻或终态字段不一致时抛出 /
            Raised for inconsistent owner, time, or terminal-state fields.
        """

        if not isinstance(self.scope, EntitlementScope):
            raise TypeError("Entitlement scope must be an EntitlementScope")
        if not isinstance(self.status, EntitlementStatus):
            raise TypeError("Entitlement status must be an EntitlementStatus")
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise TypeError("Entitlement version must be an integer")
        if self.version < 0:
            raise ValueError("Entitlement version cannot be negative")
        _validate_scope_subject(self.scope, self.subject_id)
        starts_at = normalize_instant(self.starts_at, field="Entitlement start time")
        expires_at = _optional_instant(
            self.expires_at,
            field="Entitlement expiration time",
        )
        ended_at = _optional_instant(self.ended_at, field="Entitlement end time")
        if expires_at is not None and expires_at <= starts_at:
            raise ValueError("Entitlement expiration must follow its start")
        if ended_at is not None and ended_at < starts_at:
            raise ValueError("Entitlement end cannot precede its start")
        revocation_reason = _optional_reason(
            self.revocation_reason,
            field="Entitlement revocation reason",
        )
        _validate_entitlement_state(
            status=self.status,
            expires_at=expires_at,
            ended_at=ended_at,
            revocation_reason=revocation_reason,
        )
        object.__setattr__(
            self,
            "code",
            normalize_code(self.code, field="Entitlement code"),
        )
        object.__setattr__(self, "starts_at", starts_at)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "ended_at", ended_at)
        object.__setattr__(self, "revocation_reason", revocation_reason)

    @classmethod
    def grant(
        cls,
        *,
        grant_id: UUID,
        code: str,
        scope: EntitlementScope,
        subject_id: int,
        source_order_id: UUID,
        starts_at: datetime,
        expires_at: datetime | None = None,
    ) -> EntitlementGrant:
        """@brief 创建有效权益授予 / Create an active entitlement grant.

        @param grant_id 权益授予稳定标识 / Stable entitlement-grant identity.
        @param code 权益代码 / Entitlement code.
        @param scope 权益归属范围 / Entitlement owner scope.
        @param subject_id 范围内拥有者标识 / Owner identity within scope.
        @param source_order_id 来源订单标识 / Source order identity.
        @param starts_at 开始生效时刻 / Effective-start instant.
        @param expires_at 可选自然到期时刻 / Optional natural-expiration instant.
        @return 有效权益授予 / Active entitlement grant.
        """

        return cls(
            grant_id=grant_id,
            code=code,
            scope=scope,
            subject_id=subject_id,
            source_order_id=source_order_id,
            starts_at=starts_at,
            expires_at=expires_at,
        )

    def is_active_at(self, instant: datetime) -> bool:
        """@brief 判断权益在给定时刻是否可用 / Check whether entitlement is usable at an instant.

        @param instant 待判断时刻 / Instant to evaluate.
        @return 权益有效时为 True / True when entitlement is effective.
        """

        normalized = normalize_instant(instant, field="Entitlement evaluation time")
        if self.status is not EntitlementStatus.ACTIVE:
            return False
        if normalized < self.starts_at:
            return False
        return self.expires_at is None or normalized < self.expires_at

    def expire(self, *, observed_at: datetime) -> EntitlementGrant:
        """@brief 在自然到期后固化为终态 / Materialize the terminal expired state after natural expiry.

        @param observed_at 观察到到期的时刻 / Instant at which expiry was observed.
        @return 已过期权益 / Expired entitlement.
        @raise ValueError 权益未有效、没有到期时间或尚未到期时抛出 /
            Raised when entitlement is inactive, non-expiring, or not yet expired.
        """

        if self.status is not EntitlementStatus.ACTIVE:
            raise ValueError("Only active entitlements can expire")
        if self.expires_at is None:
            raise ValueError("Non-expiring entitlements cannot expire")
        normalized = normalize_instant(
            observed_at, field="Entitlement expiry observation"
        )
        if normalized < self.expires_at:
            raise ValueError("Entitlement cannot expire before its expiration time")
        return replace(
            self,
            status=EntitlementStatus.EXPIRED,
            ended_at=self.expires_at,
            version=self.version + 1,
        )

    def revoke(self, *, revoked_at: datetime, reason: str) -> EntitlementGrant:
        """@brief 因退款、争议或后台决定撤销权益 / Revoke entitlement for a refund, dispute, or back-office decision.

        @param revoked_at 撤销生效时刻 / Revocation effective instant.
        @param reason 可审计撤销原因 / Auditable revocation reason.
        @return 已撤销权益 / Revoked entitlement.
        @raise ValueError 权益不是有效状态或撤销时刻非法时抛出 /
            Raised when entitlement is not active or revocation time is invalid.
        """

        if self.status is not EntitlementStatus.ACTIVE:
            raise ValueError("Only active entitlements can be revoked")
        normalized = normalize_instant(revoked_at, field="Entitlement revocation time")
        if normalized < self.starts_at:
            raise ValueError("Entitlement revocation cannot precede its start")
        return replace(
            self,
            status=EntitlementStatus.REVOKED,
            ended_at=normalized,
            revocation_reason=reason,
            version=self.version + 1,
        )


@dataclass(frozen=True, slots=True)
class Subscription:
    """@brief 由周期性报价履约产生的订阅 / Subscription created by fulfilling a periodic offer.

    @param subscription_id 订阅稳定标识 / Stable subscription identity.
    @param owner_id 订阅拥有用户 / Subscription-owning user.
    @param product_id 产品标识 / Product identity.
    @param offer_id 报价标识 / Offer identity.
    @param source_order_id 初始来源订单标识 / Initial source-order identity.
    @param entitlement_grant_ids 对应权益授予标识 / Corresponding entitlement-grant identities.
    @param period_starts_at 当前周期开始时刻 / Current-period start instant.
    @param period_ends_at 当前周期结束时刻 / Current-period end instant.
    @param status 订阅状态 / Subscription status.
    @param cancellation_requested_at 可选按期取消申请时刻 / Optional end-of-period cancellation request.
    @param ended_at 可选订阅终止时刻 / Optional subscription terminal instant.
    @param revocation_reason 可选撤销原因 / Optional revocation reason.
    @param version 乐观并发版本 / Optimistic-concurrency version.
    """

    subscription_id: UUID
    """@brief 订阅稳定标识 / Stable subscription identity."""

    owner_id: int
    """@brief 订阅拥有用户 / Subscription-owning user."""

    product_id: UUID
    """@brief 产品标识 / Product identity."""

    offer_id: UUID
    """@brief 报价标识 / Offer identity."""

    source_order_id: UUID
    """@brief 初始来源订单标识 / Initial source-order identity."""

    entitlement_grant_ids: tuple[UUID, ...]
    """@brief 对应权益授予标识 / Corresponding entitlement-grant identities."""

    period_starts_at: datetime
    """@brief 当前周期开始时刻 / Current-period start instant."""

    period_ends_at: datetime
    """@brief 当前周期结束时刻 / Current-period end instant."""

    status: SubscriptionStatus = SubscriptionStatus.ACTIVE
    """@brief 当前订阅状态 / Current subscription status."""

    cancellation_requested_at: datetime | None = None
    """@brief 可选按期取消申请时刻 / Optional end-of-period cancellation request."""

    ended_at: datetime | None = None
    """@brief 可选订阅终止时刻 / Optional subscription terminal instant."""

    revocation_reason: str | None = None
    """@brief 可选撤销原因 / Optional revocation reason."""

    version: int = 0
    """@brief 乐观并发版本 / Optimistic-concurrency version."""

    def __post_init__(self) -> None:
        """@brief 验证订阅周期和状态形状 / Validate subscription period and state shape.

        @return None / None.
        @raise TypeError 状态、版本或权益集合类型非法时抛出 /
            Raised for invalid status, version, or entitlement collection types.
        @raise ValueError 周期、终态或取消字段不一致时抛出 /
            Raised for inconsistent period, terminal, or cancellation fields.
        """

        require_positive_identity(self.owner_id, field="Subscription owner")
        if not isinstance(self.status, SubscriptionStatus):
            raise TypeError("Subscription status must be a SubscriptionStatus")
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise TypeError("Subscription version must be an integer")
        if self.version < 0:
            raise ValueError("Subscription version cannot be negative")
        grant_ids = tuple(self.entitlement_grant_ids)
        if not grant_ids:
            raise ValueError("Subscription must own at least one entitlement grant")
        if len(set(grant_ids)) != len(grant_ids):
            raise ValueError("Subscription entitlement grant IDs must be unique")
        period_starts_at = normalize_instant(
            self.period_starts_at,
            field="Subscription period start",
        )
        period_ends_at = normalize_instant(
            self.period_ends_at,
            field="Subscription period end",
        )
        cancellation_requested_at = _optional_instant(
            self.cancellation_requested_at,
            field="Subscription cancellation request time",
        )
        ended_at = _optional_instant(self.ended_at, field="Subscription end time")
        if period_ends_at <= period_starts_at:
            raise ValueError("Subscription period end must follow its start")
        if cancellation_requested_at is not None:
            if not period_starts_at <= cancellation_requested_at < period_ends_at:
                raise ValueError(
                    "Subscription cancellation request must be inside its period"
                )
        if ended_at is not None and ended_at < period_starts_at:
            raise ValueError("Subscription end cannot precede its period start")
        revocation_reason = _optional_reason(
            self.revocation_reason,
            field="Subscription revocation reason",
        )
        _validate_subscription_state(
            status=self.status,
            period_ends_at=period_ends_at,
            cancellation_requested_at=cancellation_requested_at,
            ended_at=ended_at,
            revocation_reason=revocation_reason,
        )
        object.__setattr__(self, "entitlement_grant_ids", grant_ids)
        object.__setattr__(self, "period_starts_at", period_starts_at)
        object.__setattr__(self, "period_ends_at", period_ends_at)
        object.__setattr__(
            self,
            "cancellation_requested_at",
            cancellation_requested_at,
        )
        object.__setattr__(self, "ended_at", ended_at)
        object.__setattr__(self, "revocation_reason", revocation_reason)

    @classmethod
    def activate(
        cls,
        *,
        subscription_id: UUID,
        owner_id: int,
        product_id: UUID,
        offer_id: UUID,
        source_order_id: UUID,
        entitlement_grant_ids: tuple[UUID, ...],
        period_starts_at: datetime,
        period_ends_at: datetime,
    ) -> Subscription:
        """@brief 创建有效订阅 / Create an active subscription.

        @param subscription_id 订阅稳定标识 / Stable subscription identity.
        @param owner_id 订阅拥有用户 / Subscription-owning user.
        @param product_id 产品标识 / Product identity.
        @param offer_id 报价标识 / Offer identity.
        @param source_order_id 初始来源订单标识 / Initial source-order identity.
        @param entitlement_grant_ids 对应权益授予标识 / Corresponding entitlement-grant identities.
        @param period_starts_at 当前周期开始时刻 / Current-period start instant.
        @param period_ends_at 当前周期结束时刻 / Current-period end instant.
        @return 有效订阅 / Active subscription.
        """

        return cls(
            subscription_id=subscription_id,
            owner_id=owner_id,
            product_id=product_id,
            offer_id=offer_id,
            source_order_id=source_order_id,
            entitlement_grant_ids=entitlement_grant_ids,
            period_starts_at=period_starts_at,
            period_ends_at=period_ends_at,
        )

    def is_active_at(self, instant: datetime) -> bool:
        """@brief 判断订阅在给定时刻是否有效 / Check whether subscription is effective at an instant.

        @param instant 待判断时刻 / Instant to evaluate.
        @return 订阅有效时为 True / True when subscription is effective.
        """

        normalized = normalize_instant(instant, field="Subscription evaluation time")
        return (
            self.status is SubscriptionStatus.ACTIVE
            and self.period_starts_at <= normalized < self.period_ends_at
        )

    def request_cancellation(self, *, requested_at: datetime) -> Subscription:
        """@brief 请求在当前周期结束时取消 / Request cancellation at the end of the current period.

        @param requested_at 请求取消时刻 / Cancellation-request instant.
        @return 带取消标记的有效订阅 / Active subscription with cancellation marker.
        @raise ValueError 订阅不再有效或请求时刻不在当前周期内时抛出 /
            Raised when subscription is inactive or request time is outside current period.
        """

        if self.status is not SubscriptionStatus.ACTIVE:
            raise ValueError("Only active subscriptions can request cancellation")
        normalized = normalize_instant(
            requested_at,
            field="Subscription cancellation request time",
        )
        if not self.period_starts_at <= normalized < self.period_ends_at:
            raise ValueError(
                "Subscription cancellation request must be inside its period"
            )
        if self.cancellation_requested_at is not None:
            return self
        return replace(
            self,
            cancellation_requested_at=normalized,
            version=self.version + 1,
        )

    def renew(
        self,
        *,
        renewed_at: datetime,
        next_period_ends_at: datetime,
        entitlement_grant_ids: tuple[UUID, ...] | None = None,
    ) -> Subscription:
        """@brief 在已确认续费后推进订阅周期 / Advance the subscription period after confirmed renewal.

        @param renewed_at 续费确认时刻 / Renewal-confirmation instant.
        @param next_period_ends_at 下一周期结束时刻 / Next-period end instant.
        @param entitlement_grant_ids 新周期生成的权益授予标识；None 保持现有标识 /
            Entitlement-grant identities generated for the next period; None retains existing IDs.
        @return 新周期的有效订阅 / Active subscription in its next period.
        @raise ValueError 订阅无效、已请求取消或新周期非法时抛出 /
            Raised when subscription is inactive, cancellation was requested, or next period is invalid.
        @note 调用方必须把成功续费的付款与此变迁放在同一事务中。/
            The caller must atomically persist a successful renewal payment with this transition.
        """

        if self.status is not SubscriptionStatus.ACTIVE:
            raise ValueError("Only active subscriptions can renew")
        if self.cancellation_requested_at is not None:
            raise ValueError("Cancellation-requested subscriptions cannot renew")
        normalized_renewal = normalize_instant(
            renewed_at,
            field="Subscription renewal time",
        )
        normalized_end = normalize_instant(
            next_period_ends_at,
            field="Subscription next-period end",
        )
        if normalized_renewal < self.period_starts_at:
            raise ValueError("Subscription renewal cannot precede its period start")
        if normalized_renewal >= self.period_ends_at:
            raise ValueError("Expired subscriptions cannot renew into a new period")
        if normalized_end <= self.period_ends_at:
            raise ValueError(
                "Subscription next period must extend beyond the current period"
            )
        next_grants = (
            self.entitlement_grant_ids
            if entitlement_grant_ids is None
            else tuple(entitlement_grant_ids)
        )
        if not next_grants:
            raise ValueError(
                "Subscription renewal requires at least one entitlement grant"
            )
        if len(set(next_grants)) != len(next_grants):
            raise ValueError("Subscription renewal entitlement grants must be unique")
        return replace(
            self,
            period_starts_at=self.period_ends_at,
            period_ends_at=normalized_end,
            entitlement_grant_ids=next_grants,
            version=self.version + 1,
        )

    def expire(self, *, observed_at: datetime) -> Subscription:
        """@brief 在当前周期结束后固化终态 / Materialize terminal state after current period ends.

        @param observed_at 观察到周期结束的时刻 / Instant at which period end was observed.
        @return 已过期或已取消订阅 / Expired or cancelled subscription.
        @raise ValueError 订阅不再有效或当前周期尚未结束时抛出 /
            Raised when subscription is inactive or current period has not ended.
        """

        if self.status is not SubscriptionStatus.ACTIVE:
            raise ValueError("Only active subscriptions can expire")
        normalized = normalize_instant(
            observed_at,
            field="Subscription expiry observation",
        )
        if normalized < self.period_ends_at:
            raise ValueError("Subscription cannot expire before its period end")
        terminal_status = (
            SubscriptionStatus.CANCELLED
            if self.cancellation_requested_at is not None
            else SubscriptionStatus.EXPIRED
        )
        return replace(
            self,
            status=terminal_status,
            ended_at=self.period_ends_at,
            version=self.version + 1,
        )

    def revoke(self, *, revoked_at: datetime, reason: str) -> Subscription:
        """@brief 因退款、争议或后台决定立即撤销订阅 / Immediately revoke a subscription for refund, dispute, or back-office decision.

        @param revoked_at 撤销生效时刻 / Revocation effective instant.
        @param reason 可审计撤销原因 / Auditable revocation reason.
        @return 已撤销订阅 / Revoked subscription.
        @raise ValueError 订阅不再有效或撤销时刻非法时抛出 /
            Raised when subscription is inactive or revocation time is invalid.
        """

        if self.status is not SubscriptionStatus.ACTIVE:
            raise ValueError("Only active subscriptions can be revoked")
        normalized = normalize_instant(revoked_at, field="Subscription revocation time")
        if normalized < self.period_starts_at:
            raise ValueError("Subscription revocation cannot precede its period start")
        return replace(
            self,
            status=SubscriptionStatus.REVOKED,
            ended_at=normalized,
            revocation_reason=reason,
            version=self.version + 1,
        )


def _validate_scope_subject(scope: EntitlementScope, subject_id: int) -> None:
    """@brief 验证权益范围拥有者标识 / Validate owner identity for an entitlement scope.

    @param scope 权益归属范围 / Entitlement owner scope.
    @param subject_id 范围内拥有者标识 / Owner identity within scope.
    @return None / None.
    @raise TypeError 标识不是严格整数时抛出 / Raised when identity is not a strict integer.
    @raise ValueError 用户标识不为正或群组标识为零时抛出 /
        Raised when user identity is non-positive or group identity is zero.
    """

    if scope is EntitlementScope.USER:
        require_positive_identity(subject_id, field="Entitlement user subject")
        return
    if isinstance(subject_id, bool) or not isinstance(subject_id, int):
        raise TypeError("Entitlement group subject must be an integer")
    if subject_id == 0:
        raise ValueError("Entitlement group subject cannot be zero")


def _optional_instant(value: datetime | None, *, field: str) -> datetime | None:
    """@brief 规范化可选时刻 / Normalize an optional instant.

    @param value 可选原始时刻 / Optional raw instant.
    @param field 出错信息中的字段名称 / Field name for error messages.
    @return UTC 时刻或 None / UTC instant or None.
    """

    if value is None:
        return None
    return normalize_instant(value, field=field)


def _optional_reason(value: str | None, *, field: str) -> str | None:
    """@brief 规范化可选撤销原因 / Normalize an optional revocation reason.

    @param value 可选原始原因 / Optional raw reason.
    @param field 出错信息中的字段名称 / Field name for error messages.
    @return 规范原因或 None / Normalized reason or None.
    """

    if value is None:
        return None
    return normalize_text(value, field=field, minimum_length=1, maximum_length=1_000)


def _validate_entitlement_state(
    *,
    status: EntitlementStatus,
    expires_at: datetime | None,
    ended_at: datetime | None,
    revocation_reason: str | None,
) -> None:
    """@brief 验证权益状态所需字段 / Validate fields required by entitlement state.

    @param status 当前权益状态 / Current entitlement status.
    @param expires_at 可选自然到期时刻 / Optional natural-expiration instant.
    @param ended_at 可选终止时刻 / Optional terminal instant.
    @param revocation_reason 可选撤销原因 / Optional revocation reason.
    @return None / None.
    @raise ValueError 状态所需字段缺失或存在禁止字段时抛出 /
        Raised when state-required fields are absent or forbidden fields are present.
    """

    if status is EntitlementStatus.ACTIVE:
        if ended_at is not None or revocation_reason is not None:
            raise ValueError("Active entitlements cannot contain terminal fields")
        return
    if status is EntitlementStatus.EXPIRED:
        if (
            expires_at is None
            or ended_at != expires_at
            or revocation_reason is not None
        ):
            raise ValueError(
                "Expired entitlements must end exactly at their expiration"
            )
        return
    if status is EntitlementStatus.REVOKED:
        if ended_at is None or revocation_reason is None:
            raise ValueError("Revoked entitlements require an end time and reason")
        return
    raise ValueError("Unsupported entitlement status")


def _validate_subscription_state(
    *,
    status: SubscriptionStatus,
    period_ends_at: datetime,
    cancellation_requested_at: datetime | None,
    ended_at: datetime | None,
    revocation_reason: str | None,
) -> None:
    """@brief 验证订阅状态所需字段 / Validate fields required by subscription state.

    @param status 当前订阅状态 / Current subscription status.
    @param period_ends_at 当前周期结束时刻 / Current-period end instant.
    @param cancellation_requested_at 可选按期取消申请时刻 / Optional end-of-period cancellation request.
    @param ended_at 可选订阅终止时刻 / Optional subscription terminal instant.
    @param revocation_reason 可选撤销原因 / Optional revocation reason.
    @return None / None.
    @raise ValueError 状态所需字段缺失或存在禁止字段时抛出 /
        Raised when state-required fields are absent or forbidden fields are present.
    """

    if status is SubscriptionStatus.ACTIVE:
        if ended_at is not None or revocation_reason is not None:
            raise ValueError("Active subscriptions cannot contain terminal fields")
        return
    if status is SubscriptionStatus.CANCELLED:
        if cancellation_requested_at is None or ended_at != period_ends_at:
            raise ValueError(
                "Cancelled subscriptions must end at a requested period end"
            )
        if revocation_reason is not None:
            raise ValueError(
                "Cancelled subscriptions cannot contain a revocation reason"
            )
        return
    if status is SubscriptionStatus.EXPIRED:
        if cancellation_requested_at is not None or ended_at != period_ends_at:
            raise ValueError(
                "Expired subscriptions must end at an unrequested period end"
            )
        if revocation_reason is not None:
            raise ValueError("Expired subscriptions cannot contain a revocation reason")
        return
    if status is SubscriptionStatus.REVOKED:
        if ended_at is None or revocation_reason is None:
            raise ValueError("Revoked subscriptions require an end time and reason")
        return
    raise ValueError("Unsupported subscription status")
