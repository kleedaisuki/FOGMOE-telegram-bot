"""@brief Alembic 数据库迁移子命令 / Alembic database migration subcommand."""

from __future__ import annotations

import argparse
from typing import Any

from fogmoe_dbctl.commands import access_sql, migration_execution
from fogmoe_dbctl.commands.access_policy import DEFAULT_ACCESS_POLICY
from fogmoe_dbctl.config import DbctlSettings


def configure_parser(subparsers: Any) -> None:
    """@brief 注册迁移子命令 / Register the migration subcommand.

    @param subparsers argparse 子命令集合 / argparse subparser collection.
    @return None / None.
    """

    parser = subparsers.add_parser(
        "migrate",
        help="Run Alembic migrations and grant runtime/reporting access.",
        description=(
            "Upgrade the configured database with the maintenance role, then grant "
            "the application role runtime privileges and the reporting role read-only "
            "privileges."
        ),
    )
    parser.add_argument("--revision", default="head")
    parser.add_argument(
        "--skip-grants",
        action="store_true",
        help="Run migrations without granting application or reporting privileges.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print operations without changing the database.",
    )
    parser.set_defaults(handler=execute)


def execute(args: argparse.Namespace, *, settings: DbctlSettings) -> None:
    """@brief 升级 schema 并收敛运行时与报表权限 / Upgrade the schema and converge runtime and reporting grants.

    @param args CLI 参数 / CLI arguments.
    @param settings CLI 组合根注入的已验证配置 / Validated settings injected by the CLI composition root.
    @return None / None.
    @raise ValueError 非 head 迁移试图应用 head 权限策略时抛出 /
        Raised when a non-head migration attempts to apply the head access policy.
    @note 命令层绝不读取配置文件；配置只在 CLI 根入口读取一次。/
        The command layer never reads a configuration file; configuration is read once at the CLI root.
    """

    if args.revision != "head" and not args.skip_grants:
        raise ValueError(
            "Non-head migrations require --skip-grants because the head allow-list "
            "may reference relations absent from the target revision"
        )

    migration_execution.run_alembic(
        settings=settings,
        revision=args.revision,
        dry_run=args.dry_run,
    )
    if args.skip_grants:
        return

    policy = DEFAULT_ACCESS_POLICY
    grant_sql = access_sql.build_runtime_grant_sql(
        database=settings.endpoint.name,
        policy=policy,
        application_role=settings.application.username,
        owner_role=settings.maintenance.username,
    ) + access_sql.build_reporting_grant_sql(
        database=settings.endpoint.name,
        policy=policy,
        reporting_role=settings.reporting.username,
        owner_role=settings.maintenance.username,
    )
    migration_execution.run_psql_grants(
        settings=settings,
        sql=grant_sql,
        dry_run=args.dry_run,
    )
