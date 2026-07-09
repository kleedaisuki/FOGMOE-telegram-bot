#!/usr/bin/env python
"""Bootstrap local PostgreSQL roles and psql service files."""

from __future__ import annotations

import argparse
import os
import secrets
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_DIR = PROJECT_ROOT / "var" / "psql"


@dataclass(frozen=True)
class RoleSecret:
    """@brief 数据库角色密码 / Database role password.

    @param role PostgreSQL 角色名 / PostgreSQL role name.
    @param password PostgreSQL 登录密码 / PostgreSQL login password.
    """

    role: str
    password: str


def parse_args() -> argparse.Namespace:
    """@brief 解析命令行参数 / Parse command-line arguments.

    @return argparse 命名空间 / argparse namespace.
    """

    parser = argparse.ArgumentParser(
        description=(
            "Create the fogmoe database, application role, migration role, "
            "and project-local psql service files."
        )
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
        help="Generate new passwords even when var/psql/pgpass already has them.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print SQL and file paths without changing PostgreSQL or files.",
    )
    return parser.parse_args()


def quote_ident(value: str) -> str:
    """@brief 引用 SQL 标识符 / Quote SQL identifier.

    @param value 标识符原文 / Raw identifier.
    @return 双引号引用后的标识符 / Double-quoted identifier.
    """

    return '"' + value.replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    """@brief 引用 SQL 字面量 / Quote SQL literal.

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


def read_existing_password(
    pgpass_path: Path,
    *,
    host: str,
    port: int,
    database: str,
    role: str,
) -> str | None:
    """@brief 读取已有 pgpass 密码 / Read an existing pgpass password.

    @param pgpass_path pgpass 文件路径 / pgpass file path.
    @param host 数据库主机 / Database host.
    @param port 数据库端口 / Database port.
    @param database 数据库名 / Database name.
    @param role PostgreSQL 角色名 / PostgreSQL role name.
    @return 匹配密码或 None / Matching password or None.
    """

    if not pgpass_path.exists():
        return None
    for line in pgpass_path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        fields = split_pgpass_line(line)
        if len(fields) != 5:
            continue
        row_host, row_port, row_database, row_role, password = fields
        if (
            row_host in {host, "*"}
            and row_port in {str(port), "*"}
            and row_database in {database, "*"}
            and row_role in {role, "*"}
        ):
            return password
    return None


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
    @return 可用于角色和配置的密码 / Password for role and config.
    """

    if explicit_password:
        return explicit_password
    if not rotate_passwords:
        existing = read_existing_password(
            pgpass_path,
            host=host,
            port=port,
            database=database,
            role=role,
        )
        if existing:
            return existing
    return secrets.token_urlsafe(32)


def build_role_sql(bot: RoleSecret, automation: RoleSecret) -> str:
    """@brief 构造角色 SQL / Build role SQL.

    @param bot bot 角色密码 / Bot role secret.
    @param automation 自动化迁移角色密码 / Automation migration role secret.
    @return 可执行 SQL / Executable SQL.
    """

    statements: list[str] = []
    for secret in (bot, automation):
        role_ident = quote_ident(secret.role)
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
        f"CREATE DATABASE {quote_ident(database)} OWNER {quote_ident(owner_role)}"
    )
    return (
        "SELECT "
        f"{create_database} "
        f"WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = {database_literal})"
        "\\gexec\n"
    )


def build_database_grant_sql(database: str, bot_role: str, automation_role: str) -> str:
    """@brief 构造数据库级授权 SQL / Build database-level grant SQL.

    @param database 数据库名 / Database name.
    @param bot_role bot 角色名 / Bot role name.
    @param automation_role 自动化迁移角色名 / Automation migration role name.
    @return 可执行 SQL / Executable SQL.
    """

    database_ident = quote_ident(database)
    bot_ident = quote_ident(bot_role)
    automation_ident = quote_ident(automation_role)
    return f"""
GRANT CONNECT ON DATABASE {database_ident} TO {bot_ident}, {automation_ident};
GRANT CREATE, TEMPORARY ON DATABASE {database_ident} TO {automation_ident};
GRANT TEMPORARY ON DATABASE {database_ident} TO {bot_ident};
""".strip() + "\n"


def psql_command(args: argparse.Namespace, database: str) -> list[str]:
    """@brief 构造 psql 命令 / Build psql command.

    @param args 命令行参数 / CLI arguments.
    @param database 连接数据库名 / Connection database name.
    @return subprocess 命令列表 / subprocess command list.
    """

    command = ["psql", "--no-psqlrc", "--set", "ON_ERROR_STOP=1", "--dbname", database]
    if args.no_sudo:
        return command
    return ["sudo", "-u", args.postgres_system_user, *command]


def run_psql(args: argparse.Namespace, database: str, sql: str) -> None:
    """@brief 执行 psql SQL / Execute SQL through psql.

    @param args 命令行参数 / CLI arguments.
    @param database 连接数据库名 / Connection database name.
    @param sql SQL 文本 / SQL text.
    @return None / None.
    """

    if args.dry_run:
        print(f"\n-- psql database={database}")
        print(sql)
        return
    subprocess.run(
        psql_command(args, database),
        input=sql,
        text=True,
        check=True,
    )


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
    """@brief 写入 psql service 和 pgpass / Write psql service and pgpass files.

    @param config_dir 配置目录 / Config directory.
    @param host 数据库主机 / Database host.
    @param port 数据库端口 / Database port.
    @param database 数据库名 / Database name.
    @param bot bot 角色密码 / Bot role secret.
    @param automation 自动化迁移角色密码 / Automation migration role secret.
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
    pgpass_lines = [
        ":".join(
            [
                escape_pgpass_field(host),
                str(port),
                escape_pgpass_field(database),
                escape_pgpass_field(bot.role),
                escape_pgpass_field(bot.password),
            ]
        ),
        ":".join(
            [
                escape_pgpass_field(host),
                str(port),
                escape_pgpass_field(database),
                escape_pgpass_field(automation.role),
                escape_pgpass_field(automation.password),
            ]
        ),
    ]
    pgpass_text = "\n".join(pgpass_lines) + "\n"

    if dry_run:
        print(f"\n-- would write {service_path}")
        print(service_text)
        print(f"-- would write {pgpass_path}")
        print(pgpass_text)
        return

    config_dir.mkdir(parents=True, exist_ok=True)
    service_path.write_text(service_text, encoding="utf-8")
    pgpass_path.write_text(pgpass_text, encoding="utf-8")
    pgpass_path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def main() -> None:
    """@brief 脚本入口 / Script entry point.

    @return None / None.
    """

    args = parse_args()
    config_dir = args.config_dir.resolve()
    pgpass_path = config_dir / "pgpass"
    bot = RoleSecret(
        role=args.bot_role,
        password=choose_password(
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
        role=args.automation_role,
        password=choose_password(
            args.automation_password,
            pgpass_path=pgpass_path,
            host=args.host,
            port=args.port,
            database=args.database,
            role=args.automation_role,
            rotate_passwords=args.rotate_passwords,
        ),
    )

    run_psql(args, "postgres", build_role_sql(bot, automation))
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
    print(f"pgpass file: {config_dir / 'pgpass'}")
    print(f"bot service: {args.bot_service}")
    print(f"automation service: {args.automation_service}")


if __name__ == "__main__":
    main()
