"""PostgreSQL 配置与转义原语 / PostgreSQL configuration and escaping primitives."""

from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.engine import URL


@dataclass(frozen=True)
class RoleSecret:
    """@brief 数据库角色凭据 / Database role credentials.

    @param role PostgreSQL 角色名 / PostgreSQL role name.
    @param password PostgreSQL 登录密码 / PostgreSQL login password.
    """

    role: str
    password: str


@dataclass(frozen=True)
class ServiceConfig:
    """@brief psql service 连接配置 / psql service connection configuration.

    @param host 数据库主机 / Database host.
    @param port 数据库端口 / Database port.
    @param database 数据库名 / Database name.
    @param user PostgreSQL 角色名 / PostgreSQL role name.
    @param password PostgreSQL 密码 / PostgreSQL password.
    """

    host: str
    port: int
    database: str
    user: str
    password: str


def quote_identifier(value: str) -> str:
    """@brief 引用 PostgreSQL 标识符 / Quote a PostgreSQL identifier.

    @param value 标识符原文 / Raw identifier.
    @return 双引号引用后的标识符 / Double-quoted identifier.
    """

    return '"' + value.replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    """@brief 引用 PostgreSQL 字面量 / Quote a PostgreSQL literal.

    @param value 字面量原文 / Raw literal.
    @return 单引号引用后的字面量 / Single-quoted literal.
    """

    return "'" + value.replace("'", "''") + "'"


def escape_pgpass_field(value: str) -> str:
    """@brief 转义 pgpass 字段 / Escape a pgpass field.

    @param value 字段原文 / Raw field value.
    @return pgpass 安全字段 / pgpass-safe field.
    """

    return value.replace("\\", "\\\\").replace(":", "\\:")


def unescape_pgpass_field(value: str) -> str:
    """@brief 反转义 pgpass 字段 / Unescape a pgpass field.

    @param value pgpass 字段 / pgpass field.
    @return 反转义后的字段 / Unescaped field.
    """

    chars: list[str] = []
    escaping = False
    for char in value:
        if escaping:
            chars.append(char)
            escaping = False
            continue
        if char == "\\":
            escaping = True
            continue
        chars.append(char)
    if escaping:
        chars.append("\\")
    return "".join(chars)


def split_pgpass_line(line: str) -> list[str]:
    """@brief 拆分 pgpass 行 / Split a pgpass line.

    @param line pgpass 原始行 / Raw pgpass line.
    @return pgpass 字段 / pgpass fields.
    """

    fields: list[str] = []
    chars: list[str] = []
    escaping = False
    for char in line.rstrip("\n"):
        if escaping:
            chars.append("\\" + char)
            escaping = False
            continue
        if char == "\\":
            escaping = True
            continue
        if char == ":" and len(fields) < 4:
            fields.append(unescape_pgpass_field("".join(chars)))
            chars = []
            continue
        chars.append(char)
    if escaping:
        chars.append("\\")
    fields.append(unescape_pgpass_field("".join(chars)))
    return fields


def find_pgpass_password(
    pgpass_path: Path,
    *,
    host: str,
    port: int,
    database: str,
    user: str,
) -> str | None:
    """@brief 从 pgpass 查找首个匹配密码 / Find the first matching pgpass password.

    @param pgpass_path pgpass 文件路径 / pgpass file path.
    @param host 数据库主机 / Database host.
    @param port 数据库端口 / Database port.
    @param database 数据库名 / Database name.
    @param user PostgreSQL 用户 / PostgreSQL user.
    @return 匹配密码；不存在时返回 None / Matching password, or None when absent.
    """

    if not pgpass_path.exists():
        return None
    for line in pgpass_path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        fields = split_pgpass_line(line)
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


def read_service(config_dir: Path, service_name: str) -> ServiceConfig:
    """@brief 读取 psql service 及其密码 / Read a psql service and its password.

    @param config_dir psql 配置目录 / psql configuration directory.
    @param service_name psql service 名 / psql service name.
    @return service 连接配置 / Service connection configuration.
    """

    service_path = config_dir / "pg_service.conf"
    pgpass_path = config_dir / "pgpass"
    parser = configparser.ConfigParser()
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

    password = find_pgpass_password(
        pgpass_path,
        host=host,
        port=port,
        database=database,
        user=user,
    )
    if password is None:
        raise RuntimeError(f"no matching pgpass entry for service user {user!r}")
    return ServiceConfig(host, port, database, user, password)


def sqlalchemy_url(
    *,
    host: str,
    port: int,
    database: str,
    user: str,
    password: str,
) -> str:
    """@brief 构造正确转义的 SQLAlchemy URL / Build an escaped SQLAlchemy URL.

    @param host 数据库主机 / Database host.
    @param port 数据库端口 / Database port.
    @param database 数据库名 / Database name.
    @param user PostgreSQL 用户 / PostgreSQL user.
    @param password PostgreSQL 密码 / PostgreSQL password.
    @return asyncpg SQLAlchemy URL / asyncpg SQLAlchemy URL.
    """

    url = URL.create(
        "postgresql+asyncpg",
        username=user,
        password=password or None,
        host=host,
        port=port,
        database=database,
    )
    return url.render_as_string(hide_password=False)


def service_sqlalchemy_url(service: ServiceConfig) -> str:
    """@brief 将 service 配置转换为 SQLAlchemy URL / Convert a service to a SQLAlchemy URL.

    @param service service 连接配置 / Service connection configuration.
    @return asyncpg SQLAlchemy URL / asyncpg SQLAlchemy URL.
    """

    return sqlalchemy_url(
        host=service.host,
        port=service.port,
        database=service.database,
        user=service.user,
        password=service.password,
    )
