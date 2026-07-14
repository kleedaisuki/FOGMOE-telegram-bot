"""@brief 银行领域模型测试 / Banking domain-model tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from fogmoe_bot.domain.banking.ledger import (
    AccountScope,
    LedgerAccount,
    LedgerEntry,
    LedgerPosting,
    LedgerReason,
)
from fogmoe_bot.domain.banking.money import (
    SystemAccountKind,
    TokenAmount,
    TokenBucket,
    WalletBalance,
)
from fogmoe_bot.domain.banking.requests import TokenRequest, TokenRequestStatus
from fogmoe_bot.infrastructure.database.banking import derive_bank_entry_key


def test_token_value_objects_reject_invalid_amounts_and_preserve_wallet_scope() -> None:
    """@brief 金额和钱包拒绝非法值 / Amounts and wallets reject invalid values."""

    with pytest.raises(ValueError, match="positive"):
        TokenAmount(0)
    with pytest.raises(TypeError, match="integer"):
        TokenAmount(True)
    with pytest.raises(ValueError, match="negative"):
        WalletBalance(TokenBucket.FREE, -1)

    balance = WalletBalance(TokenBucket.FREE, 7)
    assert balance.can_cover(TokenAmount(7))
    assert not balance.can_cover(TokenAmount(8))


def test_ledger_transfer_is_balanced_and_prohibits_degenerate_entries() -> None:
    """@brief 标准转账守恒且拒绝退化分录 / Transfers conserve and reject degenerate entries."""

    now = datetime.now(UTC)
    source = LedgerAccount.user(42, TokenBucket.FREE)
    destination = LedgerAccount.group_treasury(-100_42)
    entry = LedgerEntry.transfer(
        entry_id=uuid4(),
        idempotency_key="test:bank:contribution:1",
        reason=LedgerReason.GROUP_CONTRIBUTION,
        source=source,
        destination=destination,
        amount=TokenAmount(5),
        created_at=now,
        actor_id=42,
        metadata={"project": "lighthouse"},
    )

    assert sum(posting.delta for posting in entry.postings) == 0
    assert entry.postings[0] == LedgerPosting(source, -5)
    assert entry.postings[1] == LedgerPosting(destination, 5)
    with pytest.raises(ValueError, match="must differ"):
        LedgerEntry.transfer(
            entry_id=uuid4(),
            idempotency_key="test:bank:self:1",
            reason=LedgerReason.USER_TRANSFER,
            source=source,
            destination=source,
            amount=TokenAmount(1),
            created_at=now,
        )
    with pytest.raises(ValueError, match="balance"):
        LedgerEntry(
            entry_id=uuid4(),
            idempotency_key="test:bank:unbalanced:1",
            reason=LedgerReason.BANK_ISSUANCE,
            postings=(
                LedgerPosting(source, 2),
                LedgerPosting(LedgerAccount.system(SystemAccountKind.ISSUANCE), -1),
            ),
            created_at=now,
        )


def test_token_request_has_one_way_terminal_transitions() -> None:
    """@brief 代币请求只允许一次终态转移 / Token request permits one terminal transition."""

    now = datetime.now(UTC)
    request = TokenRequest(
        request_id=uuid4(),
        requester_id=42,
        requested_amount=TokenAmount(20),
        requested_bucket=TokenBucket.FREE,
        purpose="参加群组灯塔修复活动",
        status=TokenRequestStatus.PENDING,
        requested_at=now,
    )
    approved = request.approve(
        reviewer_id=1,
        reviewed_at=now + timedelta(seconds=1),
        ledger_entry_id=uuid4(),
        note="活动奖励批准",
    )

    assert approved.status is TokenRequestStatus.APPROVED
    assert approved.version == 1
    with pytest.raises(ValueError, match="Only pending"):
        approved.reject(reviewer_id=1, reviewed_at=now + timedelta(seconds=2))
    with pytest.raises(ValueError, match="Only the token requester"):
        request.cancel(requester_id=43, cancelled_at=now + timedelta(seconds=1))
    with pytest.raises(ValueError, match="cannot review their own"):
        request.approve(
            reviewer_id=42,
            reviewed_at=now + timedelta(seconds=1),
            ledger_entry_id=uuid4(),
        )


def test_ledger_accounts_are_exhaustively_shaped_by_scope() -> None:
    """@brief 账户范围决定唯一合法形状 / Account scope determines its only valid shape."""

    assert LedgerAccount.system(SystemAccountKind.BURN).scope is AccountScope.SYSTEM
    with pytest.raises(ValueError, match="group treasury"):
        LedgerAccount(AccountScope.GROUP, owner_id=-100_42)


def test_derived_bank_entry_key_is_stable_and_bounded() -> None:
    """@brief 派生账本键隔离子动作且受长度上限约束 / Derived ledger keys isolate sub-actions and stay bounded."""

    source = "telegram:update:" + "x" * 240
    first = derive_bank_entry_key("personal-rpg-reward", source)
    second = derive_bank_entry_key("personal-rpg-reward", source)
    distinct = derive_bank_entry_key("personal-rpg-purchase", source)

    assert first == second
    assert first != distinct
    assert len(first) <= 200
    with pytest.raises(ValueError, match="non-blank"):
        derive_bank_entry_key("", source)
