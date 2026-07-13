"""@brief Bot 启动不隐式执行数据库迁移 / Bot startup does not run database migrations implicitly."""

from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

from fogmoe_bot import main as bot_main
from fogmoe_bot.config import BotSettings
from observability_testkit import make_observability


def test_main_starts_bot_without_database_migrations(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """@brief 启动只装配运行时，不调用 dbctl migration / Startup only composes runtime and never calls dbctl migration.

    @param monkeypatch pytest 替换器 / Pytest replacement helper.
    @param tmp_path pytest 隔离目录 / Pytest isolated directory.
    @return None / None.
    """

    calls: list[str] = []
    settings = BotSettings()
    observability = make_observability()
    resources = SimpleNamespace(log_directory=tmp_path / "logs")
    monkeypatch.setattr(
        bot_main,
        "_parse_arguments",
        lambda: argparse.Namespace(config=tmp_path / "config.json"),
    )
    monkeypatch.setattr(bot_main, "read_bot_settings", lambda _: settings)
    monkeypatch.setattr(bot_main, "load_resources", lambda **_: resources)
    monkeypatch.setattr(
        bot_main.db,
        "configure_database",
        lambda _: calls.append("database"),
    )
    monkeypatch.setattr(
        bot_main,
        "build_observability",
        lambda *, settings, database: observability,
    )
    monkeypatch.setattr(
        bot_main,
        "configure_logging",
        lambda settings, directory, telemetry: calls.append("logging"),
    )
    monkeypatch.setattr(
        bot_main,
        "prepare_litellm_logging",
        lambda: calls.append("litellm-logging"),
    )
    monkeypatch.setattr(
        bot_main,
        "configure_proxy_environment",
        lambda settings: calls.append("network"),
    )
    monkeypatch.setattr(
        bot_main,
        "run",
        lambda *, settings, resources, observability: calls.append("bot"),
    )

    bot_main.main()

    assert calls == ["database", "logging", "litellm-logging", "network", "bot"]
