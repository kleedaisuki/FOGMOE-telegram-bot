#!/usr/bin/env python
"""Run Alembic migrations through the automation PostgreSQL role."""

from __future__ import annotations

import argparse
import configparser
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_DIR = PROJECT_ROOT / "var" / "psql"
DEFAULT_SCHEMAS = (
    "identity",
    "conversation",
    "assistant",
    "economy",
    "moderation",
    "crypto",
    "game",
)


@dataclass(frozen=True)
class ServiceConfig:
    """@brief psql service 配置 / psql service configuration.

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


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """@brief 解析命令行参数 / Parse command-line arguments.

    @param argv 命令行参数 / Command-line arguments.
    @return argparse 命名空间 / argparse namespace.
    """

    parser = argparse.ArgumentParser(
        description="Run alembic upgrade head using the generated migration psql service."
    )
    parser.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR)
    parser.add_argument("--service", default="fogmoe_automation")
    parser.add_argument("--bot-role", default="fogmoe-bot")
    parser.add_argument("--schemas", default=",".join(DEFAULT_SCHEMAS))
    parser.add_argument("--revision", default="head")
    parser.add_argument(
        "--skip-grants",
        action="store_true",
        help="Run migrations without granting runtime privileges to the bot role.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commands and SQL without changing the database.",
    )
    return parser.parse_args(argv)


def quote_ident(value: str) -> str:
    """@brief 引用 SQL 标识符 / Quote SQL identifier.

    @param value 标识符原文 / Raw identifier.
    @return 双引号引用后的标识符 / Double-quoted identifier.
    """

    return '"' + value.replace('"', '""') + '"'


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
    @return 五个 pgpass 字段 / Five pgpass fields.
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
) -> str:
    """@brief 从 pgpass 查找密码 / Find password from pgpass.

    @param pgpass_path pgpass 文件路径 / pgpass file path.
    @param host 数据库主机 / Database host.
    @param port 数据库端口 / Database port.
    @param database 数据库名 / Database name.
    @param user PostgreSQL 用户 / PostgreSQL user.
    @return 匹配密码 / Matching password.
    """

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
    raise RuntimeError(f"no matching pgpass entry for service user {user!r}")


def read_service(config_dir: Path, service_name: str) -> ServiceConfig:
    """@brief 读取 psql service / Read psql service.

    @param config_dir psql 配置目录 / psql config directory.
    @param service_name psql service 名 / psql service name.
    @return service 配置 / Service configuration.
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
    return ServiceConfig(
        host=host,
        port=port,
        database=database,
        user=user,
        password=password,
    )


def sqlalchemy_url(service: ServiceConfig) -> str:
    """@brief 构造 SQLAlchemy URL / Build SQLAlchemy URL.

    @param service service 配置 / Service configuration.
    @return asyncpg SQLAlchemy URL / asyncpg SQLAlchemy URL.
    """

    user = quote(service.user, safe="")
    password = quote(service.password, safe="")
    host = quote(service.host, safe="")
    database = quote(service.database, safe="")
    return f"postgresql+asyncpg://{user}:{password}@{host}:{service.port}/{database}"


def project_python() -> Path:
    """@brief 获取项目 Python 解释器 / Get project Python interpreter.

    @return venv 或当前 Python 路径 / venv or current Python path.
    """

    venv_python = PROJECT_ROOT / ".venv" / "bin" / "python"
    if venv_python.exists():
        return venv_python
    return Path(sys.executable)


def migration_env(
    service: ServiceConfig,
    config_dir: Path,
    service_name: str,
) -> dict[str, str]:
    """@brief 构造迁移环境变量 / Build migration environment.

    @param service service 配置 / Service configuration.
    @param config_dir psql 配置目录 / psql config directory.
    @param service_name psql service 名 / psql service name.
    @return 环境变量映射 / Environment mapping.
    """

    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    env["DATABASE_URL"] = sqlalchemy_url(service)
    env["PGSERVICEFILE"] = str(config_dir / "pg_service.conf")
    env["PGPASSFILE"] = str(config_dir / "pgpass")
    env["PGSERVICE"] = service_name
    return env


def run_alembic(
    *,
    service: ServiceConfig,
    config_dir: Path,
    service_name: str,
    revision: str,
    dry_run: bool,
) -> None:
    """@brief 执行 Alembic 迁移 / Run Alembic migration.

    @param service service 配置 / Service configuration.
    @param config_dir psql 配置目录 / psql config directory.
    @param service_name psql service 名 / psql service name.
    @param revision Alembic 目标 revision / Alembic target revision.
    @param dry_run 是否只打印 / Whether to print only.
    @return None / None.
    """

    command = [str(project_python()), "-m", "alembic", "upgrade", revision]
    if dry_run:
        print(" ".join(command))
        print("DATABASE_URL=postgresql+asyncpg://***:***@***")
        return
    subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=migration_env(service, config_dir, service_name),
        check=True,
    )


def build_runtime_grant_sql(
    *,
    schemas: list[str],
    bot_role: str,
    owner_role: str,
) -> str:
    """@brief 构造运行时授权 SQL / Build runtime grant SQL.

    @param schemas 应用 schema 列表 / Application schema list.
    @param bot_role bot 角色名 / Bot role name.
    @param owner_role 对象 owner 角色名 / Object owner role name.
    @return 可执行 SQL / Executable SQL.
    """

    bot_ident = quote_ident(bot_role)
    owner_ident = quote_ident(owner_role)
    statements: list[str] = []
    for schema in schemas:
        schema_ident = quote_ident(schema)
        statements.extend(
            [
                f"GRANT USAGE ON SCHEMA {schema_ident} TO {bot_ident};",
                (
                    "GRANT SELECT, INSERT, UPDATE, DELETE "
                    f"ON ALL TABLES IN SCHEMA {schema_ident} TO {bot_ident};"
                ),
                (
                    "GRANT USAGE, SELECT, UPDATE "
                    f"ON ALL SEQUENCES IN SCHEMA {schema_ident} TO {bot_ident};"
                ),
                (
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_ident} "
                    f"IN SCHEMA {schema_ident} "
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {bot_ident};"
                ),
                (
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_ident} "
                    f"IN SCHEMA {schema_ident} "
                    f"GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {bot_ident};"
                ),
            ]
        )
    return "\n".join(statements) + "\n"


def run_psql_grants(
    *,
    config_dir: Path,
    service_name: str,
    sql: str,
    dry_run: bool,
) -> None:
    """@brief 用 psql 执行授权 / Run grants through psql.

    @param config_dir psql 配置目录 / psql config directory.
    @param service_name psql service 名 / psql service name.
    @param sql SQL 文本 / SQL text.
    @param dry_run 是否只打印 / Whether to print only.
    @return None / None.
    """

    env = os.environ.copy()
    env["PGSERVICEFILE"] = str(config_dir / "pg_service.conf")
    env["PGPASSFILE"] = str(config_dir / "pgpass")
    command = [
        "psql",
        "--no-psqlrc",
        "--set",
        "ON_ERROR_STOP=1",
        f"service={service_name}",
    ]
    if dry_run:
        print(" ".join(command))
        print(sql)
        return
    subprocess.run(command, input=sql, text=True, env=env, check=True)


def schema_list(raw_schemas: str) -> list[str]:
    """@brief 解析 schema 列表 / Parse schema list.

    @param raw_schemas 逗号分隔 schema / Comma-separated schemas.
    @return 清洗后的 schema 列表 / Clean schema list.
    """

    return [schema.strip() for schema in raw_schemas.split(",") if schema.strip()]


def main(argv: Sequence[str] | None = None) -> None:
    """@brief 脚本入口 / Script entry point.

    @param argv 命令行参数 / Command-line arguments.
    @return None / None.
    """

    args = parse_args(argv)
    config_dir = args.config_dir.resolve()
    service = read_service(config_dir, args.service)
    run_alembic(
        service=service,
        config_dir=config_dir,
        service_name=args.service,
        revision=args.revision,
        dry_run=args.dry_run,
    )
    if args.skip_grants:
        return
    grant_sql = build_runtime_grant_sql(
        schemas=schema_list(args.schemas),
        bot_role=args.bot_role,
        owner_role=service.user,
    )
    run_psql_grants(
        config_dir=config_dir,
        service_name=args.service,
        sql=grant_sql,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main(sys.argv[1:])
