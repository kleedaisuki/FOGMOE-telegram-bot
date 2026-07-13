"""@brief 真实 PostgreSQL 测试的显式配置适配 / Explicit configuration adapter for real-PostgreSQL tests."""

from __future__ import annotations

from sqlalchemy.engine import make_url

from fogmoe_bot.config import BotDatabaseSettings
from fogmoe_bot.infrastructure.database import db


def database_settings_from_url(raw_url: str) -> BotDatabaseSettings:
    """@brief 将显式测试 DSN 映射为 Bot 数据库设置 / Map an explicit test DSN to Bot database settings.

    @param raw_url 仅由测试操作者提供的 PostgreSQL DSN / PostgreSQL DSN supplied only by the test operator.
    @return 已验证的 Bot 数据库设置 / Validated Bot database settings.
    @raise ValueError DSN 未提供 host、database 或 username 时抛出 /
        Raised when the DSN has no host, database, or username.
    @note 该 helper 仅位于测试层；生产配置始终来自根 ``config.json``。/
        This helper exists only in the test layer; production configuration always comes from root ``config.json``.
    """

    url = make_url(raw_url)
    if url.host is None or url.database is None or url.username is None:
        raise ValueError("test database URL must include host, database, and username")
    return BotDatabaseSettings.model_validate(
        {
            "endpoint": {
                "host": url.host,
                "port": url.port or 5432,
                "name": url.database,
            },
            "application": {
                "username": url.username,
                "password": url.password,
            },
        }
    )


def configure_bot_database(raw_url: str) -> None:
    """@brief 将显式测试 DSN 注入 Bot 数据库边界 / Inject an explicit test DSN into the Bot database boundary.

    @param raw_url 仅由测试操作者提供的 PostgreSQL DSN / PostgreSQL DSN supplied only by the test operator.
    @return None / None.
    @note 该 helper 仅位于测试层；生产组合根总是从 ``config.json`` 注入设置。/
        This helper exists only in the test layer; the production composition root always injects settings from ``config.json``.
    """

    db.configure_database(database_settings_from_url(raw_url))


__all__ = ["configure_bot_database", "database_settings_from_url"]
