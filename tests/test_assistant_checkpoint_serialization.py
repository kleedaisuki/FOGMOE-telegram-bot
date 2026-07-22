"""@brief Assistant checkpoint V2 编解码测试 / Assistant checkpoint V2 serialization tests."""

from __future__ import annotations

import pytest

from fogmoe_bot.application.assistant.completion import AssistantCompletion
from fogmoe_bot.domain.assistant.messages import CanonicalMessage, text_message
from fogmoe_bot.domain.conversation.message import MessageRole
from fogmoe_bot.infrastructure.database.assistant_tool_effects import (
    _decode_completion,
    _encode_completion,
)


def _assistant_message() -> CanonicalMessage:
    """@brief 构造含工具调用的 canonical Assistant 消息 / Build a canonical Assistant message with a tool call.

    @return 规范 V2 Assistant 消息 / Canonical V2 Assistant message.
    """

    return CanonicalMessage.from_json(
        {
            "schema_version": 2,
            "role": "assistant",
            "parts": [
                {"type": "text", "text": "我来查询天气。"},
                {
                    "type": "tool_call",
                    "call_id": "call-weather-1",
                    "name": "weather",
                    "arguments": {"city": "Shanghai", "days": 1},
                },
            ],
            "policy": {"include_in_context": True},
            "meta": {},
        }
    )


def test_checkpoint_completion_round_trip_uses_only_canonical_v2_message() -> None:
    """@brief checkpoint 只存 V2 消息，工具调用由 parts 派生 / Checkpoints store only V2 messages and derive tool calls from parts.

    @return None / None.
    """

    completion = AssistantCompletion(message=_assistant_message())

    payload = _encode_completion(completion)

    assert payload == {
        "schema_version": 2,
        "message": completion.message.to_json(),
    }
    assert "content" not in payload
    assert "tool_calls" not in payload

    restored = _decode_completion(payload)

    assert restored == completion
    assert restored.content == "我来查询天气。"
    assert len(restored.tool_calls) == 1
    call = restored.tool_calls[0]
    assert call.provider_call_id == "call-weather-1"
    assert call.name == "weather"
    assert call.arguments == {"city": "Shanghai", "days": 1}


def test_checkpoint_completion_rejects_legacy_wire_payload() -> None:
    """@brief checkpoint 不再接受旧 OpenAI wire 格式 / Checkpoints no longer accept legacy OpenAI wire payloads.

    @return None / None.
    """

    with pytest.raises(RuntimeError, match="canonical V2"):
        _decode_completion(
            {
                "content": "legacy",
                "message": {"role": "assistant", "content": "legacy"},
                "tool_calls": [],
            }
        )


def test_checkpoint_completion_rejects_extra_duplicate_tool_calls() -> None:
    """@brief checkpoint 拒绝重复保存工具调用 / Checkpoints reject separately duplicated tool calls.

    @return None / None.
    """

    message = _assistant_message().to_json()

    with pytest.raises(RuntimeError, match="canonical V2"):
        _decode_completion(
            {
                "schema_version": 2,
                "message": message,
                "tool_calls": [],
            }
        )


def test_checkpoint_completion_rejects_non_assistant_canonical_message() -> None:
    """@brief checkpoint 只能恢复 Assistant message / Checkpoints can restore only Assistant messages.

    @return None / None.
    """

    user_message = text_message(MessageRole.USER, "hello")

    with pytest.raises(RuntimeError, match="completion is invalid"):
        _decode_completion(
            {
                "schema_version": 2,
                "message": user_message.to_json(),
            }
        )
