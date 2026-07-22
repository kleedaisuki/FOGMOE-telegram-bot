"""@brief Assistant 规范消息 V2 / Canonical Assistant messages V2.

本模块定义业务层唯一可见的模型消息中间表示（intermediate representation,
IR）。OpenAI-style 与 Anthropic-style 的 wire payload 只能在基础设施边界被渲染或
解析，不能进入 Conversation、Agent checkpoint 或 Context Window。
/ This module defines the only model-message intermediate representation visible to
the business layers. OpenAI-style and Anthropic-style wire payloads are rendered or
parsed only at the infrastructure boundary and must never enter Conversation, Agent
checkpoints, or the Context Window.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import cast

from fogmoe_bot.domain.conversation.message import MessageRole
from fogmoe_bot.domain.conversation.payloads import JsonObject, JsonValue

#: @brief 当前规范消息 JSON 版本 / Current canonical-message JSON version.
CANONICAL_MESSAGE_VERSION = 2
#: @brief 单条用户定义 meta 的 UTF-8 上限 / UTF-8 limit for one user-defined meta object.
MAX_MESSAGE_META_BYTES = 8 * 1024
#: @brief JSON 元数据最大嵌套层级 / Maximum JSON metadata nesting depth.
MAX_MESSAGE_META_DEPTH = 8

type FrozenJsonScalar = None | bool | int | float | str
"""@brief 冻结 JSON 标量 / Immutable JSON scalar."""

type FrozenJsonValue = (
    FrozenJsonScalar
    | tuple["FrozenJsonValue", ...]
    | Mapping[str, "FrozenJsonValue"]
)
"""@brief 深度冻结 JSON 值 / Deeply immutable JSON value."""

type FrozenJsonObject = Mapping[str, FrozenJsonValue]
"""@brief 深度冻结 JSON 对象 / Deeply immutable JSON object."""


class CanonicalMessageError(ValueError):
    """@brief canonical message 不变量被破坏 / Canonical-message invariant violation."""


@dataclass(frozen=True, slots=True)
class MessagePolicy:
    """@brief 消息在 Context 中的显式策略 / Explicit message policy in Context.

    @param include_in_context 是否可进入后续模型上下文 / Whether it may enter later model context.
    """

    include_in_context: bool = True

    def to_json(self) -> JsonObject:
        """@brief 序列化策略 / Serialize the policy.

        @return JSON 策略对象 / JSON policy object.
        """

        return {"include_in_context": self.include_in_context}

    @classmethod
    def from_json(cls, value: object) -> MessagePolicy:
        """@brief 严格解析策略 / Strictly parse a policy.

        @param value JSON policy object.
        @return 已验证策略 / Validated policy.
        @raise CanonicalMessageError 形状或字段非法时抛出 / Raised for an invalid shape or field.
        """

        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise CanonicalMessageError("message policy must be an object")
        extra = set(value) - {"include_in_context"}
        if extra:
            raise CanonicalMessageError(
                f"message policy has unsupported keys: {sorted(extra)!r}"
            )
        include_in_context = value.get("include_in_context", True)
        if type(include_in_context) is not bool:
            raise CanonicalMessageError("policy.include_in_context must be a boolean")
        return cls(include_in_context=include_in_context)


@dataclass(frozen=True, slots=True)
class TextPart:
    """@brief 规范文本片段 / Canonical text part.

    @param text 文本内容 / Text content.
    """

    text: str

    def __post_init__(self) -> None:
        """@brief 校验文本类型 / Validate the text type.

        @return None / None.
        """

        if not isinstance(self.text, str):
            raise CanonicalMessageError("text part text must be a string")

    def to_json(self) -> JsonObject:
        """@brief 序列化文本片段 / Serialize the text part.

        @return JSON 文本片段 / JSON text part.
        """

        return {"type": "text", "text": self.text}


@dataclass(frozen=True, slots=True)
class UrlImageSource:
    """@brief URL 图像来源 / URL image source.

    @param url HTTPS URL 或 data URI / HTTPS URL or data URI.
    """

    url: str

    def __post_init__(self) -> None:
        """@brief 校验 URL 来源 / Validate the URL source.

        @return None / None.
        """

        if not isinstance(self.url, str) or not self.url.strip():
            raise CanonicalMessageError("image URL must be a non-blank string")

    def to_json(self) -> JsonObject:
        """@brief 序列化 URL 来源 / Serialize the URL source.

        @return JSON image source / JSON image source.
        """

        return {"kind": "url", "url": self.url}


@dataclass(frozen=True, slots=True)
class Base64ImageSource:
    """@brief 内联 Base64 图像来源 / Inline Base64 image source.

    @param media_type MIME 类型 / MIME type.
    @param data 不带 data URI 前缀的 Base64 数据 / Base64 data without a data-URI prefix.
    """

    media_type: str
    data: str

    def __post_init__(self) -> None:
        """@brief 校验内联图像来源 / Validate the inline image source.

        @return None / None.
        """

        if not isinstance(self.media_type, str) or not self.media_type.startswith(
            "image/"
        ):
            raise CanonicalMessageError("inline image media_type must start with image/")
        if not isinstance(self.data, str) or not self.data.strip():
            raise CanonicalMessageError("inline image data must be a non-blank string")

    def to_json(self) -> JsonObject:
        """@brief 序列化 Base64 来源 / Serialize the Base64 source.

        @return JSON image source / JSON image source.
        """

        return {
            "kind": "base64",
            "media_type": self.media_type,
            "data": self.data,
        }


type ImageSource = UrlImageSource | Base64ImageSource
"""@brief 图像来源的封闭联合 / Closed union of image sources."""


@dataclass(frozen=True, slots=True)
class ImagePart:
    """@brief 规范图像片段 / Canonical image part.

    @param source 图像来源 / Image source.
    """

    source: ImageSource

    def to_json(self) -> JsonObject:
        """@brief 序列化图像片段 / Serialize the image part.

        @return JSON image part / JSON image part.
        """

        return {"type": "image", "source": self.source.to_json()}


@dataclass(frozen=True, slots=True)
class ToolCallPart:
    """@brief Assistant 发出的工具调用片段 / Tool-call part emitted by the Assistant.

    @param call_id 跨协议稳定调用标识 / Stable cross-protocol call identifier.
    @param name 工具目录中的名称 / Tool-catalog name.
    @param arguments 已解析但未执行的 JSON object 参数 / Parsed but unexecuted JSON-object arguments.
    """

    call_id: str
    name: str
    arguments: FrozenJsonValue

    def __post_init__(self) -> None:
        """@brief 校验并冻结调用载荷 / Validate and freeze the call payload.

        @return None / None.
        """

        _require_identifier(self.call_id, label="tool call_id")
        _require_identifier(self.name, label="tool name")
        if not isinstance(self.arguments, Mapping):
            raise CanonicalMessageError("tool call arguments must be a JSON object")
        object.__setattr__(self, "arguments", _freeze_json(self.arguments))

    def to_json(self) -> JsonObject:
        """@brief 序列化工具调用片段 / Serialize the tool-call part.

        @return JSON tool-call part / JSON tool-call part.
        """

        return {
            "type": "tool_call",
            "call_id": self.call_id,
            "name": self.name,
            "arguments": _thaw_json(self.arguments),
        }


@dataclass(frozen=True, slots=True)
class ToolResultPart:
    """@brief 工具执行结果片段 / Tool-result part.

    @param call_id 对应调用标识 / Corresponding call identifier.
    @param name 工具目录中的名称 / Tool-catalog name.
    @param result JSON 安全的公开结果 / JSON-safe public result.
    @param is_error 是否代表工具错误 / Whether this represents a tool error.
    """

    call_id: str
    name: str
    result: FrozenJsonValue
    is_error: bool = False

    def __post_init__(self) -> None:
        """@brief 校验并冻结结果载荷 / Validate and freeze the result payload.

        @return None / None.
        """

        _require_identifier(self.call_id, label="tool result call_id")
        _require_identifier(self.name, label="tool result name")
        if type(self.is_error) is not bool:
            raise CanonicalMessageError("tool result is_error must be a boolean")
        object.__setattr__(self, "result", _freeze_json(self.result))

    def to_json(self) -> JsonObject:
        """@brief 序列化工具结果片段 / Serialize the tool-result part.

        @return JSON tool-result part / JSON tool-result part.
        """

        return {
            "type": "tool_result",
            "call_id": self.call_id,
            "name": self.name,
            "result": _thaw_json(self.result),
            "is_error": self.is_error,
        }


type MessagePart = TextPart | ImagePart | ToolCallPart | ToolResultPart
"""@brief canonical message 部分的封闭联合 / Closed union of canonical message parts."""


@dataclass(frozen=True, slots=True)
class CanonicalMessage:
    """@brief 与 provider 无关的规范消息 / Provider-neutral canonical message.

    @param role 会话角色 / Conversation role.
    @param parts 保序的类型化片段 / Ordered typed parts.
    @param policy Context 驻留策略 / Context-residency policy.
    @param meta 用户或业务定义的非 prompt 元数据 / User- or business-defined non-prompt metadata.
    @note ``meta`` 绝不自动进入模型提示词或 HTTP payload；若某一 provider 支持受限的
        request metadata，路由 adapter 必须显式映射。/ ``meta`` never automatically enters a
        model prompt or HTTP payload; a route adapter must explicitly map it when a provider
        supports constrained request metadata.
    """

    role: MessageRole
    parts: tuple[MessagePart, ...]
    policy: MessagePolicy = field(default_factory=MessagePolicy)
    meta: FrozenJsonObject = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        """@brief 校验角色/片段代数数据类型 / Validate the role/part algebraic data type.

        @return None / None.
        """

        if not isinstance(self.role, MessageRole):
            raise CanonicalMessageError("canonical message role must be a MessageRole")
        parts = tuple(self.parts)
        if not parts:
            raise CanonicalMessageError("canonical message must contain at least one part")
        invalid = [part for part in parts if not isinstance(part, _ALL_PART_TYPES)]
        if invalid:
            raise CanonicalMessageError("canonical message contains an unknown part type")
        permitted = _PARTS_BY_ROLE[self.role]
        if any(not isinstance(part, permitted) for part in parts):
            raise CanonicalMessageError(
                f"role {self.role.value!r} cannot contain the supplied part type"
            )
        if not isinstance(self.policy, MessagePolicy):
            raise CanonicalMessageError("canonical message policy must be MessagePolicy")
        frozen_meta = _freeze_json_object(
            self.meta,
            label="message meta",
            max_bytes=MAX_MESSAGE_META_BYTES,
            max_depth=MAX_MESSAGE_META_DEPTH,
        )
        object.__setattr__(self, "parts", parts)
        object.__setattr__(self, "meta", frozen_meta)

    @property
    def has_images(self) -> bool:
        """@brief 是否包含图像 / Whether the message contains an image.

        @return 含任意 ImagePart 时为 True / True when it contains any ImagePart.
        """

        return any(isinstance(part, ImagePart) for part in self.parts)

    @property
    def text(self) -> str:
        """@brief 按顺序提取文本片段 / Extract text parts in order.

        @return 用换行连接的文本 / Text joined with newlines.
        """

        return "\n".join(part.text for part in self.parts if isinstance(part, TextPart))

    def without_images(self) -> CanonicalMessage:
        """@brief 生成文本降级副本 / Build a text-only fallback copy.

        @return 不含 ImagePart 的独立消息 / Independent message without ImagePart.
        @raise CanonicalMessageError 移除图像后没有可发送片段时抛出 / Raised when removing images leaves no sendable part.
        """

        parts = tuple(part for part in self.parts if not isinstance(part, ImagePart))
        if not parts:
            raise CanonicalMessageError("cannot remove every part from a message")
        return CanonicalMessage(self.role, parts, self.policy, self.meta)

    def to_json(self) -> JsonObject:
        """@brief 序列化 canonical V2 / Serialize canonical V2.

        @return 可持久化 JSON 对象 / Persistable JSON object.
        """

        return {
            "schema_version": CANONICAL_MESSAGE_VERSION,
            "role": self.role.value,
            "parts": [part.to_json() for part in self.parts],
            "policy": self.policy.to_json(),
            "meta": _thaw_json(self.meta),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> CanonicalMessage:
        """@brief 严格解析 canonical V2 JSON / Strictly parse canonical V2 JSON.

        @param value 候选 JSON 对象 / Candidate JSON object.
        @return 已验证、深冻结的 message / Validated, deeply frozen message.
        @raise CanonicalMessageError JSON 并非 V2 或违反角色/片段约束时抛出 /
            Raised when JSON is not V2 or violates role/part constraints.
        """

        allowed = {"schema_version", "role", "parts", "policy", "meta"}
        extra = set(value) - allowed
        if extra:
            raise CanonicalMessageError(
                f"canonical message has unsupported keys: {sorted(extra)!r}"
            )
        if value.get("schema_version") != CANONICAL_MESSAGE_VERSION:
            raise CanonicalMessageError(
                f"canonical message schema_version must be {CANONICAL_MESSAGE_VERSION}"
            )
        role_value = value.get("role")
        if not isinstance(role_value, str):
            raise CanonicalMessageError("canonical message role is invalid")
        try:
            role = MessageRole(role_value)
        except (TypeError, ValueError) as error:
            raise CanonicalMessageError("canonical message role is invalid") from error
        raw_parts = value.get("parts")
        if not isinstance(raw_parts, Sequence) or isinstance(raw_parts, (str, bytes)):
            raise CanonicalMessageError("canonical message parts must be an array")
        parts = tuple(_part_from_json(item) for item in raw_parts)
        return cls(
            role=role,
            parts=parts,
            policy=MessagePolicy.from_json(value.get("policy")),
            meta=_freeze_json_object(
                value.get("meta", {}),
                label="message meta",
                max_bytes=MAX_MESSAGE_META_BYTES,
                max_depth=MAX_MESSAGE_META_DEPTH,
            ),
        )


def text_message(
    role: MessageRole,
    text: str,
    *,
    include_in_context: bool = True,
    meta: Mapping[str, object] | None = None,
) -> CanonicalMessage:
    """@brief 构造仅文本 canonical 消息 / Construct a text-only canonical message.

    @param role 消息角色 / Message role.
    @param text 文本 / Text.
    @param include_in_context 是否进入未来 Context / Whether to enter future Context.
    @param meta 可选非 prompt 元数据 / Optional non-prompt metadata.
    @return canonical V2 消息 / Canonical V2 message.
    """

    return CanonicalMessage(
        role=role,
        parts=(TextPart(text),),
        policy=MessagePolicy(include_in_context),
        meta={} if meta is None else _freeze_json_object(
            meta,
            label="message meta",
            max_bytes=MAX_MESSAGE_META_BYTES,
            max_depth=MAX_MESSAGE_META_DEPTH,
        ),
    )


def _part_from_json(value: object) -> MessagePart:
    """@brief 解析一个 discriminated part / Parse one discriminated part.

    @param value 候选 JSON part / Candidate JSON part.
    @return 已验证 part / Validated part.
    @raise CanonicalMessageError part 格式非法时抛出 / Raised for an invalid part.
    """

    if not isinstance(value, Mapping):
        raise CanonicalMessageError("canonical message part must be an object")
    part_type = value.get("type")
    if part_type == "text":
        _ensure_keys(value, {"type", "text"}, label="text part")
        text = value.get("text")
        if not isinstance(text, str):
            raise CanonicalMessageError("text part text must be a string")
        return TextPart(text)
    if part_type == "image":
        _ensure_keys(value, {"type", "source"}, label="image part")
        return ImagePart(_image_source_from_json(value.get("source")))
    if part_type == "tool_call":
        _ensure_keys(
            value,
            {"type", "call_id", "name", "arguments"},
            label="tool_call part",
        )
        call_id = value.get("call_id")
        name = value.get("name")
        if not isinstance(call_id, str) or not isinstance(name, str):
            raise CanonicalMessageError("tool_call identifiers must be strings")
        return ToolCallPart(call_id, name, _freeze_json(value.get("arguments")))
    if part_type == "tool_result":
        _ensure_keys(
            value,
            {"type", "call_id", "name", "result", "is_error"},
            label="tool_result part",
        )
        call_id = value.get("call_id")
        name = value.get("name")
        is_error = value.get("is_error")
        if (
            not isinstance(call_id, str)
            or not isinstance(name, str)
            or type(is_error) is not bool
        ):
            raise CanonicalMessageError("tool_result fields have invalid types")
        return ToolResultPart(
            call_id,
            name,
            _freeze_json(value.get("result")),
            is_error=is_error,
        )
    raise CanonicalMessageError(f"unknown canonical message part type: {part_type!r}")


def _image_source_from_json(value: object) -> ImageSource:
    """@brief 解析 discriminated image source / Parse a discriminated image source.

    @param value 候选来源 JSON / Candidate source JSON.
    @return 已验证 image source / Validated image source.
    @raise CanonicalMessageError 来源形状非法时抛出 / Raised for an invalid source shape.
    """

    if not isinstance(value, Mapping):
        raise CanonicalMessageError("image source must be an object")
    kind = value.get("kind")
    if kind == "url":
        _ensure_keys(value, {"kind", "url"}, label="URL image source")
        url = value.get("url")
        if not isinstance(url, str):
            raise CanonicalMessageError("image URL must be a string")
        return UrlImageSource(url)
    if kind == "base64":
        _ensure_keys(
            value,
            {"kind", "media_type", "data"},
            label="Base64 image source",
        )
        media_type = value.get("media_type")
        data = value.get("data")
        if not isinstance(media_type, str) or not isinstance(data, str):
            raise CanonicalMessageError("inline image source fields must be strings")
        return Base64ImageSource(media_type, data)
    raise CanonicalMessageError(f"unknown image source kind: {kind!r}")


def _freeze_json_object(
    value: object,
    *,
    label: str,
    max_bytes: int,
    max_depth: int,
) -> FrozenJsonObject:
    """@brief 校验边界 JSON object 并冻结 / Validate and freeze a boundary JSON object.

    @param value 候选对象 / Candidate object.
    @param label 用于错误信息的字段名 / Field name for errors.
    @param max_bytes UTF-8 大小上限 / UTF-8 size limit.
    @param max_depth 最大嵌套层级 / Maximum nesting depth.
    @return 深度冻结对象 / Deeply frozen object.
    @raise CanonicalMessageError 对象、大小或深度非法时抛出 / Raised for invalid object, size, or depth.
    """

    if not isinstance(value, Mapping):
        raise CanonicalMessageError(f"{label} must be an object")
    frozen = _freeze_json(value, depth=0, max_depth=max_depth)
    if not isinstance(frozen, Mapping):
        raise CanonicalMessageError(f"{label} must be an object")
    encoded = json.dumps(
        _thaw_json(frozen),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > max_bytes:
        raise CanonicalMessageError(f"{label} exceeds {max_bytes} UTF-8 bytes")
    return frozen


def _freeze_json(
    value: object,
    *,
    depth: int = 0,
    max_depth: int = MAX_MESSAGE_META_DEPTH,
) -> FrozenJsonValue:
    """@brief 深冻结 JSON 值 / Deep-freeze a JSON value.

    @param value 候选 JSON 值 / Candidate JSON value.
    @param depth 当前层级 / Current nesting depth.
    @param max_depth 最大嵌套层级 / Maximum nesting depth.
    @return 深度冻结 JSON 值 / Deeply frozen JSON value.
    @raise CanonicalMessageError 不是 JSON 或包含非有限浮点时抛出 /
        Raised for non-JSON data or non-finite floats.
    """

    if depth > max_depth:
        raise CanonicalMessageError("JSON value exceeds the maximum nesting depth")
    if value is None or isinstance(value, (str, bool, int)):
        return cast(FrozenJsonScalar, value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalMessageError("JSON value cannot contain a non-finite float")
        return value
    if isinstance(value, Mapping):
        frozen_items: dict[str, FrozenJsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalMessageError("JSON object keys must be strings")
            frozen_items[key] = _freeze_json(
                item,
                depth=depth + 1,
                max_depth=max_depth,
            )
        return MappingProxyType(frozen_items)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(
            _freeze_json(item, depth=depth + 1, max_depth=max_depth)
            for item in value
        )
    raise CanonicalMessageError("value is not JSON-serializable")


def _thaw_json(value: FrozenJsonValue) -> JsonValue:
    """@brief 解冻 JSON 值为可持久化副本 / Thaw a JSON value into a persistable copy.

    @param value 深冻结 JSON 值 / Deeply frozen JSON value.
    @return 新建可变 JSON 值 / Fresh mutable JSON value.
    """

    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    return [_thaw_json(item) for item in value]


def _require_identifier(value: str, *, label: str) -> None:
    """@brief 校验有界非空标识符 / Validate a bounded non-blank identifier.

    @param value 候选标识符 / Candidate identifier.
    @param label 错误字段标签 / Error field label.
    @return None / None.
    """

    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise CanonicalMessageError(f"{label} must contain 1-512 characters")


def _ensure_keys(
    value: Mapping[str, object],
    expected: set[str],
    *,
    label: str,
) -> None:
    """@brief 拒绝未知或缺失的 discriminated 字段 / Reject unknown or missing discriminated fields.

    @param value 待检验对象 / Object to inspect.
    @param expected 精确字段集合 / Exact field set.
    @param label 错误对象标签 / Error object label.
    @return None / None.
    """

    actual = set(value)
    if actual != expected:
        raise CanonicalMessageError(
            f"{label} keys must be {sorted(expected)!r}, got {sorted(actual)!r}"
        )


_ALL_PART_TYPES = (TextPart, ImagePart, ToolCallPart, ToolResultPart)
"""@brief 所有允许的 part 运行时类型 / All permitted part runtime types."""

_PARTS_BY_ROLE: Mapping[MessageRole, tuple[type[MessagePart], ...]] = MappingProxyType(
    {
        MessageRole.SYSTEM: (TextPart, ImagePart),
        MessageRole.USER: (TextPart, ImagePart),
        MessageRole.ASSISTANT: (TextPart, ToolCallPart),
        MessageRole.TOOL: (ToolResultPart,),
    }
)
"""@brief role 到允许 part 类型的闭集映射 / Closed mapping from role to permitted part types."""


__all__ = [
    "Base64ImageSource",
    "CANONICAL_MESSAGE_VERSION",
    "CanonicalMessage",
    "CanonicalMessageError",
    "FrozenJsonObject",
    "FrozenJsonValue",
    "ImagePart",
    "ImageSource",
    "MAX_MESSAGE_META_BYTES",
    "MAX_MESSAGE_META_DEPTH",
    "MessagePart",
    "MessagePolicy",
    "TextPart",
    "ToolCallPart",
    "ToolResultPart",
    "UrlImageSource",
    "text_message",
]
