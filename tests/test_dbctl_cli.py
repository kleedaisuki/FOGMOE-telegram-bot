from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from fogmoe_dbctl import cli
from fogmoe_dbctl.commands import bootstrap, export_csv, migrate, shell
from fogmoe_dbctl.config import DbctlSettings
from fogmoe_dbctl.postgres import sqlalchemy_url


def _settings() -> DbctlSettings:
    """@brief 构造测试用 dbctl 配置 / Build dbctl settings for tests.

    @return 含显式应用与维护凭据的配置 / Settings with explicit application and maintenance credentials.
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
            "bootstrap": {"system_user": "postgres"},
            "administrator": {"user_id": 42},
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
    """@brief 验证迁移显式注入 URL、schema 与管理员 ID / Verify migrations receive an explicit URL, schema, and administrator ID."""

    calls: list[tuple[object, str]] = []
    monkeypatch.setattr(
        migrate.command,
        "upgrade",
        lambda config, revision: calls.append((config, revision)),
    )

    migrate.run_alembic(settings=_settings(), revision="head", dry_run=False)

    config, revision = calls[0]
    assert revision == "head"
    assert Path(config.get_main_option("script_location")).is_absolute()
    assert config.attributes["database_url"] == (
        "postgresql+asyncpg://fogmoe-maintenance:maintenance-secret@"
        "db.example.test:5544/fogmoe"
    )
    assert config.attributes["migration_schema"] == "infra"
    assert config.attributes["admin_user_id"] == 42


def test_bootstrap_dry_run_redacts_passwords(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """@brief 验证预演不会泄露配置密码 / Verify a dry run does not expose configured passwords."""

    args = cli.build_parser().parse_args(["bootstrap", "--dry-run"])
    args.handler(args, settings=_settings())

    output = capsys.readouterr().out
    assert "app-secret" not in output
    assert "maintenance-secret" not in output
    assert "***" in output


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
