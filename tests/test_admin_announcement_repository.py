"""@brief Admin announcement PostgreSQL adapter 契约测试 / Admin announcement PostgreSQL-adapter contract tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any
from uuid import UUID

import pytest

from fogmoe_bot.domain.admin.announcement import (
    AnnouncementDeliveryCounts,
    AnnouncementDispatchContent,
    AnnouncementId,
)
from fogmoe_bot.domain.admin.recipient import (
    AnnouncementClaimToken,
    AnnouncementFailureCategory,
    AnnouncementRecipient,
    AnnouncementRecipientClaim,
    AnnouncementRecipientKind,
    AnnouncementRecipientStatus,
)
from fogmoe_bot.domain.conversation.identity import OutboundMessageId
from fogmoe_bot.infrastructure.admin.announcements import (
    PostgresAdminAnnouncementOperations,
)
from fogmoe_bot.infrastructure.database import db

NOW = datetime(2030, 1, 1, tzinfo=UTC)
"""@brief 固定 repository 测试时刻 / Fixed repository-test instant."""

ANNOUNCEMENT_ID = AnnouncementId(UUID("00000000-0000-0000-0000-000000000311"))
"""@brief 固定公告 ID / Fixed announcement ID."""

TOKEN = AnnouncementClaimToken(UUID("00000000-0000-0000-0000-000000000322"))
"""@brief 固定 CAS token / Fixed CAS token."""

OUTBOUND_ID = OutboundMessageId.parse(UUID("00000000-0000-0000-0000-000000000333"))
"""@brief 固定 outbox ID / Fixed outbox ID."""


class _Transaction:
    """@brief 最小 async transaction context / Minimal async transaction context."""

    def __init__(self) -> None:
        """@brief 创建唯一连接身份 / Create a unique connection identity."""

        self.connection = object()
        """@brief fake connection 身份 / Fake connection identity."""
        self.exit_exception: type[BaseException] | None = None
        """@brief 事务退出时观察到的异常类型 / Exception type observed at transaction exit."""

    async def __aenter__(self) -> object:
        """@brief 进入事务 / Enter the transaction.

        @return fake connection / Fake connection.
        """

        return self.connection

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        """@brief 退出且不吞异常 / Exit without suppressing errors.

        @return False / False.
        """

        self.exit_exception = exc_type
        del exc, traceback
        return False


def _recipient_row(
    *,
    status: AnnouncementRecipientStatus,
    attempt_count: int,
    updated_at: datetime,
    kind: AnnouncementRecipientKind = AnnouncementRecipientKind.USER,
    message_thread_id: int | None = None,
    reply_to_message_id: int | None = None,
    next_attempt_at: datetime | None = None,
    token: UUID | None = None,
    lease_expires_at: datetime | None = None,
    outbound_message_id: UUID | None = None,
    last_error: str | None = None,
    expanded_at: datetime | None = None,
    terminal_at: datetime | None = None,
    chat_id: int = 42,
) -> tuple[object, ...]:
    """@brief 构造 repository 的十六列 recipient 行 / Build the repository's sixteen-column recipient row.

    @return 固定列序数据库行 / Database row in fixed column order.
    """

    return (
        str(ANNOUNCEMENT_ID),
        kind.value,
        chat_id,
        message_thread_id,
        reply_to_message_id,
        status.value,
        attempt_count,
        next_attempt_at,
        token,
        lease_expires_at,
        outbound_message_id,
        last_error,
        NOW,
        updated_at,
        expanded_at,
        terminal_at,
    )


def _claim_row() -> tuple[object, ...]:
    """@brief 构造 pending pre-claim JOIN 行 / Build a pending pre-claim JOIN row.

    @return recipient 行加公告内容 / Recipient row plus announcement content.
    """

    return _recipient_row(
        status=AnnouncementRecipientStatus.PENDING,
        attempt_count=0,
        next_attempt_at=NOW,
        updated_at=NOW,
    ) + ("hello", 1, NOW, 0, 0)


def _domain_claim() -> AnnouncementRecipientClaim:
    """@brief 构造固定 token 的领域 claim / Build a domain claim with the fixed token.

    @return 领取能力 / Claim capability.
    """

    recipient = AnnouncementRecipient.restore(
        announcement_id=ANNOUNCEMENT_ID,
        recipient_kind=AnnouncementRecipientKind.USER,
        chat_id=42,
        message_thread_id=None,
        reply_to_message_id=None,
        status=AnnouncementRecipientStatus.PENDING,
        attempt_count=0,
        next_attempt_at=NOW,
        claim_token=None,
        lease_expires_at=None,
        outbound_message_id=None,
        last_error=None,
        created_at=NOW,
        updated_at=NOW,
        expanded_at=None,
        terminal_at=None,
    )
    return recipient.claim(
        token=TOKEN,
        claimed_at=NOW,
        lease_expires_at=NOW + timedelta(minutes=1),
        content=AnnouncementDispatchContent(
            body="hello",
            counts=AnnouncementDeliveryCounts(1, 0, 0),
            announcement_created_at=NOW,
        ),
    )


def _processing_row(*, attempt_count: int = 1) -> tuple[object, ...]:
    """@brief 构造固定 claim 对应的 processing 行 / Build the processing row represented by the fixed claim.

    @param attempt_count 当前尝试数 / Current attempt count.
    @return 完整 processing 持久化形状 / Complete processing persistence shape.
    """

    return _recipient_row(
        status=AnnouncementRecipientStatus.PROCESSING,
        attempt_count=attempt_count,
        token=TOKEN.value,
        lease_expires_at=NOW + timedelta(minutes=1),
        updated_at=NOW,
    )


def test_claim_hydrates_pre_and_post_state_under_existing_lock_order(
    monkeypatch: Any,
) -> None:
    """@brief claim 在既有锁序下验证 pre/post 领域转换 / Claim validates pre/post domain transition under the established lock order."""

    transaction = _Transaction()
    captured: dict[str, object] = {}

    async def fetch_all(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> list[tuple[object, ...]]:
        """@brief 返回 pre-claim 行 / Return the pre-claim row."""

        captured["select_sql"] = sql
        captured["select_params"] = params
        assert connection is transaction.connection
        return [_claim_row()]

    async def fetch_one(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> tuple[object, ...]:
        """@brief 按 UPDATE token 返回 processing 后态 / Return the processing post-state using the UPDATE token."""

        captured["update_sql"] = sql
        captured["update_params"] = params
        assert connection is transaction.connection
        return _recipient_row(
            status=AnnouncementRecipientStatus.PROCESSING,
            attempt_count=1,
            token=UUID(str(params[0])),
            lease_expires_at=NOW + timedelta(minutes=1),
            updated_at=NOW,
        )

    monkeypatch.setattr(db, "transaction", lambda: transaction)
    monkeypatch.setattr(db, "fetch_all", fetch_all)
    monkeypatch.setattr(db, "fetch_one", fetch_one)

    claims = asyncio.run(
        PostgresAdminAnnouncementOperations().claim_ready(
            now=NOW,
            lease_for=timedelta(minutes=1),
            limit=3,
        )
    )

    assert len(claims) == 1 and claims[0].recipient.attempt_count == 1
    select_sql = str(captured["select_sql"])
    assert "FOR UPDATE OF recipient SKIP LOCKED" in select_sql
    assert "CASE recipient.recipient_kind WHEN 'completion' THEN 1 ELSE 0 END" in (
        select_sql
    )
    update_sql = str(captured["update_sql"])
    assert "RETURNING announcement_id" in update_sql
    assert "AND status = %s" in update_sql
    assert "version" not in update_sql.casefold()


def test_completion_promotion_restores_blocked_to_pending_post_state(
    monkeypatch: Any,
) -> None:
    """@brief promotion 锁定真实 blocked 前态并核对 pending 后态 / Promotion locks a real blocked pre-state and checks its pending post-state."""

    transaction = _Transaction()
    promoted_at = NOW + timedelta(minutes=3)
    select_calls: list[str] = []
    update_calls: list[str] = []

    async def fetch_all(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> list[tuple[object, ...]]:
        """@brief 返回已按既有锁序锁定的公告候选 / Return an announcement candidate under the established lock order."""

        select_calls.append(sql)
        assert connection is transaction.connection
        assert params == (4,)
        return [(str(ANNOUNCEMENT_ID),)]

    async def fetch_one(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> tuple[object, ...]:
        """@brief 依次提供 blocked 前态与 pending 后态 / Supply the blocked pre-state and pending post-state in sequence."""

        assert connection is transaction.connection
        if sql.lstrip().startswith("SELECT"):
            assert params[0] == str(ANNOUNCEMENT_ID)
            select_calls.append(sql)
            return _recipient_row(
                kind=AnnouncementRecipientKind.COMPLETION,
                message_thread_id=7,
                reply_to_message_id=9,
                status=AnnouncementRecipientStatus.BLOCKED,
                attempt_count=0,
                updated_at=NOW,
            )
        assert params[2] == str(ANNOUNCEMENT_ID)
        update_calls.append(sql)
        return _recipient_row(
            kind=AnnouncementRecipientKind.COMPLETION,
            message_thread_id=7,
            reply_to_message_id=9,
            status=AnnouncementRecipientStatus.PENDING,
            attempt_count=0,
            next_attempt_at=promoted_at,
            updated_at=promoted_at,
        )

    monkeypatch.setattr(db, "transaction", lambda: transaction)
    monkeypatch.setattr(db, "fetch_all", fetch_all)
    monkeypatch.setattr(db, "fetch_one", fetch_one)

    count = asyncio.run(
        PostgresAdminAnnouncementOperations().promote_delivery_completions(
            now=promoted_at,
            limit=4,
        )
    )

    assert count == 1
    assert len(select_calls) == 2 and len(update_calls) == 1
    assert "ORDER BY announcement.created_at, announcement.announcement_id" in (
        select_calls[0]
    )
    assert "FOR UPDATE OF announcement SKIP LOCKED" in select_calls[0]
    assert "completion.status = 'blocked' FOR UPDATE" in select_calls[1]
    assert "SET status = 'pending'" in update_calls[0]
    assert "RETURNING completion.announcement_id" in update_calls[0]


def test_completion_promotion_rolls_back_on_database_post_state_mismatch(
    monkeypatch: Any,
) -> None:
    """@brief promotion 的 SQL 后态偏离领域决策时事务失败 / Promotion fails its transaction when SQL post-state diverges from the domain decision."""

    transaction = _Transaction()
    promoted_at = NOW + timedelta(minutes=3)
    calls = 0

    async def fetch_all(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> list[tuple[object, ...]]:
        """@brief 返回单一公告候选 / Return one announcement candidate."""

        del sql, params
        assert connection is transaction.connection
        return [(str(ANNOUNCEMENT_ID),)]

    async def fetch_one(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> tuple[object, ...]:
        """@brief 返回合法 blocked 前态和错误时间的 pending 后态 / Return a valid blocked pre-state and a mistimed pending post-state."""

        nonlocal calls
        del sql, params
        assert connection is transaction.connection
        calls += 1
        if calls == 1:
            return _recipient_row(
                kind=AnnouncementRecipientKind.COMPLETION,
                message_thread_id=7,
                reply_to_message_id=9,
                status=AnnouncementRecipientStatus.BLOCKED,
                attempt_count=0,
                updated_at=NOW,
            )
        wrong_time = promoted_at + timedelta(seconds=1)
        return _recipient_row(
            kind=AnnouncementRecipientKind.COMPLETION,
            message_thread_id=7,
            reply_to_message_id=9,
            status=AnnouncementRecipientStatus.PENDING,
            attempt_count=0,
            next_attempt_at=wrong_time,
            updated_at=wrong_time,
        )

    monkeypatch.setattr(db, "transaction", lambda: transaction)
    monkeypatch.setattr(db, "fetch_all", fetch_all)
    monkeypatch.setattr(db, "fetch_one", fetch_one)

    with pytest.raises(RuntimeError, match="post-state disagrees"):
        asyncio.run(
            PostgresAdminAnnouncementOperations().promote_delivery_completions(
                now=promoted_at,
                limit=1,
            )
        )

    assert transaction.exit_exception is RuntimeError


def test_claim_rejects_database_post_state_that_disagrees_with_domain(
    monkeypatch: Any,
) -> None:
    """@brief SQL 后态 attempt 错误时 claim 快速失败 / Claim fails fast when SQL post-state attempt disagrees with the domain."""

    transaction = _Transaction()

    async def fetch_all(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> list[tuple[object, ...]]:
        """@brief 返回合法 pre-state / Return a valid pre-state."""

        del sql, params, connection
        return [_claim_row()]

    async def fetch_one(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> tuple[object, ...]:
        """@brief 返回错误增量的 post-state / Return a post-state with the wrong increment."""

        del sql, connection
        return _recipient_row(
            status=AnnouncementRecipientStatus.PROCESSING,
            attempt_count=2,
            token=UUID(str(params[0])),
            lease_expires_at=NOW + timedelta(minutes=1),
            updated_at=NOW,
        )

    monkeypatch.setattr(db, "transaction", lambda: transaction)
    monkeypatch.setattr(db, "fetch_all", fetch_all)
    monkeypatch.setattr(db, "fetch_one", fetch_one)

    with pytest.raises(RuntimeError, match="disagrees with domain"):
        asyncio.run(
            PostgresAdminAnnouncementOperations().claim_ready(
                now=NOW,
                lease_for=timedelta(minutes=1),
                limit=1,
            )
        )


def test_stale_expanded_decision_returns_false_using_token_only_cas(
    monkeypatch: Any,
) -> None:
    """@brief stale token 返回 False 且 SQL 不虚构 version / A stale token returns False and SQL invents no version."""

    captured: dict[str, object] = {}
    transaction = _Transaction()

    async def fetch_one(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> None:
        """@brief 捕获锁定查询并模拟 stale token / Capture the locking query and simulate a stale token."""

        captured["sql"] = sql
        captured["params"] = params
        assert connection is transaction.connection
        return None

    monkeypatch.setattr(db, "transaction", lambda: transaction)
    monkeypatch.setattr(db, "fetch_one", fetch_one)
    decision = _domain_claim().expand(
        outbound_message_id=OUTBOUND_ID,
        completed_at=NOW + timedelta(seconds=1),
    )

    result = asyncio.run(
        PostgresAdminAnnouncementOperations().persist_expanded(decision)
    )

    assert result is False
    sql = str(captured["sql"])
    assert "status = 'processing'" in sql
    assert "claim_token = CAST(%s AS UUID)" in sql
    assert "version" not in sql.casefold()
    params = captured["params"]
    assert isinstance(params, tuple) and params[-1] == str(TOKEN)


def test_stale_retry_and_dead_letter_decisions_also_return_false(
    monkeypatch: Any,
) -> None:
    """@brief retry 与 dead-letter 共享 token-only stale 语义 / Retry and dead-letter share token-only stale semantics."""

    transaction = _Transaction()
    calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetch_one(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> None:
        """@brief 记录两个 stale 锁定查询 / Record both stale locking queries."""

        assert connection is transaction.connection
        calls.append((sql, params))
        return None

    monkeypatch.setattr(db, "transaction", lambda: transaction)
    monkeypatch.setattr(db, "fetch_one", fetch_one)
    claim = _domain_claim()

    retry_result = asyncio.run(
        PostgresAdminAnnouncementOperations().persist_retry(
            claim.retry(
                retry_at=NOW + timedelta(seconds=1),
                failure=AnnouncementFailureCategory("temporary"),
            )
        )
    )
    dead_result = asyncio.run(
        PostgresAdminAnnouncementOperations().persist_dead_letter(
            claim.dead_letter(
                failed_at=NOW + timedelta(seconds=1),
                failure=AnnouncementFailureCategory("permanent"),
            )
        )
    )

    assert retry_result is False and dead_result is False
    assert len(calls) == 2
    for sql, params in calls:
        assert "status = 'processing'" in sql
        assert "claim_token = CAST(%s AS UUID)" in sql
        assert "version" not in sql.casefold()
        assert params[-1] == str(TOKEN)


def test_recovery_restores_retry_shape_without_incrementing_attempts(
    monkeypatch: Any,
) -> None:
    """@brief recovery 从真实 processing 前态产生 retry 后态且不增加 attempts / Recovery derives retry state from a real processing pre-state without incrementing attempts."""

    transaction = _Transaction()
    recovered_at = NOW + timedelta(minutes=2)
    captured: dict[str, object] = {}

    async def fetch_all(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> list[tuple[object, ...]]:
        """@brief 返回已过期的 processing 前态 / Return an expired processing pre-state."""

        captured["select_sql"] = sql
        captured["select_params"] = params
        assert connection is transaction.connection
        return [
            _recipient_row(
                status=AnnouncementRecipientStatus.PROCESSING,
                attempt_count=3,
                token=TOKEN.value,
                lease_expires_at=NOW + timedelta(minutes=1),
                updated_at=NOW,
            )
        ]

    async def fetch_one(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> tuple[object, ...]:
        """@brief 返回与领域恢复决策相同的 retry 后态 / Return the retry post-state matching the domain recovery decision."""

        captured["update_sql"] = sql
        captured["update_params"] = params
        assert connection is transaction.connection
        return _recipient_row(
            status=AnnouncementRecipientStatus.RETRY_WAIT,
            attempt_count=3,
            next_attempt_at=recovered_at,
            last_error="lease_expired",
            updated_at=recovered_at,
        )

    monkeypatch.setattr(db, "transaction", lambda: transaction)
    monkeypatch.setattr(db, "fetch_all", fetch_all)
    monkeypatch.setattr(db, "fetch_one", fetch_one)

    recovered = asyncio.run(
        PostgresAdminAnnouncementOperations().recover_expired(
            now=recovered_at,
            limit=5,
        )
    )

    assert recovered == 1
    select_sql = str(captured["select_sql"])
    update_sql = str(captured["update_sql"])
    set_clause = update_sql.split("SET", 1)[1].split("RETURNING", 1)[0]
    assert "attempt_count" not in set_clause
    assert (
        "ORDER BY lease_expires_at, announcement_id, recipient_kind, chat_id"
        in select_sql
    )
    assert "FOR UPDATE SKIP LOCKED" in select_sql
    assert "AND claim_token = CAST(%s AS UUID)" in update_sql
    assert "RETURNING announcement_id" in update_sql


def test_settlement_rejects_database_pre_state_mismatch_and_rolls_back(
    monkeypatch: Any,
) -> None:
    """@brief settlement 在锁定前态与 claim 不一致时失败并回滚 / Settlement fails and rolls back when its locked pre-state differs from the claim."""

    transaction = _Transaction()
    calls = 0

    async def fetch_one(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> tuple[object, ...]:
        """@brief 返回 attempt 已变化的合法 processing 前态 / Return a valid processing pre-state whose attempt changed."""

        nonlocal calls
        del sql, params
        assert connection is transaction.connection
        calls += 1
        return _processing_row(attempt_count=2)

    monkeypatch.setattr(db, "transaction", lambda: transaction)
    monkeypatch.setattr(db, "fetch_one", fetch_one)
    decision = _domain_claim().retry(
        retry_at=NOW + timedelta(seconds=1),
        failure=AnnouncementFailureCategory("temporary"),
    )

    with pytest.raises(RuntimeError, match="database pre-state"):
        asyncio.run(PostgresAdminAnnouncementOperations().persist_retry(decision))

    assert calls == 1
    assert transaction.exit_exception is RuntimeError


def test_settlement_rejects_database_post_state_mismatch_and_rolls_back(
    monkeypatch: Any,
) -> None:
    """@brief settlement 在 UPDATE 后态偏离领域决策时失败并回滚 / Settlement fails and rolls back when UPDATE post-state differs from the domain decision."""

    transaction = _Transaction()
    calls = 0
    retry_at = NOW + timedelta(seconds=1)

    async def fetch_one(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> tuple[object, ...]:
        """@brief 返回精确前态和错误时间的合法后态 / Return the exact pre-state and a valid post-state with the wrong time."""

        nonlocal calls
        del sql, params
        assert connection is transaction.connection
        calls += 1
        if calls == 1:
            return _processing_row()
        wrong_time = retry_at + timedelta(seconds=1)
        return _recipient_row(
            status=AnnouncementRecipientStatus.RETRY_WAIT,
            attempt_count=1,
            next_attempt_at=wrong_time,
            last_error="temporary",
            updated_at=wrong_time,
        )

    monkeypatch.setattr(db, "transaction", lambda: transaction)
    monkeypatch.setattr(db, "fetch_one", fetch_one)
    decision = _domain_claim().retry(
        retry_at=retry_at,
        failure=AnnouncementFailureCategory("temporary"),
    )

    with pytest.raises(RuntimeError, match="post-state disagrees"):
        asyncio.run(PostgresAdminAnnouncementOperations().persist_retry(decision))

    assert calls == 2
    assert transaction.exit_exception is RuntimeError


def test_retry_persistence_uses_domain_truncated_error(monkeypatch: Any) -> None:
    """@brief retry SQL 只接收领域已截断错误类别 / Retry SQL receives only the domain-truncated failure category."""

    transaction = _Transaction()
    captured: dict[str, object] = {}
    calls = 0
    retry_at = NOW + timedelta(seconds=1)

    async def fetch_one(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> tuple[object, ...]:
        """@brief 提供 processing 前态并捕获 retry 后态写入 / Supply the processing pre-state and capture the retry post-state write."""

        nonlocal calls
        assert connection is transaction.connection
        calls += 1
        if calls == 1:
            return _processing_row()
        captured["sql"] = sql
        captured["params"] = params
        return _recipient_row(
            status=AnnouncementRecipientStatus.RETRY_WAIT,
            attempt_count=1,
            next_attempt_at=retry_at,
            last_error="e" * 100,
            updated_at=retry_at,
        )

    monkeypatch.setattr(db, "transaction", lambda: transaction)
    monkeypatch.setattr(db, "fetch_one", fetch_one)
    decision = _domain_claim().retry(
        retry_at=retry_at,
        failure=AnnouncementFailureCategory("e" * 101),
    )

    assert asyncio.run(PostgresAdminAnnouncementOperations().persist_retry(decision))
    params = captured["params"]
    assert isinstance(params, tuple) and params[1] == "e" * 100
