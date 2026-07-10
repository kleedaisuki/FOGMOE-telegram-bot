"""Alembic 数据库迁移子命令 / Alembic database migration subcommand."""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config

from fogmoe_dbctl.config import APPLICATION_SCHEMAS, DEFAULT_CONFIG_DIR, PROJECT_ROOT
from fogmoe_dbctl.postgres import (
    ServiceConfig,
    quote_identifier,
    read_service,
    service_sqlalchemy_url,
)


def configure_parser(subparsers: Any) -> None:
    """@brief 注册迁移子命令 / Register the migration subcommand.

    @param subparsers argparse 子命令集合 / argparse subparser collection.
    @return None / None.
    """

    parser = subparsers.add_parser(
        "migrate",
        aliases=["upgrade", "run-migrations-as-role"],
        help="Run Alembic migrations through the automation role and grant runtime access.",
        description=(
            "Upgrade the database with the configured automation psql service, "
            "then grant runtime privileges to the bot role."
        ),
    )
    parser.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR)
    parser.add_argument("--service", default="fogmoe_automation")
    parser.add_argument("--bot-role", default="fogmoe-bot")
    parser.add_argument("--schemas", default=",".join(APPLICATION_SCHEMAS))
    parser.add_argument("--revision", default="head")
    parser.add_argument(
        "--skip-grants",
        action="store_true",
        help="Run migrations without granting runtime privileges to the bot role.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print operations without changing the database.",
    )
    parser.set_defaults(handler=execute)


def run_alembic(
    *,
    service: ServiceConfig,
    revision: str,
    dry_run: bool,
) -> None:
    """@brief 通过程序化 API 执行 Alembic / Run Alembic through its programmatic API.

    @param service 自动化角色连接配置 / Automation-role connection configuration.
    @param revision Alembic 目标 revision / Alembic target revision.
    @param dry_run 是否只打印 / Whether to print only.
    @return None / None.
    """

    if dry_run:
        print(f"alembic upgrade {revision}")
        print("DATABASE_URL=postgresql+asyncpg://***:***@***")
        return

    alembic_config = Config(str(PROJECT_ROOT / "alembic.ini"))
    alembic_config.attributes["database_url"] = service_sqlalchemy_url(service)
    command.upgrade(alembic_config, revision)


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

    bot_ident = quote_identifier(bot_role)
    owner_ident = quote_identifier(owner_role)
    statements: list[str] = []
    for schema in schemas:
        schema_ident = quote_identifier(schema)
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
    """@brief 用 psql 执行运行时授权 / Run runtime grants through psql.

    @param config_dir psql 配置目录 / psql configuration directory.
    @param service_name psql service 名 / psql service name.
    @param sql SQL 文本 / SQL text.
    @param dry_run 是否只打印 / Whether to print only.
    @return None / None.
    """

    env = os.environ.copy()
    env["PGSERVICEFILE"] = str(config_dir / "pg_service.conf")
    env["PGPASSFILE"] = str(config_dir / "pgpass")
    command_line = [
        "psql",
        "--no-psqlrc",
        "--set",
        "ON_ERROR_STOP=1",
        f"service={service_name}",
    ]
    if dry_run:
        print(" ".join(command_line))
        print(sql)
        return
    subprocess.run(command_line, input=sql, text=True, env=env, check=True)


def parse_schemas(raw_schemas: str) -> list[str]:
    """@brief 解析 schema 列表 / Parse a schema list.

    @param raw_schemas 逗号分隔 schema / Comma-separated schemas.
    @return 清洗后的 schema 列表 / Normalized schema list.
    """

    return [schema.strip() for schema in raw_schemas.split(",") if schema.strip()]


def execute(args: argparse.Namespace) -> None:
    """@brief 执行迁移用例 / Execute the migration use case.

    @param args CLI 参数 / CLI arguments.
    @return None / None.
    """

    config_dir = args.config_dir.resolve()
    service = read_service(config_dir, args.service)
    run_alembic(service=service, revision=args.revision, dry_run=args.dry_run)
    if args.skip_grants:
        return
    grant_sql = build_runtime_grant_sql(
        schemas=parse_schemas(args.schemas),
        bot_role=args.bot_role,
        owner_role=service.user,
    )
    run_psql_grants(
        config_dir=config_dir,
        service_name=args.service,
        sql=grant_sql,
        dry_run=args.dry_run,
    )
