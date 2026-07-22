"""@brief OpenAI-style Chat Completions 协议 codec / OpenAI-style Chat Completions protocol codec.

该模块只处理明确的 OpenAI wire contract；它不会猜测 gateway 的私有参数，也不会把
无效 function arguments 静默改写为 ``{}``。/
This module handles only the explicit OpenAI wire contract. It neither guesses
gateway-private parameters nor silently rewrites invalid function arguments to ``{}``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import cast

from fogmoe_bot.application.assistant.tools.catalog import ToolDefinition
from fogmoe_bot.domain.assistant.messages import CanonicalMessage
from fogmoe_bot.domain.conversation.payloads import JsonObject, JsonValue

from .messages import (
    MessageContractError,
    ProviderPayload,
    canonical_message_parts,
    compact_json,
    copy_json_value,
    make_assistant_message,
    message_text,
    openai_image_url,
    payload_array,
    text_or_json,
    thaw_tool_schema,
)
from .provider_response import DecodedProviderCompletion


def encode_openai_request(
    *,
    model: str,
    messages: Sequence[JsonObject],
    tools: Sequence[ToolDefinition],
    tool_choice: str | JsonObject | None,
    max_tokens: int,
    metadata: Mapping[str, str],
    strict_tools: bool,
    temperature: float | None,
    top_p: float | None,
    stop_sequences: tuple[str, ...],
    seed: int | None,
    reasoning_effort: str | None,
    parallel_tool_calls: bool | None,
) -> ProviderPayload:
    """@brief 构造 OpenAI-style Chat Completions payload / Build an OpenAI-style Chat Completions payload.

    @param model route 选中的模型 / Model selected by the route.
    @param messages Canonical Message V2 历史 / Canonical Message V2 history.
    @param tools 应用层 typed tools / Application-layer typed tools.
    @param tool_choice 显式工具选择 / Explicit tool choice.
    @param max_tokens 输出 token 上限 / Output-token limit.
    @param metadata route/request 自定义元数据 / Route/request custom metadata.
    @param strict_tools 是否要求 strict function schemas / Whether strict function schemas are required.
    @param temperature 可选采样温度 / Optional sampling temperature.
    @param top_p 可选 nucleus 采样阈值 / Optional nucleus sampling threshold.
    @param stop_sequences 可选停止序列 / Optional stop sequences.
    @param seed 可选确定性 seed / Optional deterministic seed.
    @param reasoning_effort 可选 provider-neutral 推理档位 / Optional provider-neutral reasoning tier.
    @param parallel_tool_calls 是否允许并行工具调用 / Whether parallel tool calls are allowed.
    @return 新建且无 provider 私有字段的 payload / Fresh payload without provider-private fields.
    @raise MessageContractError canonical message 或 tool choice 非法时抛出 /
        Raised for invalid canonical messages or tool choice.
    """

    payload: ProviderPayload = {
        "model": _nonblank(model, context="OpenAI model"),
        "messages": payload_array(_render_openai_messages(messages)),
        "max_tokens": _positive_integer(max_tokens, context="OpenAI max_tokens"),
    }
    if tools:
        payload["tools"] = payload_array(
            _openai_tools(tools, strict_tools=strict_tools)
        )
        if tool_choice is not None:
            payload["tool_choice"] = _openai_tool_choice(tool_choice)
    elif tool_choice is not None:
        raise MessageContractError("OpenAI tool_choice requires at least one exposed tool")
    if metadata:
        payload["metadata"] = {key: value for key, value in metadata.items()}
    _apply_tuning(
        payload,
        temperature=temperature,
        top_p=top_p,
        stop_sequences=stop_sequences,
        seed=seed,
        reasoning_effort=reasoning_effort,
        parallel_tool_calls=parallel_tool_calls,
    )
    return payload


def decode_openai_response(payload: Mapping[str, object]) -> DecodedProviderCompletion:
    """@brief 严格解码一个 OpenAI-style completion 响应 / Strictly decode one OpenAI-style completion response.

    @param payload 顶层 JSON response object / Top-level JSON response object.
    @return 含 Canonical Message V2 的已解码响应 / Decoded response containing Canonical Message V2.
    @raise MessageContractError choices、message 或 function arguments 非法时抛出 /
        Raised when choices, message, or function arguments are invalid.
    """

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise MessageContractError("OpenAI response must contain a non-empty choices array")
    first = choices[0]
    if not isinstance(first, Mapping):
        raise MessageContractError("OpenAI response choice must be an object")
    message = first.get("message")
    if not isinstance(message, Mapping):
        raise MessageContractError("OpenAI response choice must contain an assistant message")
    raw_message = cast(Mapping[str, object], message)
    role = raw_message.get("role")
    if role not in {"assistant", None}:
        raise MessageContractError("OpenAI response message role must be assistant")
    parts: list[ProviderPayload] = []
    text = _response_text(raw_message.get("content"))
    if text:
        parts.append({"type": "text", "text": text})
    _openai_tool_calls(raw_message.get("tool_calls"), parts)
    return DecodedProviderCompletion(
        message=CanonicalMessage.from_json(make_assistant_message(parts)),
        input_tokens=_usage_tokens(payload.get("usage"), "prompt_tokens", "input_tokens"),
        output_tokens=_usage_tokens(
            payload.get("usage"), "completion_tokens", "output_tokens"
        ),
    )


def _render_openai_messages(messages: Sequence[JsonObject]) -> list[ProviderPayload]:
    """@brief 将 Canonical Message V2 渲染为 OpenAI message 列表 / Render Canonical Message V2 as an OpenAI message list.

    @param messages canonical history / Canonical history.
    @return OpenAI wire messages / OpenAI wire messages.
    @raise MessageContractError message 顺序或 part 组合不受协议支持时抛出 /
        Raised when message ordering or part combinations are unsupported by the protocol.
    """

    rendered: list[ProviderPayload] = []
    for ordinal, value in enumerate(messages):
        if not isinstance(value, Mapping):
            raise MessageContractError(f"Canonical messages[{ordinal}] must be an object")
        role, parts = canonical_message_parts(cast(Mapping[str, object], value))
        normal_parts = tuple(
            part for part in parts if part.get("type") in {"text", "image"}
        )
        tool_results = tuple(
            part for part in parts if part.get("type") == "tool_result"
        )
        tool_calls = tuple(
            part for part in parts if part.get("type") == "tool_call"
        )
        if role == "assistant":
            if tool_results:
                raise MessageContractError("Assistant messages cannot contain tool_result parts")
            rendered.append(_openai_assistant_message(normal_parts, tool_calls))
            continue
        if tool_calls:
            raise MessageContractError("Only assistant messages can contain tool_call parts")
        if role == "tool":
            if normal_parts or not tool_results:
                raise MessageContractError(
                    "A canonical tool message must contain only tool_result parts"
                )
            rendered.extend(_openai_tool_result_messages(tool_results))
            continue
        if tool_results:
            raise MessageContractError("Only canonical tool messages can contain tool_result parts")
        rendered.append(
            {
                "role": role,
                "content": _openai_content(normal_parts),
            }
        )
    return rendered


def _openai_assistant_message(
    normal_parts: Sequence[ProviderPayload],
    tool_calls: Sequence[ProviderPayload],
) -> ProviderPayload:
    """@brief 渲染一个 Assistant V2 消息 / Render one Assistant V2 message.

    @param normal_parts text parts / Text parts.
    @param tool_calls canonical tool-call parts / Canonical tool-call parts.
    @return OpenAI Assistant wire message / OpenAI Assistant wire message.
    """

    if any(part.get("type") == "image" for part in normal_parts):
        raise MessageContractError("OpenAI assistant messages cannot contain image parts")
    content = message_text(normal_parts)
    rendered: ProviderPayload = {
        "role": "assistant",
        "content": content if content else None,
    }
    if tool_calls:
        rendered["tool_calls"] = [_openai_tool_call(part) for part in tool_calls]
    return rendered


def _openai_tool_result_messages(
    parts: Sequence[ProviderPayload],
) -> list[ProviderPayload]:
    """@brief 将 V2 tool_result parts 展开为 OpenAI tool messages / Expand V2 tool-result parts into OpenAI tool messages.

    @param parts canonical tool-result parts / Canonical tool-result parts.
    @return 一项一个的 OpenAI tool messages / One OpenAI tool message per part.
    """

    rendered: list[ProviderPayload] = []
    for part in parts:
        call_id = _part_string(part, "call_id", context="tool_result")
        result = part.get("result")
        if result is None and "result" not in part:
            raise MessageContractError("tool_result.result is required")
        rendered.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": text_or_json(result),
            }
        )
    return rendered


def _openai_content(parts: Sequence[ProviderPayload]) -> JsonValue:
    """@brief 渲染文本/图像 parts 为 OpenAI content / Render text/image parts as OpenAI content.

    @param parts text and image parts / Text and image parts.
    @return OpenAI string or content-part array / OpenAI string or content-part array.
    """

    if not any(part.get("type") == "image" for part in parts):
        return message_text(parts)
    rendered: list[ProviderPayload] = []
    for part in parts:
        match part.get("type"):
            case "text":
                rendered.append(
                    {
                        "type": "text",
                        "text": _part_string(part, "text", context="text part"),
                    }
                )
            case "image":
                rendered.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": openai_image_url(part)},
                    }
                )
            case other:
                raise MessageContractError(
                    f"OpenAI content cannot render canonical part {other!r}"
                )
    return payload_array(rendered)


def _openai_tool_call(part: ProviderPayload) -> ProviderPayload:
    """@brief 渲染一个 V2 tool_call part / Render one V2 tool_call part.

    @param part 已验证 tool_call part / Validated tool-call part.
    @return OpenAI function tool-call object / OpenAI function tool-call object.
    """

    arguments = part.get("arguments")
    if arguments is None and "arguments" not in part:
        raise MessageContractError("tool_call.arguments is required")
    return {
        "id": _part_string(part, "call_id", context="tool_call"),
        "type": "function",
        "function": {
            "name": _part_string(part, "name", context="tool_call"),
            "arguments": compact_json(arguments),
        },
    }


def _openai_tools(
    definitions: Sequence[ToolDefinition],
    *,
    strict_tools: bool,
) -> list[ProviderPayload]:
    """@brief 序列化 typed tools 为 OpenAI functions / Serialize typed tools as OpenAI functions.

    @param definitions 应用层工具定义 / Application-layer tool definitions.
    @param strict_tools 是否为每个 function 标记 strict / Whether to mark every function strict.
    @return OpenAI function tools / OpenAI function tools.
    """

    rendered: list[ProviderPayload] = []
    for definition in definitions:
        function: ProviderPayload = {
            "name": definition.name,
            "description": definition.description,
            "parameters": thaw_tool_schema(definition),
        }
        if strict_tools:
            function["strict"] = True
        rendered.append({"type": "function", "function": function})
    return rendered


def _openai_tool_choice(value: str | JsonObject) -> JsonValue:
    """@brief 验证并渲染 OpenAI tool_choice / Validate and render OpenAI tool_choice.

    @param value provider-neutral tool-choice input / Provider-neutral tool-choice input.
    @return OpenAI wire tool choice / OpenAI wire tool choice.
    @raise MessageContractError choice 未受支持时抛出 / Raised for an unsupported choice.
    """

    if isinstance(value, str):
        if value in {"auto", "none", "required"}:
            return value
        raise MessageContractError(f"Unsupported OpenAI tool_choice string: {value!r}")
    copied = copy_json_value(value, context="OpenAI tool_choice")
    if not isinstance(copied, dict):
        raise MessageContractError("OpenAI tool_choice must be an object")
    if set(copied) == {"name"} and isinstance(copied.get("name"), str):
        return {
            "type": "function",
            "function": {"name": copied["name"]},
        }
    function = copied.get("function")
    if (
        copied.get("type") == "function"
        and isinstance(function, dict)
        and set(function) == {"name"}
        and isinstance(function.get("name"), str)
    ):
        return copied
    raise MessageContractError("OpenAI tool_choice must select exactly one function")


def _apply_tuning(
    payload: ProviderPayload,
    *,
    temperature: float | None,
    top_p: float | None,
    stop_sequences: tuple[str, ...],
    seed: int | None,
    reasoning_effort: str | None,
    parallel_tool_calls: bool | None,
) -> None:
    """@brief 显式写入允许的 OpenAI 可调参数 / Explicitly write allowed OpenAI tuning parameters.

    @param payload 待修改 payload / Payload to mutate.
    @param temperature 可选温度 / Optional temperature.
    @param top_p 可选 top-p / Optional top-p.
    @param stop_sequences 停止序列 / Stop sequences.
    @param seed 可选 seed / Optional seed.
    @param reasoning_effort 可选推理档位 / Optional reasoning tier.
    @param parallel_tool_calls 可选并行工具开关 / Optional parallel-tool switch.
    @return None / None.
    """

    if temperature is not None:
        payload["temperature"] = temperature
    if top_p is not None:
        payload["top_p"] = top_p
    if stop_sequences:
        payload["stop"] = list(stop_sequences)
    if seed is not None:
        payload["seed"] = seed
    if reasoning_effort is not None:
        payload["reasoning_effort"] = reasoning_effort
    if parallel_tool_calls is not None:
        payload["parallel_tool_calls"] = parallel_tool_calls


def _response_text(value: object) -> str:
    """@brief 解码 OpenAI Assistant content 中的文本 / Decode text from OpenAI Assistant content.

    @param value response message content / Response message content.
    @return 可展示文本 / Displayable text.
    @raise MessageContractError content block 非法时抛出 / Raised for an invalid content block.
    """

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise MessageContractError("OpenAI response message content must be text, null, or an array")
    texts: list[str] = []
    for index, block in enumerate(value):
        if not isinstance(block, Mapping):
            raise MessageContractError(
                f"OpenAI response content block {index} must be an object"
            )
        raw_block = cast(Mapping[str, object], block)
        if raw_block.get("type") not in {"text", "output_text"}:
            raise MessageContractError(
                f"Unsupported OpenAI response content block type: {raw_block.get('type')!r}"
            )
        texts.append(_required_response_string(raw_block, "text", index=index))
    return "".join(texts)


def _openai_tool_calls(
    value: object,
    parts: list[ProviderPayload],
) -> None:
    """@brief 解码 OpenAI function tool calls / Decode OpenAI function tool calls.

    @param value 原始 tool_calls 字段 / Raw tool_calls field.
    @param parts 待追加的 canonical assistant parts / Canonical assistant parts to append to.
    @return None / None.
    @raise MessageContractError arguments 不是有效 JSON object 时抛出 /
        Raised when arguments are not a valid JSON object.
    """

    if value is None:
        return
    if not isinstance(value, list):
        raise MessageContractError("OpenAI response tool_calls must be an array")
    for index, raw_call in enumerate(value):
        if not isinstance(raw_call, Mapping):
            raise MessageContractError(f"OpenAI tool_call {index} must be an object")
        call = cast(Mapping[str, object], raw_call)
        call_id = _nonblank(call.get("id"), context=f"OpenAI tool_call {index}.id")
        function = call.get("function")
        if not isinstance(function, Mapping):
            raise MessageContractError(
                f"OpenAI tool_call {index}.function must be an object"
            )
        raw_function = cast(Mapping[str, object], function)
        name = _nonblank(
            raw_function.get("name"), context=f"OpenAI tool_call {index}.function.name"
        )
        raw_arguments = raw_function.get("arguments")
        if not isinstance(raw_arguments, str):
            raise MessageContractError(
                f"OpenAI tool_call {index}.function.arguments must be a JSON string"
            )
        try:
            decoded = json.loads(raw_arguments)
        except json.JSONDecodeError as error:
            raise MessageContractError(
                f"OpenAI tool_call {index}.function.arguments is not valid JSON"
            ) from error
        if not isinstance(decoded, Mapping):
            raise MessageContractError(
                f"OpenAI tool_call {index}.function.arguments must decode to a JSON object"
            )
        arguments = copy_json_value(
            decoded,
            context=f"OpenAI tool_call {index}.function.arguments",
        )
        if not isinstance(arguments, dict):
            raise MessageContractError("OpenAI tool-call arguments lost object shape")
        parts.append(
            {
                "type": "tool_call",
                "call_id": call_id,
                "name": name,
                "arguments": arguments,
            }
        )


def _usage_tokens(value: object, *keys: str) -> int | None:
    """@brief 从 usage object 读取非负整数 token / Read a non-negative integer token count from a usage object.

    @param value 原始 usage 字段 / Raw usage field.
    @param keys 依次尝试的字段名 / Field names to try in order.
    @return token 数或 None / Token count or None.
    """

    if not isinstance(value, Mapping):
        return None
    usage = cast(Mapping[str, object], value)
    for key in keys:
        raw = usage.get(key)
        if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
            return raw
    return None


def _part_string(part: Mapping[str, JsonValue], key: str, *, context: str) -> str:
    """@brief 从已验证 part 读取非空字符串 / Read a non-blank string from a validated part.

    @param part 已验证 part / Validated part.
    @param key 字段名 / Field name.
    @param context 错误上下文 / Error context.
    @return 非空字符串 / Non-blank string.
    """

    return _nonblank(part.get(key), context=f"{context}.{key}")


def _required_response_string(
    value: Mapping[str, object],
    key: str,
    *,
    index: int,
) -> str:
    """@brief 从 response block 读取字符串 / Read a string from a response block.

    @param value response block / Response block.
    @param key 字段名 / Field name.
    @param index block 序号 / Block ordinal.
    @return 字符串 / String.
    @raise MessageContractError 字段不是字符串时抛出 / Raised when the field is not a string.
    """

    raw = value.get(key)
    if not isinstance(raw, str):
        raise MessageContractError(f"OpenAI response content block {index}.{key} must be a string")
    return raw


def _positive_integer(value: int, *, context: str) -> int:
    """@brief 验证正整数 / Validate a positive integer.

    @param value 原始整数 / Raw integer.
    @param context 错误上下文 / Error context.
    @return 已验证整数 / Validated integer.
    @raise MessageContractError 值不为正时抛出 / Raised when the value is not positive.
    """

    if isinstance(value, bool) or value < 1:
        raise MessageContractError(f"{context} must be a positive integer")
    return value


def _nonblank(value: object, *, context: str) -> str:
    """@brief 验证非空字符串 / Validate a non-blank string.

    @param value 原始值 / Raw value.
    @param context 错误上下文 / Error context.
    @return 去除首尾空白的字符串 / Trimmed string.
    @raise MessageContractError 值为空或不是字符串时抛出 / Raised when the value is blank or not a string.
    """

    if not isinstance(value, str) or not (normalized := value.strip()):
        raise MessageContractError(f"{context} must be a non-blank string")
    return normalized


__all__ = ["decode_openai_response", "encode_openai_request"]
