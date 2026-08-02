"""@brief 严格 JSONC 解码边界 / Strict JSONC decoding boundary.

本模块只负责把 UTF-8 JSONC 文档解码为递归 JSON 值；实际解析由无 Python 依赖的 C++
静态库完成。各可执行程序继续拥有自己的路径选择、语义投影、模型校验与公开配置异常。
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from . import _native  # pyright: ignore[reportMissingModuleSource]

type JSONValue = (
    None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]
)
"""@brief JSONC 可表示的递归值 / Recursive value representable by JSONC."""


class JsoncDecodeError(ValueError):
    """@brief JSONC 文档无效 / JSONC document is invalid."""


def parse_jsonc(
    source: str,
    *,
    source_path: Path | None = None,
) -> dict[str, JSONValue]:
    """@brief 解析严格 JSON 加注释 / Parse strict JSON plus comments.

    @param source 已解码 JSONC 文本 / Decoded JSONC text.
    @param source_path 可选虚拟源文件路径；用于解析 include / Optional virtual source-file
        path used to resolve includes.
    @return 严格 JSON 顶层对象 / Strict JSON top-level object.
    @raise JsoncDecodeError 注释、JSON 语法或 include 无效时抛出 /
        Raised for invalid comments, JSON syntax, or includes.
    @note 仅允许 ``//`` 和 ``/* ... */`` 注释；不接受尾逗号等 JSON5 扩展。字符串值精确
        匹配 ``"$<file_path>"`` 时，会以内联文件的 JSON 值替换。/
        Only ``//`` and ``/* ... */`` comments are accepted; JSON5 extensions such as trailing
        commas are rejected. An exact string value matching ``"$<file_path>"`` is replaced by
        the included file's JSON value.
    """

    try:
        value = _native.parse_jsonc(
            source,
            None if source_path is None else str(source_path),
        )
    except _native.NativeJsoncError as error:
        raise JsoncDecodeError(str(error)) from error
    if not isinstance(value, dict):
        # The native boundary enforces this invariant. Keep the check local so a future binding
        # cannot silently weaken the public Python contract.
        raise JsoncDecodeError("the top-level JSONC value must be an object")
    return cast(dict[str, JSONValue], value)


def load_jsonc(path: Path) -> dict[str, JSONValue]:
    """@brief 读取 UTF-8 严格 JSONC 文档 / Read a strict UTF-8 JSONC document.

    @param path 配置文件路径 / Configuration-file path.
    @return 展开 include 后的严格 JSON 顶层对象 / Strict JSON top-level object after include
        expansion.
    @raise JsoncDecodeError 文件无法读取、格式无效或 include 无效时抛出 /
        Raised when the file cannot be read, its format is invalid, or an include is invalid.
    """

    try:
        value = _native.load_jsonc(str(path))
    except _native.NativeJsoncError as error:
        message = str(error)
        if message.startswith("cannot read JSONC file "):
            raise JsoncDecodeError(message) from error
        raise JsoncDecodeError(f"invalid JSONC file {path}: {message}") from error
    if not isinstance(value, dict):
        raise JsoncDecodeError("the top-level JSONC value must be an object")
    return cast(dict[str, JSONValue], value)


__all__ = ["JSONValue", "JsoncDecodeError", "load_jsonc", "parse_jsonc"]
