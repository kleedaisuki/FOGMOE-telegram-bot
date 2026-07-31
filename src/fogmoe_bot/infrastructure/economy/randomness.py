"""@brief 每日抽奖标准库随机适配器 / Standard-library randomness adapter for the daily lottery."""

from __future__ import annotations

import random

from fogmoe_bot.domain.economy.lottery import LotteryPrizeBand


class StandardLibraryLotteryRandomness:
    """@brief 以 Python random 模块实现抽奖随机端口 / Implement lottery randomness with Python's random module."""

    def choose_prize_band(
        self,
        bands: tuple[LotteryPrizeBand, ...],
        weights: tuple[float, ...],
    ) -> LotteryPrizeBand:
        """@brief 保持既有 random.choices 调用语义选择档位 / Select a band with the established random.choices semantics.

        @param bands 有序候选档位 / Ordered candidate bands.
        @param weights 与候选一一对应的权重 / Corresponding weights.
        @return 被选中的档位 / Selected band.
        """

        return random.choices(bands, weights, k=1)[0]

    def integer_between(self, lower: int, upper: int) -> int:
        """@brief 保持既有 randint 闭区间语义取值 / Draw with the established inclusive randint semantics.

        @param lower 闭区间下界 / Inclusive lower bound.
        @param upper 闭区间上界 / Inclusive upper bound.
        @return 区间内整数 / Integer inside the interval.
        """

        return random.randint(lower, upper)


__all__ = ["StandardLibraryLotteryRandomness"]
