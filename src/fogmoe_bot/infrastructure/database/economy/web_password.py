"""@brief PostgreSQL Web 密码适配器 / PostgreSQL web-password adapter."""

from datetime import datetime
from typing import cast

from fogmoe_bot.application.economy.web_password import (
    SetWebPassword,
    WebPasswordOperations,
    WebPasswordStatus,
)
from fogmoe_bot.infrastructure.database import connection as db_connection


class PostgresWebPasswordOperations(WebPasswordOperations):
    """@brief 持久化 Web 密码摘要和元数据 / Persist web-password digests and metadata."""

    async def web_password_status(self, user_id: int) -> WebPasswordStatus:
        """@brief 读取 Web 密码元数据 / Read web-password metadata.

        @param user_id 用户 ID / User ID.
        @return 密码状态 / Password status.
        """

        row = await db_connection.fetch_one(
            "SELECT created_at, updated_at FROM identity.web_password WHERE user_id = %s",
            (user_id,),
        )
        if row is None:
            return WebPasswordStatus(False)
        return WebPasswordStatus(
            True,
            created_at=cast(datetime, row[0]),
            updated_at=cast(datetime, row[1]),
        )

    async def set_web_password(self, command: SetWebPassword) -> bool:
        """@brief 写入 Web 密码摘要 / Write a web-password digest.

        @param command 密码命令 / Password command.
        @return 写入成功为 True / True when written.
        """

        await db_connection.execute(
            "INSERT INTO identity.web_password (user_id, password) VALUES (%s, %s) "
            "ON CONFLICT (user_id) DO UPDATE SET password = EXCLUDED.password, "
            "updated_at = CURRENT_TIMESTAMP",
            (command.user_id, command.password_hash),
        )
        return True
