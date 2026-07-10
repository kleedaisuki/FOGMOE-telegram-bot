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
    for attr in ("model_dump", "dict"):
        if hasattr(value, attr):
            dump_func = getattr(value, attr)
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
    """@brief 删除值为 None 的字段 / Drop fields whose value is None.

    @param value 输入字典 / Input dictionary.
    @return 清理后的字典 / Cleaned dictionary.
    """
    return {key: item for key, item in value.items() if item is not None}


def tool_call_to_plain(tool_call: Any) -> Dict[str, Any]:
    """@brief 归一化 tool call 对象 / Normalize a tool call object.

    @param tool_call provider 返回的工具调用对象 / Tool call object from provider.
    @return OpenAI-compatible plain dict / OpenAI-compatible plain dict.
    """
    if isinstance(tool_call, dict):
        plain_call = json_safe(dict(tool_call))
        function_payload = plain_call.get("function")
        if isinstance(function_payload, dict):
            plain_function = dict(function_payload)
            arguments = plain_function.get("arguments")
            if isinstance(arguments, (dict, list)):
                plain_function["arguments"] = json.dumps(arguments, ensure_ascii=False)
            elif arguments is None:
                plain_function["arguments"] = "{}"
            plain_call["function"] = plain_function
        return drop_none_items(plain_call)

    plain_call: Dict[str, Any] | None = None
    for attr in ("model_dump", "dict"):
        if hasattr(tool_call, attr):
            try:
                plain_call = getattr(tool_call, attr)(mode="json")
            except TypeError:
                try:
                    plain_call = getattr(tool_call, attr)()
                except TypeError:
                    plain_call = getattr(tool_call, attr)(by_alias=True)
            except Exception:
                plain_call = None
            if isinstance(plain_call, dict):
                plain_call = json_safe(plain_call)
                break

    if not isinstance(plain_call, dict):
        function = getattr(tool_call, "function", None)
        arguments = getattr(function, "arguments", None) if function else None
        if isinstance(arguments, (dict, list)):
            arguments_str = json.dumps(arguments, ensure_ascii=False)
        else:
            arguments_str = arguments if arguments is not None else "{}"

        plain_call = {
            "id": getattr(tool_call, "id", None),
            "type": getattr(tool_call, "type", "function"),
            "function": {
                "name": getattr(function, "name", None) if function else None,
                "arguments": arguments_str,
            },
        }
        provider_specific_fields = getattr(
            tool_call,
            "provider_specific_fields",
            None,
        )
        if provider_specific_fields:
            plain_call["provider_specific_fields"] = json_safe(provider_specific_fields)
        return drop_none_items(plain_call)

    function_payload = plain_call.get("function")
    if not isinstance(function_payload, dict):
        for attr in ("model_dump", "dict"):
            if hasattr(function_payload, attr):
                try:
                    function_payload = getattr(function_payload, attr)()
                except TypeError:
                    function_payload = getattr(function_payload, attr)(by_alias=True)
                except Exception:
                    function_payload = None
                if isinstance(function_payload, dict):
                    plain_call["function"] = function_payload
                break

    if isinstance(function_payload, dict):
        plain_function = dict(function_payload)
        arguments = plain_function.get("arguments")
        if isinstance(arguments, (dict, list)):
            plain_function["arguments"] = json.dumps(arguments, ensure_ascii=False)
        elif arguments is None:
            plain_function["arguments"] = "{}"
        plain_call["function"] = plain_function

    return drop_none_items(plain_call)


def message_to_plain_dict(message: Any) -> Dict[str, Any]:
    """@brief 归一化消息对象 / Normalize a message object.

    @param message provider SDK 消息对象或 dict / Provider SDK message object or dict.
    @return 可存入 LLM 消息列表的 plain dict / Plain dict suitable for LLM messages.
    """
    if isinstance(message, dict):
        return drop_none_items(json_safe(dict(message)))

    for attr in ("model_dump", "dict"):
        if hasattr(message, attr):
            dump_func = getattr(message, attr)
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
    assistant_message: Any,
    *,
    content: str,
    tool_calls: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """@brief 构造 assistant 消息 dict / Build an assistant message dict.

    @param assistant_message provider SDK assistant 消息 / Provider SDK assistant message.
    @param content assistant 文本内容 / Assistant text content.
    @param tool_calls 已归一化工具调用 / Normalized tool calls.
    @return OpenAI-compatible assistant message / OpenAI-compatible assistant message.
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
    """@brief 归一化工具调用列表 / Normalize tool call list.

    @param tool_calls provider 返回的工具调用列表 / Tool calls returned by provider.
    @return plain dict 工具调用列表 / Plain dict tool call list.
    """
    if not tool_calls:
        return []
    return [tool_call_to_plain(call) for call in tool_calls]
