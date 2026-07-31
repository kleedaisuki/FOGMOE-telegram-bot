"""@brief Admin announcement PostgreSQL adapter 契约测试 / Admin announcement PostgreSQL-adapter contract tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, cast
from uuid import UUID

import pytest

from fogmoe_bot.application.admin.models import RequestAnnouncement
from fogmoe_bot.domain.admin.announcement import (
    AnnouncementDeliveryCounts,
    AnnouncementDispatchContent,
    AnnouncementId,
    AnnouncementStatus,
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
    AnnouncementIdempotencyConflict,
    PostgresAdminAnnouncementOperations,
)
from fogmoe_bot.infrastructure.database import db

NOW = datetime(2030, 1, 1, tzinfo=UTC)
"""@brief 固定 repository 测试时刻 / Fixed repository-test instant."""

IDEMPOTENCY_KEY = "admin:test:repository"
"""@brief 固定 repository 幂等键 / Fixed repository idempotency key."""

ANNOUNCEMENT_ID = AnnouncementId.for_idempotency_key(IDEMPOTENCY_KEY)
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


def _announcement_row(
    *,
    status: AnnouncementStatus,
    recipient_count: int,
    updated_at: datetime,
    completed_at: datetime | None = None,
    body: str = "hello",
) -> tuple[object, ...]:
    """@brief 构造主公告表十列行 / Build a ten-column main-announcement row.

    @param status 主聚合状态 / Main aggregate status.
    @param recipient_count 冻结受众数 / Frozen audience count.
    @param updated_at 最近转换时刻 / Latest transition instant.
    @param completed_at 可选完成时刻 / Optional completion instant.
    @param body 公告正文 / Announcement body.
    @return 规范主表列序 / Canonical main-table column order.
    """

    return (
        str(ANNOUNCEMENT_ID),
        IDEMPOTENCY_KEY,
        7,
        11,
        body,
        recipient_count,
        status.value,
        NOW,
        updated_at,
        completed_at,
    )


def _delivery_candidate(
    *,
    delivered: int = 1,
    failed: int = 0,
) -> tuple[object, ...]:
    """@brief 构造已锁定 delivering 公告与终态计数 / Build a locked delivering announcement and terminal counts.

    @param delivered 成功数 / Delivered count.
    @param failed 失败数 / Failed count.
    @return 主表十列加两项计数 / Main-table columns followed by two counts.
    """

    return _announcement_row(
        status=AnnouncementStatus.DELIVERING,
        recipient_count=delivered + failed,
        updated_at=NOW,
    ) + (delivered, failed)


def _request(*, body: str = "hello") -> RequestAnnouncement:
    """@brief 构造固定公告应用命令 / Build a fixed announcement application command.

    @param body 公告正文 / Announcement body.
    @return 已验证命令 / Validated command.
    """

    return RequestAnnouncement(
        actor_id=7,
        source_update_id=11,
        idempotency_key=IDEMPOTENCY_KEY,
        body=body,
        reply_chat_id=42,
        reply_message_id=9,
        reply_message_thread_id=7,
        requested_at=NOW,
    )


def _joined_announcement_row(
    *,
    status: AnnouncementStatus,
    recipient_count: int,
    updated_at: datetime,
    completed_at: datetime | None = None,
    body: str = "hello",
) -> tuple[object, ...]:
    """@brief 构造主公告与 completion 地址 JOIN 行 / Build a main-announcement/completion-address JOIN row.

    @return 主表十列加 completion 地址 / Main-table columns followed by the completion address.
    """

    return _announcement_row(
        status=status,
        recipient_count=recipient_count,
        updated_at=updated_at,
        completed_at=completed_at,
        body=body,
    ) + (42, 7, 9)


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


@pytest.mark.parametrize(
    ("recipient_count", "expected_status"),
    (
        (2, AnnouncementStatus.EXPANDING),
        (0, AnnouncementStatus.DELIVERING),
    ),
)
def test_accept_hydrates_initial_snapshot_and_canonical_domain_states(
    monkeypatch: Any,
    recipient_count: int,
    expected_status: AnnouncementStatus,
) -> None:
    """@brief accept hydrate 初态、领域快照后态与规范重放行 / Accept hydrates its initial state, domain snapshot post-state, and canonical replay row."""

    transaction = _Transaction()
    fetch_calls = 0
    execute_calls: list[str] = []

    async def execute(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> int:
        """@brief 记录三类 recipient 快照插入 / Record the three recipient-snapshot inserts."""

        del params
        assert connection is transaction.connection
        execute_calls.append(sql)
        return 1

    async def fetch_one(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> tuple[object, ...] | None:
        """@brief 依次模拟 INSERT、计数、快照 UPDATE 与规范 SELECT / Simulate INSERT, counting, snapshot UPDATE, and canonical SELECT in sequence."""

        nonlocal fetch_calls
        assert connection is transaction.connection
        fetch_calls += 1
        if fetch_calls == 1:
            assert "INSERT INTO admin.announcements" in sql
            assert "RETURNING announcement_id" in sql
            return _announcement_row(
                status=AnnouncementStatus.EXPANDING,
                recipient_count=0,
                updated_at=NOW,
            )
        if fetch_calls == 2:
            assert "SELECT COUNT(*)" in sql
            return (recipient_count,)
        if fetch_calls == 3:
            assert "UPDATE admin.announcements" in sql
            assert params[1] == expected_status.value
            assert "RETURNING announcement_id" in sql
            return _announcement_row(
                status=expected_status,
                recipient_count=recipient_count,
                updated_at=NOW,
            )
        assert "FOR UPDATE OF announcement, completion" in sql
        return _joined_announcement_row(
            status=expected_status,
            recipient_count=recipient_count,
            updated_at=NOW,
        )

    monkeypatch.setattr(db, "transaction", lambda: transaction)
    monkeypatch.setattr(db, "execute", execute)
    monkeypatch.setattr(db, "fetch_one", fetch_one)

    acceptance = asyncio.run(PostgresAdminAnnouncementOperations().accept(_request()))

    assert acceptance.inserted
    assert acceptance.announcement_id == ANNOUNCEMENT_ID
    assert acceptance.recipient_count == recipient_count
    assert len(execute_calls) == 3
    assert fetch_calls == 4


def test_idempotent_replay_uses_domain_intent_semantics(monkeypatch: Any) -> None:
    """@brief 同键不同正文由领域 intent 比较拒绝 / Domain intent comparison rejects the same key with a different body."""

    transaction = _Transaction()
    calls = 0

    async def fetch_one(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> tuple[object, ...] | None:
        """@brief 模拟幂等冲突后的规范行读取 / Simulate canonical-row loading after an idempotency conflict."""

        nonlocal calls
        del params
        assert connection is transaction.connection
        calls += 1
        if calls == 1:
            assert "ON CONFLICT (idempotency_key) DO NOTHING" in sql
            return None
        return _joined_announcement_row(
            status=AnnouncementStatus.EXPANDING,
            recipient_count=2,
            updated_at=NOW,
            body="hello",
        )

    monkeypatch.setattr(db, "transaction", lambda: transaction)
    monkeypatch.setattr(db, "fetch_one", fetch_one)

    with pytest.raises(AnnouncementIdempotencyConflict):
        asyncio.run(
            PostgresAdminAnnouncementOperations().accept(_request(body="different"))
        )

    assert calls == 2


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
        return [_delivery_candidate()]

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
        update_calls.append(sql)
        if "UPDATE admin.announcements" in sql:
            assert params[2] == str(ANNOUNCEMENT_ID)
            return _announcement_row(
                status=AnnouncementStatus.COMPLETED,
                recipient_count=1,
                updated_at=promoted_at,
                completed_at=promoted_at,
            )
        assert params[2] == str(ANNOUNCEMENT_ID)
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
    assert len(select_calls) == 2 and len(update_calls) == 2
    assert (
        "ORDER BY announcement.created_at, announcement.announcement_id"
        in (select_calls[0])
    )
    assert "FOR UPDATE OF announcement SKIP LOCKED" in select_calls[0]
    assert "completion.status = 'blocked' FOR UPDATE" in select_calls[1]
    assert "state = 'completed'" in update_calls[0]
    assert "RETURNING announcement_id" in update_calls[0]
    assert "SET status = 'pending'" in update_calls[1]
    assert "RETURNING completion.announcement_id" in update_calls[1]


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
        return [_delivery_candidate()]

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
        return _announcement_row(
            status=AnnouncementStatus.COMPLETED,
            recipient_count=1,
            updated_at=wrong_time,
            completed_at=wrong_time,
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


def test_completion_promotion_rolls_back_if_compound_recipient_post_mismatches(
    monkeypatch: Any,
) -> None:
    """@brief 公告 UPDATE 后 completion 后态不符仍回滚整个复合事务 / A mismatched completion post-state after the announcement UPDATE still rolls back the compound transaction."""

    transaction = _Transaction()
    promoted_at = NOW + timedelta(minutes=3)
    calls = 0

    async def fetch_all(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> list[tuple[object, ...]]:
        """@brief 返回一个 delivering 候选 / Return one delivering candidate."""

        del sql, params
        assert connection is transaction.connection
        return [_delivery_candidate()]

    async def fetch_one(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> tuple[object, ...]:
        """@brief 返回正确公告后态但错误 completion 后态 / Return the correct announcement post-state but a wrong completion post-state."""

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
        if calls == 2:
            return _announcement_row(
                status=AnnouncementStatus.COMPLETED,
                recipient_count=1,
                updated_at=promoted_at,
                completed_at=promoted_at,
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

    with pytest.raises(RuntimeError, match="completion release post-state"):
        asyncio.run(
            PostgresAdminAnnouncementOperations().promote_delivery_completions(
                now=promoted_at,
                limit=1,
            )
        )

    assert calls == 3
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


def test_last_audience_settlement_hydrates_and_advances_main_aggregate(
    monkeypatch: Any,
) -> None:
    """@brief 最后一个 audience settlement 锁定主聚合并调用领域转换 / The final audience settlement locks the main aggregate and invokes its domain transition."""

    transaction = _Transaction()
    completed_at = NOW + timedelta(seconds=1)
    calls = 0
    sql_calls: list[str] = []

    async def fetch_one(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> tuple[object, ...]:
        """@brief 依次提供 recipient 与主公告 pre/post 行 / Supply recipient and main-announcement pre/post rows in sequence."""

        nonlocal calls
        del params
        assert connection is transaction.connection
        calls += 1
        sql_calls.append(sql)
        if calls == 1:
            return _processing_row()
        if calls == 2:
            return _recipient_row(
                status=AnnouncementRecipientStatus.EXPANDED,
                attempt_count=1,
                outbound_message_id=OUTBOUND_ID.value,
                expanded_at=completed_at,
                updated_at=completed_at,
            )
        if calls == 3:
            return _joined_announcement_row(
                status=AnnouncementStatus.EXPANDING,
                recipient_count=1,
                updated_at=NOW,
            )
        if calls == 4:
            return (1, 1)
        return _announcement_row(
            status=AnnouncementStatus.DELIVERING,
            recipient_count=1,
            updated_at=completed_at,
        )

    monkeypatch.setattr(db, "transaction", lambda: transaction)
    monkeypatch.setattr(db, "fetch_one", fetch_one)
    decision = _domain_claim().expand(
        outbound_message_id=OUTBOUND_ID,
        completed_at=completed_at,
    )

    assert asyncio.run(PostgresAdminAnnouncementOperations().persist_expanded(decision))
    assert calls == 5
    assert "FOR UPDATE OF announcement" in sql_calls[2]
    assert "COUNT(*) FILTER" in sql_calls[3]
    assert "state = 'delivering'" in sql_calls[4]
    assert "RETURNING announcement_id" in sql_calls[4]


def test_concurrent_final_audience_closeout_advances_main_once(
    monkeypatch: Any,
) -> None:
    """@brief 并发末尾收口经 announcement 行锁只转换一次 / Concurrent final closeout transitions exactly once through the announcement row lock."""

    row_lock = asyncio.Lock()
    current_status = AnnouncementStatus.EXPANDING
    update_count = 0
    progress_reads = 0
    finished_at = NOW + timedelta(seconds=1)

    async def fetch_one(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> tuple[object, ...]:
        """@brief 模拟 PostgreSQL 行锁串行化两个末尾事务 / Simulate PostgreSQL row-lock serialization of two final transactions."""

        nonlocal current_status, progress_reads, update_count
        del params, connection
        if "FOR UPDATE OF announcement" in sql:
            await row_lock.acquire()
            row = _joined_announcement_row(
                status=current_status,
                recipient_count=2,
                updated_at=NOW
                if current_status is AnnouncementStatus.EXPANDING
                else finished_at,
            )
            if current_status is not AnnouncementStatus.EXPANDING:
                row_lock.release()
            return row
        if "COUNT(*) FILTER" in sql:
            progress_reads += 1
            if progress_reads == 1:
                row_lock.release()
                return (2, 1)
            return (2, 2)
        assert "UPDATE admin.announcements" in sql
        assert row_lock.locked()
        update_count += 1
        current_status = AnnouncementStatus.DELIVERING
        row_lock.release()
        return _announcement_row(
            status=AnnouncementStatus.DELIVERING,
            recipient_count=2,
            updated_at=finished_at,
        )

    monkeypatch.setattr(db, "fetch_one", fetch_one)

    async def scenario() -> None:
        """@brief 并发执行两个末尾收口尝试 / Run two final-closeout attempts concurrently.

        @return None / None.
        """

        operations = PostgresAdminAnnouncementOperations()
        await asyncio.gather(
            operations._advance_audience_expansion(  # noqa: SLF001
                cast(Any, object()),
                ANNOUNCEMENT_ID,
                now=finished_at,
            ),
            operations._advance_audience_expansion(  # noqa: SLF001
                cast(Any, object()),
                ANNOUNCEMENT_ID,
                now=finished_at,
            ),
        )

    asyncio.run(scenario())

    assert current_status is AnnouncementStatus.DELIVERING
    assert progress_reads == 2
    assert update_count == 1
    assert not row_lock.locked()


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
