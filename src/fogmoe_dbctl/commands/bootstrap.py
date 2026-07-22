"""@brief 初始化 PostgreSQL 子命令 / PostgreSQL bootstrap subcommand."""

from __future__ import annotations

import argparse
import subprocess
from typing import Any

from fogmoe_dbctl.config import DbctlSettings, reveal_secret
from fogmoe_dbctl.postgres import (
    RoleSecret,
    clean_postgres_environment,
    direct_psql_environment,
    dollar_quote,
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
        help=(
            "Use database.endpoint directly instead of local Unix-socket peer "
            "authentication through sudo -u database.bootstrap.system_user."
        ),
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

    statements: list[str] = ["BEGIN;"]
    role_literals: list[str] = []
    for secret in (application, maintenance, reporting):
        role_ident = quote_identifier(secret.role)
        role_literal = quote_literal(secret.role)
        password_literal = quote_literal(secret.password)
        role_literals.append(role_literal)
        body = f"""
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {role_literal}) THEN
    CREATE ROLE {role_ident}
      LOGIN PASSWORD {password_literal}
      NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
  ELSE
    ALTER ROLE {role_ident}
      WITH LOGIN PASSWORD {password_literal}
      NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
  END IF;
END;
""".strip()
        statements.append(f"DO {dollar_quote(body)};")
        statements.append(f"ALTER ROLE {role_ident} RESET ALL;")
    managed_roles = ", ".join(role_literals)
    membership_guard = f"""
BEGIN
  IF EXISTS (
    SELECT 1
    FROM pg_auth_members AS grant_edge
    JOIN pg_roles AS granted_role ON granted_role.oid = grant_edge.roleid
    JOIN pg_roles AS member_role ON member_role.oid = grant_edge.member
    WHERE member_role.rolname IN ({managed_roles})
       OR granted_role.rolname IN ({managed_roles})
  ) THEN
    RAISE EXCEPTION
      'managed FogMoe login roles must not inherit or SET ROLE to another role'
      USING ERRCODE = '42501';
  END IF;
END;
""".strip()
    statements.append(f"DO {dollar_quote(membership_guard)};")
    statements.append(
        f"ALTER ROLE {quote_identifier(reporting.role)} "
        "SET default_transaction_read_only = on;"
    )
    statements.append("COMMIT;")
    return "\n\n".join(statements) + "\n"


def build_role_boundary_preflight_sql(
    application_role: str,
    reporting_role: str,
) -> str:
    """@brief 对既有非 owner 角色执行无破坏的权限边界预检 / Preflight existing non-owner roles without destructive cleanup.

    @param application_role 应用角色名 / Application role name.
    @param reporting_role 报表角色名 / Reporting role name.
    @return 在目标数据库执行的只读 guard SQL / Read-only guard SQL executed in the target database.
    @note ``REASSIGN OWNED`` 和 ``DROP OWNED`` 会触及跨库 shared objects，因此 bootstrap
        不做隐式修复；遇到历史 ownership、default ACL 或列级 ACL 时立即失败。/
        ``REASSIGN OWNED`` and ``DROP OWNED`` can affect cross-database shared objects, so
        bootstrap never repairs them implicitly and fails on historical ownership, default
        ACLs, or column-level ACLs.
    """

    managed_roles = ", ".join(
        (quote_literal(application_role), quote_literal(reporting_role))
    )
    body = f"""
DECLARE
  target_database_oid OID;
BEGIN
  SELECT oid INTO STRICT target_database_oid
  FROM pg_database
  WHERE datname = CURRENT_DATABASE();

  IF EXISTS (
    SELECT 1
    FROM pg_database AS owned_database
    JOIN pg_roles AS owner_role ON owner_role.oid = owned_database.datdba
    WHERE owner_role.rolname IN ({managed_roles})
  ) OR EXISTS (
    SELECT 1
    FROM pg_tablespace AS owned_tablespace
    JOIN pg_roles AS owner_role ON owner_role.oid = owned_tablespace.spcowner
    WHERE owner_role.rolname IN ({managed_roles})
  ) OR EXISTS (
    SELECT 1
    FROM pg_shdepend AS dependency
    JOIN pg_roles AS owner_role ON owner_role.oid = dependency.refobjid
    WHERE dependency.dbid = target_database_oid
      AND dependency.deptype = 'o'
      AND owner_role.rolname IN ({managed_roles})
  ) THEN
    RAISE EXCEPTION
      'application/reporting roles own database objects; transfer ownership explicitly before bootstrap'
      USING ERRCODE = '42501';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM pg_default_acl AS default_acl
    JOIN pg_roles AS owner_role ON owner_role.oid = default_acl.defaclrole
    WHERE owner_role.rolname IN ({managed_roles})
  ) OR EXISTS (
    SELECT 1
    FROM pg_default_acl AS default_acl
    CROSS JOIN LATERAL aclexplode(default_acl.defaclacl) AS privilege
    JOIN pg_roles AS grantee_role ON grantee_role.oid = privilege.grantee
    WHERE grantee_role.rolname = {quote_literal(reporting_role)}
  ) OR EXISTS (
    SELECT 1
    FROM pg_attribute AS attribute
    CROSS JOIN LATERAL aclexplode(attribute.attacl) AS privilege
    JOIN pg_roles AS grantee_role ON grantee_role.oid = privilege.grantee
    WHERE attribute.attnum > 0
      AND NOT attribute.attisdropped
      AND grantee_role.rolname IN ({managed_roles})
  ) THEN
    RAISE EXCEPTION
      'application/reporting roles have default or column ACLs; revoke them explicitly before bootstrap'
      USING ERRCODE = '42501';
  END IF;
END;
""".strip()
    return (
        """
BEGIN;
""".lstrip()
        + f"DO {dollar_quote(body)};\n"
        + """
COMMIT;
""".lstrip()
    )


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
BEGIN;
REVOKE ALL PRIVILEGES ON DATABASE {database_ident} FROM PUBLIC;
REVOKE ALL PRIVILEGES ON DATABASE {database_ident} FROM {application_ident};
REVOKE ALL PRIVILEGES ON DATABASE {database_ident} FROM {reporting_ident};
ALTER ROLE {application_ident} IN DATABASE {database_ident} RESET ALL;
ALTER ROLE {maintenance_ident} IN DATABASE {database_ident} RESET ALL;
ALTER ROLE {reporting_ident} IN DATABASE {database_ident} RESET ALL;
ALTER ROLE {reporting_ident} IN DATABASE {database_ident}
  SET default_transaction_read_only = on;
GRANT CONNECT ON DATABASE {database_ident} TO {application_ident}, {maintenance_ident}, {reporting_ident};
GRANT CREATE, TEMPORARY ON DATABASE {database_ident} TO {maintenance_ident};
GRANT TEMPORARY ON DATABASE {database_ident} TO {application_ident};
COMMIT;
""".strip()
        + "\n"
    )


def psql_command(
    *,
    args: argparse.Namespace,
    settings: DbctlSettings,
    database: str,
) -> list[str]:
    """@brief 构造系统管理员 psql 命令 / Build the system-administrator psql command.

    @param args CLI 参数 / CLI arguments.
    @param settings dbctl 配置投影 / dbctl configuration projection.
    @param database 目标数据库名 / Target database name.
    @return 不含密码的 subprocess argv / Subprocess argv without a password.
    @note 默认路径不传 ``host`` 或 ``user``：sudo 选择 OS 身份，libpq 选择本地 Unix
        socket，并由 peer authentication 绑定二者。只有 ``--no-sudo`` 使用配置 endpoint。/
        The default path passes neither ``host`` nor ``user``: sudo selects the OS identity,
        libpq selects a local Unix socket, and peer authentication binds them. Only
        ``--no-sudo`` uses the configured endpoint.
    """

    command = ["psql", "--no-psqlrc", "--set", "ON_ERROR_STOP=1"]
    if args.no_sudo:
        return command
    return [
        "sudo",
        "-u",
        settings.bootstrap.system_user,
        "--",
        *command,
        "--port",
        str(settings.endpoint.port),
        "--dbname",
        database,
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
    if args.no_sudo:
        environment = direct_psql_environment(
            host=settings.endpoint.host,
            port=settings.endpoint.port,
            database=database,
            user=settings.bootstrap.system_user,
            password=None,
        )
    else:
        environment = clean_postgres_environment()
    subprocess.run(
        psql_command(args=args, settings=settings, database=database),
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
        database=settings.endpoint.name,
        sql=build_role_boundary_preflight_sql(
            application.role,
            reporting.role,
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
    print(
        "Run fogmoe-dbctl migrate before restarting application or Dashboard clients."
    )


__all__ = [
    "build_database_grant_sql",
    "build_database_sql",
    "build_role_boundary_preflight_sql",
    "build_role_sql",
    "configure_parser",
    "execute",
    "psql_command",
    "run_psql",
]
