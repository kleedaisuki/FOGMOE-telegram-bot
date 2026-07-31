"""@brief PostgreSQL durable inbox adapter 测试 / PostgreSQL durable-inbox adapter tests."""

import asyncio
from datetime import timedelta
from typing import Any

from conversation_workflow_testkit import (
    NOW,
    _inbound_claim_row,
    _inbound_row,
    _TransactionContext,
)

from fogmoe_bot.domain.conversation.identity import LeaseToken
from fogmoe_bot.domain.conversation.inbox import InboxClaim, InboxFailure
from fogmoe_bot.infrastructure.database import db
from fogmoe_bot.infrastructure.database.conversation_workflow import (
    inbox as inbox_repository,
)
from fogmoe_bot.infrastructure.database.conversation_workflow.inbox import (
    PostgresInboxRepository,
)


def test_claim_inbound_preserves_conversation_order_across_workers(
    monkeypatch: Any,
) -> None:
    """@brief inbox 领取用会话头部谓词保持跨 worker 顺序 / Inbox claim uses a conversation-head predicate to preserve cross-worker order."""

    connection = object()
    captured: dict[str, object] = {}

    async def fake_fetch_all(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> list[tuple[object, ...]]:
        """@brief 捕获 inbox 领取 SQL / Capture inbox claim SQL."""

        captured["sql"] = sql
        captured["connection"] = connection
        return [_inbound_claim_row()]

    monkeypatch.setattr(
        db,
        "transaction",
        lambda: _TransactionContext(connection),
    )
    monkeypatch.setattr(db, "fetch_all", fake_fetch_all)

    claims = asyncio.run(
        PostgresInboxRepository().claim_inbound(
            now=NOW,
            limit=5,
            lease_for=timedelta(seconds=30),
        )
    )

    assert len(claims) == 1
    sql = str(captured["sql"])
    assert "FOR UPDATE OF candidate SKIP LOCKED" in sql
    assert "earlier.conversation_id = candidate.conversation_id" in sql
    assert "earlier.update_id < candidate.update_id" in sql
    assert captured["connection"] is connection


def test_fail_inbound_uses_fencing_token_and_explicit_failure_time(
    monkeypatch: Any,
) -> None:
    """@brief inbox 最终失败受 token 防护并使用显式时间 / Inbox final failure is fenced and uses an explicit timestamp."""

    captured: dict[str, object] = {}

    async def fake_execute(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object | None = None,
    ) -> int:
        """@brief 捕获最终失败更新 / Capture the final-failure update."""

        captured["sql"] = sql
        captured["params"] = params
        return 1

    monkeypatch.setattr(db, "execute", fake_execute)
    token = LeaseToken.new()
    item = inbox_repository._map_inbox_item(_inbound_row())
    claim = InboxClaim.from_processing(
        item,
        token=token,
        lease_expires_at=NOW + timedelta(seconds=30),
    )
    failed_at = NOW + timedelta(seconds=2)
    decision = item.dead_letter(
        claim,
        failed_at=failed_at,
        failure=InboxFailure("invalid update payload"),
    )

    asyncio.run(
        PostgresInboxRepository().dead_letter_inbound(decision)
    )

    assert "status = 'failed_final'" in str(captured["sql"])
    assert "version = %s AND claim_token" in str(captured["sql"])
    assert "claim_token = CAST(%s AS UUID)" in str(captured["sql"])
    params = captured["params"]
    assert isinstance(params, tuple)
    assert params[1] == failed_at
    assert params[-2] == claim.expected_version
    assert params[-1] == str(token)


def test_lease_recovery_preserves_attempts_and_matches_domain_retry_shape(
    monkeypatch: Any,
) -> None:
    """@brief lease recovery 只推进版本并令 next=updated=now / Lease recovery advances only the version and sets next=updated=now.

    @param monkeypatch pytest patch 工具 / Pytest patch helper.
    @return None / None.
    """

    captured: dict[str, object] = {}

    async def fake_execute(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object | None = None,
    ) -> int:
        """@brief 捕获 lease recovery SQL / Capture lease-recovery SQL."""

        del connection
        captured["sql"] = sql
        captured["params"] = params
        return 3

    monkeypatch.setattr(db, "execute", fake_execute)

    recovered = asyncio.run(
        PostgresInboxRepository().recover_expired_inbound_leases(now=NOW)
    )

    assert recovered == 3
    sql = str(captured["sql"])
    assert "status = 'retry_wait'" in sql
    assert "version = version + 1" in sql
    assert "attempt_count" not in sql
    assert "next_attempt_at = %s" in sql
    assert "updated_at = %s" in sql
    assert "COALESCE(last_error, 'recovered expired worker lease')" in sql
    assert captured["params"] == (NOW, NOW, NOW)
