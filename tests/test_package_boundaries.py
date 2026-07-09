from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src" / "fogmoe_bot"
DBCTL_ROOT = PROJECT_ROOT / "src" / "fogmoe_dbctl"
MIGRATIONS_ROOT = DBCTL_ROOT / "migrations"


def test_old_ai_package_names_do_not_return():
    forbidden_paths = [
        SRC_ROOT / "application" / "ai",
        SRC_ROOT / "domain" / "ai",
        SRC_ROOT / "infrastructure" / "ai",
    ]

    assert [path for path in forbidden_paths if path.exists()] == []


def test_telegram_features_live_in_application_layer():
    assert not (SRC_ROOT / "presentation" / "telegram" / "features").exists()
    assert (SRC_ROOT / "application" / "telegram" / "features").is_dir()


def test_telegram_handlers_do_not_return_to_presentation_layer():
    forbidden_paths = [
        SRC_ROOT / "presentation" / "telegram" / "bot_commands.py",
        SRC_ROOT / "presentation" / "telegram" / "bot_conversation.py",
        SRC_ROOT / "presentation" / "telegram" / "bot_monitoring.py",
        SRC_ROOT / "presentation" / "telegram" / "archive_utils.py",
        SRC_ROOT / "presentation" / "telegram" / "command_cooldown.py",
    ]

    assert [path for path in forbidden_paths if path.exists()] == []


def test_presentation_layer_does_not_contain_sql():
    presentation_root = SRC_ROOT / "presentation"
    forbidden_snippets = [
        "connection.",
        "exec_driver_sql(",
        "SELECT ",
        "INSERT ",
        "UPDATE ",
        "DELETE ",
    ]

    offenders = []
    for path in presentation_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if any(snippet in text for snippet in forbidden_snippets):
            offenders.append(path.relative_to(SRC_ROOT))

    assert offenders == []


def test_application_layer_does_not_contain_direct_sql():
    guarded_paths = [
        SRC_ROOT / "application",
    ]
    forbidden_snippets = [
        "db_connection.fetch_one",
        "db_connection.fetch_all",
        "db_connection.execute",
        "exec_driver_sql(",
        "SELECT ",
        "INSERT ",
        "UPDATE ",
        "DELETE ",
    ]

    offenders = []
    for guarded_path in guarded_paths:
        paths = [guarded_path] if guarded_path.is_file() else guarded_path.rglob("*.py")
        for path in paths:
            text = path.read_text(encoding="utf-8")
            if any(snippet in text for snippet in forbidden_snippets):
                offenders.append(path.relative_to(SRC_ROOT))

    assert offenders == []


def test_unnecessary_assistant_facades_do_not_return():
    forbidden_files = [
        SRC_ROOT / "application" / "assistant" / "ai_chat.py",
        SRC_ROOT / "application" / "assistant" / "ai_tools.py",
    ]

    assert [path for path in forbidden_files if path.exists()] == []


def test_database_control_plane_stays_out_of_bot_package():
    assert not (SRC_ROOT / "infrastructure" / "database" / "migrations").exists()
    assert not (SRC_ROOT / "infrastructure" / "database" / "migration_service.py").exists()
    assert (DBCTL_ROOT / "cli.py").is_file()
    assert MIGRATIONS_ROOT.is_dir()


def test_database_control_plane_does_not_import_bot_package():
    offenders = []
    for path in DBCTL_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "from fogmoe_bot" in text or "import fogmoe_bot" in text:
            offenders.append(path.relative_to(DBCTL_ROOT))

    assert offenders == []


def test_alembic_versions_are_backend_agnostic_sql_wrappers():
    forbidden_snippets = [
        "from alembic import op",
        "op.execute",
        "CREATE TABLE",
        "ALTER TABLE",
        "DROP TABLE",
        "INSERT INTO",
        "UPDATE ",
        "DELETE ",
        "SELECT ",
    ]

    offenders = []
    missing_sql_files = []
    sql_root = MIGRATIONS_ROOT / "sql" / "postgresql"
    assert not (MIGRATIONS_ROOT / "sql" / "mysql").exists()
    for path in (MIGRATIONS_ROOT / "versions").glob("*.py"):
        if path.name == "__init__.py":
            continue
        text = path.read_text(encoding="utf-8")
        if any(snippet in text for snippet in forbidden_snippets):
            offenders.append(path.relative_to(DBCTL_ROOT))
        if "run_migration_sql" in text and not (sql_root / f"{path.stem}.sql").is_file():
            missing_sql_files.append(path.relative_to(DBCTL_ROOT))

    assert offenders == []
    assert missing_sql_files == []
