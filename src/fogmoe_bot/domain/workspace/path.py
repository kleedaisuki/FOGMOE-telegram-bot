"""@brief Workspace runtime 内的强类型路径 / Strongly typed paths inside a Workspace runtime."""

from __future__ import annotations

import re
from dataclasses import dataclass

_WORKSPACE_RELATIVE_PATH_PATTERN = re.compile(
    r"^(?:[A-Za-z0-9][A-Za-z0-9._-]*)(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*$|^\.$"
)
"""@brief 相对 ``/workspace`` 的目录语法 / Directory grammar relative to ``/workspace``."""


@dataclass(frozen=True, slots=True)
class WorkspaceRelativePath:
    """@brief 任务 Overlay 内的相对工作目录 / Relative working directory inside the task Overlay.

    @param value ``/workspace`` 之下、无父目录回退的相对目录 /
        Relative directory below ``/workspace`` without parent traversal.
    @note 该值对象表达 runtime 内路径，绝不表达 host 路径或 mount 选择。/
        This value object expresses a runtime-internal path, never a host path or mount selection.
    """

    value: str = "."
    """@brief 未加 ``/workspace`` 前缀的规范相对目录 /
    Canonical relative directory without the ``/workspace`` prefix.
    """

    def __post_init__(self) -> None:
        """@brief 验证相对工作目录 / Validate the relative working directory.

        @return None / None.
        @raise TypeError 目录不是字符串时抛出 / Raised when the directory is not a string.
        @raise ValueError 目录不是允许的相对路径时抛出 /
            Raised when the directory is not an allowed relative path.
        """

        if not isinstance(self.value, str):
            raise TypeError("Workspace working directory must be a string")
        if _WORKSPACE_RELATIVE_PATH_PATTERN.fullmatch(self.value) is None:
            raise ValueError("Workspace working directory must be a safe relative path")

    @property
    def runtime_path(self) -> str:
        """@brief 返回 runtime 内的绝对工作目录 / Return the absolute working directory inside the runtime.

        @return ``/workspace`` 或其安全子目录 / ``/workspace`` or one safe child directory.
        """

        return "/workspace" if self.value == "." else f"/workspace/{self.value}"


__all__ = ["WorkspaceRelativePath"]
