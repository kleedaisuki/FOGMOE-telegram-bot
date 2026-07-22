"""@brief Provider request metadata 的领域边界 / Domain boundary for provider request metadata."""

from __future__ import annotations

import json
from collections.abc import Mapping
from types import MappingProxyType

#: @brief 一次请求允许的 metadata 键数 / Maximum metadata keys in one request.
MAX_REQUEST_META_ITEMS = 16
#: @brief metadata key 的最大字符数 / Maximum metadata-key characters.
MAX_REQUEST_META_KEY_LENGTH = 64
#: @brief metadata value 的最大字符数 / Maximum metadata-value characters.
MAX_REQUEST_META_VALUE_LENGTH = 512
#: @brief 完整 metadata UTF-8 上限 / UTF-8 limit for the whole metadata object.
MAX_REQUEST_META_BYTES = 8 * 1024

type RequestMeta = Mapping[str, str]
"""@brief 显式可传给 provider 的受限字符串 metadata / Explicit bounded string metadata eligible for provider mapping."""


class RequestMetaError(ValueError):
    """@brief 请求 metadata 违反边界 / Request metadata violates its boundary."""


def normalize_request_meta(value: object) -> RequestMeta:
    """@brief 校验并冻结请求 metadata / Validate and freeze request metadata.

    @param value 候选 JSON object / Candidate JSON object.
    @return 深度不可变的字符串 mapping / Deeply immutable string mapping.
    @raise RequestMetaError 类型、大小或控制字符非法时抛出 /
        Raised for invalid type, size, or control characters.
    @note 该对象只在 route adapter 显式映射时进入 HTTP payload；它绝不是 ``**kwargs``
        逃生通道。/ It enters an HTTP payload only when a route adapter explicitly maps it;
        it is never a ``**kwargs`` escape hatch.
    """

    if not isinstance(value, Mapping):
        raise RequestMetaError("request meta must be an object")
    if len(value) > MAX_REQUEST_META_ITEMS:
        raise RequestMetaError(
            f"request meta cannot contain more than {MAX_REQUEST_META_ITEMS} items"
        )
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key or len(key) > MAX_REQUEST_META_KEY_LENGTH:
            raise RequestMetaError(
                "request meta keys must contain 1-64 characters"
            )
        if not isinstance(item, str) or len(item) > MAX_REQUEST_META_VALUE_LENGTH:
            raise RequestMetaError(
                "request meta values must be strings with at most 512 characters"
            )
        if any(character in key or character in item for character in "\r\n\x00"):
            raise RequestMetaError("request meta cannot contain CR, LF, or NUL")
        result[key] = item
    encoded = json.dumps(
        result,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_REQUEST_META_BYTES:
        raise RequestMetaError(
            f"request meta exceeds {MAX_REQUEST_META_BYTES} UTF-8 bytes"
        )
    return MappingProxyType(result)


def request_meta_to_json(value: RequestMeta) -> dict[str, str]:
    """@brief 复制冻结 metadata 供 JSON 序列化 / Copy frozen metadata for JSON serialization.

    @param value 已验证 metadata / Validated metadata.
    @return 独立的可变 JSON object / Independent mutable JSON object.
    """

    return dict(normalize_request_meta(value))


__all__ = [
    "MAX_REQUEST_META_BYTES",
    "MAX_REQUEST_META_ITEMS",
    "MAX_REQUEST_META_KEY_LENGTH",
    "MAX_REQUEST_META_VALUE_LENGTH",
    "RequestMeta",
    "RequestMetaError",
    "normalize_request_meta",
    "request_meta_to_json",
]
