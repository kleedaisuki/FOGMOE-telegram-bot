"""@brief Canonical Assistant message V2 测试 / Tests for canonical Assistant message V2."""

from __future__ import annotations

import pytest

from fogmoe_bot.domain.assistant.messages import (
    Base64ImageSource,
    CANONICAL_MESSAGE_VERSION,
    CanonicalMessage,
    CanonicalMessageError,
    ImagePart,
    MessagePolicy,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    UrlImageSource,
    text_message,
)
from fogmoe_bot.domain.conversation.message import MessageRole


def test_canonical_message_round_trips_and_deep_freezes_meta() -> None:
    """@brief V2 round trip 保留 parts 并隔离可变 meta / V2 round trip preserves parts and isolates mutable meta."""

    meta = {"tenant": {"id": "klee"}}
    message = CanonicalMessage(
        role=MessageRole.ASSISTANT,
        parts=(
            TextPart("正在查询。"),
            ToolCallPart("call_1", "get_current_time", {"timezone": "Asia/Shanghai"}),
        ),
        meta=meta,
    )
    meta["tenant"]["id"] = "mutated"  # type: ignore[index]

    encoded = message.to_json()
    assert encoded == {
        "schema_version": CANONICAL_MESSAGE_VERSION,
        "role": "assistant",
        "parts": [
            {"type": "text", "text": "正在查询。"},
            {
                "type": "tool_call",
                "call_id": "call_1",
                "name": "get_current_time",
                "arguments": {"timezone": "Asia/Shanghai"},
            },
        ],
        "policy": {"include_in_context": True},
        "meta": {"tenant": {"id": "klee"}},
    }
    restored = CanonicalMessage.from_json(encoded)
    assert restored.to_json() == encoded


def test_canonical_message_enforces_role_part_algebra() -> None:
    """@brief role 不能混入另一类 part / A role cannot mix in a foreign part."""

    with pytest.raises(CanonicalMessageError, match="cannot contain"):
        CanonicalMessage(
            role=MessageRole.USER,
            parts=(ToolResultPart("call_1", "get_current_time", {}),),
        )

    with pytest.raises(
        CanonicalMessageError,
        match="arguments must be a JSON object",
    ):
        ToolCallPart("call_1", "get_current_time", "not-an-object")


def test_canonical_message_supports_url_and_base64_images() -> None:
    """@brief 图像来源是显式可区分联合 / Image sources form an explicit discriminated union."""

    message = CanonicalMessage(
        role=MessageRole.USER,
        parts=(
            TextPart("看看这张图"),
            ImagePart(UrlImageSource("https://example.test/a.png")),
            ImagePart(Base64ImageSource("image/jpeg", "YWJj")),
        ),
    )

    assert message.has_images is True
    assert message.without_images().to_json()["parts"] == [
        {"type": "text", "text": "看看这张图"}
    ]


def test_canonical_message_rejects_unknown_shape_and_non_json_meta() -> None:
    """@brief decoder 和构造器都拒绝模糊载荷 / Both decoder and constructor reject ambiguous payloads."""

    with pytest.raises(CanonicalMessageError, match="unsupported keys"):
        CanonicalMessage.from_json(
            {
                "schema_version": 2,
                "role": "user",
                "parts": [{"type": "text", "text": "x"}],
                "policy": {"include_in_context": True},
                "meta": {},
                "content": "legacy",
            }
        )
    with pytest.raises(CanonicalMessageError, match="not JSON-serializable"):
        CanonicalMessage(
            role=MessageRole.USER,
            parts=(TextPart("x"),),
            meta={"opaque": object()},
        )


def test_text_message_defaults_to_empty_meta_and_explicit_policy() -> None:
    """@brief 便捷构造器默认空 meta / Convenience constructor defaults meta to empty."""

    message = text_message(
        MessageRole.USER,
        "隔离文本",
        include_in_context=False,
    )

    assert message.policy == MessagePolicy(include_in_context=False)
    assert message.to_json()["meta"] == {}
