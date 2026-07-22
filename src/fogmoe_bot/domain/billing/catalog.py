"""@brief Billing 产品、报价与原生支付金额 / Billing products, offers, and native payment amounts."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final
from uuid import UUID

from ._validation import normalize_code, normalize_instant, normalize_text

_CURRENCY_PATTERN: Final = re.compile(r"^[A-Z0-9]{3,16}$")
"""@brief 支付渠道原生币种代码的允许字符集 / Allowed alphabet for provider-native currency codes."""


class ProductKind(StrEnum):
    """@brief Billing 产品交付形态 / Billing product delivery kind."""

    ONE_TIME = "one_time"
    """@brief 一次性权益产品 / One-time entitlement product."""

    SUBSCRIPTION = "subscription"
    """@brief 周期性订阅权益产品 / Recurring subscription entitlement product."""


class ProductStatus(StrEnum):
    """@brief Billing 产品目录状态 / Billing product-catalog status."""

    ACTIVE = "active"
    """@brief 可创建新订单 / Available for new orders."""

    RETIRED = "retired"
    """@brief 停售但保留审计记录 / Retired from sale while retained for audit."""


class OfferStatus(StrEnum):
    """@brief Billing 报价状态 / Billing offer status."""

    ACTIVE = "active"
    """@brief 可供购买 / Available for purchase."""

    RETIRED = "retired"
    """@brief 不再接受新订单 / No longer accepts new orders."""


@dataclass(frozen=True, slots=True)
class PaymentAmount:
    """@brief 支付渠道的原生整数金额 / Native integral amount of a payment provider.

    @param currency 支付渠道定义的原生币种代码 / Provider-defined native currency code.
    @param units 渠道的严格正整数单位 / Strictly positive integer units in that provider.
    @note 此类型刻意没有汇率、法币换算或金币映射。/
        This type deliberately has no exchange rate, fiat conversion, or token mapping.
    """

    currency: str
    """@brief 规范化的原生币种代码 / Normalized native currency code."""

    units: int
    """@brief 严格正的原生支付单位 / Strictly positive native payment units."""

    def __post_init__(self) -> None:
        """@brief 验证原生支付金额 / Validate the native payment amount.

        @return None / None.
        @raise TypeError 币种或金额类型非法时抛出 / Raised for invalid currency or amount types.
        @raise ValueError 币种格式或金额范围非法时抛出 /
            Raised for invalid currency format or amount range.
        """

        if not isinstance(self.currency, str):
            raise TypeError("Payment currency must be a string")
        currency = self.currency.strip().upper()
        if _CURRENCY_PATTERN.fullmatch(currency) is None:
            raise ValueError(
                "Payment currency must contain 3-16 uppercase letters or digits"
            )
        if isinstance(self.units, bool) or not isinstance(self.units, int):
            raise TypeError("Payment units must be an integer")
        if self.units <= 0:
            raise ValueError("Payment units must be positive")
        object.__setattr__(self, "currency", currency)


@dataclass(frozen=True, slots=True)
class BillingProduct:
    """@brief 稳定的可售 Billing 产品 / Stable sellable billing product.

    @param product_id 产品稳定标识 / Stable product identity.
    @param code 面向代码的产品代码 / Code-facing product code.
    @param display_name 面向用户的产品名称 / User-facing product name.
    @param kind 一次性或订阅产品 / One-time or subscription product.
    @param status 目录生命周期状态 / Catalog lifecycle status.
    @param description 可选产品说明 / Optional product description.
    """

    product_id: UUID
    """@brief 产品稳定标识 / Stable product identity."""

    code: str
    """@brief 规范化产品代码 / Normalized product code."""

    display_name: str
    """@brief 面向用户的名称 / User-facing name."""

    kind: ProductKind
    """@brief 产品交付形态 / Product delivery kind."""

    status: ProductStatus = ProductStatus.ACTIVE
    """@brief 当前目录状态 / Current catalog status."""

    description: str = ""
    """@brief 可选短说明 / Optional short description."""

    def __post_init__(self) -> None:
        """@brief 规范化产品展示字段 / Normalize product display fields.

        @return None / None.
        @raise TypeError 产品类别或状态类型非法时抛出 /
            Raised when product kind or status has an invalid type.
        @raise ValueError 文本字段不合法时抛出 / Raised when text fields are invalid.
        """

        if not isinstance(self.kind, ProductKind):
            raise TypeError("Product kind must be a ProductKind")
        if not isinstance(self.status, ProductStatus):
            raise TypeError("Product status must be a ProductStatus")
        object.__setattr__(
            self, "code", normalize_code(self.code, field="Product code")
        )
        object.__setattr__(
            self,
            "display_name",
            normalize_text(
                self.display_name,
                field="Product display name",
                minimum_length=1,
                maximum_length=120,
            ),
        )
        if not isinstance(self.description, str):
            raise TypeError("Product description must be a string")
        description = self.description.strip()
        if len(description) > 2_000:
            raise ValueError("Product description cannot exceed 2000 characters")
        object.__setattr__(self, "description", description)

    @property
    def is_active(self) -> bool:
        """@brief 判断产品是否可售 / Check whether the product is sellable.

        @return 产品可售时为 True / True when the product is sellable.
        """

        return self.status is ProductStatus.ACTIVE

    def retire(self) -> BillingProduct:
        """@brief 停售产品且保留其历史 / Retire the product while retaining its history.

        @return 已停售的产品快照 / Retired product snapshot.
        """

        if self.status is ProductStatus.RETIRED:
            return self
        return replace(self, status=ProductStatus.RETIRED)


@dataclass(frozen=True, slots=True)
class BillingOffer:
    """@brief 指向产品的不可变报价快照 / Immutable offer snapshot pointing to a product.

    @param offer_id 报价稳定标识 / Stable offer identity.
    @param product_id 所属产品标识 / Owning product identity.
    @param product_kind 冗余的产品形态快照 / Redundant product-kind snapshot.
    @param price 渠道原生售价 / Provider-native selling price.
    @param entitlement_codes 购买后可交付的权益代码 / Entitlement codes fulfilled after purchase.
    @param created_at 报价创建时刻 / Offer creation instant.
    @param subscription_period 订阅周期；一次性产品为 None / Subscription interval; None for one-time products.
    @param available_from 可选开始出售时刻 / Optional sale-start instant.
    @param available_until 可选停止出售时刻 / Optional sale-end instant.
    @param status 报价状态 / Offer status.
    """

    offer_id: UUID
    """@brief 报价稳定标识 / Stable offer identity."""

    product_id: UUID
    """@brief 所属产品标识 / Owning product identity."""

    product_kind: ProductKind
    """@brief 产品形态快照 / Product-kind snapshot."""

    price: PaymentAmount
    """@brief 原生支付售价 / Native payment price."""

    entitlement_codes: tuple[str, ...]
    """@brief 交付权益代码序列 / Sequence of delivered entitlement codes."""

    created_at: datetime
    """@brief 报价创建时刻 / Offer creation instant."""

    subscription_period: timedelta | None = None
    """@brief 可选订阅周期 / Optional subscription period."""

    available_from: datetime | None = None
    """@brief 可选可售开始时刻 / Optional availability-start instant."""

    available_until: datetime | None = None
    """@brief 可选可售结束时刻 / Optional availability-end instant."""

    status: OfferStatus = OfferStatus.ACTIVE
    """@brief 报价生命周期状态 / Offer lifecycle status."""

    def __post_init__(self) -> None:
        """@brief 验证报价与订阅周期形状 / Validate offer and subscription-period shape.

        @return None / None.
        @raise TypeError 产品形态、金额或状态类型非法时抛出 /
            Raised when product kind, price, or status has an invalid type.
        @raise ValueError 权益、时刻或周期不变量不满足时抛出 /
            Raised when entitlement, timing, or interval invariants are violated.
        """

        if not isinstance(self.product_kind, ProductKind):
            raise TypeError("Offer product kind must be a ProductKind")
        if not isinstance(self.price, PaymentAmount):
            raise TypeError("Offer price must be a PaymentAmount")
        if not isinstance(self.status, OfferStatus):
            raise TypeError("Offer status must be an OfferStatus")
        normalized_codes = tuple(
            normalize_code(code, field="Entitlement code")
            for code in self.entitlement_codes
        )
        if not normalized_codes:
            raise ValueError("Offer must deliver at least one entitlement")
        if len(set(normalized_codes)) != len(normalized_codes):
            raise ValueError("Offer entitlement codes must be unique")
        created_at = normalize_instant(self.created_at, field="Offer creation time")
        available_from = (
            None
            if self.available_from is None
            else normalize_instant(
                self.available_from, field="Offer availability start"
            )
        )
        available_until = (
            None
            if self.available_until is None
            else normalize_instant(self.available_until, field="Offer availability end")
        )
        if available_from is not None and available_until is not None:
            if available_until <= available_from:
                raise ValueError("Offer availability end must follow its start")
        if self.product_kind is ProductKind.SUBSCRIPTION:
            if (
                self.subscription_period is None
                or self.subscription_period <= timedelta(0)
            ):
                raise ValueError(
                    "Subscription offers require a positive subscription period"
                )
        elif self.subscription_period is not None:
            raise ValueError("One-time offers cannot define a subscription period")
        object.__setattr__(self, "entitlement_codes", normalized_codes)
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "available_from", available_from)
        object.__setattr__(self, "available_until", available_until)

    def is_available_at(self, instant: datetime) -> bool:
        """@brief 判断报价在指定时刻是否可售 / Check whether the offer is sellable at an instant.

        @param instant 待判断时刻 / Instant to evaluate.
        @return 可接受新订单时为 True / True when new orders are accepted.
        """

        normalized = normalize_instant(instant, field="Offer availability instant")
        if self.status is not OfferStatus.ACTIVE:
            return False
        if self.available_from is not None and normalized < self.available_from:
            return False
        return self.available_until is None or normalized < self.available_until

    def retire(self) -> BillingOffer:
        """@brief 停售报价 / Retire the offer.

        @return 已停售的报价快照 / Retired offer snapshot.
        """

        if self.status is OfferStatus.RETIRED:
            return self
        return replace(self, status=OfferStatus.RETIRED)
