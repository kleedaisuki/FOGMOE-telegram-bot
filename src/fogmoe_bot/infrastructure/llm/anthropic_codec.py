"""@brief 原生 Anthropic Messages 协议 codec / Native Anthropic Messages protocol codec.

该实现直接使用 Messages API 的 ``tool_use`` / ``tool_result`` block，不经 OpenAI
兼容层；provider 的 thinking blocks 不进入可持久化 canonical history。/
This implementation directly uses Messages API ``tool_use`` / ``tool_result`` blocks,
without an OpenAI compatibility layer; provider thinking blocks never enter the
persistable canonical history.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

from fogmoe_bot.application.assistant.tools.catalog import ToolDefinition
from fogmoe_bot.domain.assistant.messages import CanonicalMessage
from fogmoe_bot.domain.conversation.payloads import JsonObject, JsonValue

from .messages import (
    MessageContractError,
    ProviderPayload,
    anthropic_image_source,
    canonical_message_parts,
    copy_json_value,
    make_assistant_message,
    payload_array,
    text_or_json,
    thaw_tool_schema,
)
from .provider_response import DecodedProviderCompletion


def encode_anthropic_request(
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
) -> ProviderPayload:
    """@brief 构造原生 Anthropic Messages payload / Build a native Anthropic Messages payload.

    @param model route 选中的模型 / Model selected by the route.
    @param messages Canonical Message V2 历史 / Canonical Message V2 history.
    @param tools 应用层 typed tools / Application-layer typed tools.
    @param tool_choice 显式工具选择 / Explicit tool choice.
    @param max_tokens 输出 token 上限 / Output-token limit.
    @param metadata 仅允许 user_id 的路由元数据 / Route metadata permitting user_id only.
    @param strict_tools 是否为每个工具启用 strict / Whether to enable strict mode for every tool.
    @param temperature 可选采样温度 / Optional sampling temperature.
    @param top_p 可选 nucleus 采样阈值 / Optional nucleus sampling threshold.
    @param stop_sequences 可选停止序列 / Optional stop sequences.
    @return 新建的 Anthropic payload / Fresh Anthropic payload.
    @raise MessageContractError canonical message、metadata 或 tool choice 非法时抛出 /
        Raised for invalid canonical messages, metadata, or tool choice.
    """

    system, rendered_messages = _render_anthropic_messages(messages)
    payload: ProviderPayload = {
        "model": _nonblank(model, context="Anthropic model"),
        "max_tokens": _positive_integer(max_tokens, context="Anthropic max_tokens"),
        "messages": payload_array(rendered_messages),
    }
    if system:
        payload["system"] = payload_array(system)
    if tools:
        payload["tools"] = payload_array(
            _anthropic_tools(tools, strict_tools=strict_tools)
        )
        if tool_choice is not None:
            payload["tool_choice"] = _anthropic_tool_choice(tool_choice)
    elif tool_choice is not None:
        raise MessageContractError("Anthropic tool_choice requires at least one exposed tool")
    if metadata:
        if set(metadata) != {"user_id"}:
            raise MessageContractError(
                "Anthropic request metadata may contain only user_id"
            )
        payload["metadata"] = {"user_id": metadata["user_id"]}
    if temperature is not None:
        payload["temperature"] = temperature
    if top_p is not None:
        payload["top_p"] = top_p
    if stop_sequences:
        payload["stop_sequences"] = list(stop_sequences)
    return payload


def decode_anthropic_response(
    payload: Mapping[str, object],
) -> DecodedProviderCompletion:
    """@brief 严格解码原生 Anthropic Messages 响应 / Strictly decode a native Anthropic Messages response.

    @param payload 顶层 JSON response object / Top-level JSON response object.
    @return 含 Canonical Message V2 的已解码响应 / Decoded response containing Canonical Message V2.
    @raise MessageContractError content blocks 或 tool_use input 非法时抛出 /
        Raised for invalid content blocks or tool_use input.
    """

    content = payload.get("content")
    if not isinstance(content, list):
        raise MessageContractError("Anthropic response content must be an array")
    parts: list[ProviderPayload] = []
    for index, raw_block in enumerate(content):
        if not isinstance(raw_block, Mapping):
            raise MessageContractError(
                f"Anthropic response content block {index} must be an object"
            )
        block = cast(Mapping[str, object], raw_block)
        kind = block.get("type")
        if kind == "text":
            text = block.get("text")
            if not isinstance(text, str):
                raise MessageContractError(
                    f"Anthropic text block {index}.text must be a string"
                )
            if text:
                parts.append({"type": "text", "text": text})
            continue
        if kind == "tool_use":
            call_id = _nonblank(block.get("id"), context=f"Anthropic tool_use {index}.id")
            name = _nonblank(
                block.get("name"), context=f"Anthropic tool_use {index}.name"
            )
            raw_input = block.get("input")
            if not isinstance(raw_input, Mapping):
                raise MessageContractError(
                    f"Anthropic tool_use {index}.input must be a JSON object"
                )
            arguments = copy_json_value(
                raw_input,
                context=f"Anthropic tool_use {index}.input",
            )
            if not isinstance(arguments, dict):
                raise MessageContractError("Anthropic tool_use input lost object shape")
            parts.append(
                {
                    "type": "tool_call",
                    "call_id": call_id,
                    "name": name,
                    "arguments": arguments,
                }
            )
            continue
        if kind in {"thinking", "redacted_thinking"}:
            continue
        raise MessageContractError(
            f"Unsupported Anthropic response content block type: {kind!r}"
        )
    return DecodedProviderCompletion(
        message=CanonicalMessage.from_json(make_assistant_message(parts)),
        input_tokens=_usage_tokens(payload.get("usage"), "input_tokens"),
        output_tokens=_usage_tokens(payload.get("usage"), "output_tokens"),
    )


def _render_anthropic_messages(
    messages: Sequence[JsonObject],
) -> tuple[list[ProviderPayload], list[ProviderPayload]]:
    """@brief 将 V2 history 拆为 Anthropic system 与 messages / Split V2 history into Anthropic system and messages.

    @param messages canonical history / Canonical history.
    @return system blocks 与 conversation messages / System blocks and conversation messages.
    @raise MessageContractError role/part 组合不符合 Anthropic 语义时抛出 /
        Raised when role/part combinations violate Anthropic semantics.
    """

    system: list[ProviderPayload] = []
    rendered: list[ProviderPayload] = []
    pending_tool_results: list[ProviderPayload] = []

    def flush_tool_results() -> None:
        """@brief 将连续 canonical tool 消息聚合为一个 Anthropic user turn / Aggregate consecutive canonical tool messages into one Anthropic user turn.

        @return None / None.
        """

        if not pending_tool_results:
            return
        rendered.append(
            {
                "role": "user",
                "content": payload_array(
                    _anthropic_tool_result_blocks(pending_tool_results)
                ),
            }
        )
        pending_tool_results.clear()

    for ordinal, value in enumerate(messages):
        if not isinstance(value, Mapping):
            raise MessageContractError(f"Canonical messages[{ordinal}] must be an object")
        role, parts = canonical_message_parts(cast(Mapping[str, object], value))
        if role == "tool":
            pending_tool_results.extend(parts)
            continue
        flush_tool_results()
        if role == "system":
            if any(part.get("type") != "text" for part in parts):
                raise MessageContractError(
                    "Anthropic system messages may contain only text parts"
                )
            system.extend(_anthropic_text_blocks(parts))
            continue
        if role == "assistant":
            rendered.append(
                {
                    "role": "assistant",
                    "content": payload_array(_anthropic_assistant_blocks(parts)),
                }
            )
            continue
        rendered.append(
            {
                "role": "user",
                "content": payload_array(_anthropic_user_blocks(parts)),
            }
        )
    flush_tool_results()
    if not rendered:
        raise MessageContractError("Anthropic request must contain at least one user or assistant message")
    return system, rendered


def _anthropic_text_blocks(parts: Sequence[ProviderPayload]) -> list[ProviderPayload]:
    """@brief 渲染纯文本 parts / Render text-only parts.

    @param parts text parts / Text parts.
    @return Anthropic text blocks / Anthropic text blocks.
    """

    return [
        {"type": "text", "text": _part_string(part, "text", context="text part")}
        for part in parts
    ]


def _anthropic_assistant_blocks(parts: Sequence[ProviderPayload]) -> list[ProviderPayload]:
    """@brief 渲染 assistant parts / Render assistant parts.

    @param parts canonical assistant parts / Canonical assistant parts.
    @return Anthropic assistant content blocks / Anthropic assistant content blocks.
    @raise MessageContractError assistant part 非法时抛出 / Raised for an invalid assistant part.
    """

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
            case "tool_call":
                raw_arguments = part.get("arguments")
                if not isinstance(raw_arguments, dict):
                    raise MessageContractError(
                        "Anthropic tool_call.arguments must be a JSON object"
                    )
                rendered.append(
                    {
                        "type": "tool_use",
                        "id": _part_string(part, "call_id", context="tool_call"),
                        "name": _part_string(part, "name", context="tool_call"),
                        "input": dict(raw_arguments),
                    }
                )
            case other:
                raise MessageContractError(
                    f"Anthropic assistant cannot render canonical part {other!r}"
                )
    if not rendered:
        raise MessageContractError("Anthropic assistant message must contain at least one part")
    return rendered


def _anthropic_user_blocks(parts: Sequence[ProviderPayload]) -> list[ProviderPayload]:
    """@brief 渲染 user text/image parts / Render user text/image parts.

    @param parts canonical user parts / Canonical user parts.
    @return Anthropic user content blocks / Anthropic user content blocks.
    @raise MessageContractError user part 非法时抛出 / Raised for an invalid user part.
    """

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
                        "type": "image",
                        "source": anthropic_image_source(part),
                    }
                )
            case other:
                raise MessageContractError(
                    f"Anthropic user cannot render canonical part {other!r}"
                )
    if not rendered:
        raise MessageContractError("Anthropic user message must contain at least one part")
    return rendered


def _anthropic_tool_result_blocks(
    parts: Sequence[ProviderPayload],
) -> list[ProviderPayload]:
    """@brief 将连续 canonical tool parts 渲染为 Anthropic tool_result blocks / Render consecutive canonical tool parts as Anthropic tool_result blocks.

    @param parts canonical tool-result parts / Canonical tool-result parts.
    @return Anthropic user-turn tool_result blocks / Anthropic user-turn tool_result blocks.
    @raise MessageContractError part 不是完整 tool_result 时抛出 / Raised when a part is not a complete tool_result.
    """

    rendered: list[ProviderPayload] = []
    for part in parts:
        if part.get("type") != "tool_result":
            raise MessageContractError("Canonical tool messages must contain tool_result parts")
        raw_result = part.get("result")
        if raw_result is None and "result" not in part:
            raise MessageContractError("tool_result.result is required")
        raw_is_error = part.get("is_error")
        if not isinstance(raw_is_error, bool):
            raise MessageContractError("tool_result.is_error must be a boolean")
        rendered.append(
            {
                "type": "tool_result",
                "tool_use_id": _part_string(
                    part,
                    "call_id",
                    context="tool_result",
                ),
                "content": text_or_json(raw_result),
                "is_error": raw_is_error,
            }
        )
    if not rendered:
        raise MessageContractError("Canonical tool message must contain at least one tool_result")
    return rendered


def _anthropic_tools(
    definitions: Sequence[ToolDefinition],
    *,
    strict_tools: bool,
) -> list[ProviderPayload]:
    """@brief 序列化 typed tools 为 Anthropic tools / Serialize typed tools as Anthropic tools.

    @param definitions 应用层工具定义 / Application-layer tool definitions.
    @param strict_tools 是否为每个工具启用 strict / Whether to enable strict mode for every tool.
    @return Anthropic tool definitions / Anthropic tool definitions.
    """

    rendered: list[ProviderPayload] = []
    for definition in definitions:
        tool: ProviderPayload = {
            "name": definition.name,
            "description": definition.description,
            "input_schema": thaw_tool_schema(definition),
        }
        if strict_tools:
            tool["strict"] = True
        rendered.append(tool)
    return rendered


def _anthropic_tool_choice(value: str | JsonObject) -> ProviderPayload:
    """@brief 验证并渲染 Anthropic tool_choice / Validate and render Anthropic tool_choice.

    @param value provider-neutral tool-choice input / Provider-neutral tool-choice input.
    @return Anthropic wire tool choice / Anthropic wire tool choice.
    @raise MessageContractError choice 未受支持时抛出 / Raised for an unsupported choice.
    """

    if isinstance(value, str):
        if value == "auto":
            return {"type": "auto"}
        if value == "required":
            return {"type": "any"}
        raise MessageContractError(
            f"Unsupported Anthropic tool_choice string: {value!r}"
        )
    copied = copy_json_value(value, context="Anthropic tool_choice")
    if not isinstance(copied, dict):
        raise MessageContractError("Anthropic tool_choice must be an object")
    if set(copied) == {"name"} and isinstance(copied.get("name"), str):
        return {"type": "tool", "name": copied["name"]}
    if (
        copied.get("type") == "tool"
        and set(copied) == {"type", "name"}
        and isinstance(copied.get("name"), str)
    ):
        return {"type": "tool", "name": copied["name"]}
    function = copied.get("function")
    if (
        copied.get("type") == "function"
        and isinstance(function, dict)
        and set(function) == {"name"}
        and isinstance(function.get("name"), str)
    ):
        return {"type": "tool", "name": function["name"]}
    raise MessageContractError("Anthropic tool_choice must select exactly one tool")


def _usage_tokens(value: object, key: str) -> int | None:
    """@brief 从 Anthropic usage 读取非负整数 / Read a non-negative integer from Anthropic usage.

    @param value 原始 usage 对象 / Raw usage object.
    @param key token 字段名 / Token field name.
    @return token 数或 None / Token count or None.
    """

    if not isinstance(value, Mapping):
        return None
    raw = cast(Mapping[str, object], value).get(key)
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else None


def _part_string(part: Mapping[str, JsonValue], key: str, *, context: str) -> str:
    """@brief 从已验证 part 读取非空字符串 / Read a non-blank string from a validated part.

    @param part 已验证 part / Validated part.
    @param key 字段名 / Field name.
    @param context 错误上下文 / Error context.
    @return 非空字符串 / Non-blank string.
    """

    return _nonblank(part.get(key), context=f"{context}.{key}")


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


__all__ = ["decode_anthropic_response", "encode_anthropic_request"]
