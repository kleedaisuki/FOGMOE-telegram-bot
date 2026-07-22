"""@brief 可用于 UI 与测试的骰宝风格示例 / Sic-Bo-like examples suitable for UI and tests."""

from __future__ import annotations

from fractions import Fraction
from itertools import product
from typing import Final

from .rules import ChanceOutcome, ChanceRule, ChanceRuleset

SICBO_LIKE_HOUSE_EDGE: Final[Fraction] = Fraction(1, 20)
"""@brief 骰宝风格示例的 5% 精确庄家优势 / Exact 5% house edge for the Sic-Bo-like example."""


def sicbo_like_ruleset(
    *,
    house_edge: Fraction = SICBO_LIKE_HOUSE_EDGE,
) -> ChanceRuleset:
    """@brief 建立三骰、整数权重的骰宝风格规则集 / Build a three-die, integer-weight Sic-Bo-like ruleset.

    示例包含大、小、单双、任意围骰和六种指定围骰。它仅是通用机会活动核心的
    示例配置；所有派彩仍由负期望公式统一计算，而非使用旧硬编码赔率表。
    The example includes big, small, odd, even, any triple, and six exact triples. It is
    only an example configuration for the generic chance core; all payouts still come from
    the negative-EV formula rather than a legacy hard-coded payout table.

    @param house_edge 每条规则使用的精确庄家优势 / Exact house edge used by every rule.
    @return 不可变骰宝风格规则集 / Immutable Sic-Bo-like ruleset.
    """

    dice = tuple(product(range(1, 7), repeat=3))
    outcomes = tuple(
        ChanceOutcome(_dice_code(first, second, third), 1)
        for first, second, third in dice
    )
    big: set[str] = set()
    small: set[str] = set()
    odd: set[str] = set()
    even: set[str] = set()
    any_triple: set[str] = set()
    exact_triples: dict[int, set[str]] = {face: set() for face in range(1, 7)}
    for first, second, third in dice:
        code = _dice_code(first, second, third)
        total = first + second + third
        is_triple = first == second == third
        if 11 <= total <= 17 and not is_triple:
            big.add(code)
        if 4 <= total <= 10 and not is_triple:
            small.add(code)
        (odd if total % 2 else even).add(code)
        if is_triple:
            any_triple.add(code)
            exact_triples[first].add(code)
    rules = (
        ChanceRule("big", frozenset(big), house_edge),
        ChanceRule("small", frozenset(small), house_edge),
        ChanceRule("odd", frozenset(odd), house_edge),
        ChanceRule("even", frozenset(even), house_edge),
        ChanceRule("any-triple", frozenset(any_triple), house_edge),
        *(
            ChanceRule(
                f"triple-{face}",
                frozenset(exact_triples[face]),
                house_edge,
            )
            for face in range(1, 7)
        ),
    )
    return ChanceRuleset(
        code="sicbo-like",
        revision=1,
        outcomes=outcomes,
        rules=rules,
    )


def _dice_code(first: int, second: int, third: int) -> str:
    """@brief 编码三枚骰子的有序结果 / Encode an ordered three-die outcome.

    @param first 第一枚骰子 / First die.
    @param second 第二枚骰子 / Second die.
    @param third 第三枚骰子 / Third die.
    @return 稳定且符合规则编码语法的结果编码 / Stable outcome code matching rule-code syntax.
    """

    return f"dice-{first}-{second}-{third}"
