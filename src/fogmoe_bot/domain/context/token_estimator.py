from __future__ import annotations

import json
import math
from collections.abc import Iterable

from fogmoe_bot.domain.assistant.messages import CanonicalMessage

DEFAULT_GUARD_RATIO = 1.15
DEFAULT_MESSAGE_OVERHEAD = 4.0

EN_WEIGHT = 1.0 / 3.0
ZH_WEIGHT = 1.1
OTHER_WEIGHT = 1.8


def estimate_tokens(
    text: str,
    *,
    guard_ratio: float | None = DEFAULT_GUARD_RATIO,
) -> int:
    """Estimate tokens for a text string using a conservative heuristic."""
    estimate = estimate_tokens_raw(text)
    return _apply_guard_and_round(estimate, guard_ratio=guard_ratio)


def estimate_message_tokens(
    messages: Iterable[CanonicalMessage],
    *,
    guard_ratio: float | None = DEFAULT_GUARD_RATIO,
    per_message_overhead: float = DEFAULT_MESSAGE_OVERHEAD,
    include_tool_calls: bool = True,
) -> int:
    """@brief 估算 canonical 消息列表的 token / Estimate tokens for canonical messages.

    @param messages canonical V2 消息 / Canonical V2 messages.
    @param guard_ratio 保守保护系数 / Conservative guard ratio.
    @param per_message_overhead 协议每消息开销 / Per-message protocol overhead.
    @param include_tool_calls 是否统计工具 part / Whether to count tool parts.
    @return 保护后 token 估计 / Guarded token estimate.
    """
    total = estimate_message_tokens_raw(
        messages,
        per_message_overhead=per_message_overhead,
        include_tool_calls=include_tool_calls,
    )

    return _apply_guard_and_round(total, guard_ratio=guard_ratio)


def estimate_message_tokens_raw(
    messages: Iterable[CanonicalMessage],
    *,
    per_message_overhead: float = DEFAULT_MESSAGE_OVERHEAD,
    include_tool_calls: bool = True,
) -> float:
    """@brief 估算未保护的 canonical 消息 token / Estimate unguarded canonical-message tokens.

    @param messages canonical V2 消息 / Canonical V2 messages.
    @param per_message_overhead 协议每消息开销 / Per-message protocol overhead.
    @param include_tool_calls 是否统计工具 part / Whether to count tool parts.
    @return 未取整 token 估计 / Unrounded token estimate.
    """
    total = 0.0
    for message in messages:
        total += per_message_overhead
        parts_value = message.to_json()["parts"]
        if not isinstance(parts_value, list):
            raise AssertionError("Canonical message parts must serialize as an array")
        parts = parts_value
        if not include_tool_calls:
            parts = [
                part
                for part in parts
                if isinstance(part, dict)
                and part.get("type") not in {"tool_call", "tool_result"}
            ]
        total += estimate_tokens_raw(
            json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
        )
    return total


def estimate_tokens_raw(text: str) -> float:
    if not text:
        return 0.0
    en_chars, zh_chars, other_chars = _count_char_categories(text)
    return (
        (en_chars * EN_WEIGHT) + (zh_chars * ZH_WEIGHT) + (other_chars * OTHER_WEIGHT)
    )


def _apply_guard_and_round(
    token_count: float,
    *,
    guard_ratio: float | None,
) -> int:
    if guard_ratio:
        token_count *= guard_ratio
    return int(math.ceil(token_count)) if token_count > 0 else 0


def _count_char_categories(text: str) -> tuple[int, int, int]:
    en_chars = 0
    zh_chars = 0
    other_chars = 0

    for ch in text:
        codepoint = ord(ch)
        if codepoint <= 0x7F:
            en_chars += 1
        elif _is_cjk(codepoint):
            zh_chars += 1
        else:
            other_chars += 1

    return en_chars, zh_chars, other_chars


def _is_cjk(codepoint: int) -> bool:
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x2A6DF
        or 0x2A700 <= codepoint <= 0x2B73F
        or 0x2B740 <= codepoint <= 0x2B81F
        or 0x2B820 <= codepoint <= 0x2CEAF
        or 0x2F800 <= codepoint <= 0x2FA1F
    )
