from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping

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
    messages: Iterable[Mapping[str, object]],
    *,
    guard_ratio: float | None = DEFAULT_GUARD_RATIO,
    per_message_overhead: float = DEFAULT_MESSAGE_OVERHEAD,
    include_tool_calls: bool = True,
) -> int:
    """Estimate tokens for a list of chat messages."""
    total = estimate_message_tokens_raw(
        messages,
        per_message_overhead=per_message_overhead,
        include_tool_calls=include_tool_calls,
    )

    return _apply_guard_and_round(total, guard_ratio=guard_ratio)


def estimate_message_tokens_raw(
    messages: Iterable[Mapping[str, object]],
    *,
    per_message_overhead: float = DEFAULT_MESSAGE_OVERHEAD,
    include_tool_calls: bool = True,
) -> float:
    """Estimate tokens for a list of chat messages without guard or rounding."""
    total = 0.0
    for message in messages:
        total += per_message_overhead
        content = message.get("content")
        if content:
            total += estimate_tokens_raw(str(content))

        if include_tool_calls:
            tool_calls = message.get("tool_calls")
            if tool_calls:
                try:
                    tool_payload = json.dumps(tool_calls, ensure_ascii=False)
                except TypeError:
                    tool_payload = str(tool_calls)
                total += estimate_tokens_raw(tool_payload)
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
