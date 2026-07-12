"""@brief Games Telegram callback 协议测试 / Games Telegram callback protocol tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fogmoe_bot.domain.games import (
    GambleSession,
    GameSessionId,
    SicBoSession,
)
from fogmoe_bot.presentation.telegram.game_handlers import (
    gamble as gamble_handlers,
    sicbo as sicbo_handlers,
)


def test_sicbo_callback_binds_session_and_version_within_bot_api_limit() -> None:
    """@brief 骰宝 callback 绑定聚合身份与 OCC 版本 / Sic Bo callbacks bind aggregate identity and OCC version."""

    callback = sicbo_handlers._SicBoCallback(
        GameSessionId(UUID("12345678-1234-5678-1234-567812345678")),
        999,
        "b:any_triple",
    )
    encoded = callback.encode()

    assert len(encoded.encode()) <= 64
    assert sicbo_handlers._SicBoCallback.decode(encoded) == callback


def test_generated_pool_and_sicbo_keyboards_use_new_bound_protocols() -> None:
    """@brief 新键盘不再依赖当前进程全局游戏 / New keyboards no longer depend on process-global current games."""

    session_id = GameSessionId(UUID("12345678-1234-5678-1234-567812345678"))
    gamble = GambleSession(
        session_id,
        -100,
        10,
        datetime(2030, 1, 1, tzinfo=UTC),
    )
    sicbo = SicBoSession(
        session_id,
        42,
        -100,
        11,
        datetime(2030, 1, 1, tzinfo=UTC),
        version=17,
    )

    gamble_values = [
        button.callback_data
        for row in gamble_handlers._gamble_keyboard(gamble).inline_keyboard
        for button in row
    ]
    sicbo_values = [
        button.callback_data
        for row in sicbo_handlers._sicbo_main_keyboard(sicbo).inline_keyboard
        for button in row
    ]
    assert all(
        isinstance(value, str)
        and value.startswith("gamble:12345678123456781234567812345678:")
        and len(value.encode()) <= 64
        for value in gamble_values
    )
    assert all(
        isinstance(value, str)
        and value.startswith("sb:12345678123456781234567812345678:17:")
        and len(value.encode()) <= 64
        for value in sicbo_values
    )
