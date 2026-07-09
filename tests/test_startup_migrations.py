from fogmoe_bot import main as bot_main
from fogmoe_bot.infrastructure.database import migration_service


def test_main_runs_migrations_before_bot(monkeypatch):
    calls = []

    monkeypatch.setattr(bot_main, "configure_logging", lambda: calls.append("logging"))
    monkeypatch.setattr(bot_main, "run_startup_migrations", lambda: calls.append("migrate"))
    monkeypatch.setattr(bot_main, "run", lambda: calls.append("bot"))

    bot_main.main()

    assert calls == ["logging", "migrate", "bot"]


def test_startup_migrations_can_be_disabled(monkeypatch):
    calls = []

    monkeypatch.setattr(migration_service.config, "DB_AUTO_MIGRATE", False)
    monkeypatch.setattr(
        migration_service.command,
        "upgrade",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    migration_service.run_startup_migrations()

    assert calls == []
