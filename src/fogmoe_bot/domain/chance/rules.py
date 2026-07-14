"""@brief 精确赔率与负期望规则 / Exact-odds and negative-expectation rules."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import hashlib
import json
import re
from types import MappingProxyType
from typing import Final, Mapping

from .fairness import MAX_UNIFORM_BOUND
from .money import FreeTokenPayout, FreeTokenStake


RULESET_FINGERPRINT_DOMAIN: Final[bytes] = b"fogmoe/chance/ruleset/v1\x00"
"""@brief 规则集指纹的域分离前缀 / Domain-separation prefix for ruleset fingerprints."""

_CODE_PATTERN: Final[re.Pattern[str]] = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
"""@brief 稳定规则编码的受限语法 / Restricted syntax for stable rule codes."""


@dataclass(frozen=True, slots=True)
class ChanceOutcome:
    """@brief 离散随机结果及其整数权重 / Discrete random outcome and its integer weight.

    @param code 在同一规则集中唯一的稳定结果编码 / Stable outcome code unique within one ruleset.
    @param weight 正整数离散权重 / Positive integral discrete weight.
    """

    code: str
    """@brief 稳定结果编码 / Stable outcome code."""

    weight: int
    """@brief 正整数权重 / Positive integral weight."""

    def __post_init__(self) -> None:
        """@brief 校验离散结果 / Validate a discrete outcome.

        @return None / None.
        @raise ValueError 编码或权重非法时抛出 / Raised when code or weight is invalid.
        """

        _validate_code(self.code, label="Chance outcome")
        if isinstance(self.weight, bool) or not isinstance(self.weight, int):
            raise ValueError("Chance outcome weight must be an integer")
        if self.weight <= 0:
            raise ValueError("Chance outcome weight must be positive")


@dataclass(frozen=True, slots=True)
class ChanceRule:
    """@brief 一个以结果集合定义的可下注规则 / One wagerable rule defined by a set of outcomes.

    @param code 规则集内唯一的稳定规则编码 / Stable rule code unique within a ruleset.
    @param winning_outcome_codes 触发胜利的结果编码集合 / Outcome-code set that triggers a win.
    @param house_edge 严格介于 0 与 1 的精确庄家优势 / Exact house edge strictly between 0 and 1.
    """

    code: str
    """@brief 稳定规则编码 / Stable rule code."""

    winning_outcome_codes: frozenset[str]
    """@brief 中奖结果集合 / Winning outcome set."""

    house_edge: Fraction
    """@brief 精确庄家优势 / Exact house edge."""

    def __post_init__(self) -> None:
        """@brief 校验规则自身不变量 / Validate rule-local invariants.

        @return None / None.
        @raise TypeError 庄家优势不是 Fraction 时抛出 / Raised when the house edge is not a Fraction.
        @raise ValueError 编码、中奖集或庄家优势非法时抛出 /
            Raised when code, winning set, or house edge is invalid.
        """

        _validate_code(self.code, label="Chance rule")
        normalized_winners = frozenset(self.winning_outcome_codes)
        if not normalized_winners:
            raise ValueError("Chance rule needs at least one winning outcome")
        for outcome_code in normalized_winners:
            _validate_code(outcome_code, label="Winning outcome")
        if not isinstance(self.house_edge, Fraction):
            raise TypeError("Chance house edge must be fractions.Fraction")
        if not Fraction(0, 1) < self.house_edge < Fraction(1, 1):
            raise ValueError("Chance house edge must be strictly between zero and one")
        object.__setattr__(self, "winning_outcome_codes", normalized_winners)


@dataclass(frozen=True, slots=True)
class ChanceQuote:
    """@brief 一笔押注的精确赔率报价 / Exact-odds quote for one stake.

    对胜率 ``p``、押注 ``A`` 与庄家优势 ``h``，总派彩为
    ``floor(A * (1 - h) / p)``。其中 ``0 < h < 1``。因下取整只会降低派彩，
    所以 ``E[net] = p * payout - A <= -h * A < 0``。
    For win probability ``p``, stake ``A``, and edge ``h``, gross payout is
    ``floor(A * (1 - h) / p)``. With ``0 < h < 1``, flooring can only reduce the
    payout, so ``E[net] = p * payout - A <= -h * A < 0``.

    @param ruleset_code 规则集稳定编码 / Stable ruleset code.
    @param ruleset_revision 规则集不可变版本 / Immutable ruleset revision.
    @param rule_code 被选择规则的稳定编码 / Stable code of the selected rule.
    @param stake 玩家免费金币押注 / Player's free-token stake.
    @param win_probability 精确胜率 / Exact win probability.
    @param configured_house_edge 规则配置的庄家优势 / Configured rule house edge.
    @param gross_payout 胜利时返还的总派彩 / Gross payout returned on a win.
    @param expected_gross_payout 精确期望总派彩 / Exact expected gross payout.
    @param expected_net_change 精确期望净变化 / Exact expected net change.
    """

    ruleset_code: str
    """@brief 规则集稳定编码 / Stable ruleset code."""

    ruleset_revision: int
    """@brief 规则集不可变版本 / Immutable ruleset revision."""

    rule_code: str
    """@brief 选中规则编码 / Selected rule code."""

    stake: FreeTokenStake
    """@brief 免费金币押注 / Free-token stake."""

    win_probability: Fraction
    """@brief 精确胜率 / Exact win probability."""

    configured_house_edge: Fraction
    """@brief 配置的精确庄家优势 / Configured exact house edge."""

    gross_payout: FreeTokenPayout
    """@brief 胜利时含本金的总派彩 / Gross payout including stake on a win."""

    expected_gross_payout: Fraction
    """@brief 精确期望总派彩 / Exact expected gross payout."""

    expected_net_change: Fraction
    """@brief 精确期望净变化 / Exact expected net change."""

    def __post_init__(self) -> None:
        """@brief 校验报价仍是严格负期望 / Validate that the quote remains strictly negative EV.

        @return None / None.
        @raise TypeError 精确概率字段不是 Fraction 时抛出 /
            Raised when exact probability fields are not Fractions.
        @raise ValueError 报价违反概率或期望不变量时抛出 /
            Raised when the quote violates probability or expectation invariants.
        """

        _validate_code(self.ruleset_code, label="Chance ruleset")
        _validate_code(self.rule_code, label="Chance rule")
        if (
            isinstance(self.ruleset_revision, bool)
            or not isinstance(self.ruleset_revision, int)
            or self.ruleset_revision <= 0
        ):
            raise ValueError("Chance ruleset revision must be positive")
        if not isinstance(self.stake, FreeTokenStake):
            raise TypeError("Chance quote requires FreeTokenStake")
        if not isinstance(self.gross_payout, FreeTokenPayout):
            raise TypeError("Chance quote requires FreeTokenPayout")
        for fraction, label in (
            (self.win_probability, "win probability"),
            (self.configured_house_edge, "house edge"),
            (self.expected_gross_payout, "expected gross payout"),
            (self.expected_net_change, "expected net change"),
        ):
            if not isinstance(fraction, Fraction):
                raise TypeError(f"Chance {label} must be fractions.Fraction")
        if not Fraction(0, 1) < self.win_probability <= Fraction(1, 1):
            raise ValueError("Chance win probability must fall in (0, 1]")
        if not Fraction(0, 1) < self.configured_house_edge < Fraction(1, 1):
            raise ValueError("Chance house edge must fall in (0, 1)")
        expected_gross = self.win_probability * self.gross_payout.value
        if self.expected_gross_payout != expected_gross:
            raise ValueError("Chance expected gross payout does not match its odds")
        expected_net = expected_gross - self.stake.value
        if self.expected_net_change != expected_net:
            raise ValueError("Chance expected net change does not match its odds")
        if self.expected_net_change >= 0:
            raise ValueError("Chance quote must have strictly negative expected value")

    @property
    def effective_house_edge(self) -> Fraction:
        """@brief 返回下取整后的实际庄家优势 / Return the effective edge after flooring.

        @return 不小于配置优势的精确实际优势 / Exact effective edge no smaller than the configured edge.
        """

        return -self.expected_net_change / self.stake.value


@dataclass(frozen=True, slots=True)
class ChanceRuleset:
    """@brief 可审计的离散随机规则集 / Auditable discrete chance ruleset.

    规则集用有限整数权重空间表达概率，并把每个可下注规则映射到一个获胜结果集合。
    所有赔率均由 ``quote`` 统一计算；调用方不能自行提供派彩倍数。
    A ruleset expresses probability through a finite integer-weight space and maps each
    wagerable rule to a winning-outcome set. ``quote`` is the only payout calculator;
    callers cannot supply their own payout multiplier.

    @param code 稳定规则集编码 / Stable ruleset code.
    @param revision 不可变规则集版本 / Immutable ruleset revision.
    @param outcomes 保持顺序的离散结果空间 / Ordered discrete outcome space.
    @param rules 可选规则集合 / Wagerable rule collection.
    """

    code: str
    """@brief 稳定规则集编码 / Stable ruleset code."""

    revision: int
    """@brief 不可变规则集版本 / Immutable ruleset revision."""

    outcomes: tuple[ChanceOutcome, ...]
    """@brief 保持顺序的离散结果空间 / Ordered discrete outcome space."""

    rules: tuple[ChanceRule, ...]
    """@brief 可下注规则集合 / Wagerable rule collection."""

    def __post_init__(self) -> None:
        """@brief 校验完整规则集 / Validate the complete ruleset.

        @return None / None.
        @raise TypeError 结果或规则类型不匹配时抛出 /
            Raised when outcome or rule types do not match.
        @raise ValueError 编码、版本、结果空间或规则引用非法时抛出 /
            Raised when code, revision, outcome space, or rule references are invalid.
        """

        _validate_code(self.code, label="Chance ruleset")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int):
            raise TypeError("Chance ruleset revision must be an integer")
        if self.revision <= 0:
            raise ValueError("Chance ruleset revision must be positive")
        normalized_outcomes = tuple(self.outcomes)
        normalized_rules = tuple(self.rules)
        if not normalized_outcomes:
            raise ValueError("Chance ruleset needs at least one outcome")
        if not normalized_rules:
            raise ValueError("Chance ruleset needs at least one rule")
        if not all(isinstance(outcome, ChanceOutcome) for outcome in normalized_outcomes):
            raise TypeError("Chance ruleset outcomes must be ChanceOutcome instances")
        if not all(isinstance(rule, ChanceRule) for rule in normalized_rules):
            raise TypeError("Chance ruleset rules must be ChanceRule instances")
        outcome_codes = tuple(outcome.code for outcome in normalized_outcomes)
        if len(set(outcome_codes)) != len(outcome_codes):
            raise ValueError("Chance ruleset outcome codes must be unique")
        rule_codes = tuple(rule.code for rule in normalized_rules)
        if len(set(rule_codes)) != len(rule_codes):
            raise ValueError("Chance ruleset rule codes must be unique")
        total_weight = sum(outcome.weight for outcome in normalized_outcomes)
        if total_weight > MAX_UNIFORM_BOUND:
            raise ValueError("Chance ruleset weight exceeds the fairness-protocol bound")
        known_outcomes = frozenset(outcome_codes)
        for rule in normalized_rules:
            unknown_outcomes = rule.winning_outcome_codes - known_outcomes
            if unknown_outcomes:
                raise ValueError(
                    "Chance rule references outcomes outside its ruleset: "
                    + ", ".join(sorted(unknown_outcomes))
                )
        object.__setattr__(self, "outcomes", normalized_outcomes)
        object.__setattr__(self, "rules", normalized_rules)

    @property
    def total_weight(self) -> int:
        """@brief 返回离散结果空间总权重 / Return total weight of the outcome space.

        @return 正整数总权重 / Positive integral total weight.
        """

        return sum(outcome.weight for outcome in self.outcomes)

    @property
    def fingerprint(self) -> str:
        """@brief 返回规则与顺序均绑定的稳定指纹 / Return a stable fingerprint binding rules and ordering.

        @return SHA-256 规则集指纹 / SHA-256 ruleset fingerprint.
        """

        payload = {
            "code": self.code,
            "revision": self.revision,
            "outcomes": [
                {"code": outcome.code, "weight": outcome.weight}
                for outcome in self.outcomes
            ],
            "rules": [
                {
                    "code": rule.code,
                    "winning_outcomes": sorted(rule.winning_outcome_codes),
                    "house_edge": [
                        rule.house_edge.numerator,
                        rule.house_edge.denominator,
                    ],
                }
                for rule in self.rules
            ],
        }
        encoded_payload = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(RULESET_FINGERPRINT_DOMAIN + encoded_payload).hexdigest()

    def rule_for(self, rule_code: str) -> ChanceRule:
        """@brief 按稳定编码获取下注规则 / Look up a wager rule by stable code.

        @param rule_code 规则编码 / Rule code.
        @return 匹配的下注规则 / Matching wager rule.
        @raise ValueError 规则不存在时抛出 / Raised when the rule does not exist.
        """

        _validate_code(rule_code, label="Chance rule")
        for rule in self.rules:
            if rule.code == rule_code:
                return rule
        raise ValueError(f"Unknown chance rule: {rule_code}")

    def win_probability(self, rule_code: str) -> Fraction:
        """@brief 计算规则的精确胜率 / Compute a rule's exact win probability.

        @param rule_code 规则编码 / Rule code.
        @return 不经过浮点运算的精确胜率 / Exact win probability without floating point.
        """

        rule = self.rule_for(rule_code)
        winning_weight = sum(
            outcome.weight
            for outcome in self.outcomes
            if outcome.code in rule.winning_outcome_codes
        )
        return Fraction(winning_weight, self.total_weight)

    def quote(self, rule_code: str, stake: FreeTokenStake) -> ChanceQuote:
        """@brief 计算严格负期望的总派彩报价 / Calculate a strictly negative-EV gross-payout quote.

        @param rule_code 规则编码 / Rule code.
        @param stake 只能为免费金币押注 / Free-token-only stake.
        @return 包含精确期望值的报价 / Quote containing exact expected value.
        @raise TypeError 押注不是 FreeTokenStake 时抛出 /
            Raised when stake is not FreeTokenStake.
        @raise ValueError 小额押注下派彩为零时抛出 /
            Raised when a small stake would produce a zero payout.
        """

        if not isinstance(stake, FreeTokenStake):
            raise TypeError("Chance rules only accept FreeTokenStake")
        rule = self.rule_for(rule_code)
        probability = self.win_probability(rule.code)
        raw_payout = Fraction(stake.value, 1) * (1 - rule.house_edge) / probability
        gross_payout_value = raw_payout.numerator // raw_payout.denominator
        if gross_payout_value <= 0:
            raise ValueError(
                "Chance stake is too small for a positive payout at this house edge"
            )
        gross_payout = FreeTokenPayout(gross_payout_value)
        expected_gross = probability * gross_payout.value
        expected_net = expected_gross - stake.value
        if expected_net >= 0:
            raise AssertionError("Negative-EV payout formula unexpectedly produced nonnegative EV")
        return ChanceQuote(
            ruleset_code=self.code,
            ruleset_revision=self.revision,
            rule_code=rule.code,
            stake=stake,
            win_probability=probability,
            configured_house_edge=rule.house_edge,
            gross_payout=gross_payout,
            expected_gross_payout=expected_gross,
            expected_net_change=expected_net,
        )

    def outcome_for_ticket(self, ticket: int) -> ChanceOutcome:
        """@brief 将无偏整数票映射到离散结果 / Map an unbiased integer ticket to a discrete outcome.

        @param ticket ``[0, total_weight)`` 内的无偏整数 / Unbiased integer in ``[0, total_weight)``.
        @return 唯一对应的离散结果 / The uniquely corresponding discrete outcome.
        @raise ValueError 票不在规则集范围内时抛出 / Raised when the ticket is outside ruleset bounds.
        """

        if isinstance(ticket, bool) or not isinstance(ticket, int):
            raise ValueError("Chance ticket must be an integer")
        if ticket < 0 or ticket >= self.total_weight:
            raise ValueError("Chance ticket falls outside the ruleset weight range")
        cursor = 0
        for outcome in self.outcomes:
            cursor += outcome.weight
            if ticket < cursor:
                return outcome
        raise AssertionError("Validated chance ticket did not map to an outcome")

    def outcome_weights(self) -> Mapping[str, int]:
        """@brief 返回不可变的结果权重视图 / Return an immutable outcome-weight view.

        @return 编码到正整数权重的只读映射 / Read-only mapping from code to positive integer weight.
        """

        return MappingProxyType(
            {outcome.code: outcome.weight for outcome in self.outcomes}
        )


def _validate_code(value: str, *, label: str) -> None:
    """@brief 校验稳定规则编码 / Validate a stable rule code.

    @param value 待校验编码 / Code to validate.
    @param label 用于错误说明的实体名称 / Entity label used in error messages.
    @return None / None.
    @raise ValueError 编码不符合稳定协议语法时抛出 /
        Raised when the code does not meet stable protocol syntax.
    """

    if not isinstance(value, str) or _CODE_PATTERN.fullmatch(value) is None:
        raise ValueError(
            f"{label} code must match [a-z][a-z0-9_.-]{{0,63}}"
        )
