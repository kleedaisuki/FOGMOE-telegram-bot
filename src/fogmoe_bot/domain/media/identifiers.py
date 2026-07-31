"""@brief 媒体上下文共享的稳定标识 / Stable identifiers shared by media capabilities."""

import re
from dataclasses import dataclass
from typing import NewType

_ARTIFACT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
"""@brief durable artifact 的不透明标识 grammar / Opaque durable-artifact identifier grammar."""


@dataclass(frozen=True, slots=True, order=True)
class ArtifactId:
    """@brief 持久化媒体制品标识 / Durable media-artifact identifier.

    @param value 32 位小写十六进制不透明值 / A 32-character lowercase hexadecimal opaque value.
    """

    value: str
    """@brief manifest、文件名与 outbox 共用的规范值 / Canonical value shared by manifests, filenames, and outbox."""

    def __post_init__(self) -> None:
        """@brief 校验制品标识 / Validate the artifact identifier.

        @return None / None.
        @raise TypeError 值不是字符串时抛出 / Raised when the value is not a string.
        @raise ValueError 值不符合固定 grammar 时抛出 / Raised when the value violates the fixed grammar.
        """

        if not isinstance(self.value, str):
            raise TypeError("artifact ID must be a string")
        if _ARTIFACT_ID_PATTERN.fullmatch(self.value) is None:
            raise ValueError("artifact ID must be 32 lowercase hexadecimal characters")

    def __str__(self) -> str:
        """@brief 返回持久化值 / Return the persistence value.

        @return 32 位小写十六进制值 / The 32-character lowercase hexadecimal value.
        """

        return self.value


UserId = NewType("UserId", int)
"""@brief Telegram 用户标识 / Telegram user identifier."""
