from fogmoe_bot import main as bot_main
from observability_testkit import make_observability


def test_main_starts_bot_without_database_migrations(monkeypatch):
    calls = []

    observability = make_observability()
    monkeypatch.setattr(bot_main, "build_observability", lambda: observability)
    monkeypatch.setattr(
        bot_main,
        "configure_logging",
        lambda telemetry: calls.append("logging"),
    )
    monkeypatch.setattr(bot_main, "run", lambda assembly: calls.append("bot"))

    bot_main.main()

    assert calls == ["logging", "bot"]
