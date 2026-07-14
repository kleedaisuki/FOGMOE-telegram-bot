"""@brief Alembic 数据库迁移子命令 / Alembic database migration subcommand."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config

from fogmoe_dbctl.config import DbctlSettings, reveal_secret
from fogmoe_dbctl.postgres import (
    direct_psql_environment,
    quote_identifier,
    sqlalchemy_url,
)


_PROJECT_ROOT = Path(__file__).resolve().parents[3]
"""@brief 仓库根目录 / Repository root directory."""

_APPLICATION_SCHEMAS = (
    "identity",
    "conversation",
    "context_window",
    "retrieval",
    "user_profile",
    "assistant",
    "economy",
    "moderation",
    "crypto",
    "game",
    "media",
    "admin",
    "observability",
    "bank",
    "billing",
    "town",
    "chance",
    "personal_rpg",
)
"""@brief 迁移拥有且应用需要访问的 schema / Schemas owned by migrations and used by the application."""


def configure_parser(subparsers: Any) -> None:
    """@brief 注册迁移子命令 / Register the migration subcommand.

    @param subparsers argparse 子命令集合 / argparse subparser collection.
    @return None / None.
    """

    parser = subparsers.add_parser(
        "migrate",
        help="Run Alembic migrations and grant configured application access.",
        description=(
            "Upgrade the configured database with the maintenance role, then grant "
            "the configured application role runtime privileges."
        ),
    )
    parser.add_argument("--revision", default="head")
    parser.add_argument(
        "--skip-grants",
        action="store_true",
        help="Run migrations without granting runtime privileges to the application role.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print operations without changing the database.",
    )
    parser.set_defaults(handler=execute)


def maintenance_database_url(settings: DbctlSettings) -> str:
    """@brief 构造维护角色的 SQLAlchemy URL / Build the maintenance-role SQLAlchemy URL.

    @param settings dbctl 配置投影 / dbctl configuration projection.
    @return asyncpg SQLAlchemy URL / asyncpg SQLAlchemy URL.
    """

    return sqlalchemy_url(
        host=settings.endpoint.host,
        port=settings.endpoint.port,
        database=settings.endpoint.name,
        user=settings.maintenance.username,
        password=reveal_secret(
            settings.maintenance.password,
            field_name="database.maintenance.password",
        ),
    )


def run_alembic(
    *,
    settings: DbctlSettings,
    revision: str,
    dry_run: bool,
) -> None:
    """@brief 通过程序化 API 执行 Alembic / Run Alembic through its programmatic API.

    @param settings dbctl 配置投影 / dbctl configuration projection.
    @param revision Alembic 目标 revision / Alembic target revision.
    @param dry_run 是否只打印 / Whether to print only.
    @return None / None.
    @note 所有迁移输入显式写入 Alembic attributes，迁移环境不读取环境变量。/
        All migration inputs are injected into Alembic attributes; the migration environment reads no environment variables.
    """

    if dry_run:
        print(f"alembic upgrade {revision}")
        return

    alembic_config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    alembic_config.attributes["database_url"] = maintenance_database_url(settings)
    alembic_config.attributes["migration_schema"] = (
        settings.maintenance.migration_schema
    )
    alembic_config.attributes["admin_user_id"] = settings.administrator.user_id
    command.upgrade(alembic_config, revision)


def build_runtime_grant_sql(
    *,
    schemas: tuple[str, ...],
    application_role: str,
    owner_role: str,
) -> str:
    """@brief 构造运行时授权 SQL / Build runtime grant SQL.

    @param schemas 应用 schema 列表 / Application schema list.
    @param application_role 应用角色名 / Application role name.
    @param owner_role 对象 owner 角色名 / Object owner role name.
    @return 可执行 SQL / Executable SQL.
    """

    application_ident = quote_identifier(application_role)
    owner_ident = quote_identifier(owner_role)
    statements: list[str] = []
    for schema in schemas:
        schema_ident = quote_identifier(schema)
        statements.extend(
            [
                f"GRANT USAGE ON SCHEMA {schema_ident} TO {application_ident};",
                (
                    "GRANT SELECT, INSERT, UPDATE, DELETE "
                    f"ON ALL TABLES IN SCHEMA {schema_ident} TO {application_ident};"
                ),
                (
                    "GRANT USAGE, SELECT, UPDATE "
                    f"ON ALL SEQUENCES IN SCHEMA {schema_ident} TO {application_ident};"
                ),
                (
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_ident} "
                    f"IN SCHEMA {schema_ident} "
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {application_ident};"
                ),
                (
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_ident} "
                    f"IN SCHEMA {schema_ident} "
                    f"GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {application_ident};"
                ),
            ]
        )
    return "\n".join(statements) + "\n"


def run_psql_grants(
    *,
    settings: DbctlSettings,
    sql: str,
    dry_run: bool,
) -> None:
    """@brief 用 psql 执行运行时授权 / Run runtime grants through psql.

    @param settings dbctl 配置投影 / dbctl configuration projection.
    @param sql SQL 文本 / SQL text.
    @param dry_run 是否只打印 / Whether to print only.
    @return None / None.
    """

    command_line = ["psql", "--no-psqlrc", "--set", "ON_ERROR_STOP=1"]
    if dry_run:
        print("psql --set ON_ERROR_STOP=1")
        print(sql)
        return
    environment = direct_psql_environment(
        host=settings.endpoint.host,
        port=settings.endpoint.port,
        database=settings.endpoint.name,
        user=settings.maintenance.username,
        password=reveal_secret(
            settings.maintenance.password,
            field_name="database.maintenance.password",
        ),
    )
    subprocess.run(command_line, input=sql, text=True, env=environment, check=True)


def execute(args: argparse.Namespace, *, settings: DbctlSettings) -> None:
    """@brief 执行迁移用例 / Execute the migration use case.

    @param args CLI 参数 / CLI arguments.
    @param settings CLI 组合根注入的已验证配置 / Validated settings injected by the CLI composition root.
    @return None / None.
    @note 命令层绝不读取配置文件；配置只在 CLI 根入口读取一次。/
        The command layer never reads a configuration file; configuration is read once at the CLI root.
    """

    run_alembic(
        settings=settings,
        revision=args.revision,
        dry_run=args.dry_run,
    )
    if args.skip_grants:
        return
    run_psql_grants(
        settings=settings,
        sql=build_runtime_grant_sql(
            schemas=_APPLICATION_SCHEMAS,
            application_role=settings.application.username,
            owner_role=settings.maintenance.username,
        ),
        dry_run=args.dry_run,
    )


__all__ = [
    "build_runtime_grant_sql",
    "configure_parser",
    "execute",
    "maintenance_database_url",
    "run_alembic",
    "run_psql_grants",
]
