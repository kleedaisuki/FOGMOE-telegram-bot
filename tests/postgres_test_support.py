"""@brief 真实 PostgreSQL 测试的显式配置适配 / Explicit configuration adapter for real-PostgreSQL tests."""

from __future__ import annotations

import re

from sqlalchemy.engine import make_url

from fogmoe_bot.config import BotDatabaseSettings
from fogmoe_bot.infrastructure.database import db
from fogmoe_dbctl.config import default_config_path, read_dbctl_settings

_DEDICATED_TEST_DATABASE_NAME = re.compile(
    r"(?:^test(?:[_-]|$)|(?:^|[_-])test(?:[_-]|$))",
    re.IGNORECASE,
)
"""@brief 显式测试库命名哨兵 / Explicit test-database naming sentinel."""


def _normalize_host(host: str) -> str:
    """@brief 规范化等价的本机端点 / Normalize equivalent loopback endpoints.

    @param host DSN 主机名 / DSN hostname.
    @return 用于安全比较的主机键 / Host key used for safety comparison.
    """

    normalized = host.casefold().rstrip(".")
    if normalized in {"localhost", "127.0.0.1", "::1"}:
        return "loopback"
    return normalized


def _configured_production_endpoint() -> tuple[str, int, str] | None:
    """@brief 读取当前部署的生产端点指纹 / Read the configured deployment endpoint fingerprint.

    @return ``(host, port, database)``；无部署配置时为 None /
        ``(host, port, database)``, or None when no deployment configuration exists.
    @note 仅读取端点，不取出或返回任何密钥 / Reads only endpoint data and never reveals or returns a secret.
    """

    path = default_config_path()
    if not path.is_file():
        return None
    endpoint = read_dbctl_settings(path).endpoint
    return (_normalize_host(endpoint.host), endpoint.port, endpoint.name.casefold())


def _require_dedicated_test_database(raw_url: str) -> None:
    """@brief 拒绝生产端点与非测试库名 / Reject production endpoints and non-test database names.

    @param raw_url 测试操作者提供的 PostgreSQL DSN / Operator-supplied PostgreSQL DSN.
    @return None / None.
    @raise ValueError DSN 指向当前部署或库名缺少 test 哨兵时抛出 /
        Raised when the DSN targets the configured deployment or lacks a test-name sentinel.
    """

    url = make_url(raw_url)
    if url.host is None or url.database is None:
        raise ValueError("test database URL must include host and database")
    candidate = (
        _normalize_host(url.host),
        url.port or 5432,
        url.database.casefold(),
    )
    if candidate == _configured_production_endpoint():
        raise ValueError(
            "real-PostgreSQL tests refuse the configured production database"
        )
    if _DEDICATED_TEST_DATABASE_NAME.search(url.database) is None:
        raise ValueError(
            "real-PostgreSQL tests require a dedicated database name containing "
            "a standalone 'test' segment"
        )


def database_settings_from_url(raw_url: str) -> BotDatabaseSettings:
    """@brief 将显式测试 DSN 映射为 Bot 数据库设置 / Map an explicit test DSN to Bot database settings.

    @param raw_url 仅由测试操作者提供的 PostgreSQL DSN / PostgreSQL DSN supplied only by the test operator.
    @return 已验证的 Bot 数据库设置 / Validated Bot database settings.
    @raise ValueError DSN 未提供 host、database 或 username 时抛出 /
        Raised when the DSN has no host, database, or username.
    @note 该 helper 仅位于测试层；生产配置始终来自根 ``config.json``。/
        This helper exists only in the test layer; production configuration always comes from root ``config.json``.
    """

    _require_dedicated_test_database(raw_url)
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
