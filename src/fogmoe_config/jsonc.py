"""@brief 严格 JSONC 解码边界 / Strict JSONC decoding boundary.

本模块只负责把 UTF-8 JSONC 文档解码为递归 JSON 值；各可执行程序继续拥有
自己的路径选择、语义投影、模型校验与公开配置异常。
"""

from __future__ import annotations

import json
from enum import Enum, auto
from math import isfinite
from pathlib import Path
from typing import Never, cast

type JSONValue = (
    None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]
)
"""@brief JSONC 可表示的递归值 / Recursive value representable by JSONC."""


class JsoncDecodeError(ValueError):
    """@brief JSONC 文档无效 / JSONC document is invalid."""


class _ScanState(Enum):
    """@brief JSONC 注释扫描状态 / JSONC comment-scanning state."""

    NORMAL = auto()
    """@brief 常规 JSON 语法位置 / Ordinary JSON syntax position."""

    STRING = auto()
    """@brief 双引号字符串内部 / Inside a double-quoted string."""

    ESCAPE = auto()
    """@brief 字符串转义符之后 / Immediately after a string escape."""

    LINE_COMMENT = auto()
    """@brief 行注释内部 / Inside a line comment."""

    BLOCK_COMMENT = auto()
    """@brief 块注释内部 / Inside a block comment."""


def load_jsonc(path: Path) -> dict[str, JSONValue]:
    """@brief 读取 UTF-8 严格 JSONC 文档 / Read a strict UTF-8 JSONC document.

    @param path 配置文件路径 / Configuration-file path.
    @return 严格 JSON 顶层对象 / Strict JSON top-level object.
    @raise JsoncDecodeError 文件无法读取或格式无效时抛出 /
        Raised when the file cannot be read or its format is invalid.
    """

    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise JsoncDecodeError(f"cannot read JSONC file {path}: {error}") from error
    try:
        return parse_jsonc(source)
    except JsoncDecodeError as error:
        raise JsoncDecodeError(f"invalid JSONC file {path}: {error}") from error


def parse_jsonc(source: str) -> dict[str, JSONValue]:
    """@brief 解析严格 JSON 加注释 / Parse strict JSON plus comments.

    @param source 已解码 JSONC 文本 / Decoded JSONC text.
    @return 严格 JSON 顶层对象 / Strict JSON top-level object.
    @raise JsoncDecodeError 注释或 JSON 语法无效时抛出 /
        Raised for invalid comments or JSON syntax.
    @note 仅允许 ``//`` 和 ``/* ... */`` 注释；不接受尾逗号等 JSON5 扩展。/
        Only ``//`` and ``/* ... */`` comments are accepted; JSON5 extensions such
        as trailing commas are rejected.
    """

    try:
        value = cast(
            JSONValue,
            json.loads(
                _strip_comments(source),
                object_pairs_hook=_object_without_duplicate_keys,
                parse_constant=_reject_non_json_number,
                parse_float=_parse_finite_json_float,
            ),
        )
    except JsoncDecodeError:
        raise
    except json.JSONDecodeError as error:
        raise JsoncDecodeError(
            f"invalid JSON at line {error.lineno}, column {error.colno}: {error.msg}"
        ) from error
    if not isinstance(value, dict):
        raise JsoncDecodeError("the top-level JSONC value must be an object")
    return value


def _strip_comments(source: str) -> str:
    """@brief 将注释替换为等位空白 / Replace comments with position-preserving whitespace.

    @param source 原始 JSONC 文本 / Raw JSONC text.
    @return 与原文行列对齐的严格 JSON 文本 / Strict JSON aligned with source positions.
    @raise JsoncDecodeError 块注释未闭合时抛出 /
        Raised when a block comment is unterminated.
    """

    characters = list(source)
    state = _ScanState.NORMAL
    block_start: int | None = None
    index = 0
    while index < len(characters):
        character = characters[index]
        following = characters[index + 1] if index + 1 < len(characters) else ""
        if state is _ScanState.STRING:
            if character == "\\":
                state = _ScanState.ESCAPE
            elif character == '"':
                state = _ScanState.NORMAL
            index += 1
            continue
        if state is _ScanState.ESCAPE:
            state = _ScanState.STRING
            index += 1
            continue
        if state is _ScanState.LINE_COMMENT:
            if character in "\r\n":
                state = _ScanState.NORMAL
            else:
                characters[index] = " "
            index += 1
            continue
        if state is _ScanState.BLOCK_COMMENT:
            if character == "*" and following == "/":
                characters[index] = " "
                characters[index + 1] = " "
                state = _ScanState.NORMAL
                index += 2
                continue
            if character not in "\r\n":
                characters[index] = " "
            index += 1
            continue
        if character == '"':
            state = _ScanState.STRING
            index += 1
            continue
        if character == "/" and following == "/":
            characters[index] = " "
            characters[index + 1] = " "
            state = _ScanState.LINE_COMMENT
            index += 2
            continue
        if character == "/" and following == "*":
            characters[index] = " "
            characters[index + 1] = " "
            block_start = index
            state = _ScanState.BLOCK_COMMENT
            index += 2
            continue
        index += 1
    if state is _ScanState.BLOCK_COMMENT:
        assert block_start is not None
        line = source.count("\n", 0, block_start) + 1
        column = block_start - source.rfind("\n", 0, block_start)
        raise JsoncDecodeError(
            f"unterminated block comment at line {line}, column {column}"
        )
    return "".join(characters)


def _object_without_duplicate_keys(
    pairs: list[tuple[str, JSONValue]],
) -> dict[str, JSONValue]:
    """@brief 构造无重复键的对象 / Build an object without duplicate keys.

    @param pairs JSON 解码器给出的成员对 / Member pairs emitted by the JSON decoder.
    @return 键唯一对象 / Object with unique keys.
    @raise JsoncDecodeError 存在重复键时抛出 / Raised when a duplicate key exists.
    """

    result: dict[str, JSONValue] = {}
    for key, value in pairs:
        if key in result:
            raise JsoncDecodeError(f"duplicate object key {key!r}")
        result[key] = value
    return result


def _reject_non_json_number(token: str) -> Never:
    """@brief 拒绝 NaN 与 Infinity / Reject NaN and Infinity.

    @param token 非标准数值 token / Non-standard numeric token.
    @raise JsoncDecodeError 始终抛出 / Always raised.
    """

    raise JsoncDecodeError(
        f"non-standard JSON numeric constant {token!r} is not allowed"
    )


def _parse_finite_json_float(token: str) -> float:
    """@brief 解析且限制有限 JSON 浮点数 / Parse and require a finite JSON float.

    @param token JSON 数字 token / JSON numeric token.
    @return 有限浮点数 / Finite floating-point value.
    @raise JsoncDecodeError 指数溢出为无穷大时抛出 /
        Raised when an exponent overflows to infinity.
    @note ``json.loads`` 会把合法词法形式 ``1e999`` 转成 ``inf``；配置数值不接受
        该非有限结果。/ ``json.loads`` turns lexically valid ``1e999`` into ``inf``;
        configuration values reject that non-finite result.
    """

    value = float(token)
    if not isfinite(value):
        raise JsoncDecodeError(
            f"non-finite JSON numeric value {token!r} is not allowed"
        )
    return value
