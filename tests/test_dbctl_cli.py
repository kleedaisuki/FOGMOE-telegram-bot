from pathlib import Path

from fogmoe_dbctl import cli
from fogmoe_dbctl.commands import bootstrap, export_csv, migrate
from fogmoe_dbctl.postgres import (
    ServiceConfig,
    escape_pgpass_field,
    find_pgpass_password,
    service_sqlalchemy_url,
    split_pgpass_line,
)


def test_cli_registers_commands_and_compatibility_aliases():
    """@brief 验证统一 CLI 的命令与兼容别名 / Verify commands and compatibility aliases."""

    parser = cli.build_parser()

    assert parser.parse_args(["bootstrap"]).handler is bootstrap.execute
    assert parser.parse_args(["bootstrap-postgres"]).handler is bootstrap.execute
    assert parser.parse_args(["migrate"]).handler is migrate.execute
    assert parser.parse_args(["upgrade"]).handler is migrate.execute
    assert parser.parse_args(["run-migrations-as-role"]).handler is migrate.execute
    assert parser.parse_args(["export-csv", "--table", "conversation.chat_records", "--output", "records.csv"]).handler is export_csv.execute
    assert parser.parse_args(["export", "--table", "conversation.chat_records", "--output", "records.csv"]).handler is export_csv.execute


def test_cli_without_command_prints_help(capsys):
    """@brief 验证空命令打印帮助 / Verify that an empty command prints help."""

    cli.main([])

    output = capsys.readouterr().out
    assert "bootstrap-postgres" in output
    assert "migrate" in output


def test_pgpass_round_trip_and_first_match(tmp_path: Path):
    """@brief 验证共享 pgpass 解析 / Verify shared pgpass parsing."""

    password = r"secret:with\\escapes"
    line = ":".join(
        escape_pgpass_field(value)
        for value in ("localhost", "5432", "fogmoe", "fogmoe-bot", password)
    )
    pgpass = tmp_path / "pgpass"
    pgpass.write_text(f"{line}\n*:*:*:*:fallback\n", encoding="utf-8")

    assert split_pgpass_line(line)[4] == password
    assert find_pgpass_password(
        pgpass,
        host="localhost",
        port=5432,
        database="fogmoe",
        user="fogmoe-bot",
    ) == password


def test_service_url_uses_sqlalchemy_escaping():
    """@brief 验证 URL 由共享基础层正确转义 / Verify shared URL escaping."""

    service = ServiceConfig(
        host="localhost",
        port=5432,
        database="fog/moe",
        user="fogmoe@bot",
        password="p@ss/word",
    )

    assert service_sqlalchemy_url(service) == (
        "postgresql+asyncpg://fogmoe%40bot:p%40ss%2Fword@localhost:5432/fog/moe"
    )


def test_migrate_calls_alembic_programmatically(monkeypatch):
    """@brief 验证迁移不再启动 Alembic 子进程 / Verify direct Alembic invocation."""

    calls = []
    service = ServiceConfig("localhost", 5432, "fogmoe", "automation", "secret")
    monkeypatch.setattr(
        migrate.command,
        "upgrade",
        lambda config, revision: calls.append((config, revision)),
    )

    migrate.run_alembic(service=service, revision="head", dry_run=False)

    config, revision = calls[0]
    assert revision == "head"
    assert Path(config.get_main_option("script_location")).is_absolute()
    assert config.attributes["database_url"].startswith(
        "postgresql+asyncpg://automation:secret@localhost:5432/fogmoe"
    )


def test_bootstrap_dry_run_redacts_passwords(tmp_path: Path, capsys):
    """@brief 验证预演不会泄露密码 / Verify dry-run password redaction."""

    cli.main(
        [
            "bootstrap",
            "--config-dir",
            str(tmp_path),
            "--bot-password",
            "bot-secret",
            "--automation-password",
            "automation-secret",
            "--dry-run",
        ]
    )

    output = capsys.readouterr().out
    assert "bot-secret" not in output
    assert "automation-secret" not in output
    assert "***" in output
    assert not (tmp_path / "pgpass").exists()


def test_export_csv_accepts_only_schema_qualified_identifiers():
    """@brief 导出命令拒绝任意 SQL / Export command rejects arbitrary SQL."""

    assert export_csv.parse_table_name("conversation.chat_records") == (
        "conversation",
        "chat_records",
    )
    assert export_csv.build_copy_sql("conversation", "chat_records") == (
        'COPY (SELECT * FROM "conversation"."chat_records") '
        "TO STDOUT WITH (FORMAT CSV, HEADER TRUE, ENCODING 'UTF8');"
    )

    for invalid_name in ("chat_records", "conversation.chat-records", "conversation.chat_records;DROP TABLE users"):
        try:
            export_csv.parse_table_name(invalid_name)
        except ValueError:
            continue
        raise AssertionError(f"expected invalid table name to fail: {invalid_name}")


def test_export_csv_writes_atomically_through_psql_service(tmp_path: Path, monkeypatch):
    """@brief CSV 经 service 原子写入，失败不会毁坏旧文件 / CSV writes atomically via service."""

    output = tmp_path / "records.csv"
    output.write_text("old\n", encoding="utf-8")
    calls = []

    def fake_run(command, *, stdout, env, check):
        calls.append((command, env, check))
        stdout.write(b"id\n1\n")

    monkeypatch.setattr(export_csv.subprocess, "run", fake_run)
    export_csv.export_table(
        config_dir=tmp_path / "psql",
        service_name="fogmoe_automation",
        schema="conversation",
        table="chat_records",
        output_path=output,
        force=True,
    )

    assert output.read_text(encoding="utf-8") == "id\n1\n"
    command, environment, check = calls[0]
    assert command[0] == "psql"
    assert "service=fogmoe_automation" in command
    assert environment["PGSERVICEFILE"] == str(tmp_path / "psql" / "pg_service.conf")
    assert environment["PGPASSFILE"] == str(tmp_path / "psql" / "pgpass")
    assert check is True
