"""@brief LLM provider 消息协议归一化 / LLM-provider message normalization.

Provider SDK 对 message 与 tool call 使用不同的模型对象。本模块把这些外部对象
收敛为 JSON 安全的 OpenAI-compatible 数据结构，避免 SDK 类型泄漏到应用层。
/ Provider SDKs expose different message and tool-call model objects. This module
normalizes those external objects into JSON-safe OpenAI-compatible structures so
SDK types do not leak into the application layer.
"""

import base64
import json
from collections.abc import Mapping, Sequence

type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)
"""@brief 递归 JSON 值 / Recursive JSON value."""

type ProviderPayload = dict[str, JsonValue]
"""@brief 归一化后的 Provider 对象 / Normalized provider payload."""

_PROVIDER_SPECIFIC_KEYS = frozenset({"provider_specific_fields"})
"""@brief 不能跨 Provider 重放的响应字段 / Response fields that cannot be replayed across providers."""


def _model_dump(value: object) -> ProviderPayload | None:
    """@brief 尝试导出 SDK/Pydantic 模型 / Try to dump an SDK or Pydantic model.

    @param value 外部模型对象 / External model object.
    @return JSON 对象；无法导出时为 None / JSON object, or None when unavailable.
    """

    for attribute in ("model_dump", "dict"):
        dump_func = getattr(value, attribute, None)
        if not callable(dump_func):
            continue
        for kwargs in ({"mode": "json"}, {}, {"by_alias": True}):
            try:
                dumped = dump_func(**kwargs)
            except TypeError:
                continue
            except Exception:
                break
            safe_dump = json_safe(dumped)
            if isinstance(safe_dump, dict):
                return safe_dump
    return None


def json_safe(value: object) -> JsonValue:
    """@brief 转换为 JSON 安全值 / Convert a value into JSON-safe data.

    @param value 待转换值 / Value to convert.
    @return 可 JSON 序列化的值 / JSON-serializable value.
    """

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    dumped = _model_dump(value)
    return dumped if dumped is not None else str(value)


def drop_none_items(value: Mapping[str, JsonValue]) -> ProviderPayload:
    """@brief 删除值为 None 的字段 / Drop fields whose value is None.

    @param value 输入对象 / Input object.
    @return 清理后的对象 / Cleaned object.
    """

    return {key: item for key, item in value.items() if item is not None}


def tool_call_to_plain(tool_call: object) -> ProviderPayload:
    """@brief 归一化 Provider tool call / Normalize a provider tool call.

    @param tool_call Provider 返回的工具调用对象 / Provider tool-call object.
    @return OpenAI-compatible 普通对象 / OpenAI-compatible plain object.
    """

    if isinstance(tool_call, Mapping):
        plain_call: ProviderPayload = {
            str(key): json_safe(item) for key, item in tool_call.items()
        }
    else:
        plain_call = _model_dump(tool_call) or {}
        if not plain_call:
            function = getattr(tool_call, "function", None)
            arguments = (
                getattr(function, "arguments", None) if function is not None else None
            )
            normalized_arguments = (
                json.dumps(json_safe(arguments), ensure_ascii=False)
                if isinstance(arguments, (Mapping, list, tuple))
                else (str(arguments) if arguments is not None else "{}")
            )
            plain_call = {
                "id": json_safe(getattr(tool_call, "id", None)),
                "type": json_safe(getattr(tool_call, "type", "function")),
                "function": {
                    "name": json_safe(
                        getattr(function, "name", None)
                        if function is not None
                        else None
                    ),
                    "arguments": normalized_arguments,
                },
            }

    function_payload = plain_call.get("function")
    if isinstance(function_payload, dict):
        plain_function = dict(function_payload)
        arguments = plain_function.get("arguments")
        plain_function["arguments"] = (
            json.dumps(arguments, ensure_ascii=False)
            if isinstance(arguments, (dict, list))
            else (str(arguments) if arguments is not None else "{}")
        )
        plain_call["function"] = plain_function
    return drop_none_items(plain_call)


def message_to_plain_dict(message: object) -> ProviderPayload:
    """@brief 归一化 Provider message / Normalize a provider message.

    @param message Provider SDK message 或字典 / Provider SDK message or mapping.
    @return 可加入模型消息列表的对象 / Object suitable for the model message list.
    """

    if isinstance(message, Mapping):
        return drop_none_items(
            {str(key): json_safe(item) for key, item in message.items()}
        )
    dumped = _model_dump(message)
    if dumped is not None:
        return drop_none_items(dumped)

    result: ProviderPayload = {}
    for key in (
        "role",
        "content",
        "tool_calls",
        "function_call",
        "provider_specific_fields",
        "reasoning_content",
    ):
        value = getattr(message, key, None)
        if value is not None:
            result[key] = json_safe(value)
    return drop_none_items(result)


def assistant_message_to_plain(
    assistant_message: object,
    *,
    content: str,
    tool_calls: Sequence[ProviderPayload],
) -> ProviderPayload:
    """@brief 构造 Assistant message / Build an Assistant message.

    @param assistant_message Provider Assistant 消息 / Provider Assistant message.
    @param content Assistant 文本 / Assistant text.
    @param tool_calls 已归一化的工具调用 / Normalized tool calls.
    @return OpenAI-compatible Assistant 消息 / OpenAI-compatible Assistant message.
    """

    message = message_to_plain_dict(assistant_message)
    message["role"] = "assistant"
    message["content"] = content
    if tool_calls:
        message["tool_calls"] = list(tool_calls)
    else:
        message.pop("tool_calls", None)
    return message


def normalise_tool_calls(
    tool_calls: Sequence[object] | None,
) -> list[ProviderPayload]:
    """@brief 归一化工具调用列表 / Normalize a tool-call sequence.

    @param tool_calls Provider 工具调用序列 / Provider tool-call sequence.
    @return 普通 JSON 对象列表 / Plain JSON-object list.
    """

    return [] if not tool_calls else [tool_call_to_plain(call) for call in tool_calls]


def sanitize_tool_call_for_provider(
    tool_call: Mapping[str, JsonValue],
    provider: str,
) -> ProviderPayload:
    """@brief 清理一个待重放的工具调用 / Sanitize one tool call before replay.

    @param tool_call OpenAI-compatible 工具调用 / OpenAI-compatible tool call.
    @param provider 目标 Provider / Target provider.
    @return 不含跨 Provider 私有字段的独立对象 / Independent payload without cross-provider private fields.
    """

    sanitized = dict(tool_call)
    if provider != "gemini":
        for key in _PROVIDER_SPECIFIC_KEYS:
            sanitized.pop(key, None)
    else:
        sanitized.pop("id", None)
    return sanitized


def sanitize_message_for_provider(
    message: Mapping[str, JsonValue],
    provider: str,
) -> ProviderPayload:
    """@brief 在唯一外部协议边界清理历史消息 / Sanitize history at the sole external protocol boundary.

    @param message 规范的持久化消息 / Canonical persisted message.
    @param provider 目标 Provider / Target provider.
    @return 可安全发送且不含空 ``tool_calls`` 的新消息 / New provider-safe message without empty ``tool_calls``.
    @note 空工具列表没有协议语义，且部分 OpenAI-compatible Provider 会直接拒绝；
        因此这里也修复数据库中已经持久化的历史载荷。/ Empty tool-call lists carry no
        protocol meaning and are rejected by some OpenAI-compatible providers, so this
        boundary also repairs already-persisted history payloads.
    """

    sanitized = dict(message)
    if provider != "gemini":
        for key in _PROVIDER_SPECIFIC_KEYS:
            sanitized.pop(key, None)

    tool_calls = sanitized.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        sanitized["tool_calls"] = [
            sanitize_tool_call_for_provider(tool_call, provider)
            if isinstance(tool_call, Mapping)
            else tool_call
            for tool_call in tool_calls
        ]
    else:
        sanitized.pop("tool_calls", None)

    if (
        provider == "gemini"
        and sanitized.get("role") == "assistant"
        and sanitized.get("tool_calls")
        and not str(sanitized.get("content") or "").strip()
    ):
        sanitized.pop("content", None)
    if provider == "gemini" and sanitized.get("role") == "tool":
        sanitized.pop("tool_call_id", None)
    return sanitized


def sanitize_messages_for_provider(
    messages: Sequence[Mapping[str, JsonValue]],
    provider: str,
) -> list[ProviderPayload]:
    """@brief 清理完整 Provider 历史 / Sanitize complete provider history.

    @param messages 规范消息序列 / Canonical message sequence.
    @param provider 目标 Provider / Target provider.
    @return 与输入顺序一致的独立消息 / Independent messages preserving input order.
    """

    return [sanitize_message_for_provider(message, provider) for message in messages]


__all__ = [
    "JsonValue",
    "ProviderPayload",
    "assistant_message_to_plain",
    "json_safe",
    "normalise_tool_calls",
    "sanitize_message_for_provider",
    "sanitize_messages_for_provider",
    "sanitize_tool_call_for_provider",
]
