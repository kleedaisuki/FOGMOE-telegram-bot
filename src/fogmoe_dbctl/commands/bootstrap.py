"""@brief 初始化 PostgreSQL 子命令 / PostgreSQL bootstrap subcommand."""

from __future__ import annotations

import argparse
import subprocess
from typing import Any

from fogmoe_dbctl.config import DbctlSettings, reveal_secret
from fogmoe_dbctl.postgres import (
    RoleSecret,
    direct_psql_environment,
    quote_identifier,
    quote_literal,
)


def configure_parser(subparsers: Any) -> None:
    """@brief 注册初始化子命令 / Register the bootstrap subcommand.

    @param subparsers argparse 子命令集合 / argparse subparser collection.
    @return None / None.
    """

    parser = subparsers.add_parser(
        "bootstrap",
        help="Create the configured PostgreSQL roles and database.",
        description=(
            "Create the database plus the application, maintenance, and read-only "
            "reporting roles declared in config.json."
        ),
    )
    parser.add_argument(
        "--no-sudo",
        action="store_true",
        help="Run psql directly instead of sudo -u database.bootstrap.system_user.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print redacted SQL without changing PostgreSQL.",
    )
    parser.set_defaults(handler=execute)


def build_role_sql(
    application: RoleSecret,
    maintenance: RoleSecret,
    reporting: RoleSecret,
) -> str:
    """@brief 构造应用、维护与只读报表角色 SQL / Build application, maintenance, and read-only reporting role SQL.

    @param application 应用运行角色凭据 / Application runtime-role credential.
    @param maintenance 维护角色凭据 / Maintenance-role credential.
    @param reporting 只读报表角色凭据 / Read-only reporting-role credential.
    @return 可执行 SQL / Executable SQL.
    """

    statements: list[str] = []
    for secret in (application, maintenance, reporting):
        role_ident = quote_identifier(secret.role)
        role_literal = quote_literal(secret.role)
        password_literal = quote_literal(secret.password)
        statements.append(
            f"""
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {role_literal}) THEN
    CREATE ROLE {role_ident}
      LOGIN PASSWORD {password_literal}
      NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
  ELSE
    ALTER ROLE {role_ident}
      WITH LOGIN PASSWORD {password_literal}
      NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
  END IF;
END;
$$;
""".strip()
        )
    statements.append(
        f"ALTER ROLE {quote_identifier(reporting.role)} "
        "SET default_transaction_read_only = on;"
    )
    return "\n\n".join(statements) + "\n"


def build_database_sql(database: str, owner_role: str) -> str:
    """@brief 构造建库 SQL / Build database creation SQL.

    @param database 数据库名 / Database name.
    @param owner_role 数据库 owner 角色名 / Database owner role name.
    @return 可执行 SQL / Executable SQL.
    @note 无论数据库是新建还是已存在，owner 都收敛到维护角色，报表角色因此不能
        成为数据库 owner。/ Whether the database is new or pre-existing, ownership
        converges on the maintenance role so the reporting role cannot own it.
    """

    database_ident = quote_identifier(database)
    owner_ident = quote_identifier(owner_role)
    database_literal = quote_literal(database)
    create_database = quote_literal(
        f"CREATE DATABASE {database_ident} OWNER {owner_ident}"
    )
    return (
        "SELECT "
        f"{create_database} "
        f"WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = {database_literal})"
        "\\gexec\n"
        f"ALTER DATABASE {database_ident} OWNER TO {owner_ident};\n"
    )


def build_database_grant_sql(
    database: str,
    application_role: str,
    maintenance_role: str,
    reporting_role: str,
) -> str:
    """@brief 构造数据库级授权 SQL / Build database-level grant SQL.

    @param database 数据库名 / Database name.
    @param application_role 应用角色名 / Application role name.
    @param maintenance_role 维护角色名 / Maintenance role name.
    @param reporting_role 只读报表角色名 / Read-only reporting role name.
    @return 可执行 SQL / Executable SQL.
    """

    database_ident = quote_identifier(database)
    application_ident = quote_identifier(application_role)
    maintenance_ident = quote_identifier(maintenance_role)
    reporting_ident = quote_identifier(reporting_role)
    return (
        f"""
REVOKE ALL PRIVILEGES ON DATABASE {database_ident} FROM PUBLIC;
REVOKE ALL PRIVILEGES ON DATABASE {database_ident} FROM {reporting_ident};
GRANT CONNECT ON DATABASE {database_ident} TO {application_ident}, {maintenance_ident}, {reporting_ident};
GRANT CREATE, TEMPORARY ON DATABASE {database_ident} TO {maintenance_ident};
GRANT TEMPORARY ON DATABASE {database_ident} TO {application_ident};
""".strip()
        + "\n"
    )


def psql_command(*, args: argparse.Namespace, settings: DbctlSettings) -> list[str]:
    """@brief 构造系统管理员 psql 命令 / Build the system-administrator psql command.

    @param args CLI 参数 / CLI arguments.
    @param settings dbctl 配置投影 / dbctl configuration projection.
    @return 不含连接或密码参数的 subprocess argv / Subprocess argv without connection or password arguments.
    @note 连接信息仅通过子进程环境变量传递。/ Connection information is passed only through the child environment.
    """

    command = ["psql", "--no-psqlrc", "--set", "ON_ERROR_STOP=1"]
    if args.no_sudo:
        return command
    return [
        "sudo",
        "--preserve-env=PGHOST,PGPORT,PGDATABASE,PGUSER",
        "-u",
        settings.bootstrap.system_user,
        "--",
        *command,
    ]


def run_psql(
    *,
    args: argparse.Namespace,
    settings: DbctlSettings,
    database: str,
    sql: str,
) -> None:
    """@brief 执行系统管理员 SQL / Execute system-administrator SQL.

    @param args CLI 参数 / CLI arguments.
    @param settings dbctl 配置投影 / dbctl configuration projection.
    @param database 连接数据库名 / Database name to connect to.
    @param sql SQL 文本 / SQL text.
    @return None / None.
    """

    if args.dry_run:
        print(f"\n-- psql database={database}")
        print(sql)
        return
    environment = direct_psql_environment(
        host=settings.endpoint.host,
        port=settings.endpoint.port,
        database=database,
        user=settings.bootstrap.system_user,
        password=None,
    )
    subprocess.run(
        psql_command(args=args, settings=settings),
        input=sql,
        text=True,
        env=environment,
        check=True,
    )


def execute(args: argparse.Namespace, *, settings: DbctlSettings) -> None:
    """@brief 执行初始化用例 / Execute the bootstrap use case.

    @param args CLI 参数 / CLI arguments.
    @param settings CLI 组合根注入的已验证配置 / Validated settings injected by the CLI composition root.
    @return None / None.
    @note 命令层绝不读取配置文件；配置只在 CLI 根入口读取一次。/
        The command layer never reads a configuration file; configuration is read once at the CLI root.
    """

    if args.dry_run:
        application = RoleSecret(settings.application.username, "***")
        maintenance = RoleSecret(settings.maintenance.username, "***")
        reporting = RoleSecret(settings.reporting.username, "***")
    else:
        application = RoleSecret(
            settings.application.username,
            reveal_secret(
                settings.application.password,
                field_name="database.application.password",
            ),
        )
        maintenance = RoleSecret(
            settings.maintenance.username,
            reveal_secret(
                settings.maintenance.password,
                field_name="database.maintenance.password",
            ),
        )
        reporting = RoleSecret(
            settings.reporting.username,
            reveal_secret(
                settings.reporting.password,
                field_name="database.reporting.password",
            ),
        )
    run_psql(
        args=args,
        settings=settings,
        database="postgres",
        sql=build_role_sql(application, maintenance, reporting),
    )
    run_psql(
        args=args,
        settings=settings,
        database="postgres",
        sql=build_database_sql(
            settings.endpoint.name,
            maintenance.role,
        ),
    )
    run_psql(
        args=args,
        settings=settings,
        database="postgres",
        sql=build_database_grant_sql(
            settings.endpoint.name,
            application.role,
            maintenance.role,
            reporting.role,
        ),
    )
    if args.dry_run:
        print("Bootstrap plan completed; no PostgreSQL changes were made.")
        return
    print(f"Bootstrapped PostgreSQL database: {settings.endpoint.name}")


__all__ = [
    "build_database_grant_sql",
    "build_database_sql",
    "build_role_sql",
    "configure_parser",
    "execute",
    "psql_command",
    "run_psql",
]
