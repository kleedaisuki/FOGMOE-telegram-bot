from fogmoe_bot import main as bot_main


def test_main_starts_bot_without_database_migrations(monkeypatch):
    calls = []

    monkeypatch.setattr(bot_main, "configure_logging", lambda: calls.append("logging"))
    monkeypatch.setattr(bot_main, "run", lambda: calls.append("bot"))

    bot_main.main()

    assert calls == ["logging", "bot"]
