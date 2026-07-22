"""@brief 由实时业务事实推导账户方案 / Derive account plans from live business facts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AccountPlan(StrEnum):
    """@brief 用户可见的封闭账户方案 / Closed set of user-visible account plans."""

    FREE = "free"
    """@brief 无当前有效订阅 / No currently effective subscription."""

    PAID = "paid"
    """@brief 拥有当前有效用户订阅 / Owns a currently effective user subscription."""

    ADMIN = "admin"
    """@brief 配置声明的唯一管理员身份 / Sole administrator identity declared by configuration."""


@dataclass(frozen=True, slots=True)
class AccountPlanPolicy:
    """@brief 以管理员身份和订阅事实推导方案 / Derive a plan from administrator identity and subscription facts.

    @param administrator_id 配置声明的唯一管理员 ID / Sole administrator ID declared by configuration.
    @note 管理员身份优先于商业订阅。由于当前 Billing catalog 尚未声明 product-to-plan
        映射，非管理员只要存在当前有效的 user subscription 即为 ``paid``，否则为
        ``free``。/ Administrator identity takes precedence over commercial subscriptions.
        Because the current Billing catalog has no product-to-plan mapping, a non-administrator
        is ``paid`` exactly while a user subscription is currently effective, and ``free``
        otherwise.
    """

    administrator_id: int
    """@brief 配置管理员 ID / Configured administrator identifier."""

    def __post_init__(self) -> None:
        """@brief 校验管理员身份 / Validate the administrator identity.

        @return None / None.
        @raise TypeError 管理员 ID 不是严格整数 / Administrator ID is not a strict integer.
        @raise ValueError 管理员 ID 非正 / Administrator ID is not positive.
        """

        if isinstance(self.administrator_id, bool) or not isinstance(
            self.administrator_id, int
        ):
            raise TypeError("Account-plan administrator_id must be an integer")
        if self.administrator_id <= 0:
            raise ValueError("Account-plan administrator_id must be positive")

    def resolve(
        self,
        *,
        user_id: int,
        has_active_subscription: bool,
    ) -> AccountPlan:
        """@brief 从实时事实推导封闭方案 / Derive the closed plan from live facts.

        @param user_id 待分类用户 / User to classify.
        @param has_active_subscription 当前时刻是否存在有效用户订阅 /
            Whether an effective user subscription exists now.
        @return admin、paid 或 free / ``admin``, ``paid``, or ``free``.
        @raise TypeError 输入类型非法 / An input has an invalid type.
        @raise ValueError 用户 ID 非正 / User ID is not positive.
        """

        if isinstance(user_id, bool) or not isinstance(user_id, int):
            raise TypeError("Account-plan user_id must be an integer")
        if user_id <= 0:
            raise ValueError("Account-plan user_id must be positive")
        if not isinstance(has_active_subscription, bool):
            raise TypeError("has_active_subscription must be a Boolean")
        if user_id == self.administrator_id:
            return AccountPlan.ADMIN
        if has_active_subscription:
            return AccountPlan.PAID
        return AccountPlan.FREE


__all__ = ["AccountPlan", "AccountPlanPolicy"]
