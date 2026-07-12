"""@brief Dashboard 组合根配置 / Dashboard composition-root configuration."""

from __future__ import annotations

import configparser
import os
import stat
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.engine import URL


PROJECT_ROOT = Path(__file__).resolve().parents[2]
"""@brief 稳定项目根目录 / Stable project root."""
DEFAULT_CONFIG_DIR = PROJECT_ROOT / "var" / "psql"
"""@brief 默认 libpq 配置目录 / Default libpq configuration directory."""


def load_project_env() -> None:
    """@brief 加载项目 .env 且不覆盖环境 / Load project .env without overriding the environment.

    @return None / None.
    """

    load_dotenv(PROJECT_ROOT / ".env", override=False)


def service_database_url(config_dir: Path, service_name: str) -> str:
    """@brief 独立读取 Dashboard service 与 pgpass / Independently read the Dashboard service and pgpass.

    @param config_dir pg_service.conf 与 pgpass 目录 / Directory containing pg_service.conf and pgpass.
    @param service_name service section 名 / Service section name.
    @return asyncpg SQLAlchemy URL / Asyncpg SQLAlchemy URL.
    """

    parser = configparser.ConfigParser()
    service_path = config_dir / "pg_service.conf"
    parser.read(service_path, encoding="utf-8")
    if service_name not in parser:
        raise RuntimeError(f"service {service_name!r} not found in {service_path}")
    section = parser[service_name]
    host = section.get("host", "localhost")
    port = section.getint("port", 5432)
    database = section.get("dbname")
    user = section.get("user")
    if not database or not user:
        raise RuntimeError(f"service {service_name!r} must define dbname and user")
    password = _pgpass_password(
        config_dir / "pgpass",
        host=host,
        port=port,
        database=database,
        user=user,
    )
    if password is None:
        raise RuntimeError(f"no matching pgpass entry for service user {user!r}")
    url = URL.create(
        "postgresql+asyncpg",
        username=user,
        password=password or None,
        host=host,
        port=port,
        database=database,
    )
    return url.render_as_string(hide_password=False)


def _pgpass_password(
    path: Path,
    *,
    host: str,
    port: int,
    database: str,
    user: str,
) -> str | None:
    """@brief 返回首个匹配 pgpass 密码 / Return the first matching pgpass password."""

    if not path.exists():
        return None
    if os.name != "nt":
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise RuntimeError(f"pgpass permissions must be 0600 or stricter: {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        fields = _split_pgpass(line)
        if len(fields) != 5:
            continue
        row_host, row_port, row_database, row_user, password = fields
        if (
            row_host in {host, "*"}
            and row_port in {str(port), "*"}
            and row_database in {database, "*"}
            and row_user in {user, "*"}
        ):
            return password
    return None


def _split_pgpass(line: str) -> tuple[str, ...]:
    """@brief 按 libpq 反斜杠规则拆分 pgpass / Split pgpass using libpq backslash rules."""

    fields: list[str] = []
    characters: list[str] = []
    escaping = False
    for character in line:
        if escaping:
            characters.append(character)
            escaping = False
        elif character == "\\":
            escaping = True
        elif character == ":" and len(fields) < 4:
            fields.append("".join(characters))
            characters = []
        else:
            characters.append(character)
    if escaping:
        characters.append("\\")
    fields.append("".join(characters))
    return tuple(fields)


__all__ = [
    "DEFAULT_CONFIG_DIR",
    "PROJECT_ROOT",
    "load_project_env",
    "service_database_url",
]
