"""@brief 未配置支付渠道时的安全拒绝适配器 / Safe rejection adapter for unconfigured payment providers."""

from __future__ import annotations

from fogmoe_bot.domain.billing.orders import PaymentEvent


class DenyUnconfiguredPaymentEventVerifier:
    """@brief 拒绝所有未接入验证链的支付事件 / Reject every payment event without an integrated verification chain.

    @note 这不是占位式“成功”实现。运行时未显式接入渠道签名校验、来源认证和事件去重前，
        拒绝是唯一安全默认值。/ This is not a placeholder that returns success. Until the
        runtime explicitly integrates provider-signature verification, origin authentication, and
        event deduplication, rejection is the only safe default.
    """

    async def verify(self, event: PaymentEvent) -> bool:
        """@brief 拒绝未经配置验证器的支付事件 / Reject a payment event lacking a configured verifier.

        @param event 待验证的外部支付事件 / External payment event to verify.
        @return 始终为 False / Always False.
        """

        del event
        return False
