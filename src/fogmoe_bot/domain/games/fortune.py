"""@brief 御神签确定性领域规则 / Deterministic domain rules for Omikuji."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from enum import StrEnum
import hashlib
import random
from types import MappingProxyType
from typing import Final


class FortuneLevel(StrEnum):
    """@brief 御神签运势级别 / Omikuji fortune levels."""

    GREAT_BLESSING = "大吉"
    MIDDLE_BLESSING = "中吉"
    SMALL_BLESSING = "小吉"
    LATE_BLESSING = "末吉"
    CURSE = "凶"
    GREAT_CURSE = "大凶"

    @property
    def is_favorable(self) -> bool:
        """@brief 判断是否为好运签 / Return whether this is a favorable fortune.

        @return 大吉、中吉、小吉为 True / True for the three favorable levels.
        """

        return self in {
            FortuneLevel.GREAT_BLESSING,
            FortuneLevel.MIDDLE_BLESSING,
            FortuneLevel.SMALL_BLESSING,
        }


FORTUNE_WEIGHTS: Final[Mapping[FortuneLevel, int]] = MappingProxyType(
    {
        FortuneLevel.GREAT_BLESSING: 10,
        FortuneLevel.MIDDLE_BLESSING: 20,
        FortuneLevel.SMALL_BLESSING: 30,
        FortuneLevel.LATE_BLESSING: 20,
        FortuneLevel.CURSE: 15,
        FortuneLevel.GREAT_CURSE: 5,
    }
)
"""@brief 御神签概率权重 / Omikuji fortune weights."""


def daily_fortune(user_id: int, day: date) -> FortuneLevel:
    """@brief 无全局随机状态地复现每日御神签 / Reproduce daily Omikuji without global RNG state.

    @param user_id 用户 ID / User ID.
    @param day 业务日期 / Business date.
    @return 确定性运势 / Deterministic fortune.
    @raise ValueError 用户 ID 不为正时抛出 / Raised when the user ID is not positive.
    @note 保留既有 MD5 种子与 ``random.choices`` 语义，避免改变已抽取日期的结果。
        / The existing MD5 seed and ``random.choices`` semantics are retained so already drawn
        dates do not change result.
    """

    if user_id <= 0:
        raise ValueError("Omikuji user_id must be positive")
    seed = f"{user_id}_{day:%Y-%m-%d}"
    digest = int(hashlib.md5(seed.encode(), usedforsecurity=False).hexdigest(), 16)
    generator = random.Random(digest)
    levels = tuple(FORTUNE_WEIGHTS)
    return generator.choices(
        levels,
        weights=tuple(FORTUNE_WEIGHTS[level] for level in levels),
        k=1,
    )[0]


def daily_fortune_variant(
    user_id: int,
    day: date,
    *,
    choices: int = 4,
) -> tuple[int, ...]:
    """@brief 生成稳定文案下标 / Generate stable copy-variant indices.

    @param user_id 用户 ID / User ID.
    @param day 业务日期 / Business date.
    @param choices 每一栏的文案数量 / Number of variants per section.
    @return 五个展示栏的稳定下标 / Stable indices for five display sections.
    @raise ValueError 文案候选数不为正时抛出 / Raised when the candidate count is not positive.
    """

    if choices <= 0:
        raise ValueError("Fortune variant count must be positive")
    seed = f"{user_id}_{day:%Y-%m-%d}"
    digest = int(hashlib.md5(seed.encode(), usedforsecurity=False).hexdigest(), 16)
    generator = random.Random(digest)
    return tuple(generator.randrange(choices) for _ in range(5))


__all__ = [
    "FORTUNE_WEIGHTS",
    "FortuneLevel",
    "daily_fortune",
    "daily_fortune_variant",
]
