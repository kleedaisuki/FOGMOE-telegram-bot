"""@brief fogmoe_config 原生绑定类型 / Type declarations for the fogmoe_config native binding."""

from typing import Any


class NativeJsoncError(ValueError):
    """@brief C++ JSONC 解析错误 / C++ JSONC parsing error."""


def parse_jsonc(source: str, source_path: str | None = ...) -> Any:
    """@brief 解析 JSONC / Parse JSONC."""


def load_jsonc(path: str) -> Any:
    """@brief 读取并解析 JSONC 文件 / Load and parse a JSONC file."""
