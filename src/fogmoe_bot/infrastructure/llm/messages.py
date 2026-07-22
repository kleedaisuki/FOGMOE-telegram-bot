"""@brief Canonical Message V2 到 provider wire 的安全辅助 / Safe Canonical Message V2 helpers for provider wire formats.

这里不定义领域消息模型；领域层拥有 Message V2 的权威类型。本模块只在基础设施边界
验证其 JSON 投影、复制可持久化值，并生成 OpenAI 与 Anthropic renderer 所需的局部
表示。/
This module does not define the domain message model. The domain owns the authoritative
Message V2 types. This infrastructure boundary only validates their JSON projections,
copies persistable values, and produces local representations required by OpenAI and
Anthropic renderers.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from typing import Literal, cast

from fogmoe_bot.application.assistant.tools.catalog import (
    FrozenSchemaValue,
    ToolDefinition,
)
from fogmoe_bot.domain.conversation.payloads import JsonObject, JsonValue

type ProviderPayload = dict[str, JsonValue]
"""@brief Provider wire object 的 JSON 投影 / JSON projection of a provider wire object."""

type CanonicalRole = Literal["system", "user", "assistant", "tool"]
"""@brief Canonical Message V2 支持的角色 / Roles supported by Canonical Message V2."""

CANONICAL_MESSAGE_SCHEMA_VERSION = 2
"""@brief 当前 canonical message schema 版本 / Current canonical-message schema version."""

_CANONICAL_ROLES = frozenset({"system", "user", "assistant", "tool"})
"""@brief 允许的 canonical role 集合 / Set of accepted canonical roles."""


class MessageContractError(ValueError):
    """@brief Canonical Message V2 或内部 JSON 投影非法 / Invalid Canonical Message V2 or internal JSON projection."""


def copy_json_value(value: object, *, context: str = "JSON value") -> JsonValue:
    """@brief 深复制并验证严格 JSON 值 / Deep-copy and validate a strict JSON value.

    @param value 待复制的外部值 / External value to copy.
    @param context 面向操作者的字段上下文 / Operator-facing field context.
    @return 独立、可 JSON 持久化的值 / Independent JSON-persistable value.
    @raise MessageContractError 值不是有限 JSON 树时抛出 / Raised when the value is not a finite JSON tree.
    """

    if value is None or isinstance(value, str | bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise MessageContractError(f"{context} cannot contain a non-finite float")
        return value
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        copied: dict[str, JsonValue] = {}
        for key, item in mapping.items():
            if not isinstance(key, str):
                raise MessageContractError(f"{context} object keys must be strings")
            copied[key] = copy_json_value(item, context=f"{context}.{key}")
        return copied
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        sequence = cast(Sequence[object], value)
        return [
            copy_json_value(item, context=f"{context}[{index}]")
            for index, item in enumerate(sequence)
        ]
    raise MessageContractError(
        f"{context} must be a JSON value, got {type(value).__name__}"
    )


def canonical_message_parts(
    message: Mapping[str, object],
) -> tuple[CanonicalRole, tuple[ProviderPayload, ...]]:
    """@brief 校验一个 Canonical Message V2 JSON 投影 / Validate one Canonical Message V2 JSON projection.

    @param message 领域层导出的 canonical message / Canonical message exported by the domain layer.
    @return 已验证 role 与独立 parts / Validated role and independent parts.
    @raise MessageContractError schema、policy、meta 或 part 违反 V2 契约时抛出 /
        Raised when schema, policy, meta, or a part violates the V2 contract.
    """

    schema_version = message.get("schema_version")
    if isinstance(schema_version, bool) or schema_version != CANONICAL_MESSAGE_SCHEMA_VERSION:
        raise MessageContractError(
            "Canonical message must use schema_version=2 before reaching an LLM provider"
        )
    raw_role = message.get("role")
    if not isinstance(raw_role, str) or raw_role not in _CANONICAL_ROLES:
        raise MessageContractError(
            "Canonical message role must be system, user, assistant, or tool"
        )
    role = cast(CanonicalRole, raw_role)
    _validate_policy(message.get("policy"))
    _validate_meta(message.get("meta"))
    raw_parts = message.get("parts")
    if not isinstance(raw_parts, list):
        raise MessageContractError("Canonical message parts must be a JSON array")
    parts = tuple(
        _canonical_part(raw_part, role=role, index=index)
        for index, raw_part in enumerate(raw_parts)
    )
    return role, parts


def message_text(parts: Sequence[ProviderPayload]) -> str:
    """@brief 按 part 顺序提取文本 / Extract text in part order.

    @param parts 已验证的 canonical parts / Validated canonical parts.
    @return 所有 text part 的串接文本 / Concatenation of all text parts.
    """

    values: list[str] = []
    for part in parts:
        if part.get("type") != "text":
            continue
        text = part.get("text")
        if not isinstance(text, str):
            raise MessageContractError("Validated text part lost its text field")
        values.append(text)
    return "".join(values)


def text_or_json(value: JsonValue) -> str:
    """@brief 将 tool result 变为 provider 允许的文本 / Convert a tool result into provider-allowed text.

    @param value 工具的 canonical JSON 结果 / Canonical JSON result from a tool.
    @return 原字符串或紧凑 JSON 文本 / Original string or compact JSON text.
    """

    return value if isinstance(value, str) else json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def compact_json(value: JsonValue) -> str:
    """@brief 将 canonical JSON 值编码为无空白 JSON / Encode a canonical JSON value without whitespace.

    @param value 已验证 JSON 值 / Validated JSON value.
    @return 稳定的紧凑 JSON / Stable compact JSON.
    """

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def make_assistant_message(parts: Sequence[ProviderPayload]) -> JsonObject:
    """@brief 构造 provider-neutral Assistant Message V2 / Build a provider-neutral Assistant Message V2.

    @param parts 已验证、可持久化的 assistant parts / Validated persistable assistant parts.
    @return 新建的 Canonical Message V2 JSON object / Fresh Canonical Message V2 JSON object.
    """

    copied_parts = [copy_json_value(part, context="assistant.parts") for part in parts]
    if any(not isinstance(part, dict) for part in copied_parts):
        raise MessageContractError("Assistant parts must serialize to JSON objects")
    message: JsonObject = {
        "schema_version": CANONICAL_MESSAGE_SCHEMA_VERSION,
        "role": "assistant",
        "parts": copied_parts,
        "policy": {"include_in_context": True},
        "meta": {},
    }
    canonical_message_parts(message)
    return message


def thaw_tool_schema(definition: ToolDefinition) -> JsonObject:
    """@brief 将冻结的工具 schema 解冻为 JSON object / Thaw a frozen tool schema into a JSON object.

    @param definition 应用层权威工具定义 / Authoritative application-layer tool definition.
    @return 独立 JSON Schema object / Independent JSON Schema object.
    @raise MessageContractError schema 不能序列化为对象时抛出 / Raised when schema cannot serialize to an object.
    """

    value = _thaw_schema_value(definition.parameters_schema)
    if not isinstance(value, dict):
        raise MessageContractError("Tool parameters schema must serialize to a JSON object")
    return value


def payload_array(values: Sequence[ProviderPayload]) -> list[JsonValue]:
    """@brief 将 provider object 序列提升为 JSON array / Lift a provider-object sequence into a JSON array.

    @param values provider JSON objects / Provider JSON objects.
    @return 独立 JSON array / Independent JSON array.
    """

    result: list[JsonValue] = []
    for value in values:
        result.append(dict(value))
    return result


def openai_image_url(part: Mapping[str, JsonValue]) -> str:
    """@brief 渲染 V2 image part 为 OpenAI image URL / Render a V2 image part as an OpenAI image URL.

    @param part 已验证 image part / Validated image part.
    @return HTTP(S) 或 data URL / HTTP(S) or data URL.
    @raise MessageContractError image source 非法时抛出 / Raised for an invalid image source.
    """

    source = _required_object(part, "source", context="image part")
    kind = _required_string(source, "kind", context="image source")
    if kind == "url":
        return _required_string(source, "url", context="URL image source")
    if kind == "base64":
        media_type = _required_string(source, "media_type", context="base64 image source")
        data = _required_string(source, "data", context="base64 image source")
        return f"data:{media_type};base64,{data}"
    raise MessageContractError(f"Unsupported image source kind: {kind!r}")


def anthropic_image_source(part: Mapping[str, JsonValue]) -> ProviderPayload:
    """@brief 渲染 V2 image part 为 Anthropic source / Render a V2 image part as an Anthropic source.

    @param part 已验证 image part / Validated image part.
    @return Anthropic image source object / Anthropic image source object.
    @raise MessageContractError image source 非法时抛出 / Raised for an invalid image source.
    """

    source = _required_object(part, "source", context="image part")
    kind = _required_string(source, "kind", context="image source")
    if kind == "url":
        return {
            "type": "url",
            "url": _required_string(source, "url", context="URL image source"),
        }
    if kind == "base64":
        return {
            "type": "base64",
            "media_type": _required_string(
                source,
                "media_type",
                context="base64 image source",
            ),
            "data": _required_string(source, "data", context="base64 image source"),
        }
    raise MessageContractError(f"Unsupported image source kind: {kind!r}")


def _canonical_part(
    value: object,
    *,
    role: CanonicalRole,
    index: int,
) -> ProviderPayload:
    """@brief 校验一个 canonical part / Validate one canonical part.

    @param value 原始 JSON part / Raw JSON part.
    @param role 所属消息角色 / Owning message role.
    @param index part 顺序 / Part ordinal.
    @return 独立且字段受限的 part / Independent part with constrained fields.
    @raise MessageContractError part 类型、角色或字段非法时抛出 / Raised for invalid part kind, role, or fields.
    """

    if not isinstance(value, Mapping):
        raise MessageContractError(f"Canonical message parts[{index}] must be an object")
    raw = cast(Mapping[str, object], value)
    kind = _required_string(raw, "type", context=f"canonical parts[{index}]")
    match kind:
        case "text":
            _require_only_keys(raw, {"type", "text"}, context=f"text part {index}")
            return {
                "type": "text",
                "text": _required_string(raw, "text", context=f"text part {index}"),
            }
        case "image":
            if role == "assistant":
                raise MessageContractError("Assistant messages cannot contain image parts")
            _require_only_keys(raw, {"type", "source"}, context=f"image part {index}")
            return {
                "type": "image",
                "source": _image_source(raw.get("source"), index=index),
            }
        case "tool_call":
            if role != "assistant":
                raise MessageContractError("tool_call parts are valid only for assistant messages")
            _require_only_keys(
                raw,
                {"type", "call_id", "name", "arguments"},
                context=f"tool_call part {index}",
            )
            arguments = copy_json_value(
                raw.get("arguments"),
                context=f"tool_call part {index}.arguments",
            )
            if not isinstance(arguments, dict):
                raise MessageContractError(
                    f"tool_call part {index}.arguments must be a JSON object"
                )
            return {
                "type": "tool_call",
                "call_id": _nonblank_string(
                    raw.get("call_id"), context=f"tool_call part {index}.call_id"
                ),
                "name": _nonblank_string(
                    raw.get("name"), context=f"tool_call part {index}.name"
                ),
                "arguments": arguments,
            }
        case "tool_result":
            if role != "tool":
                raise MessageContractError("tool_result parts are valid only for tool messages")
            _require_only_keys(
                raw,
                {"type", "call_id", "name", "result", "is_error"},
                context=f"tool_result part {index}",
            )
            raw_is_error = raw.get("is_error")
            if not isinstance(raw_is_error, bool):
                raise MessageContractError(
                    f"tool_result part {index}.is_error must be a boolean"
                )
            return {
                "type": "tool_result",
                "call_id": _nonblank_string(
                    raw.get("call_id"), context=f"tool_result part {index}.call_id"
                ),
                "name": _nonblank_string(
                    raw.get("name"), context=f"tool_result part {index}.name"
                ),
                "result": copy_json_value(
                    raw.get("result"),
                    context=f"tool_result part {index}.result",
                ),
                "is_error": raw_is_error,
            }
        case _:
            raise MessageContractError(f"Unsupported canonical part type: {kind!r}")


def _image_source(value: object, *, index: int) -> ProviderPayload:
    """@brief 校验 V2 image source / Validate a V2 image source.

    @param value 原始 source 对象 / Raw source object.
    @param index 所属 part 序号 / Owning part ordinal.
    @return 独立 image source / Independent image source.
    @raise MessageContractError source 形状非法时抛出 / Raised for an invalid source shape.
    """

    if not isinstance(value, Mapping):
        raise MessageContractError(f"image part {index}.source must be an object")
    source = cast(Mapping[str, object], value)
    kind = _required_string(source, "kind", context=f"image part {index}.source")
    if kind == "url":
        _require_only_keys(source, {"kind", "url"}, context=f"URL image source {index}")
        return {
            "kind": "url",
            "url": _nonblank_string(
                source.get("url"), context=f"URL image source {index}.url"
            ),
        }
    if kind == "base64":
        _require_only_keys(
            source,
            {"kind", "media_type", "data"},
            context=f"base64 image source {index}",
        )
        return {
            "kind": "base64",
            "media_type": _nonblank_string(
                source.get("media_type"),
                context=f"base64 image source {index}.media_type",
            ),
            "data": _nonblank_string(
                source.get("data"), context=f"base64 image source {index}.data"
            ),
        }
    raise MessageContractError(f"Unsupported image source kind: {kind!r}")


def _validate_policy(value: object) -> None:
    """@brief 校验 Canonical Message V2 policy / Validate Canonical Message V2 policy.

    @param value 原始 policy 对象 / Raw policy object.
    @return None / None.
    @raise MessageContractError policy 不是最小 V2 结构时抛出 / Raised when policy is not the minimal V2 structure.
    """

    if not isinstance(value, Mapping):
        raise MessageContractError("Canonical message policy must be an object")
    policy = cast(Mapping[str, object], value)
    _require_only_keys(policy, {"include_in_context"}, context="canonical policy")
    if not isinstance(policy.get("include_in_context"), bool):
        raise MessageContractError("Canonical policy.include_in_context must be a boolean")


def _validate_meta(value: object) -> None:
    """@brief 校验 Canonical Message V2 meta JSON object / Validate the Canonical Message V2 meta JSON object.

    @param value 原始 meta 对象 / Raw meta object.
    @return None / None.
    @raise MessageContractError meta 不可 JSON 持久化时抛出 / Raised when meta is not JSON-persistable.
    """

    copied = copy_json_value(value, context="canonical message meta")
    if not isinstance(copied, dict):
        raise MessageContractError("Canonical message meta must be an object")


def _thaw_schema_value(value: FrozenSchemaValue) -> JsonValue:
    """@brief 递归解冻一个 schema 节点 / Recursively thaw one schema node.

    @param value 不可变 schema 节点 / Immutable schema node.
    @return 独立 JSON 值 / Independent JSON value.
    """

    if isinstance(value, Mapping):
        mapping = value
        return {
            key: _thaw_schema_value(item)
            for key, item in mapping.items()
        }
    if isinstance(value, tuple):
        return [_thaw_schema_value(item) for item in value]
    return value


def _required_object(
    value: Mapping[str, JsonValue],
    key: str,
    *,
    context: str,
) -> ProviderPayload:
    """@brief 从已验证 part 读取对象字段 / Read an object field from a validated part.

    @param value 已验证的对象 / Validated object.
    @param key 必需字段名 / Required field name.
    @param context 面向操作者的上下文 / Operator-facing context.
    @return 对象字段的独立类型视图 / Object-field typed view.
    @raise MessageContractError 字段不是对象时抛出 / Raised when the field is not an object.
    """

    raw = value.get(key)
    if not isinstance(raw, dict):
        raise MessageContractError(f"{context}.{key} must be an object")
    return raw


def _required_string(
    value: Mapping[str, object] | Mapping[str, JsonValue],
    key: str,
    *,
    context: str,
) -> str:
    """@brief 从对象读取字符串字段 / Read a string field from an object.

    @param value 原始或已验证对象 / Raw or validated object.
    @param key 必需字段名 / Required field name.
    @param context 面向操作者的上下文 / Operator-facing context.
    @return 原样字符串 / Original string.
    @raise MessageContractError 字段不是字符串时抛出 / Raised when the field is not a string.
    """

    raw = value.get(key)
    if not isinstance(raw, str):
        raise MessageContractError(f"{context}.{key} must be a string")
    return raw


def _nonblank_string(value: object, *, context: str) -> str:
    """@brief 校验非空字符串 / Validate a non-blank string.

    @param value 原始值 / Raw value.
    @param context 面向操作者的字段上下文 / Operator-facing field context.
    @return 去除首尾空白后的字符串 / Trimmed string.
    @raise MessageContractError 值为空或不是字符串时抛出 / Raised when the value is blank or not a string.
    """

    if not isinstance(value, str) or not (normalized := value.strip()):
        raise MessageContractError(f"{context} must be a non-blank string")
    return normalized


def _require_only_keys(
    value: Mapping[str, object],
    allowed: set[str],
    *,
    context: str,
) -> None:
    """@brief 拒绝 V2 part 中的隐式字段 / Reject implicit fields in a V2 part.

    @param value 原始对象 / Raw object.
    @param allowed 允许字段集 / Allowed field set.
    @param context 面向操作者的上下文 / Operator-facing context.
    @return None / None.
    @raise MessageContractError 含未知字段时抛出 / Raised when unknown fields are present.
    """

    unknown = set(value).difference(allowed)
    if unknown:
        rendered = ", ".join(sorted(unknown))
        raise MessageContractError(f"{context} has unsupported fields: {rendered}")


__all__ = [
    "CANONICAL_MESSAGE_SCHEMA_VERSION",
    "CanonicalRole",
    "MessageContractError",
    "ProviderPayload",
    "anthropic_image_source",
    "canonical_message_parts",
    "compact_json",
    "copy_json_value",
    "make_assistant_message",
    "message_text",
    "openai_image_url",
    "payload_array",
    "text_or_json",
    "thaw_tool_schema",
]
