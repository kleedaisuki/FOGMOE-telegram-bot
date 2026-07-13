"""@brief Context Window 的保守 token 计数 adapter / Conservative token-counting adapter for context windows."""

from __future__ import annotations

from collections.abc import Sequence

from fogmoe_bot.domain.context.token_estimator import estimate_message_tokens
from fogmoe_bot.domain.conversation.payloads import JsonObject
from fogmoe_bot.domain.context_window.budget import TokenCount


class ConservativeHistoryTokenCounter:
    """@brief 对 provider-neutral 消息应用显式保护系数 / Apply an explicit guard ratio to provider-neutral messages."""

    def __init__(self, *, guard_ratio: float = 1.15) -> None:
        """@brief 保存保守保护系数 / Store the conservative guard ratio.

        @param guard_ratio 大于等于一的保护系数 / Guard ratio at least one.
        @raise ValueError 保护系数非法 / Raised for an invalid guard ratio.
        """

        if guard_ratio < 1.0:
            raise ValueError("History token guard_ratio must be at least one")
        self._guard_ratio = guard_ratio

    def count_messages(self, messages: Sequence[JsonObject]) -> TokenCount:
        """@brief 估算包含 tool-call payload 的完整消息 token / Estimate complete messages including tool-call payloads.

        @param messages provider-neutral messages / Provider-neutral messages.
        @return 有保护系数的 token 数 / Guarded token count.
        """

        return TokenCount(
            estimate_message_tokens(
                messages,
                guard_ratio=self._guard_ratio,
                include_tool_calls=True,
            )
        )


__all__ = ["ConservativeHistoryTokenCounter"]
