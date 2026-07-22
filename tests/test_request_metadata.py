"""@brief 请求 metadata 边界与 durable 传递测试 / Request-metadata boundary and durable-propagation tests."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from uuid import uuid4

import pytest

from fogmoe_bot.application.assistant.inference_command import (
    DurableAssistantInferenceCommand,
    DurableAssistantScope,
    DurableAssistantUser,
)
from fogmoe_bot.domain.accounts.plan import AccountPlan
from fogmoe_bot.domain.assistant.request_metadata import (
    MAX_REQUEST_META_ITEMS,
    MAX_REQUEST_META_KEY_LENGTH,
    MAX_REQUEST_META_VALUE_LENGTH,
    RequestMeta,
    RequestMetaError,
    normalize_request_meta,
    request_meta_to_json,
)


def _command(meta: RequestMeta = normalize_request_meta({})) -> DurableAssistantInferenceCommand:
    """@brief 构造最小合法 durable Assistant 命令 / Build a minimal valid durable Assistant command.

    @param meta 要冻结到命令中的请求 metadata / Request metadata to freeze into the command.
    @return 严格 durable command / Strict durable command.
    """

    return DurableAssistantInferenceCommand(
        conversation_id="assistant-user:7",
        turn_id=str(uuid4()),
        delivery_stream_id="telegram:primary:chat:7:thread:0",
        chat_id=7,
        user=DurableAssistantUser(
            user_id=7,
            username=None,
            display_name="Klee",
            coins=0,
            plan=AccountPlan.FREE,
            permission=0,
        ),
        scope=DurableAssistantScope(is_group=False),
        meta=meta,
    )


def _too_large_metadata() -> dict[str, str]:
    """@brief 构造各单项合法但整体超过 UTF-8 上限的 metadata / Build metadata whose entries are valid but whose total UTF-8 size is too large.

    @return 超过整体 byte 上限的 string mapping / String mapping exceeding the total byte limit.
    """

    return {
        f"{index:02d}" + ("k" * (MAX_REQUEST_META_KEY_LENGTH - 2)): "v"
        * MAX_REQUEST_META_VALUE_LENGTH
        for index in range(MAX_REQUEST_META_ITEMS)
    }


def test_normalize_request_meta_freezes_an_independent_copy() -> None:
    """@brief 请求 metadata 会复制并冻结，JSON 副本不会反向修改它 / Request metadata is copied and frozen, and a JSON copy cannot mutate it.

    @return None / None.
    """

    caller_meta = {"trace_id": "accepted"}
    normalized = normalize_request_meta(caller_meta)
    caller_meta["trace_id"] = "mutated-after-acceptance"

    assert normalized == {"trace_id": "accepted"}
    with pytest.raises(TypeError):
        normalized["other"] = "forbidden"  # type: ignore[index]

    json_copy = request_meta_to_json(normalized)
    json_copy["trace_id"] = "serialization-copy-only"
    assert normalized == {"trace_id": "accepted"}


@pytest.mark.parametrize(
    "factory",
    (
        lambda: [],
        lambda: {"": "value"},
        lambda: {"   ": "value"},
        lambda: {1: "value"},
        lambda: {"key": 1},
        lambda: {"key\r": "value"},
        lambda: {"key": "value\x00"},
        lambda: {"k" * (MAX_REQUEST_META_KEY_LENGTH + 1): "value"},
        lambda: {"key": "v" * (MAX_REQUEST_META_VALUE_LENGTH + 1)},
        lambda: {str(index): "value" for index in range(MAX_REQUEST_META_ITEMS + 1)},
        _too_large_metadata,
    ),
)
def test_normalize_request_meta_rejects_invalid_types_and_bounds(
    factory: Callable[[], object],
) -> None:
    """@brief 类型、控制字符和各层大小边界必须在入口失败 / Types, control characters, and every size boundary fail at ingress.

    @param factory 构造一个无效 metadata 候选 / Factory producing an invalid metadata candidate.
    @return None / None.
    """

    with pytest.raises(RequestMetaError):
        normalize_request_meta(factory())


def test_durable_command_meta_defaults_empty_is_immutable_and_round_trips() -> None:
    """@brief durable 命令的默认 metadata 为空，且在 JSON round-trip 前后冻结 / A durable command defaults metadata to empty and keeps it frozen across a JSON round trip.

    @return None / None.
    """

    assert _command().meta == {}

    caller_meta: Mapping[str, str] = {"trace_id": "accepted"}
    command = _command(caller_meta)
    assert command.meta == {"trace_id": "accepted"}
    with pytest.raises(TypeError):
        command.meta["other"] = "forbidden"  # type: ignore[index]

    payload = command.to_json()
    raw_meta = payload["meta"]
    assert isinstance(raw_meta, dict)
    raw_meta["trace_id"] = "payload-copy-only"
    assert command.meta == {"trace_id": "accepted"}

    restored = DurableAssistantInferenceCommand.from_json(command.to_json())
    assert restored.meta == {"trace_id": "accepted"}
    with pytest.raises(TypeError):
        restored.meta["other"] = "forbidden"  # type: ignore[index]
