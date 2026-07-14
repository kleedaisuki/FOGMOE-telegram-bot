"""@brief 可验证负期望随机活动核心测试 / Tests for the verifiable negative-EV chance core."""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from typing import cast
from uuid import UUID

import pytest

from fogmoe_bot.application.chance.models import CommitChanceRound
from fogmoe_bot.application.chance.service import ChanceService, ServerSeedSource
from fogmoe_bot.domain.banking.money import TokenAmount
from fogmoe_bot.domain.chance.examples import sicbo_like_ruleset
from fogmoe_bot.domain.chance.fairness import (
    ClientSeed,
    FairnessSample,
    ServerSeed,
    commit_server_seed,
    sample_uniform_ticket,
    verify_fairness_proof,
)
from fogmoe_bot.domain.chance.money import FreeTokenStake
from fogmoe_bot.domain.chance.rounds import ChanceRound, ChanceSettlement
from fogmoe_bot.domain.chance.rules import ChanceRule
from fogmoe_bot.domain.chance.scope import GroupRoundScope, PersonalRoundScope


_ROUND_ID = UUID("00000000-0000-0000-0000-000000000123")
"""@brief 测试中稳定的轮次 UUID / Stable round UUID used in tests."""

_SERVER_SEED = ServerSeed(bytes(range(32)))
"""@brief 固定且仅用于测试的服务器种子 / Fixed server seed used only for tests."""


class _FixedSeeds:
    """@brief 返回一个固定种子的测试来源 / Test source returning one fixed seed."""

    def __init__(self, seed: ServerSeed) -> None:
        """@brief 保存固定种子 / Retain the fixed seed.

        @param seed 待返回的服务器种子 / Server seed to return.
        """

        self._seed = seed
        self.calls = 0

    def next_server_seed(self) -> ServerSeed:
        """@brief 返回固定服务器种子 / Return the fixed server seed.

        @return 固定服务器种子 / Fixed server seed.
        """

        self.calls += 1
        return self._seed


def _command(*, scope: PersonalRoundScope | GroupRoundScope) -> CommitChanceRound:
    """@brief 构造有效的承诺阶段命令 / Build a valid commitment-stage command.

    @param scope 个人或群组活动范围 / Personal or group activity scope.
    @return 有效承诺命令 / Valid commitment command.
    """

    return CommitChanceRound(
        round_id=_ROUND_ID,
        scope=scope,
        player_id=42,
        ruleset=sicbo_like_ruleset(),
        rule_code="big",
        stake=FreeTokenStake(100),
        nonce=7,
    )


def test_sicbo_like_quotes_use_fraction_math_and_are_strictly_negative_ev() -> None:
    """@brief 骰宝风格规则以 Fraction 定价且所有报价严格负期望 / Sic-Bo-like rules use Fractions and every quote is strictly negative EV."""

    ruleset = sicbo_like_ruleset()
    quote = ruleset.quote("big", FreeTokenStake(100))

    assert ruleset.total_weight == 216
    assert ruleset.win_probability("big") == Fraction(35, 72)
    assert quote.gross_payout.value == 195
    assert quote.expected_gross_payout == Fraction(2275, 24)
    assert quote.expected_net_change == Fraction(-125, 24)
    assert quote.expected_net_change < 0
    assert quote.effective_house_edge == Fraction(5, 96)
    assert quote.effective_house_edge >= quote.configured_house_edge

    for rule in ruleset.rules:
        for amount in (1, 5, 100, 1_000):
            assert ruleset.quote(rule.code, FreeTokenStake(amount)).expected_net_change < 0

    with pytest.raises(ValueError, match="strictly between"):
        ChanceRule("broken", frozenset({"dice-1-1-1"}), Fraction(0, 1))


def test_every_telegram_exposed_regular_and_high_variance_rule_has_negative_ev() -> None:
    """@brief 所有 Telegram 公开骰宝规则（含高方差围骰）都保持严格负 EV /
    Every Telegram-exposed Sic-Bo rule, including high-variance triples, retains strictly negative EV.

    @return None / None.
    """

    ruleset = sicbo_like_ruleset()
    exposed_rules = (
        "big",
        "small",
        "odd",
        "even",
        "any-triple",
        "triple-1",
        "triple-2",
        "triple-3",
        "triple-4",
        "triple-5",
        "triple-6",
    )

    quotes = tuple(ruleset.quote(rule_code, FreeTokenStake(1)) for rule_code in exposed_rules)

    assert all(quote.expected_net_change < 0 for quote in quotes)
    assert all(quote.configured_house_edge > 0 for quote in quotes)
    assert quotes[-1].win_probability == Fraction(1, 216)
    assert quotes[-1].gross_payout.value > quotes[0].gross_payout.value


def test_round_types_exclude_paid_or_generic_assets_and_preserve_scope_boundary() -> (
    None
):
    """@brief 轮次只收免费押注，个人与群组范围不可混淆 / Rounds accept only free stakes and do not confuse personal and group scope."""

    ruleset = sicbo_like_ruleset()
    commitment = commit_server_seed(_SERVER_SEED)
    with pytest.raises(ValueError, match="only be played by their owner"):
        ChanceRound(
            round_id=_ROUND_ID,
            scope=PersonalRoundScope(7),
            player_id=42,
            ruleset=ruleset,
            rule_code="big",
            stake=FreeTokenStake(5),
            commitment=commitment,
            client_seed=ClientSeed("scope-test"),
            nonce=0,
        )
    with pytest.raises(TypeError, match="FreeTokenStake"):
        ChanceRound(
            round_id=_ROUND_ID,
            scope=GroupRoundScope(-100_42, topic_id=9),
            player_id=42,
            ruleset=ruleset,
            rule_code="big",
            stake=cast(FreeTokenStake, TokenAmount(5)),
            commitment=commitment,
            client_seed=ClientSeed("paid-boundary-test"),
            nonce=0,
        )

    group_round = ChanceRound(
        round_id=_ROUND_ID,
        scope=GroupRoundScope(-100_42, topic_id=9),
        player_id=42,
        ruleset=ruleset,
        rule_code="big",
        stake=FreeTokenStake(5),
        commitment=commitment,
        client_seed=ClientSeed("group-test"),
        nonce=0,
    )
    assert group_round.scope.kind.value == "group"
    assert not hasattr(group_round.stake, "bucket")


def test_commit_then_client_seed_then_settlement_produces_a_verifiable_proof() -> (
    None
):
    """@brief 承诺先于客户端种子，结算生成可独立复验的证明 / Commitment precedes client seed and settlement creates an independently verifiable proof."""

    seeds = _FixedSeeds(_SERVER_SEED)
    service = ChanceService(cast(ServerSeedSource, seeds))
    committed = service.commit(_command(scope=PersonalRoundScope(42)))

    assert seeds.calls == 1
    assert committed.committed_round.commitment == commit_server_seed(_SERVER_SEED)
    assert committed.committed_round.ruleset_fingerprint == sicbo_like_ruleset().fingerprint
    assert "redacted" in repr(committed.server_seed)

    prepared = service.bind_client_seed(committed, ClientSeed("Klee-seed-v1"))
    settlement = service.settle(prepared)

    assert settlement.proof.verifies()
    assert verify_fairness_proof(settlement.proof)
    assert settlement.outcome == prepared.round.ruleset.outcome_for_ticket(
        settlement.proof.sample.ticket
    )
    assert settlement.credited in {0, prepared.round.quote.gross_payout.value}
    assert settlement.won is (settlement.credited > 0)
    with pytest.raises(ValueError, match="does not match"):
        prepared.round.settle(ServerSeed(b"x" * 32))

    changed_ticket = (settlement.proof.sample.ticket + 1) % settlement.proof.upper_bound
    tampered_proof = replace(
        settlement.proof,
        sample=FairnessSample(
            ticket=changed_ticket,
            attempt=settlement.proof.sample.attempt,
            digest_hex=settlement.proof.sample.digest_hex,
        ),
    )
    assert not verify_fairness_proof(tampered_proof)
    with pytest.raises(ValueError, match="invalid fairness proof"):
        ChanceSettlement(
            round=prepared.round,
            outcome=prepared.round.ruleset.outcome_for_ticket(changed_ticket),
            proof=tampered_proof,
            payout=None,
        )


def test_rejection_sampling_retries_instead_of_using_modulo_biased_digest() -> None:
    """@brief 拒绝路径会重试，避免直接取模偏差 / Rejection path retries instead of applying biased direct modulo."""

    upper_bound = (1 << 255) + 1
    sample = sample_uniform_ticket(
        server_seed=ServerSeed(b"\x02" * 32),
        round_id=_ROUND_ID,
        client_seed=ClientSeed("rejection-test"),
        nonce=0,
        upper_bound=upper_bound,
    )

    assert sample.attempt == 3
    assert 0 <= sample.ticket < upper_bound
