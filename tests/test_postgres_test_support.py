"""@brief 真实 PostgreSQL 测试隔离哨兵 / Real-PostgreSQL test-isolation sentinels."""

from __future__ import annotations

import postgres_test_support as support
import pytest


def test_database_settings_require_a_dedicated_test_name() -> None:
    """@brief 普通库名即使 DSN 完整也失败关闭 / An ordinary database name fails closed even with a complete DSN."""

    with pytest.raises(ValueError, match="dedicated database name"):
        support.database_settings_from_url(
            "postgresql+asyncpg://tester:secret@db.example.invalid:5432/fogmoe"
        )


def test_database_settings_accept_an_explicit_test_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 与生产端点不同的 test segment 库名可用 / A test-segment database distinct from production is accepted."""

    monkeypatch.setattr(support, "_configured_production_endpoint", lambda: None)

    settings = support.database_settings_from_url(
        "postgresql+asyncpg://tester:secret@localhost:5432/fogmoe_test"
    )

    assert settings.endpoint.name == "fogmoe_test"
    assert settings.application.username == "tester"


def test_database_settings_reject_the_configured_production_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 即使库名含 test，当前部署指纹仍不可用 / The configured deployment fingerprint is rejected even when its name contains test."""

    monkeypatch.setattr(
        support,
        "_configured_production_endpoint",
        lambda: ("loopback", 5432, "fogmoe_test"),
    )

    with pytest.raises(ValueError, match="configured production database"):
        support.database_settings_from_url(
            "postgresql+asyncpg://tester:secret@127.0.0.1:5432/fogmoe_test"
        )
