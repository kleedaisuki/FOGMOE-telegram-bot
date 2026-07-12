"""@brief Crypto 纯领域规则测试 / Tests for pure Crypto domain rules."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from fogmoe_bot.domain.crypto import (
    Blockchain,
    CoinStake,
    ContractAddress,
    PredictionDirection,
    PriceQuote,
    SolanaWalletAddress,
    calculate_prediction_outcome,
)


def test_chain_aliases_normalize_without_transport_special_cases() -> None:
    """@brief 链别名收敛为四个领域值 / Chain aliases converge to four domain values."""

    assert Blockchain.parse("Solana") is Blockchain.SOLANA
    assert Blockchain.parse("ethereum") is Blockchain.ETHEREUM
    assert Blockchain.parse("BNB") is Blockchain.BSC
    assert Blockchain.parse("blast") is Blockchain.BLAST
    with pytest.raises(ValueError, match="Unsupported"):
        Blockchain.parse("bitcoin")


def test_addresses_and_prices_reject_ambiguous_or_unsafe_values() -> None:
    """@brief 地址与报价在领域边界拒绝路径注入和非有限数 / Addresses and quotes reject path injection and non-finite values at the domain boundary."""

    assert str(ContractAddress("0xABC123")) == "0xABC123"
    wallet = "5iz3epFDf9SKvLNHWQ42f4wMMrENaudE9eMkxfBLFd2n"
    assert str(SolanaWalletAddress(wallet)) == wallet
    with pytest.raises(ValueError):
        ContractAddress("../../admin")
    with pytest.raises(ValueError):
        SolanaWalletAddress("0" * 44)
    with pytest.raises(ValueError):
        PriceQuote(Decimal("NaN"))


@pytest.mark.parametrize(
    ("direction", "end_price", "correct", "reward"),
    [
        (PredictionDirection.UP, "101", True, 36),
        (PredictionDirection.UP, "99", False, 0),
        (PredictionDirection.DOWN, "99", True, 36),
        (PredictionDirection.DOWN, "100", True, 36),
    ],
)
def test_prediction_outcome_preserves_reward_and_tie_rules(
    direction: PredictionDirection,
    end_price: str,
    correct: bool,
    reward: int,
) -> None:
    """@brief 预测保持 80% 奖励与持平算 down 的旧语义 / Prediction preserves the 80% reward and tie-counts-as-down legacy semantics."""

    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    due_at = started_at + timedelta(minutes=10)
    outcome = calculate_prediction_outcome(
        request_key="test:prediction:1",
        user_id=1,
        chat_id=-100,
        direction=direction,
        amount=CoinStake(20),
        start_price=PriceQuote(Decimal("100")),
        end_price=PriceQuote(Decimal(end_price)),
        started_at=started_at,
        due_at=due_at,
        settled_at=due_at,
    )
    assert outcome.correct is correct
    assert outcome.reward == reward
