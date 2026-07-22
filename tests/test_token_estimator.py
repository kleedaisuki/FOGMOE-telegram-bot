"""@brief Canonical V2 token 估算测试 / Canonical-V2 token-estimation tests."""

import json
import math

from fogmoe_bot.domain.assistant.messages import (
    CanonicalMessage,
    ToolCallPart,
    text_message,
)
from fogmoe_bot.domain.context.token_estimator import (
    DEFAULT_MESSAGE_OVERHEAD,
    estimate_message_tokens,
    estimate_message_tokens_raw,
    estimate_tokens,
    estimate_tokens_raw,
)
from fogmoe_bot.domain.conversation.message import MessageRole


def test_estimate_tokens_raw_weights_ascii_cjk_and_other_text() -> None:
    """@brief 验证字符类别的原始权重 / Verify raw weights for character categories.

    @return None / None.
    """

    assert estimate_tokens_raw("abc") == 1.0
    assert estimate_tokens_raw("你") == 1.1
    assert estimate_tokens_raw("🙂") == 1.8


def test_estimate_tokens_applies_guard_ratio_and_rounding() -> None:
    """@brief 验证保护系数与向上取整 / Verify guard ratio and ceiling.

    @return None / None.
    """

    assert estimate_tokens("abc", guard_ratio=None) == 1
    assert estimate_tokens("abc", guard_ratio=1.15) == 2


def test_estimate_message_tokens_includes_canonical_part_payload() -> None:
    """@brief canonical part JSON 与消息固定开销均被计算 / Count canonical part JSON and fixed overhead.

    @return None / None.
    """

    message = text_message(MessageRole.USER, "abc")
    part_payload = json.dumps(
        message.to_json()["parts"],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    expected = DEFAULT_MESSAGE_OVERHEAD + estimate_tokens_raw(part_payload)

    assert estimate_message_tokens_raw((message,)) == expected
    assert estimate_message_tokens((message,), guard_ratio=None) == math.ceil(expected)


def test_estimate_message_tokens_can_ignore_canonical_tool_parts() -> None:
    """@brief 关闭工具统计时仅保留空 parts JSON / Ignore canonical tool parts when requested.

    @return None / None.
    """

    message = CanonicalMessage(
        MessageRole.ASSISTANT,
        (ToolCallPart("call-1", "get_help_text", {}),),
    )

    with_tools = estimate_message_tokens_raw((message,), include_tool_calls=True)
    without_tools = estimate_message_tokens_raw((message,), include_tool_calls=False)

    assert with_tools > without_tools
    assert without_tools == DEFAULT_MESSAGE_OVERHEAD + estimate_tokens_raw("[]")
