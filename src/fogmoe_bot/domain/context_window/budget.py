"""@brief Context Window token 预算值对象 / Context-window token-budget value objects."""

from __future__ import annotations

import math
from dataclasses import dataclass


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
    """@brief 模型输入投影与摘要的显式 token 预算 / Explicit token budget for model-input projection and summarization.

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


__all__ = ["ContextTokenBudget", "TokenCount"]
