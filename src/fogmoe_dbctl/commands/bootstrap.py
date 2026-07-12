"""初始化 PostgreSQL 子命令 / PostgreSQL bootstrap subcommand."""

from __future__ import annotations

import argparse
import os
import secrets
import stat
import subprocess
from pathlib import Path
from typing import Any

from fogmoe_dbctl.config import DEFAULT_CONFIG_DIR
from fogmoe_dbctl.postgres import (
    RoleSecret,
    escape_pgpass_field,
    find_pgpass_password,
    quote_identifier,
    quote_literal,
)


def configure_parser(subparsers: Any) -> None:
    """@brief 注册初始化子命令 / Register the bootstrap subcommand.

    @param subparsers argparse 子命令集合 / argparse subparser collection.
    @return None / None.
    """

    parser = subparsers.add_parser(
        "bootstrap-postgres",
        aliases=["bootstrap"],
        help="Create PostgreSQL roles, database, and psql service files.",
        description=(
            "Create the FogMoe database, application role, migration role, "
            "and project-local psql service files."
        ),
    )
    parser.add_argument("--database", default="fogmoe")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--bot-role", default="fogmoe-bot")
    parser.add_argument("--automation-role", default="fogmoe_automation")
    parser.add_argument("--bot-service", default="fogmoe_bot")
    parser.add_argument("--automation-service", default="fogmoe_automation")
    parser.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR)
    parser.add_argument("--postgres-system-user", default="postgres")
    parser.add_argument(
        "--no-sudo",
        action="store_true",
        help="Run psql directly instead of sudo -u postgres psql.",
    )
    parser.add_argument(
        "--bot-password",
        default=os.environ.get("FOGMOE_BOT_DB_PASSWORD"),
        help="Password for the bot role. Defaults to env or a generated value.",
    )
    parser.add_argument(
        "--automation-password",
        default=(
            os.environ.get("FOGMOE_AUTOMATION_DB_PASSWORD")
            or os.environ.get("FOGMOE_MIGRATOR_DB_PASSWORD")
        ),
        help="Password for the migration role. Defaults to env or a generated value.",
    )
    parser.add_argument(
        "--rotate-passwords",
        action="store_true",
        help="Generate new passwords even when pgpass already contains them.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print SQL and file paths without changing PostgreSQL or files.",
    )
    parser.set_defaults(handler=execute)


def choose_password(
    explicit_password: str | None,
    *,
    pgpass_path: Path,
    host: str,
    port: int,
    database: str,
    role: str,
    rotate_passwords: bool,
) -> str:
    """@brief 选择角色密码 / Choose a role password.

    @param explicit_password 命令行或环境变量密码 / CLI or environment password.
    @param pgpass_path pgpass 文件路径 / pgpass file path.
    @param host 数据库主机 / Database host.
    @param port 数据库端口 / Database port.
    @param database 数据库名 / Database name.
    @param role PostgreSQL 角色名 / PostgreSQL role name.
    @param rotate_passwords 是否强制轮换 / Whether to rotate forcibly.
    @return 可用于角色和配置的密码 / Password for the role and configuration.
    """

    if explicit_password:
        return explicit_password
    if not rotate_passwords:
        existing = find_pgpass_password(
            pgpass_path,
            host=host,
            port=port,
            database=database,
            user=role,
        )
        if existing:
            return existing
    return secrets.token_urlsafe(32)


def build_role_sql(bot: RoleSecret, automation: RoleSecret) -> str:
    """@brief 构造角色 SQL / Build role SQL.

    @param bot bot 角色密码 / Bot role secret.
    @param automation 自动化迁移角色密码 / Automation role secret.
    @return 可执行 SQL / Executable SQL.
    """

    statements: list[str] = []
    for secret in (bot, automation):
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
      NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
  ELSE
    ALTER ROLE {role_ident}
      WITH LOGIN PASSWORD {password_literal}
      NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
  END IF;
END;
$$;
""".strip()
        )
    return "\n\n".join(statements) + "\n"


def build_database_sql(database: str, owner_role: str) -> str:
    """@brief 构造建库 SQL / Build database creation SQL.

    @param database 数据库名 / Database name.
    @param owner_role 数据库 owner 角色名 / Database owner role name.
    @return 可执行 SQL / Executable SQL.
    """

    database_literal = quote_literal(database)
    create_database = quote_literal(
        f"CREATE DATABASE {quote_identifier(database)} "
        f"OWNER {quote_identifier(owner_role)}"
    )
    return (
        "SELECT "
        f"{create_database} "
        f"WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = {database_literal})"
        "\\gexec\n"
    )


def build_database_grant_sql(
    database: str,
    bot_role: str,
    automation_role: str,
) -> str:
    """@brief 构造数据库级授权 SQL / Build database-level grant SQL.

    @param database 数据库名 / Database name.
    @param bot_role bot 角色名 / Bot role name.
    @param automation_role 自动化迁移角色名 / Automation role name.
    @return 可执行 SQL / Executable SQL.
    """

    database_ident = quote_identifier(database)
    bot_ident = quote_identifier(bot_role)
    automation_ident = quote_identifier(automation_role)
    return (
        f"""
GRANT CONNECT ON DATABASE {database_ident} TO {bot_ident}, {automation_ident};
GRANT CREATE, TEMPORARY ON DATABASE {database_ident} TO {automation_ident};
GRANT TEMPORARY ON DATABASE {database_ident} TO {bot_ident};
""".strip()
        + "\n"
    )


def psql_command(args: argparse.Namespace, database: str) -> list[str]:
    """@brief 构造管理员 psql 命令 / Build the administrator psql command.

    @param args CLI 参数 / CLI arguments.
    @param database 连接数据库名 / Connection database name.
    @return subprocess 命令 / subprocess command.
    """

    command = ["psql", "--no-psqlrc", "--set", "ON_ERROR_STOP=1", "--dbname", database]
    if args.no_sudo:
        return command
    return ["sudo", "-u", args.postgres_system_user, *command]


def run_psql(args: argparse.Namespace, database: str, sql: str) -> None:
    """@brief 执行管理员 SQL / Execute administrator SQL.

    @param args CLI 参数 / CLI arguments.
    @param database 连接数据库名 / Connection database name.
    @param sql SQL 文本 / SQL text.
    @return None / None.
    """

    if args.dry_run:
        print(f"\n-- psql database={database}")
        print(sql)
        return
    subprocess.run(psql_command(args, database), input=sql, text=True, check=True)


def write_psql_config(
    *,
    config_dir: Path,
    host: str,
    port: int,
    database: str,
    bot: RoleSecret,
    automation: RoleSecret,
    bot_service: str,
    automation_service: str,
    dry_run: bool,
) -> None:
    """@brief 写入项目 psql 配置 / Write project psql configuration.

    @param config_dir psql 配置目录 / psql configuration directory.
    @param host 数据库主机 / Database host.
    @param port 数据库端口 / Database port.
    @param database 数据库名 / Database name.
    @param bot bot 角色密码 / Bot role secret.
    @param automation 自动化角色密码 / Automation role secret.
    @param bot_service bot psql service 名 / Bot psql service name.
    @param automation_service 自动化 psql service 名 / Automation psql service name.
    @param dry_run 是否只打印 / Whether to print only.
    @return None / None.
    """

    service_path = config_dir / "pg_service.conf"
    pgpass_path = config_dir / "pgpass"
    service_text = f"""[{bot_service}]
host={host}
port={port}
dbname={database}
user={bot.role}

[{automation_service}]
host={host}
port={port}
dbname={database}
user={automation.role}
"""
    pgpass_text = (
        "\n".join(
            ":".join(
                escape_pgpass_field(value)
                for value in (host, str(port), database, secret.role, secret.password)
            )
            for secret in (bot, automation)
        )
        + "\n"
    )

    if dry_run:
        pgpass_preview = (
            "\n".join(
                ":".join(
                    escape_pgpass_field(value)
                    for value in (host, str(port), database, secret.role, "***")
                )
                for secret in (bot, automation)
            )
            + "\n"
        )
        print(f"\n-- would write {service_path}")
        print(service_text)
        print(f"-- would write {pgpass_path}")
        print(pgpass_preview)
        return

    config_dir.mkdir(parents=True, exist_ok=True)
    service_path.write_text(service_text, encoding="utf-8")
    pgpass_path.write_text(pgpass_text, encoding="utf-8")
    pgpass_path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def execute(args: argparse.Namespace) -> None:
    """@brief 执行初始化用例 / Execute the bootstrap use case.

    @param args CLI 参数 / CLI arguments.
    @return None / None.
    """

    config_dir = args.config_dir.resolve()
    pgpass_path = config_dir / "pgpass"
    bot = RoleSecret(
        args.bot_role,
        choose_password(
            args.bot_password,
            pgpass_path=pgpass_path,
            host=args.host,
            port=args.port,
            database=args.database,
            role=args.bot_role,
            rotate_passwords=args.rotate_passwords,
        ),
    )
    automation = RoleSecret(
        args.automation_role,
        choose_password(
            args.automation_password,
            pgpass_path=pgpass_path,
            host=args.host,
            port=args.port,
            database=args.database,
            role=args.automation_role,
            rotate_passwords=args.rotate_passwords,
        ),
    )

    displayed_bot = RoleSecret(bot.role, "***") if args.dry_run else bot
    displayed_automation = (
        RoleSecret(automation.role, "***") if args.dry_run else automation
    )
    run_psql(
        args,
        "postgres",
        build_role_sql(displayed_bot, displayed_automation),
    )
    run_psql(args, "postgres", build_database_sql(args.database, args.automation_role))
    run_psql(
        args,
        "postgres",
        build_database_grant_sql(args.database, args.bot_role, args.automation_role),
    )
    write_psql_config(
        config_dir=config_dir,
        host=args.host,
        port=args.port,
        database=args.database,
        bot=bot,
        automation=automation,
        bot_service=args.bot_service,
        automation_service=args.automation_service,
        dry_run=args.dry_run,
    )

    print(f"psql service file: {config_dir / 'pg_service.conf'}")
    print(f"pgpass file: {pgpass_path}")
    print(f"bot service: {args.bot_service}")
    print(f"automation service: {args.automation_service}")
