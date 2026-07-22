from __future__ import annotations

import inspect
from pathlib import Path
from typing import cast

import pytest
from alembic.config import Config

from fogmoe_dbctl import cli
from fogmoe_dbctl.commands import bootstrap, export_csv, migrate, shell
from fogmoe_dbctl.config import DbctlSettings, ReportingRoleSettings
from fogmoe_dbctl.postgres import RoleSecret, dollar_quote, sqlalchemy_url


def _settings() -> DbctlSettings:
    """@brief 构造测试用 dbctl 配置 / Build dbctl settings for tests.

    @return 含显式应用、维护与报表凭据的配置 / Settings with explicit application, maintenance, and reporting credentials.
    """

    return DbctlSettings.model_validate(
        {
            "endpoint": {"host": "db.example.test", "port": 5544, "name": "fogmoe"},
            "application": {"username": "fogmoe-app", "password": "app-secret"},
            "maintenance": {
                "username": "fogmoe-maintenance",
                "password": "maintenance-secret",
                "migration_schema": "infra",
            },
            "reporting": {
                "username": "fogmoe-dashboard",
                "password": "reporting-secret",
            },
            "bootstrap": {"system_user": "postgres"},
            "administrator": {"user_id": 42},
        }
    )


def test_reporting_settings_are_strict_and_have_a_dedicated_default() -> None:
    """@brief 报表设置严格拒绝强制转换与未知字段 / Reporting settings strictly reject coercion and unknown fields.

    @return None / None.
    """

    assert ReportingRoleSettings().username == "fogmoe-dashboard"
    with pytest.raises(ValueError):
        ReportingRoleSettings.model_validate({"username": 42})
    with pytest.raises(ValueError):
        ReportingRoleSettings.model_validate(
            {"username": "fogmoe-dashboard", "write_access": False}
        )


@pytest.mark.parametrize(
    ("application", "maintenance", "reporting", "system_user"),
    (
        ("shared", "shared", "reporting", "postgres"),
        ("application", "shared", "shared", "postgres"),
        ("shared", "maintenance", "shared", "postgres"),
        ("postgres", "maintenance", "reporting", "postgres"),
        ("application", "postgres", "reporting", "postgres"),
        ("application", "maintenance", "postgres", "postgres"),
    ),
)
def test_dbctl_login_roles_must_be_pairwise_distinct(
    application: str,
    maintenance: str,
    reporting: str,
    system_user: str,
) -> None:
    """@brief 配置模型拒绝受管角色或系统管理员身份复用 / The configuration model rejects managed-role or system-administrator identity reuse.

    @param application 应用角色名 / Application role name.
    @param maintenance 维护角色名 / Maintenance role name.
    @param reporting 报表角色名 / Reporting role name.
    @param system_user bootstrap 系统管理员角色 / Bootstrap system-administrator role.
    @return None / None.
    """

    with pytest.raises(ValueError, match="pairwise distinct"):
        DbctlSettings.model_validate(
            {
                "application": {"username": application},
                "maintenance": {"username": maintenance},
                "reporting": {"username": reporting},
                "bootstrap": {"system_user": system_user},
            }
        )


def test_cli_registers_commands() -> None:
    """@brief 验证统一 CLI 的规范命令 / Verify unified CLI canonical commands."""

    parser = cli.build_parser()

    assert parser.parse_args(["bootstrap"]).handler is bootstrap.execute
    assert parser.parse_args(["migrate"]).handler is migrate.execute
    assert parser.parse_args(["shell"]).handler is shell.execute
    assert (
        parser.parse_args(
            [
                "export-csv",
                "--table",
                "conversation.chat_records",
                "--output",
                "records.csv",
            ]
        ).handler
        is export_csv.execute
    )


def test_cli_reads_root_configuration_once_and_injects_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 根组合器只读取一次配置并注入处理函数 / The composition root reads configuration once and injects it into the handler."""

    settings = _settings()
    calls: list[object] = []

    def fake_reader(path: Path) -> DbctlSettings:
        calls.append(path)
        return settings

    def fake_handler(*_args: object, **kwargs: object) -> None:
        calls.append(kwargs["settings"])

    monkeypatch.setattr(cli, "read_dbctl_settings", fake_reader)
    monkeypatch.setattr(bootstrap, "execute", fake_handler)
    config_path = tmp_path / "config.json"

    cli.main(["--config", str(config_path), "bootstrap"])

    assert calls == [config_path, settings]


def test_commands_require_settings_injected_by_the_cli_root() -> None:
    """@brief 命令只接受根入口注入的配置 / Commands accept only settings injected by the CLI root.

    @return None / None.
    @note 这条边界防止子命令重新读取配置，从而产生多份不一致的配置快照。/
        This boundary prevents subcommands from re-reading configuration and creating inconsistent snapshots.
    """

    for command_module in (bootstrap, migrate, shell, export_csv):
        settings_parameter = inspect.signature(command_module.execute).parameters[
            "settings"
        ]
        assert settings_parameter.default is inspect.Parameter.empty
        assert not hasattr(command_module, "read_dbctl_settings")


def test_cli_without_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    """@brief 验证空命令打印帮助且不读取配置 / Verify an empty command prints help without reading configuration."""

    cli.main([])

    output = capsys.readouterr().out
    assert "bootstrap" in output
    assert "migrate" in output


def test_sqlalchemy_url_uses_escaping() -> None:
    """@brief 验证 URL 原语正确转义 / Verify the URL primitive escapes correctly."""

    url = sqlalchemy_url(
        host="localhost",
        port=5432,
        database="fog/moe",
        user="fogmoe@bot",
        password="p@ss/word",
    )

    assert url == (
        "postgresql+asyncpg://fogmoe%40bot:p%40ss%2Fword@localhost:5432/fog/moe"
    )


def test_migrate_injects_all_migration_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 验证迁移显式注入 URL、schema、管理员与应用角色 / Verify migrations receive an explicit URL, schema, administrator, and application role."""

    calls: list[tuple[object, str]] = []
    monkeypatch.setattr(
        migrate.command,
        "upgrade",
        lambda config, revision: calls.append((config, revision)),
    )

    migrate.run_alembic(settings=_settings(), revision="head", dry_run=False)

    raw_config, revision = calls[0]
    config = cast(Config, raw_config)
    assert revision == "head"
    assert Path(config.get_main_option("script_location")).is_absolute()
    assert config.attributes["database_url"] == (
        "postgresql+asyncpg://fogmoe-maintenance:maintenance-secret@"
        "db.example.test:5544/fogmoe"
    )
    assert config.attributes["migration_schema"] == "infra"
    assert config.attributes["admin_user_id"] == 42
    assert config.attributes["application_role"] == "fogmoe-app"


def test_runtime_grants_include_every_new_bounded_context_schema() -> None:
    """@brief 运行时角色必须获得 Scheduling、银行、账单、活动和 RPG schema 权限 / The runtime role must receive Scheduling, bank, billing, activity, and RPG schema privileges.

    @return None / None.
    """

    sql = migrate.build_runtime_grant_sql(
        database="fogmoe",
        schemas=migrate._APPLICATION_SCHEMAS,
        functions=migrate._APPLICATION_FUNCTIONS,
        application_role="fogmoe-app",
        owner_role="fogmoe-maintenance",
    )

    for schema in (
        "scheduling",
        "bank",
        "billing",
        "town",
        "chance",
        "personal_rpg",
    ):
        assert schema in migrate._APPLICATION_SCHEMAS
        assert f'GRANT USAGE ON SCHEMA "{schema}" TO "fogmoe-app";' in sql
        assert (
            f'REVOKE EXECUTE ON ALL ROUTINES IN SCHEMA "{schema}" FROM PUBLIC;' in sql
        )
        assert (
            f'REVOKE EXECUTE ON ALL ROUTINES IN SCHEMA "{schema}" '
            'FROM "fogmoe-app";' in sql
        )

    assert (
        'ALTER DEFAULT PRIVILEGES FOR ROLE "fogmoe-maintenance" '
        "REVOKE EXECUTE ON ROUTINES FROM PUBLIC;"
    ) in sql
    assert "GRANT EXECUTE ON ALL ROUTINES" not in sql
    assert "GRANT EXECUTE ON ROUTINES" not in sql
    for schema, function, signature in migrate._APPLICATION_FUNCTIONS:
        assert (
            f'GRANT EXECUTE ON FUNCTION "{schema}"."{function}"({signature}) '
            'TO "fogmoe-app";'
        ) in sql


def test_reporting_grants_are_read_only_for_explicit_observability_models() -> None:
    """@brief 报表授权仅包含显式观测读模型 / Reporting grants contain only explicit observability read models.

    @return None / None.
    """

    sql = migrate.build_reporting_grant_sql(
        database="fogmoe",
        owned_schemas=migrate._APPLICATION_SCHEMAS,
        relations=migrate._REPORTING_RELATIONS,
        reporting_role="fogmoe-dashboard",
        owner_role="fogmoe-maintenance",
    )

    assert 'GRANT CONNECT ON DATABASE "fogmoe" TO "fogmoe-dashboard";' in sql
    for schema in migrate._APPLICATION_SCHEMAS:
        assert (
            f'REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA "{schema}" '
            'FROM "fogmoe-dashboard";'
        ) in sql
        assert (
            f'REVOKE EXECUTE ON ALL ROUTINES IN SCHEMA "{schema}" FROM PUBLIC;' in sql
        )
        assert (
            f'REVOKE ALL PRIVILEGES ON ALL ROUTINES IN SCHEMA "{schema}" '
            'FROM "fogmoe-dashboard";'
        ) in sql

    for schema, relations in migrate._REPORTING_RELATIONS:
        assert f'GRANT USAGE ON SCHEMA "{schema}" TO "fogmoe-dashboard";' in sql
        qualified = ", ".join(f'"{schema}"."{relation}"' for relation in relations)
        assert f'GRANT SELECT ON TABLE {qualified} TO "fogmoe-dashboard";' in sql

    for sensitive_schema in (
        "identity",
        "conversation",
        "retrieval",
        "user_profile",
        "bank",
        "billing",
        "admin",
    ):
        assert (
            f'GRANT USAGE ON SCHEMA "{sensitive_schema}" TO "fogmoe-dashboard";'
            not in sql
        )

    assert (
        'ALTER DEFAULT PRIVILEGES FOR ROLE "fogmoe-maintenance" '
        "REVOKE EXECUTE ON ROUTINES FROM PUBLIC;"
    ) in sql
    assert (
        'ALTER DEFAULT PRIVILEGES FOR ROLE "fogmoe-maintenance" '
        'REVOKE ALL PRIVILEGES ON ROUTINES FROM "fogmoe-dashboard";'
    ) in sql

    for forbidden_grant in (
        "GRANT INSERT",
        "GRANT UPDATE",
        "GRANT DELETE",
        "GRANT TRUNCATE",
        "GRANT CREATE",
        "GRANT TEMPORARY",
        "GRANT USAGE, SELECT",
        "GRANT SELECT ON ALL SEQUENCES",
        "GRANT SELECT ON SEQUENCES",
        "GRANT SELECT ON ALL TABLES",
        "GRANT SELECT ON TABLES",
        "GRANT SELECT ON ROUTINES",
    ):
        assert forbidden_grant not in sql

    assert tuple(schema for schema, _relations in migrate._REPORTING_RELATIONS) == (
        "observability",
    )


def test_bootstrap_hardens_reporting_role_and_database_ownership() -> None:
    """@brief bootstrap 强制报表只读默认值与维护 owner / Bootstrap enforces the reporting read-only default and maintenance ownership.

    @return None / None.
    """

    role_sql = bootstrap.build_role_sql(
        RoleSecret("fogmoe-app", "app-secret"),
        RoleSecret("fogmoe-maintenance", "maintenance-secret"),
        RoleSecret("fogmoe-dashboard", "reporting-secret"),
    )
    assert "NOBYPASSRLS" in role_sql
    assert role_sql.count("NOBYPASSRLS") == 6
    assert role_sql.count("NOINHERIT") == 6
    assert role_sql.count("FROM pg_auth_members") == 1
    assert "managed FogMoe login roles must not inherit" in role_sql
    assert role_sql.startswith("BEGIN;")
    assert role_sql.endswith("COMMIT;\n")
    assert (
        'ALTER ROLE "fogmoe-dashboard" SET default_transaction_read_only = on;'
    ) in role_sql

    preflight_sql = bootstrap.build_role_boundary_preflight_sql(
        "fogmoe-app",
        "fogmoe-dashboard",
    )
    assert "FROM pg_database AS owned_database" in preflight_sql
    assert "FROM pg_tablespace AS owned_tablespace" in preflight_sql
    assert "FROM pg_shdepend AS dependency" in preflight_sql
    assert "FROM pg_default_acl AS default_acl" in preflight_sql
    assert "aclexplode(attribute.attacl)" in preflight_sql
    assert "REASSIGN OWNED" not in preflight_sql
    assert "DROP OWNED" not in preflight_sql
    assert preflight_sql.startswith("BEGIN;")
    assert preflight_sql.endswith("COMMIT;\n")

    database_sql = bootstrap.build_database_sql("fogmoe", "fogmoe-maintenance")
    assert 'ALTER DATABASE "fogmoe" OWNER TO "fogmoe-maintenance";' in database_sql

    grant_sql = bootstrap.build_database_grant_sql(
        "fogmoe",
        "fogmoe-app",
        "fogmoe-maintenance",
        "fogmoe-dashboard",
    )
    assert 'REVOKE ALL PRIVILEGES ON DATABASE "fogmoe" FROM PUBLIC;' in grant_sql
    for role in ("fogmoe-app", "fogmoe-maintenance", "fogmoe-dashboard"):
        assert f'ALTER ROLE "{role}" IN DATABASE "fogmoe" RESET ALL;' in grant_sql
    assert 'ALTER ROLE "fogmoe-dashboard" IN DATABASE "fogmoe" RESET ALL;' in grant_sql
    assert (
        'ALTER ROLE "fogmoe-dashboard" IN DATABASE "fogmoe"\n'
        "  SET default_transaction_read_only = on;" in grant_sql
    )
    assert (
        'GRANT CONNECT ON DATABASE "fogmoe" TO "fogmoe-app", '
        '"fogmoe-maintenance", "fogmoe-dashboard";'
    ) in grant_sql
    assert (
        'GRANT CREATE, TEMPORARY ON DATABASE "fogmoe" TO "fogmoe-maintenance";'
    ) in grant_sql
    assert 'GRANT TEMPORARY ON DATABASE "fogmoe" TO "fogmoe-app";' in grant_sql
    assert 'TO "fogmoe-dashboard";' not in "\n".join(
        line
        for line in grant_sql.splitlines()
        if "CREATE" in line or "TEMPORARY" in line
    )
    assert grant_sql.startswith("BEGIN;")
    assert grant_sql.endswith("COMMIT;\n")


def test_bootstrap_dollar_quotes_role_secrets_without_a_fixed_delimiter() -> None:
    """@brief bootstrap 安全容纳含 dollar delimiter 的合法凭据 / Bootstrap safely accepts credentials containing dollar delimiters.

    @return None / None.
    """

    sql = bootstrap.build_role_sql(
        RoleSecret("app$$role", "secret$fogmoe_0$$fogmoe_1$tail"),
        RoleSecret("maintenance", "secret$$tail"),
        RoleSecret("reporting", "secret$fogmoe_1$tail"),
    )

    assert "DO $$" not in sql
    assert 'CREATE ROLE "app$$role"' in sql
    assert "PASSWORD 'secret$fogmoe_0$$fogmoe_1$tail'" in sql
    assert "$fogmoe_2$" in sql


@pytest.mark.parametrize("prefix", ("1invalid", "hyphen-tag", "étiquette"))
def test_dollar_quote_rejects_invalid_postgresql_tags(prefix: str) -> None:
    """@brief dollar quote tag 遵守 PostgreSQL 标识符词法 / Dollar-quote tags obey PostgreSQL identifier lexical rules.

    @param prefix 待拒绝的 tag 前缀 / Tag prefix to reject.
    @return None / None.
    """

    with pytest.raises(ValueError, match="ASCII letter or underscore"):
        dollar_quote("body", prefix=prefix)


def test_migrate_applies_runtime_and_reporting_grants_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief migrate 在同一授权事务中应用运行时与只读边界 / Migrate applies runtime and read-only boundaries in one grant transaction.

    @param monkeypatch pytest 替换器 / pytest monkey patcher.
    @return None / None.
    """

    granted_sql: list[str] = []
    monkeypatch.setattr(migrate, "run_alembic", lambda **_kwargs: None)
    monkeypatch.setattr(
        migrate,
        "run_psql_grants",
        lambda *, settings, sql, dry_run: granted_sql.append(sql),
    )
    args = cli.build_parser().parse_args(["migrate"])

    args.handler(args, settings=_settings())

    assert len(granted_sql) == 1
    assert 'TO "fogmoe-app";' in granted_sql[0]
    assert 'TO "fogmoe-dashboard";' in granted_sql[0]
    reporting_sql = granted_sql[0].split(
        'REVOKE ALL PRIVILEGES ON DATABASE "fogmoe" FROM "fogmoe-dashboard";',
        maxsplit=1,
    )[1]
    assert "INSERT" not in reporting_sql
    assert "UPDATE" not in reporting_sql
    assert "DELETE" not in reporting_sql


def test_non_head_migration_requires_explicit_grant_skip() -> None:
    """@brief 非 head revision 不得套用 head ACL allow-list / A non-head revision cannot apply the head ACL allow-list.

    @return None / None.
    """

    args = cli.build_parser().parse_args(["migrate", "--revision", "0063"])
    with pytest.raises(ValueError, match="require --skip-grants"):
        args.handler(args, settings=_settings())


def test_psql_grants_use_one_explicit_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 授权收敛通过 psql 单事务执行 / Grant convergence executes in one psql transaction.

    @param monkeypatch pytest 替换器 / Pytest monkey patcher.
    @return None / None.
    """

    calls: list[tuple[list[str], str, bool]] = []

    def fake_run(
        command: list[str],
        *,
        input: str,
        text: bool,
        env: dict[str, str],
        check: bool,
    ) -> None:
        """@brief 记录授权子进程 / Record the grant subprocess.

        @param command 子进程参数 / Subprocess arguments.
        @param input 标准输入 SQL / Standard-input SQL.
        @param text 是否文本模式 / Whether text mode is enabled.
        @param env 显式连接环境 / Explicit connection environment.
        @param check 是否检查退出码 / Whether the exit status is checked.
        @return None / None.
        """

        assert env["PGUSER"] == "fogmoe-maintenance"
        calls.append((command, input, check))

    monkeypatch.setattr(migrate.subprocess, "run", fake_run)

    migrate.run_psql_grants(
        settings=_settings(),
        sql="SELECT 1;",
        dry_run=False,
    )

    command, sql, check = calls[0]
    assert command == [
        "psql",
        "--no-psqlrc",
        "--single-transaction",
        "--set",
        "ON_ERROR_STOP=1",
    ]
    assert sql == "SELECT 1;"
    assert check is True


def test_bootstrap_dry_run_redacts_passwords(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """@brief 验证预演不会泄露配置密码 / Verify a dry run does not expose configured passwords."""

    args = cli.build_parser().parse_args(["bootstrap", "--dry-run"])
    args.handler(args, settings=_settings())

    output = capsys.readouterr().out
    assert "app-secret" not in output
    assert "maintenance-secret" not in output
    assert "reporting-secret" not in output
    assert "***" in output
    assert "-- psql database=fogmoe" in output
    assert "FROM pg_shdepend AS dependency" in output
    assert "REASSIGN OWNED" not in output
    assert "DROP OWNED" not in output


def test_export_csv_accepts_only_schema_qualified_identifiers() -> None:
    """@brief 导出命令拒绝任意 SQL / Export command rejects arbitrary SQL."""

    assert export_csv.parse_table_name("conversation.chat_records") == (
        "conversation",
        "chat_records",
    )
    assert export_csv.build_copy_sql("conversation", "chat_records") == (
        'COPY (SELECT * FROM "conversation"."chat_records") '
        "TO STDOUT WITH (FORMAT CSV, HEADER TRUE, ENCODING 'UTF8');"
    )

    for invalid_name in (
        "chat_records",
        "conversation.chat-records",
        "conversation.chat_records;DROP TABLE users",
    ):
        with pytest.raises(ValueError):
            export_csv.parse_table_name(invalid_name)


def test_export_csv_writes_atomically_through_explicit_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief CSV 通过显式子进程环境原子写入 / CSV writes atomically through an explicit child environment."""

    output = tmp_path / "records.csv"
    output.write_text("old\n", encoding="utf-8")
    calls: list[tuple[list[str], dict[str, str], bool]] = []

    def fake_run(
        command: list[str],
        *,
        stdout: object,
        env: dict[str, str],
        check: bool,
    ) -> None:
        calls.append((command, env, check))
        stdout.write(b"id\n1\n")  # type: ignore[attr-defined]

    monkeypatch.setattr(export_csv.subprocess, "run", fake_run)
    monkeypatch.setenv("PGHOST", "ambient-host")
    monkeypatch.setenv("PGOPTIONS", "--search_path=wrong")
    export_csv.export_table(
        settings=_settings(),
        schema="conversation",
        table="chat_records",
        output_path=output,
        force=True,
    )

    assert output.read_text(encoding="utf-8") == "id\n1\n"
    command, environment, check = calls[0]
    assert command[0] == "psql"
    assert "--dbname" not in command
    assert "service=fogmoe_automation" not in command
    assert environment["PGHOST"] == "db.example.test"
    assert environment["PGPORT"] == "5544"
    assert environment["PGDATABASE"] == "fogmoe"
    assert environment["PGUSER"] == "fogmoe-maintenance"
    assert environment["PGPASSWORD"] == "maintenance-secret"
    assert "PGOPTIONS" not in environment
    assert check is True


def test_shell_uses_explicit_maintenance_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief shell 使用显式维护连接且密码不进入 argv / Shell uses the explicit maintenance connection and keeps its password out of argv."""

    calls: list[tuple[list[str], dict[str, str], bool]] = []

    class Result:
        """@brief 成功 subprocess 结果 / Successful subprocess result."""

        returncode = 0

    def fake_run(
        command: list[str],
        *,
        env: dict[str, str],
        check: bool,
    ) -> Result:
        calls.append((command, env, check))
        return Result()

    monkeypatch.setattr(shell.subprocess, "run", fake_run)
    args = cli.build_parser().parse_args(["shell", "--no-psqlrc"])
    args.handler(args, settings=_settings())

    command, environment, check = calls[0]
    assert command[0] == "psql"
    assert "--dbname" not in command
    assert "maintenance-secret" not in command
    assert environment["PGUSER"] == "fogmoe-maintenance"
    assert environment["PGPASSWORD"] == "maintenance-secret"
    assert environment["PGAPPNAME"] == "fogmoe-dbctl-shell"
    assert check is False


def test_shell_propagates_psql_exit_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """@brief shell 保留 psql 退出状态 / Shell preserves the psql exit status."""

    class Result:
        """@brief 失败 subprocess 结果 / Failed subprocess result."""

        returncode = 7

    monkeypatch.setattr(
        shell.subprocess,
        "run",
        lambda command, *, env, check: Result(),
    )
    args = cli.build_parser().parse_args(["shell"])

    with pytest.raises(SystemExit) as error:
        args.handler(args, settings=_settings())
    assert error.value.code == 7
