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


def test_model_context_owns_prompt_formatting_and_token_budgeting():
    """@brief 验证模型上下文边界 / Verify model-context ownership boundary."""
    context_root = SRC_ROOT / "domain" / "context"
    old_conversation_root = SRC_ROOT / "domain" / "conversation"

    assert (context_root / "formatting.py").is_file()
    assert (context_root / "token_estimator.py").is_file()
    assert not (old_conversation_root / "__init__.py").exists()


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


def test_agent_runtime_owns_agent_tooling():
    runtime_root = SRC_ROOT / "domain" / "agent_runtime"
    forbidden_paths = [
        SRC_ROOT / "application" / "assistant" / "tools",
        SRC_ROOT / "application" / "assistant" / "tool_calling",
        SRC_ROOT / "application" / "assistant" / "tool_runner.py",
        SRC_ROOT / "application" / "assistant" / "runtime.py",
        SRC_ROOT / "application" / "assistant" / "tool_history.py",
        SRC_ROOT / "application" / "assistant" / "generated_image_sender.py",
        SRC_ROOT / "application" / "assistant" / "generated_audio_sender.py",
        SRC_ROOT / "application" / "assistant" / "delivery",
        SRC_ROOT / "application" / "assistant" / "types.py",
        SRC_ROOT / "application" / "assistant" / "agent_response.py",
        SRC_ROOT / "application" / "assistant" / "conversation_locks.py",
        SRC_ROOT / "application" / "assistant" / "conversation_context.py",
        SRC_ROOT / "application" / "assistant" / "router.py",
        SRC_ROOT / "application" / "assistant" / "chat_capabilities.py",
        SRC_ROOT / "application" / "assistant" / "providers",
        SRC_ROOT / "application" / "assistant" / "routing",
        SRC_ROOT / "application" / "assistant" / "task_runner.py",
        SRC_ROOT / "application" / "assistant" / "provider_resolver.py",
        SRC_ROOT / "application" / "assistant" / "summary.py",
        SRC_ROOT / "application" / "assistant" / "message_content.py",
        SRC_ROOT / "application" / "assistant" / "context_state.py",
        SRC_ROOT / "application" / "assistant" / "sticker_sender.py",
        SRC_ROOT / "application" / "assistant" / "telegram_visible_sender.py",
        SRC_ROOT / "application" / "economy" / "process_user.py",
        SRC_ROOT / "application" / "user",
    ]

    assert runtime_root.is_dir()
    assert (runtime_root / "runtime.py").is_file()
    assert (runtime_root / "tools").is_dir()
    assert (SRC_ROOT / "application" / "conversation_lock_manager.py").is_file()
    assert (SRC_ROOT / "application" / "assistant" / "inference").is_dir()
    assert (SRC_ROOT / "application" / "assistant" / "inference" / "task_runner.py").is_file()
    assert (SRC_ROOT / "application" / "assistant" / "tasks" / "summary.py").is_file()
    assert (SRC_ROOT / "application" / "assistant" / "inference" / "message_content.py").is_file()
    assert (SRC_ROOT / "application" / "accounts" / "service.py").is_file()
    assert (SRC_ROOT / "application" / "accounts" / "context.py").is_file()
    assert (SRC_ROOT / "application" / "telegram" / "assistant_visible_sender.py").is_file()
    assert (SRC_ROOT / "application" / "telegram" / "generated_image_sender.py").is_file()
    assert (SRC_ROOT / "application" / "telegram" / "generated_audio_sender.py").is_file()
    assert not (runtime_root / "image_delivery.py").exists()
    assert not (runtime_root / "audio_delivery.py").exists()
    assert (SRC_ROOT / "domain" / "agent_routing").is_dir()
    assert [path for path in forbidden_paths if path.exists()] == []


def test_runtime_user_tools_do_not_depend_on_application_services():
    user_tools_path = SRC_ROOT / "domain" / "agent_runtime" / "tools" / "user_tools.py"

    assert "fogmoe_bot.application" not in user_tools_path.read_text(encoding="utf-8")


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
