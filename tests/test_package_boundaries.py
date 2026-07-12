import ast
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


def test_context_and_conversation_have_distinct_domain_ownership():
    """@brief 上下文格式化与会话工作流边界分离 / Context formatting and workflow ownership stay separate."""
    context_root = SRC_ROOT / "domain" / "context"
    conversation_root = SRC_ROOT / "domain" / "conversation"

    assert (context_root / "formatting.py").is_file()
    assert (context_root / "token_estimator.py").is_file()
    assert (conversation_root / "__init__.py").is_file()
    assert not (conversation_root / "models.py").exists()
    for feature in (
        "payloads.py",
        "identity.py",
        "temporal.py",
        "turn.py",
        "inbox.py",
        "inference.py",
        "message.py",
        "outbox.py",
        "workflow_results.py",
    ):
        assert (conversation_root / feature).is_file()
    assert "from ." not in (conversation_root / "__init__.py").read_text(
        encoding="utf-8"
    )


def test_conversation_types_are_imported_from_owning_feature_modules() -> None:
    """代码与测试不得依赖 Conversation root 或已删除的 mega models。"""

    forbidden = {
        "fogmoe_bot.application.conversation",
        "fogmoe_bot.domain.conversation",
        "fogmoe_bot.domain.conversation.models",
    }
    offenders: list[str] = []
    for root in (SRC_ROOT, PROJECT_ROOT / "tests"):
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module in forbidden:
                    offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}")
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name in forbidden:
                            offenders.append(
                                f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}"
                            )

    assert offenders == []


def test_assistant_routing_types_have_direct_module_ownership() -> None:
    """Assistant routing roots must not become nested re-export facades."""

    assistant_root = SRC_ROOT / "domain" / "assistant"
    routing_root = assistant_root / "routing"
    assert "from ." not in (assistant_root / "__init__.py").read_text(encoding="utf-8")
    assert "from ." not in (routing_root / "__init__.py").read_text(encoding="utf-8")
    forbidden = {
        "fogmoe_bot.domain.assistant",
        "fogmoe_bot.domain.assistant.routing",
    }
    offenders: list[str] = []
    for root in (SRC_ROOT, PROJECT_ROOT / "tests"):
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module in forbidden:
                    offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}")
    assert offenders == []
    application_root = SRC_ROOT / "application" / "conversation"
    assert "from ." not in (application_root / "__init__.py").read_text(
        encoding="utf-8"
    )
    assert "ConversationResetWorkflow" not in (application_root / "reset.py").read_text(
        encoding="utf-8"
    )


def test_moderation_types_are_imported_from_owning_feature_modules() -> None:
    """Moderation 消费方不得依赖跨特性的领域根 facade。"""

    root_module = "fogmoe_bot.domain.moderation"
    offenders: list[str] = []
    for root in (SRC_ROOT, PROJECT_ROOT / "tests"):
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == root_module:
                    offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}")
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == root_module:
                            offenders.append(
                                f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}"
                            )

    assert offenders == []
    package_source = (SRC_ROOT / "domain" / "moderation" / "__init__.py").read_text(
        encoding="utf-8"
    )
    assert "from ." not in package_source


def test_conversation_workflow_adapters_have_explicit_feature_ownership() -> None:
    """@brief Conversation persistence 不再暴露 mega repository / Conversation persistence exposes no mega repository."""

    database_root = SRC_ROOT / "infrastructure" / "database"
    workflow_root = database_root / "conversation_workflow"
    old_adapter = (
        database_root / "repositories" / ("conversation_" + "workflow_repository.py")
    )
    old_domain_ports = SRC_ROOT / "domain" / "conversation" / "ports.py"
    old_test = (
        PROJECT_ROOT / "tests" / ("test_conversation_" + "workflow_repository.py")
    )

    assert not old_adapter.exists()
    assert not old_domain_ports.exists()
    assert not old_test.exists()
    assert (SRC_ROOT / "domain" / "conversation" / "errors.py").is_file()
    for feature in ("inbox.py", "turn.py", "inference.py", "outbox.py"):
        assert (workflow_root / feature).is_file()
    for shared in ("common.py", "turn_uow.py"):
        source = (workflow_root / shared).read_text(encoding="utf-8")
        assert "class Postgres" not in source
    package_source = (workflow_root / "__init__.py").read_text(encoding="utf-8")
    assert "from ." not in package_source


def test_telegram_features_are_flattened_into_application_domains():
    assert not (SRC_ROOT / "presentation" / "telegram" / "features").exists()
    assert not (SRC_ROOT / "application" / "telegram" / "features").exists()
    assert not (SRC_ROOT / "application" / "telegram" / "bot_monitoring.py").exists()
    for domain in ("admin", "crypto", "economy", "games", "media", "moderation"):
        assert (SRC_ROOT / "application" / domain).is_dir()
    assert not (SRC_ROOT / "application" / "crypto" / "bot_monitoring.py").exists()
    assert (SRC_ROOT / "application" / "crypto" / "market_monitor.py").is_file()
    assert (SRC_ROOT / "infrastructure" / "crypto" / "binance_monitor.py").is_file()


def test_btc_monitor_has_one_structured_lifecycle_and_no_legacy_globals() -> None:
    """@brief BTC 监控不再拥有第二 Bot 或 detached task / BTC monitoring owns no second Bot or detached task."""

    monitor_path = SRC_ROOT / "application" / "crypto" / "market_monitor.py"
    adapter_path = SRC_ROOT / "infrastructure" / "crypto" / "binance_monitor.py"
    handler_path = SRC_ROOT / "presentation" / "telegram" / "monitor_handlers.py"
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (monitor_path, adapter_path, handler_path)
    )

    assert "monitor_thread" not in combined
    assert "ThreadPoolExecutor" not in combined
    assert "Bot(token=" not in combined
    assert "asyncio.create_task(delayed" not in combined
    assert not (SRC_ROOT / "infrastructure" / "crypto" / "biance_api.py").exists()


def test_crypto_application_types_are_imported_from_owning_modules() -> None:
    """Crypto consumers must not depend on a package-root re-export facade."""

    root_module = "fogmoe_bot.application.crypto"
    package_path = SRC_ROOT / "application" / "crypto" / "__init__.py"
    package_tree = ast.parse(
        package_path.read_text(encoding="utf-8"),
        filename=str(package_path),
    )
    assert not any(
        isinstance(node, (ast.Import, ast.ImportFrom))
        for node in ast.walk(package_tree)
    )

    offenders: list[str] = []
    for root in (SRC_ROOT, PROJECT_ROOT / "tests"):
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == root_module:
                    offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}")
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == root_module:
                            offenders.append(
                                f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}"
                            )

    assert offenders == []


def test_application_context_roots_do_not_reexport_owned_types() -> None:
    """Application consumers must import types from their owning modules."""

    root_modules = {
        "fogmoe_bot.application.admin",
        "fogmoe_bot.application.chat",
        "fogmoe_bot.application.economy",
        "fogmoe_bot.application.scheduling",
    }
    for package in ("admin", "chat", "economy", "scheduling"):
        package_path = SRC_ROOT / "application" / package / "__init__.py"
        package_tree = ast.parse(
            package_path.read_text(encoding="utf-8"),
            filename=str(package_path),
        )
        assert not any(
            isinstance(node, (ast.Import, ast.ImportFrom))
            for node in ast.walk(package_tree)
        )

    offenders: list[str] = []
    for root in (SRC_ROOT, PROJECT_ROOT / "tests"):
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module in root_modules:
                    offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}")
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name in root_modules:
                            offenders.append(
                                f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}"
                            )

    assert offenders == []


def test_crypto_workflows_have_pure_core_thin_telegram_and_no_process_locks() -> None:
    """@brief Crypto 写流程只通过端口访问外层且无进程级正确性状态 / Crypto write workflows reach outer layers only through ports and own no process-local correctness state."""

    application_root = SRC_ROOT / "application" / "crypto"
    domain_root = SRC_ROOT / "domain" / "crypto"
    presentation_root = SRC_ROOT / "presentation" / "telegram" / "crypto_handlers"
    adapter_root = SRC_ROOT / "infrastructure" / "database" / "crypto_operations"
    removed_presentation = SRC_ROOT / "presentation" / "telegram" / "crypto_handlers.py"
    removed_repository = (
        SRC_ROOT
        / "infrastructure"
        / "database"
        / "repositories"
        / "crypto_repository.py"
    )
    removed = (
        application_root / "crypto_predict.py",
        application_root / "chart.py",
        application_root / "swap_fogmoe_solana_token.py",
    )
    assert [path for path in removed if path.exists()] == []
    assert presentation_root.is_dir()
    assert adapter_root.is_dir()
    assert not removed_presentation.exists()
    assert not removed_repository.exists()
    assert "from ." not in (presentation_root / "__init__.py").read_text(
        encoding="utf-8"
    )
    assert "from ." not in (adapter_root / "__init__.py").read_text(encoding="utf-8")

    core_text = "\n".join(
        path.read_text(encoding="utf-8")
        for root in (application_root, domain_root)
        for path in root.rglob("*.py")
    )
    assert "from telegram" not in core_text
    assert "import telegram" not in core_text
    assert "fogmoe_bot.infrastructure" not in core_text
    assert "db_connection" not in core_text
    assert "UMFutures" not in core_text

    crypto_text = "\n".join(
        path.read_text(encoding="utf-8")
        for root in (presentation_root, adapter_root, application_root)
        for path in root.rglob("*.py")
    )
    assert "active_predict_tasks" not in crypto_text
    assert "user_locks" not in crypto_text
    assert "button_click_cooldown" not in crypto_text
    assert "token_cache" not in crypto_text
    assert "asyncio.sleep(600)" not in crypto_text
    assert "db.run_sync" not in crypto_text


def test_games_workflows_have_pure_core_durable_sessions_and_thin_telegram() -> None:
    """@brief Games 正确性不依赖进程全局状态或 Telegram / Games correctness depends on neither process globals nor Telegram."""

    application_root = SRC_ROOT / "application" / "games"
    ports_root = application_root / "ports"
    domain_root = SRC_ROOT / "domain" / "games"
    presentation_root = SRC_ROOT / "presentation" / "telegram" / "game_handlers"
    removed_monolith = SRC_ROOT / "presentation" / "telegram" / "games_handlers.py"
    adapter_root = SRC_ROOT / "infrastructure" / "database" / "game_operations"
    removed_adapter = SRC_ROOT / "infrastructure" / "database" / "games_operations.py"
    removed_port = application_root / "ports.py"
    removed = (
        application_root / "models.py",
        application_root / "service.py",
        application_root / "rpg" / "battles.py",
        application_root / "rpg" / "characters.py",
        application_root / "rpg" / "commands.py",
        application_root / "rpg" / "monsters.py",
        SRC_ROOT
        / "infrastructure"
        / "database"
        / "repositories"
        / "game_repository.py",
    )

    assert [path for path in removed if path.exists()] == []
    assert not removed_monolith.exists()
    assert presentation_root.is_dir()
    assert not removed_adapter.exists()
    assert adapter_root.is_dir()
    assert not removed_port.exists()
    assert ports_root.is_dir()
    for feature in ("gamble", "sicbo", "omikuji", "rpg"):
        assert (application_root / feature).is_dir()
        assert "from " not in (application_root / feature / "__init__.py").read_text(
            encoding="utf-8"
        )
    for service in (
        application_root / "gamble" / "service.py",
        application_root / "sicbo" / "service.py",
        application_root / "omikuji" / "service.py",
        application_root / "rpg" / "character_service.py",
        application_root / "rpg" / "inventory_service.py",
    ):
        assert service.is_file()
    assert not (application_root / "rpg" / "equipment_service.py").exists()
    assert not (application_root / "common.py").exists()
    assert "from ." not in (adapter_root / "__init__.py").read_text(encoding="utf-8")
    assert "from ." not in (ports_root / "__init__.py").read_text(encoding="utf-8")
    assert "from " not in (application_root / "__init__.py").read_text(encoding="utf-8")
    core_text = "\n".join(
        path.read_text(encoding="utf-8")
        for root in (application_root, domain_root)
        for path in root.rglob("*.py")
    )
    assert "from telegram" not in core_text
    assert "import telegram" not in core_text
    assert "fogmoe_bot.infrastructure" not in core_text
    assert "db_connection" not in core_text
    assert "command_cooldown" not in core_text

    migrated_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            *presentation_root.rglob("*.py"),
            *adapter_root.rglob("*.py"),
            *application_root.rglob("*.py"),
        )
    )
    assert "gamble_game =" not in migrated_text
    assert "active_games" not in migrated_text
    assert "player_battle_cooldowns" not in migrated_text
    assert "monster_battle_cooldowns" not in migrated_text
    assert "db.run_sync" not in migrated_text
    assert "class GamesOperations" not in migrated_text
    assert "PostgresGamesOperations" not in migrated_text
    assert "class GamesService" not in migrated_text
    assert "GAMES_SERVICE_DATA_KEY" not in migrated_text


def test_telegram_handlers_do_not_return_to_presentation_layer():
    forbidden_paths = [
        SRC_ROOT / "presentation" / "telegram" / "bot_commands.py",
        SRC_ROOT / "presentation" / "telegram" / "bot_conversation.py",
        SRC_ROOT / "presentation" / "telegram" / "bot_monitoring.py",
        SRC_ROOT / "presentation" / "telegram" / "archive_utils.py",
        SRC_ROOT / "presentation" / "telegram" / "command_cooldown.py",
    ]

    assert [path for path in forbidden_paths if path.exists()] == []


def test_telegram_handler_catalog_has_no_registry_or_feature_setup_facades():
    """@brief Telegram handler 只有一个显式目录 / Telegram handlers have one explicit catalog."""

    telegram_root = SRC_ROOT / "presentation" / "telegram"
    catalog_path = telegram_root / "handler_catalog.py"
    composition_path = telegram_root / "handler_composition.py"
    catalog_route_path = telegram_root / "catalog_route.py"
    assert catalog_path.is_file()
    assert composition_path.is_file()
    assert catalog_route_path.is_file()
    assert not (telegram_root / "handler_registry.py").exists()
    assert not (telegram_root / "handler_groups.py").exists()
    assert not (telegram_root / "legacy_route.py").exists()

    setup_offenders = []
    add_handler_offenders = []
    for path in (SRC_ROOT / "application").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "def setup_" in text or "async def setup_" in text:
            setup_offenders.append(path.relative_to(SRC_ROOT))
        if ".add_handler(" in text or ".add_error_handler(" in text:
            add_handler_offenders.append(path.relative_to(SRC_ROOT))

    assert setup_offenders == []
    assert add_handler_offenders == []
    catalog_text = catalog_path.read_text(encoding="utf-8")
    assert "bot_conversation" not in catalog_text
    assert "conversation.command" not in catalog_text
    assert "conversation.message" not in catalog_text
    assert "fogmoe_bot.infrastructure" not in catalog_text
    assert "assemble_handler_capabilities" not in catalog_text


def test_presentation_reaches_infrastructure_only_from_composition_roots() -> None:
    """@brief Telegram 适配器仅在组合根实例化基础设施 / Telegram adapters instantiate infrastructure only in composition roots."""

    telegram_root = SRC_ROOT / "presentation" / "telegram"
    allowed = {
        "bot_app.py",
        "handler_composition.py",
        "moderation_composition.py",
    }
    offenders = []
    for path in telegram_root.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "fogmoe_bot.infrastructure" in text and path.name not in allowed:
            offenders.append(path.name)

    assert offenders == []


def test_moderation_runtime_has_no_legacy_callbacks_globals_or_thread_locks() -> None:
    """@brief 治理 P1 状态由 capability 有界拥有 / Moderation P1 state is bounded and capability-owned."""

    moderation_root = SRC_ROOT / "application" / "moderation"
    combined = "\n".join(
        path.read_text(encoding="utf-8") for path in moderation_root.rglob("*.py")
    )

    assert "threading.Timer" not in combined
    assert "threading.Lock" not in combined
    assert "defaultdict" not in combined
    assert "initialize_spam_control_runtime" not in combined
    assert "create_spam_warning_reset_timer" not in combined
    assert "_reset_warning_counters" not in combined
    assert not (moderation_root / "spam_control.py").exists()
    assert not (moderation_root / "keyword_handler.py").exists()
    assert not (moderation_root / "report.py").exists()
    assert not (moderation_root / "sf.py").exists()
    assert not (moderation_root / "share_link.py").exists()
    assert not (SRC_ROOT / "infrastructure" / "moderation" / "share_link.py").exists()


def test_moderation_persistence_is_split_by_application_port() -> None:
    """@brief 治理持久化不再暴露万能仓储 / Moderation persistence no longer exposes a universal repository."""

    database_root = SRC_ROOT / "infrastructure" / "database"
    adapter_root = database_root / "moderation"
    old_repository = database_root / "repositories" / "moderation_repository.py"
    composition = (
        SRC_ROOT / "presentation" / "telegram" / "moderation_composition.py"
    ).read_text(encoding="utf-8")

    assert not old_repository.exists()
    assert "from " not in (adapter_root / "__init__.py").read_text(encoding="utf-8")
    expected = {
        "group.py": ("PostgresModerationGroupRepository", "GroupModerationRepository"),
        "effects.py": (
            "PostgresModerationEffectRepository",
            "ModerationEffectRepository",
        ),
        "reports.py": (
            "PostgresModerationReportRepository",
            "ReportRepository",
        ),
    }
    for filename, names in expected.items():
        source = (adapter_root / filename).read_text(encoding="utf-8")
        assert all(name in source for name in names)
        assert "PostgresModerationRepository" not in source
        assert names[0] in composition


def test_moderation_domain_and_application_do_not_import_adapters() -> None:
    """@brief 治理核心不依赖 Telegram、aiohttp 或 infrastructure / Moderation core does not depend on Telegram, aiohttp, or infrastructure."""

    roots = (
        SRC_ROOT / "domain" / "moderation",
        SRC_ROOT / "application" / "moderation",
    )
    forbidden = (
        "import telegram",
        "from telegram",
        "import aiohttp",
        "from aiohttp",
        "fogmoe_bot.infrastructure",
    )
    offenders = []
    for root in roots:
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if any(snippet in text for snippet in forbidden):
                offenders.append(path.relative_to(SRC_ROOT))

    assert offenders == []


def test_admin_core_is_pure_and_announcement_fanout_is_durable() -> None:
    """@brief Admin 权限在纯核心，公告不再由 handler 直接 fanout / Admin authorization lives in the pure core and handlers no longer fan out announcements directly."""

    roots = (
        SRC_ROOT / "domain" / "admin",
        SRC_ROOT / "application" / "admin",
    )
    forbidden = (
        "import telegram",
        "from telegram",
        "fogmoe_bot.infrastructure",
        "SELECT ",
        "INSERT ",
        "UPDATE ",
        "DELETE ",
    )
    offenders = []
    for root in roots:
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if any(snippet in text for snippet in forbidden):
                offenders.append(path.relative_to(SRC_ROOT))

    assert offenders == []
    assert not (SRC_ROOT / "application" / "admin" / "developer.py").exists()
    handler_text = (
        SRC_ROOT / "presentation" / "telegram" / "admin_handlers.py"
    ).read_text(encoding="utf-8")
    log_text = (SRC_ROOT / "infrastructure" / "admin" / "log_reader.py").read_text(
        encoding="utf-8"
    )
    assert ".send_message(" not in handler_text
    assert "asyncio.sleep(" not in handler_text
    assert "import tempfile" not in handler_text
    assert "import tempfile" not in log_text
    assert ".readlines(" not in log_text


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


def test_database_engine_has_one_event_loop_owner() -> None:
    """The removed secondary loops must not bring back an engine registry."""

    source = (SRC_ROOT / "infrastructure" / "database" / "db.py").read_text(
        encoding="utf-8"
    )
    assert "WeakKeyDictionary" not in source
    assert "threading.Lock" not in source
    assert "_ENGINES" not in source
    assert "scheduling daemon" not in source


def test_domain_and_application_module_state_is_not_a_mutable_literal() -> None:
    """Business packages may expose immutable catalogs, not mutable global state."""

    offenders: list[str] = []
    for layer in (SRC_ROOT / "domain", SRC_ROOT / "application"):
        for path in layer.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in tree.body:
                if not isinstance(node, ast.Assign | ast.AnnAssign):
                    continue
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                if any(
                    isinstance(target, ast.Name) and target.id == "__all__"
                    for target in targets
                ):
                    continue
                if isinstance(node.value, ast.Dict | ast.List | ast.Set):
                    offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}")

    assert offenders == []


def test_unnecessary_assistant_facades_do_not_return():
    forbidden_files = [
        SRC_ROOT / "application" / "assistant" / "ai_chat.py",
        SRC_ROOT / "application" / "assistant" / "ai_tools.py",
    ]

    assert [path for path in forbidden_files if path.exists()] == []


def test_agent_runtime_is_application_owned_and_old_domain_package_is_removed():
    """@brief Agent 执行状态属于应用层 / Agent execution state belongs to application."""
    removed_runtime_root = SRC_ROOT / "domain" / "agent_runtime"
    assistant_root = SRC_ROOT / "application" / "assistant"
    conversation_root = SRC_ROOT / "application" / "conversation"
    llm_root = SRC_ROOT / "infrastructure" / "llm"
    forbidden_paths = [
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

    assert not removed_runtime_root.exists()
    assert (assistant_root / "tool_runtime.py").is_file()
    assert (assistant_root / "tools").is_dir()
    assert (assistant_root / "tools" / "catalog.py").is_file()
    assert not (assistant_root / "tools" / "runtime.py").exists()
    assert not (assistant_root / "tools" / "models.py").exists()
    assert not (assistant_root / "tools" / "schemas.py").exists()
    assert not (assistant_root / "tools" / "registry.py").exists()
    for legacy_tool in (
        "context.py",
        "memory_tools.py",
        "schedule_tools.py",
        "user_tools.py",
        "image_tools.py",
        "voice_tools.py",
        "sticker_tools.py",
        "sandbox_tools.py",
    ):
        assert not (assistant_root / "tools" / legacy_tool).exists()
    assistant_tool_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (assistant_root / "tools").rglob("*.py")
    )
    assert "fogmoe_bot.infrastructure" not in assistant_tool_text
    assert "run_sync" not in assistant_tool_text
    assert "threading.Lock" not in assistant_tool_text
    assert "ContextVar" not in assistant_tool_text
    assert (conversation_root / "__init__.py").is_file()
    assert not (conversation_root / "tool_history.py").exists()
    assert (llm_root / "protocol.py").is_file()
    assert (llm_root / "tool_serialization.py").is_file()
    assert not (SRC_ROOT / "application" / "conversation_lock_manager.py").exists()
    assert not (SRC_ROOT / "application" / "telegram" / "bot_conversation.py").exists()
    assert (SRC_ROOT / "application" / "assistant" / "inference").is_dir()
    assert not (
        SRC_ROOT / "application" / "assistant" / "inference" / "task_runner.py"
    ).exists()
    assert not (
        SRC_ROOT / "application" / "assistant" / "tasks" / "summary.py"
    ).exists()
    assert not (
        SRC_ROOT / "application" / "assistant" / "tasks" / "translate.py"
    ).exists()
    assert (
        SRC_ROOT / "application" / "assistant" / "inference" / "message_content.py"
    ).is_file()
    assert not (SRC_ROOT / "application" / "accounts" / "service.py").exists()
    assert not (SRC_ROOT / "application" / "accounts" / "context.py").exists()
    assert not (
        SRC_ROOT / "infrastructure" / "telegram" / "generated_media_delivery.py"
    ).exists()
    assert not (SRC_ROOT / "presentation" / "telegram" / "sticker_delivery.py").exists()
    assert not (SRC_ROOT / "application" / "games" / "omikuji.py").exists()
    assert not (SRC_ROOT / "application" / "media" / "pic.py").exists()
    assert not (SRC_ROOT / "application" / "media" / "music.py").exists()
    assert not (SRC_ROOT / "domain" / "agent_routing").exists()
    assert (SRC_ROOT / "domain" / "assistant" / "routing").is_dir()
    assert [path for path in forbidden_paths if path.exists()] == []


def test_assistant_tool_catalog_has_no_parallel_registry_or_provider_dicts():
    """@brief 工具元数据只有一个权威目录 / Tool metadata has one authoritative catalog."""

    forbidden_symbols = ("AI_TOOL_ARG_MODELS", "AI_TOOL_HANDLERS", "OPENAI_TOOLS")
    offenders = []
    for path in SRC_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if any(symbol in text for symbol in forbidden_symbols):
            offenders.append(path.relative_to(SRC_ROOT))

    assert offenders == []


def test_assistant_tool_operations_have_explicit_feature_ownership() -> None:
    """Tool operations 不再由 mega adapter 或 package facade 隐藏。"""

    infrastructure_root = SRC_ROOT / "infrastructure" / "assistant"
    operation_root = infrastructure_root / "tool_operations"
    assert not (infrastructure_root / "tool_operations.py").exists()
    assert operation_root.is_dir()
    for feature in (
        "diary.py",
        "dispatcher.py",
        "external.py",
        "group.py",
        "memory.py",
        "outbound.py",
        "parsing.py",
        "schedule.py",
        "social.py",
    ):
        assert (operation_root / feature).is_file()
    assert "from ." not in (operation_root / "__init__.py").read_text(encoding="utf-8")
    dispatcher = (operation_root / "dispatcher.py").read_text(encoding="utf-8")
    assert "DEFAULT_TOOL_CATALOG" not in dispatcher
    assert "define_tool" not in dispatcher
    assert "PostgresAssistantToolOperations" not in dispatcher


def test_assistant_external_adapters_have_explicit_feature_ownership() -> None:
    """External reads、generated media 与 sticker catalog 不得回归 mega adapter。"""

    infrastructure_root = SRC_ROOT / "infrastructure" / "assistant"
    assert not (infrastructure_root / "external_tools.py").exists()
    for feature in (
        "external_reads.py",
        "generated_media.py",
        "sticker_catalog.py",
    ):
        assert (infrastructure_root / feature).is_file()
    composition = (infrastructure_root / "composition.py").read_text(encoding="utf-8")
    assert ".external_tools" not in composition


def test_old_domain_agent_runtime_imports_do_not_return():
    """@brief 禁止旧包兼容导入 / Forbid compatibility imports from the removed package."""
    offenders = []
    for path in SRC_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "fogmoe_bot.domain.agent_runtime" in text:
            offenders.append(path.relative_to(SRC_ROOT))

    assert offenders == []


def test_transactional_outbox_worker_uses_narrow_application_ports():
    """@brief outbox worker 与 Telegram adapter 分层 / Layer the outbox worker and Telegram adapter."""

    worker_path = SRC_ROOT / "application" / "conversation" / "outbox_worker.py"
    adapter_path = SRC_ROOT / "infrastructure" / "telegram" / "outbox_delivery.py"

    assert worker_path.is_file()
    assert adapter_path.is_file()
    worker_text = worker_path.read_text(encoding="utf-8")
    assert "fogmoe_bot.infrastructure" not in worker_text
    assert "from telegram" not in worker_text
    assert "import telegram" not in worker_text
    assert "threading.Lock" not in worker_text
    assert "asyncio.Lock" not in worker_text


def test_database_control_plane_stays_out_of_bot_package():
    assert not (SRC_ROOT / "infrastructure" / "database" / "migrations").exists()
    assert not (
        SRC_ROOT / "infrastructure" / "database" / "migration_service.py"
    ).exists()
    assert (DBCTL_ROOT / "cli.py").is_file()
    assert (DBCTL_ROOT / "commands" / "bootstrap.py").is_file()
    assert (DBCTL_ROOT / "commands" / "migrate.py").is_file()
    assert (DBCTL_ROOT / "postgres.py").is_file()
    assert not (DBCTL_ROOT / "bootstrap_postgres.py").exists()
    assert not (DBCTL_ROOT / "migrate_as_role.py").exists()
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
        if (
            "run_migration_sql" in text
            and not (sql_root / f"{path.stem}.sql").is_file()
        ):
            missing_sql_files.append(path.relative_to(DBCTL_ROOT))

    assert offenders == []
    assert missing_sql_files == []


def test_blocking_offloads_have_explicit_admission_control() -> None:
    """@brief 默认线程池前必须有显式准入边界 / Default-pool offloads require explicit admission control."""

    allowed = {
        Path("infrastructure/blocking.py"),
        Path("infrastructure/admin/log_reader.py"),
    }
    offenders = []
    for path in SRC_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "asyncio.to_thread(" not in text:
            continue
        relative = path.relative_to(SRC_ROOT)
        if relative not in allowed:
            offenders.append(relative)

    assert offenders == []
    log_source = (SRC_ROOT / "infrastructure" / "admin" / "log_reader.py").read_text(
        encoding="utf-8"
    )
    assert "asyncio.Semaphore(max_concurrency)" in log_source


def test_media_picture_and_music_have_explicit_feature_ownership() -> None:
    """Media 不再由跨图片与音乐的 facade 或 mega adapter 隐藏。"""

    application_root = SRC_ROOT / "application" / "media"
    domain_root = SRC_ROOT / "domain" / "media"
    database_root = SRC_ROOT / "infrastructure" / "database"
    adapter_root = database_root / "media"
    http_root = SRC_ROOT / "infrastructure" / "http" / "media"
    storage_root = SRC_ROOT / "infrastructure" / "media"
    handler_root = SRC_ROOT / "presentation" / "telegram" / "media_handlers"
    telegram_root = SRC_ROOT / "presentation" / "telegram"

    for path in (
        application_root / "service.py",
        application_root / "ports.py",
        database_root / "media_repository.py",
        telegram_root / "media_handlers.py",
        PROJECT_ROOT / "tests" / "test_media_service.py",
        PROJECT_ROOT / "tests" / "test_media_repository_postgres.py",
        PROJECT_ROOT / "tests" / "test_media_handlers.py",
    ):
        assert not path.exists()
    for path in (
        application_root / "picture_service.py",
        application_root / "picture_ports.py",
        application_root / "music_service.py",
        application_root / "music_ports.py",
        adapter_root / "picture.py",
        adapter_root / "music.py",
        http_root / "picture.py",
        http_root / "music.py",
        storage_root / "file_artifact_store.py",
        handler_root / "picture.py",
        handler_root / "music.py",
    ):
        assert path.is_file()
    for package in (
        application_root,
        domain_root,
        adapter_root,
        http_root,
        storage_root,
        handler_root,
    ):
        assert "from ." not in (package / "__init__.py").read_text(encoding="utf-8")

    core_text = "\n".join(
        path.read_text(encoding="utf-8")
        for root in (application_root, domain_root)
        for path in root.rglob("*.py")
    )
    assert "from telegram" not in core_text
    assert "fogmoe_bot.infrastructure" not in core_text
    assert "db_connection" not in core_text

    source_text = "\n".join(
        path.read_text(encoding="utf-8") for path in SRC_ROOT.rglob("*.py")
    )
    for symbol in (
        "Media" + "Service",
        "Media" + "Repository",
        "Postgres" + "MediaRepository",
    ):
        assert symbol not in source_text
    assert "from fogmoe_bot.domain.media import" not in source_text
    assert "from fogmoe_bot.application.media import" not in source_text
