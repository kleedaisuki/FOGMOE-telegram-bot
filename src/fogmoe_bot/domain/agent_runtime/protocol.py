"""@brief LLM 工具调用协议归一化 / LLM tool-call protocol normalization."""

import base64
import json
from typing import Any, Dict, List, Optional


def json_safe(value: Any) -> Any:
    """@brief 转换为 JSON 安全值 / Convert a value into JSON-safe data.

    @param value 待转换值 / Value to convert.
    @return 可序列化值 / JSON-serializable value.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    for attribute in ("model_dump", "dict"):
        if hasattr(value, attribute):
            dump_func = getattr(value, attribute)
            for kwargs in ({"mode": "json"}, {}, {"by_alias": True}):
                try:
                    dumped = dump_func(**kwargs)
                except TypeError:
                    continue
                except Exception:
                    break
                return json_safe(dumped)
    return str(value)


def drop_none_items(value: Dict[str, Any]) -> Dict[str, Any]:
    """@brief 删除 None 字段 / Drop fields whose value is None.

    @param value 输入字典 / Input dictionary.
    @return 清理后的字典 / Cleaned dictionary.
    """
    return {key: item for key, item in value.items() if item is not None}


def tool_call_to_plain(tool_call: Any) -> Dict[str, Any]:
    """@brief 归一化 tool call / Normalize a provider tool call.

    @param tool_call provider 返回的工具调用对象 / Tool-call object from a provider.
    @return OpenAI 兼容字典 / OpenAI-compatible plain dictionary.
    """
    if isinstance(tool_call, dict):
        plain_call = json_safe(dict(tool_call))
    else:
        plain_call = None
        for attribute in ("model_dump", "dict"):
            if hasattr(tool_call, attribute):
                try:
                    plain_call = getattr(tool_call, attribute)(mode="json")
                except TypeError:
                    plain_call = getattr(tool_call, attribute)()
                except Exception:
                    plain_call = None
                if isinstance(plain_call, dict):
                    plain_call = json_safe(plain_call)
                    break
        if not isinstance(plain_call, dict):
            function = getattr(tool_call, "function", None)
            arguments = getattr(function, "arguments", None) if function else None
            plain_call = {
                "id": getattr(tool_call, "id", None),
                "type": getattr(tool_call, "type", "function"),
                "function": {
                    "name": getattr(function, "name", None) if function else None,
                    "arguments": json.dumps(arguments, ensure_ascii=False)
                    if isinstance(arguments, (dict, list))
                    else (arguments if arguments is not None else "{}"),
                },
            }

    function_payload = plain_call.get("function")
    if isinstance(function_payload, dict):
        plain_function = dict(function_payload)
        arguments = plain_function.get("arguments")
        plain_function["arguments"] = (
            json.dumps(arguments, ensure_ascii=False)
            if isinstance(arguments, (dict, list))
            else (arguments if arguments is not None else "{}")
        )
        plain_call["function"] = plain_function
    return drop_none_items(plain_call)


def message_to_plain_dict(message: Any) -> Dict[str, Any]:
    """@brief 归一化 provider 消息 / Normalize a provider message.

    @param message provider SDK message object or dict / Provider SDK message object or dict.
    @return 可存入 LLM 消息列表的字典 / Dictionary suitable for LLM messages.
    """
    if isinstance(message, dict):
        return drop_none_items(json_safe(dict(message)))
    for attribute in ("model_dump", "dict"):
        if hasattr(message, attribute):
            dump_func = getattr(message, attribute)
            for kwargs in ({"mode": "json"}, {}, {"by_alias": True}):
                try:
                    dumped = dump_func(**kwargs)
                except TypeError:
                    continue
                except Exception:
                    break
                if isinstance(dumped, dict):
                    return drop_none_items(json_safe(dumped))
    result: Dict[str, Any] = {}
    for key in ("role", "content", "tool_calls", "function_call", "provider_specific_fields", "reasoning_content"):
        value = getattr(message, key, None)
        if value is not None:
            result[key] = json_safe(value)
    return drop_none_items(result)


def assistant_message_to_plain(
    assistant_message: Any,
    *,
    content: str,
    tool_calls: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """@brief 构造 assistant 消息 / Build an assistant message.

    @param assistant_message provider assistant 消息 / Provider assistant message.
    @param content assistant 文本内容 / Assistant text content.
    @param tool_calls 已归一化调用 / Normalized tool calls.
    @return OpenAI 兼容 assistant 消息 / OpenAI-compatible assistant message.
    """
    message = message_to_plain_dict(assistant_message)
    message["role"] = "assistant"
    message["content"] = content
    if tool_calls:
        message["tool_calls"] = tool_calls
    else:
        message.pop("tool_calls", None)
    return message


def normalise_tool_calls(tool_calls: Optional[List[Any]]) -> List[Dict[str, Any]]:
    """@brief 归一化工具调用列表 / Normalize a tool-call list.

    @param tool_calls provider 工具调用列表 / Provider tool-call list.
    @return 普通字典列表 / Plain dictionary list.
    """
    return [] if not tool_calls else [tool_call_to_plain(call) for call in tool_calls]
