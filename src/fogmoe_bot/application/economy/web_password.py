"""@brief Web 密码应用模型与端口 / Web-password models and port."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class WebPasswordStatus:
    """@brief Web 密码状态 / Web-password status.

    @param exists 是否已设置 / Whether configured.
    @param created_at 创建时间 / Creation time.
    @param updated_at 更新时间 / Update time.
    """

    exists: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SetWebPassword:
    """@brief 设置 Web 密码命令 / Set-web-password command.

    @param user_id 用户 ID / User ID.
    @param password_hash SHA-256 摘要 / SHA-256 digest.
    """

    user_id: int
    password_hash: str


class WebPasswordOperations(Protocol):
    """@brief Web 密码持久化能力端口 / Web-password persistence capability port."""

    async def web_password_status(self, user_id: int) -> WebPasswordStatus:
        """@brief 读取 Web 密码状态 / Read web-password status.

        @param user_id 用户 ID / User ID.
        @return 密码状态 / Password status.
        """

        ...

    async def set_web_password(self, command: SetWebPassword) -> bool:
        """@brief 设置 Web 密码 / Set a web password.

        @param command 设置密码命令 / Set-password command.
        @return 是否覆盖旧密码 / Whether an existing password was replaced.
        """

        ...


def validate_web_password(password: str) -> str | None:
    """@brief 校验旧 Web 密码规则 / Validate legacy web-password rules.

    @param password 原始密码 / Raw password.
    @return 错误文本；合法为 None / Error text, or None when valid.
    """

    if not 6 <= len(password) <= 20:
        return "密码长度必须在6-20位之间"
    if re.fullmatch(r"[a-zA-Z0-9]+", password) is None:
        return "密码只能包含字母和数字"
    if (
        re.search(r"[a-zA-Z]", password) is None
        or re.search(r"[0-9]", password) is None
    ):
        return "密码必须包含至少一个字母和一个数字"
    return None


__all__ = [
    "SetWebPassword",
    "WebPasswordOperations",
    "WebPasswordStatus",
    "validate_web_password",
]
