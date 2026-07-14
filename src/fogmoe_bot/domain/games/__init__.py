"""@brief 御神签领域模型公共接口 / Public domain API for Omikuji."""

from fogmoe_bot.domain.games.fortune import (
    FORTUNE_WEIGHTS,
    FortuneLevel,
    daily_fortune,
    daily_fortune_variant,
)
__all__ = [
    "FORTUNE_WEIGHTS",
    "FortuneLevel",
    "daily_fortune",
    "daily_fortune_variant",
]
"""@brief 御神签领域导出的稳定符号 / Stable symbols exported by the Omikuji domain."""
