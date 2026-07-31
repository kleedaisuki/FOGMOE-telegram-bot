"""@brief Dreaming PostgreSQL adapter 的领域转换契约测试 / Domain-transition contract tests for the Dreaming PostgreSQL adapter."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import TracebackType
from uuid import UUID

import pytest

from fogmoe_bot.application.user_profile.ports import (
    DreamProfileUnchanged,
    DreamProfileUpdated,
)
from fogmoe_bot.domain.user_profile.dream import (
    DreamActivity,
    DreamActivityDraft,
    DreamClaim,
    DreamFailure,
    DreamLease,
    DreamLeaseToken,
    DreamResult,
    ProcessingDream,
    ProfileBaseline,
    StaleDreamClaimError,
)
from fogmoe_bot.domain.user_profile.models import (
    DreamId,
    ProfileClaimKind,
    ProfileConfidence,
    ProfileDocument,
    ProfileEvidence,
    ProfileMetadata,
    ProfilePatch,
    UpsertProfileClaim,
)
from fogmoe_bot.infrastructure.database import db
from fogmoe_bot.infrastructure.database.user_profile.mapping import (
    _document_json,
    _metadata_json,
    _patch_json,
)
from fogmoe_bot.infrastructure.database.user_profile.store import (
    PostgresUserProfileStore,
)

CREATED_AT = datetime(2036, 5, 6, 7, 8, tzinfo=UTC)
"""@brief 固定 Dream 建立时刻 / Fixed Dream creation time."""

CLAIMED_AT = CREATED_AT + timedelta(minutes=1)
"""@brief 固定 claim 时刻 / Fixed claim time."""

LEASE_EXPIRES_AT = CLAIMED_AT + timedelta(minutes=2)
"""@brief 固定 lease 截止时刻 / Fixed lease deadline."""

COMPLETED_AT = CLAIMED_AT + timedelta(seconds=30)
"""@brief 固定 settlement 时刻 / Fixed settlement time."""

DREAM_ID = DreamId(UUID("00000000-0000-0000-0000-000000000701"))
"""@brief 固定 Dream identity / Fixed Dream identity."""

TOKEN = DreamLeaseToken.parse("00000000-0000-0000-0000-000000000702")
"""@brief 固定 fencing token / Fixed fencing token."""


class _Transaction:
    """@brief 最小异步事务替身 / Minimal asynchronous transaction double."""

    def __init__(self) -> None:
        """@brief 创建唯一 connection identity / Create a unique connection identity."""

        self.connection = object()

    async def __aenter__(self) -> object:
        """@brief 进入事务 / Enter the transaction."""

        return self.connection

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        """@brief 退出事务且不吞异常 / Exit without suppressing errors."""

        del exc_type, exc, traceback
        return False


def _metadata() -> ProfileMetadata:
    """@brief 构造冻结用户元信息 / Build frozen user metadata."""

    return ProfileMetadata("Klee", "klee", "CS researcher")


def _evidence(event_id: int = 1) -> ProfileEvidence:
    """@brief 构造一条持久化 evidence / Build one persisted evidence item."""

    return ProfileEvidence(
        event_id=event_id,
        source_turn_id=UUID(f"00000000-0000-0000-0000-{event_id:012d}"),
        owner_user_id=42,
        user_text="I prefer tea",
        assistant_text="Understood",
        occurred_at=CLAIMED_AT,
        metadata=_metadata(),
    )


def _evidence_row(evidence: ProfileEvidence) -> tuple[object, ...]:
    """@brief 将 evidence 投影为 adapter 规范列 / Project evidence into adapter canonical columns."""

    return (
        evidence.event_id,
        evidence.source_turn_id,
        evidence.owner_user_id,
        evidence.user_text,
        evidence.assistant_text,
        evidence.occurred_at,
        _metadata_json(evidence.metadata),
    )


def _pending() -> DreamActivity:
    """@brief 构造待领取聚合 / Build a pending aggregate."""

    return DreamActivity.enqueue(
        DreamActivityDraft(
            dream_id=DREAM_ID,
            owner_user_id=42,
            baseline=ProfileBaseline(
                revision=0,
                observed_through_event_id=0,
            ),
            through_event_id=1,
            source_count=1,
            metadata=_metadata(),
            created_at=CREATED_AT,
        )
    )


def _claim() -> DreamClaim:
    """@brief 从 pending 聚合签发固定 claim / Issue a fixed claim from a pending aggregate."""

    return _pending().claim(
        token=TOKEN,
        claimed_at=CLAIMED_AT,
        lease_expires_at=LEASE_EXPIRES_AT,
        current_document=ProfileDocument(),
        evidence=(_evidence(),),
    )


def _activity_row(activity: DreamActivity) -> tuple[object, ...]:
    """@brief 将聚合投影为十九列 persistence row / Project an aggregate into the nineteen-column persistence row."""

    result = activity.result
    state = activity.state
    return (
        str(activity.dream_id),
        activity.owner_user_id,
        activity.baseline.revision,
        activity.baseline.observed_through_event_id,
        activity.through_event_id,
        activity.source_count,
        _metadata_json(activity.metadata),
        activity.status.value,
        activity.version,
        activity.attempt_count,
        activity.next_attempt_at,
        _patch_json(result) if result is not None else None,
        result.route_key if result is not None else None,
        activity.last_error,
        activity.created_at,
        activity.updated_at,
        activity.completed_at,
        state.token.value if isinstance(state, ProcessingDream) else None,
        state.lease_expires_at if isinstance(state, ProcessingDream) else None,
    )


def _updated_result() -> DreamResult:
    """@brief 构造会新增 revision 的结果 / Build a result that creates a revision."""

    return DreamResult(
        patch=ProfilePatch(
            (
                UpsertProfileClaim(
                    key="drink.preference",
                    kind=ProfileClaimKind.PREFERENCE,
                    statement="偏好茶",
                    confidence=ProfileConfidence.EXPLICIT,
                    evidence_event_ids=(1,),
                ),
            )
        ),
        route_key="test:profile-model",
        prompt_version=2,
    )


def test_read_profile_maps_revision_provenance_not_scheduler_head_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief NO_OP 推进 head cursor 后读取仍映射 immutable revision provenance / Reads still map immutable revision provenance after a NO_OP advances the head cursor.

    @param monkeypatch pytest patch fixture / pytest patch fixture.
    @return None / None.
    """

    async def scenario() -> None:
        """@brief 模拟 head cursor=2、current revision watermark=1 的读取 / Simulate a read with head cursor 2 and current-revision watermark 1.

        @return None / None.
        """

        document = _claim().evaluate_result(_updated_result()).document

        async def fetch_one(
            sql: str,
            params: tuple[object, ...],
        ) -> tuple[object, ...]:
            """@brief 返回 current revision 行并验证 provenance 列来源 / Return the current revision row and verify the provenance-column source.

            @param sql Profile read SQL / Profile read SQL.
            @param params SQL parameters / SQL parameters.
            @return revision-backed snapshot row / Revision-backed snapshot row.
            """

            assert params == (42,)
            assert "revision.observed_through_event_id" in sql
            assert "profile.observed_through_event_id" not in sql
            return (
                42,
                1,
                _document_json(document),
                1,
                CREATED_AT,
                COMPLETED_AT,
                "test:profile-model",
                2,
            )

        monkeypatch.setattr(db, "fetch_one", fetch_one)

        snapshot = await PostgresUserProfileStore().read_profile(42)

        assert snapshot is not None
        assert snapshot.revision == 1
        assert snapshot.observed_through_event_id == 1
        assert snapshot.document == document

    asyncio.run(scenario())


def test_claim_locks_hydrates_then_applies_claim_only_counter_increment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief claim 在领域转换前完整 hydrate，且仅此路径推进 version/attempt / Claim fully hydrates before transition and is the only path advancing version/attempt."""

    async def scenario() -> None:
        """@brief 执行 claim adapter 契约 / Exercise the claim-adapter contract."""

        transaction = _Transaction()
        pending = _pending()
        evidence = _evidence()
        calls: list[str] = []

        async def fetch_all(
            sql: str,
            params: tuple[object, ...],
            *,
            connection: object,
        ) -> tuple[tuple[object, ...], ...]:
            """@brief 提供 locked candidate 与 workload / Supply the locked candidate and workload."""

            assert connection is transaction.connection
            if "FROM user_profile.dreams" in sql:
                calls.append("lock_candidate")
                assert "FOR UPDATE SKIP LOCKED LIMIT %s" in sql
                assert params == (CLAIMED_AT, 1)
                return (_activity_row(pending),)
            calls.append("hydrate_evidence")
            assert "dream_sources" in sql
            return (_evidence_row(evidence),)

        async def fetch_one(
            sql: str,
            params: tuple[object, ...],
            *,
            connection: object,
        ) -> tuple[object, ...]:
            """@brief 将 claim SQL 参数回投为 processing 行 / Reflect claim SQL parameters as a processing row."""

            assert connection is transaction.connection
            calls.append("claim_cas")
            assert "UPDATE user_profile.dreams" in sql
            assert "version = %s" in sql and "attempt_count = %s" in sql
            assert "version + 1" not in sql and "attempt_count + 1" not in sql
            assert params[1:3] == (1, 1)
            target = pending.claim(
                token=DreamLeaseToken.parse(str(params[4])),
                claimed_at=CLAIMED_AT,
                lease_expires_at=LEASE_EXPIRES_AT,
                current_document=ProfileDocument(),
                evidence=(evidence,),
            ).activity
            return _activity_row(target)

        monkeypatch.setattr(db, "transaction", lambda: transaction)
        monkeypatch.setattr(db, "fetch_all", fetch_all)
        monkeypatch.setattr(db, "fetch_one", fetch_one)

        claims = await PostgresUserProfileStore().claim_dreams(
            now=CLAIMED_AT,
            limit=1,
            lease_for=timedelta(minutes=2),
        )

        assert calls == ["lock_candidate", "hydrate_evidence", "claim_cas"]
        assert len(claims) == 1
        assert (claims[0].activity.version, claims[0].activity.attempt_count) == (
            1,
            1,
        )

    asyncio.run(scenario())


@pytest.mark.parametrize("has_backlog", (False, True))
def test_no_op_completion_uses_dual_profile_fence_and_advances_watermark(
    monkeypatch: pytest.MonkeyPatch,
    has_backlog: bool,
) -> None:
    """@brief NO_OP 不建 revision，但双重 fence 推进 watermark 并按 backlog 规划 eligibility / NO_OP creates no revision but advances the watermark under a dual fence and backlog-aware eligibility."""

    async def scenario() -> None:
        """@brief 执行 NO_OP completion / Execute NO_OP completion."""

        transaction = _Transaction()
        claim = _claim()
        prepared = claim.evaluate_result(
            DreamResult(ProfilePatch(), "test:no-op", 2)
        ).prepare(completed_at=COMPLETED_AT)
        calls: list[str] = []
        profile_params: tuple[object, ...] | None = None
        expected_eligibility = (
            COMPLETED_AT if has_backlog else COMPLETED_AT + timedelta(hours=6)
        )

        async def lock_user_profile(connection: object, user_id: int) -> None:
            """@brief 记录 advisory lock / Record the advisory lock."""

            assert connection is transaction.connection and user_id == 42
            calls.append("advisory_lock")

        async def fetch_one(
            sql: str,
            params: tuple[object, ...],
            *,
            connection: object,
        ) -> tuple[object, ...] | None:
            """@brief 依次提供 Dream、Profile、backlog 与 Dream CAS / Supply Dream, Profile, backlog, and Dream CAS in order."""

            assert connection is transaction.connection
            if sql.startswith("SELECT dream_id"):
                calls.append("dream_lock")
                assert "FOR UPDATE" in sql
                return _activity_row(claim.activity)
            if sql.startswith("SELECT COALESCE(current_revision"):
                calls.append("profile_lock")
                assert "FOR UPDATE" in sql
                return (0, 0, CREATED_AT)
            if "SELECT 1 FROM user_profile.evidence_events" in sql:
                calls.append("backlog")
                return (1,) if has_backlog else None
            if sql.startswith("UPDATE user_profile.profiles"):
                nonlocal profile_params
                calls.append("profile_cas")
                assert "COALESCE(current_revision, 0) = %s" in sql
                assert "observed_through_event_id = %s" in sql
                assert "RETURNING user_id" in sql
                profile_params = params
                return (
                    42,
                    0,
                    1,
                    expected_eligibility,
                    CREATED_AT,
                    COMPLETED_AT,
                )
            calls.append("dream_cas")
            assert "UPDATE user_profile.dreams" in sql
            assert params[1:3] == (1, 1)
            assert "lease_expires_at = %s" in sql
            return _activity_row(prepared.activity)

        async def fetch_all(
            sql: str,
            params: tuple[object, ...],
            *,
            connection: object,
        ) -> tuple[tuple[object, ...], ...]:
            """@brief 返回 settlement 时重新加载的 canonical evidence / Return canonical evidence reloaded at settlement.

            @param sql evidence 查询 / Evidence query.
            @param params 查询参数 / Query parameters.
            @param connection 当前事务 / Current transaction.
            @return canonical evidence row / Canonical evidence row.
            """

            assert connection is transaction.connection
            calls.append("canonical_workload")
            assert "dream_sources" in sql
            assert params == (str(DREAM_ID),)
            return (_evidence_row(_evidence()),)

        monkeypatch.setattr(db, "transaction", lambda: transaction)
        monkeypatch.setattr(db, "fetch_one", fetch_one)
        monkeypatch.setattr(db, "fetch_all", fetch_all)
        monkeypatch.setattr(
            "fogmoe_bot.infrastructure.database.user_profile.store.lock_user_profile",
            lock_user_profile,
        )

        receipt = await PostgresUserProfileStore().complete_dream(
            prepared,
            refresh_after=timedelta(hours=6),
        )

        assert receipt == DreamProfileUnchanged(
            owner_user_id=42,
            retained_revision=0,
            scheduler_head_event_id=1,
        )
        assert calls == [
            "advisory_lock",
            "dream_lock",
            "canonical_workload",
            "profile_lock",
            "backlog",
            "profile_cas",
            "dream_cas",
        ]
        assert profile_params == (
            None,
            1,
            expected_eligibility,
            COMPLETED_AT,
            42,
            0,
            0,
        )

    asyncio.run(scenario())


def test_changed_completion_inserts_revision_before_backlog_and_returns_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief changed completion 先建 revision，再观察 backlog，并原子返回 snapshot / A changed completion inserts its revision before observing backlog and atomically returns a snapshot."""

    async def scenario() -> None:
        """@brief 执行 changed completion / Execute changed completion."""

        transaction = _Transaction()
        claim = _claim()
        prepared = claim.evaluate_result(_updated_result()).prepare(
            completed_at=COMPLETED_AT
        )
        calls: list[str] = []
        expected_eligibility = COMPLETED_AT + timedelta(hours=6)

        async def lock_user_profile(connection: object, user_id: int) -> None:
            """@brief 验证 advisory lock / Verify the advisory lock."""

            assert connection is transaction.connection and user_id == 42
            calls.append("advisory_lock")

        async def fetch_one(
            sql: str,
            params: tuple[object, ...],
            *,
            connection: object,
        ) -> tuple[object, ...] | None:
            """@brief 模拟完整 changed commit reads / Simulate changed-commit reads."""

            assert connection is transaction.connection
            if sql.startswith("SELECT dream_id"):
                calls.append("dream_lock")
                return _activity_row(claim.activity)
            if sql.startswith("SELECT COALESCE(current_revision"):
                calls.append("profile_lock")
                return (0, 0, CREATED_AT)
            if sql.startswith("INSERT INTO user_profile.profile_revisions"):
                calls.append("revision_insert")
                assert "RETURNING user_id" in sql
                assert params[1] == 1 and params[3] == 1
                return (
                    42,
                    1,
                    _document_json(prepared.document),
                    1,
                    COMPLETED_AT,
                    "test:profile-model",
                    2,
                )
            if "SELECT 1 FROM user_profile.evidence_events" in sql:
                calls.append("backlog")
                return None
            if sql.startswith("UPDATE user_profile.profiles"):
                calls.append("profile_cas")
                assert "RETURNING user_id" in sql
                assert params[0] == 1 and params[1] == 1
                return (
                    42,
                    1,
                    1,
                    expected_eligibility,
                    CREATED_AT,
                    COMPLETED_AT,
                )
            calls.append("dream_cas")
            assert params[1:3] == (1, 1)
            return _activity_row(prepared.activity)

        async def fetch_all(
            sql: str,
            params: tuple[object, ...],
            *,
            connection: object,
        ) -> tuple[tuple[object, ...], ...]:
            """@brief 返回 settlement canonical evidence / Return canonical settlement evidence.

            @param sql evidence 查询 / Evidence query.
            @param params 查询参数 / Query parameters.
            @param connection 当前事务 / Current transaction.
            @return canonical evidence row / Canonical evidence row.
            """

            assert connection is transaction.connection
            calls.append("canonical_workload")
            assert "dream_sources" in sql
            assert params == (str(DREAM_ID),)
            return (_evidence_row(_evidence()),)

        monkeypatch.setattr(db, "transaction", lambda: transaction)
        monkeypatch.setattr(db, "fetch_one", fetch_one)
        monkeypatch.setattr(db, "fetch_all", fetch_all)
        monkeypatch.setattr(
            "fogmoe_bot.infrastructure.database.user_profile.store.lock_user_profile",
            lock_user_profile,
        )

        receipt = await PostgresUserProfileStore().complete_dream(
            prepared,
            refresh_after=timedelta(hours=6),
        )

        assert isinstance(receipt, DreamProfileUpdated)
        snapshot = receipt.snapshot
        assert snapshot.revision == 1
        assert snapshot.observed_through_event_id == 1
        assert snapshot.document == prepared.document
        assert calls == [
            "advisory_lock",
            "dream_lock",
            "canonical_workload",
            "profile_lock",
            "revision_insert",
            "backlog",
            "profile_cas",
            "dream_cas",
        ]

    asyncio.run(scenario())


@pytest.mark.parametrize("drift_kind", ("document", "evidence"))
def test_completion_reloads_and_rejects_mismatched_claim_workload(
    monkeypatch: pytest.MonkeyPatch,
    drift_kind: str,
) -> None:
    """@brief settlement 重新加载 canonical evidence 并拒绝错绑 capability / Settlement reloads canonical evidence and rejects a misbound capability.

    @param monkeypatch pytest patch fixture / pytest patch fixture.
    @param drift_kind 错绑的 workload 组成 / Misbound workload component.
    @return None / None.
    """

    async def scenario() -> None:
        """@brief 尝试提交绑定了漂移 workload 的 claim / Attempt to settle a claim bound to drifted workload.

        @return None / None.
        """

        transaction = _Transaction()
        durable_evidence = _evidence()
        claim = _claim()
        if drift_kind == "document":
            injected_document = claim.evaluate_result(_updated_result()).document
            object.__setattr__(claim, "current_document", injected_document)
        else:
            drifted_evidence = replace(durable_evidence, user_text="I prefer coffee")
            object.__setattr__(claim, "evidence", (drifted_evidence,))
        prepared = claim.evaluate_result(
            DreamResult(ProfilePatch(), "test:no-op", 2)
        ).prepare(completed_at=COMPLETED_AT)

        async def lock_user_profile(connection: object, user_id: int) -> None:
            """@brief 验证 settlement advisory lock / Verify the settlement advisory lock.

            @param connection 当前事务 / Current transaction.
            @param user_id Profile owner / Profile owner.
            @return None / None.
            """

            assert connection is transaction.connection and user_id == 42

        async def fetch_one(
            sql: str,
            params: tuple[object, ...],
            *,
            connection: object,
        ) -> tuple[object, ...]:
            """@brief 仅允许锁定 Dream；之后不得写 Profile / Allow only the Dream lock; no Profile write may follow.

            @param sql SQL text / SQL text.
            @param params SQL parameters / SQL parameters.
            @param connection 当前事务 / Current transaction.
            @return processing Dream row / Processing Dream row.
            """

            assert connection is transaction.connection
            assert sql.startswith("SELECT dream_id")
            assert params == (str(DREAM_ID),)
            return _activity_row(claim.activity)

        async def fetch_all(
            sql: str,
            params: tuple[object, ...],
            *,
            connection: object,
        ) -> tuple[tuple[object, ...], ...]:
            """@brief 返回数据库中的 canonical evidence / Return canonical durable evidence.

            @param sql SQL text / SQL text.
            @param params SQL parameters / SQL parameters.
            @param connection 当前事务 / Current transaction.
            @return canonical evidence row / Canonical evidence row.
            """

            assert connection is transaction.connection
            assert "dream_sources" in sql
            return (_evidence_row(durable_evidence),)

        monkeypatch.setattr(db, "transaction", lambda: transaction)
        monkeypatch.setattr(db, "fetch_one", fetch_one)
        monkeypatch.setattr(db, "fetch_all", fetch_all)
        monkeypatch.setattr(
            "fogmoe_bot.infrastructure.database.user_profile.store.lock_user_profile",
            lock_user_profile,
        )

        with pytest.raises(StaleDreamClaimError, match="workload"):
            await PostgresUserProfileStore().complete_dream(
                prepared,
                refresh_after=timedelta(hours=6),
            )

    asyncio.run(scenario())


def test_completion_replays_and_rejects_tampered_empty_patch_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief settlement 重放空 patch 并拒绝任意 document 注入 / Settlement replays an empty patch and rejects arbitrary-document injection.

    @param monkeypatch pytest patch fixture / pytest patch fixture.
    @return None / None.
    """

    async def scenario() -> None:
        """@brief 绕过 frozen guard 构造攻击性测试输入 / Bypass the frozen guard to construct an adversarial test input.

        @return None / None.
        """

        transaction = _Transaction()
        claim = _claim()
        prepared = claim.evaluate_result(
            DreamResult(ProfilePatch(), "test:no-op", 2)
        ).prepare(completed_at=COMPLETED_AT)
        injected_document = claim.evaluate_result(_updated_result()).document
        object.__setattr__(prepared, "document", injected_document)
        object.__setattr__(prepared, "changed", True)

        async def lock_user_profile(connection: object, user_id: int) -> None:
            """@brief 验证 settlement advisory lock / Verify the settlement advisory lock.

            @param connection 当前事务 / Current transaction.
            @param user_id Profile owner / Profile owner.
            @return None / None.
            """

            assert connection is transaction.connection and user_id == 42

        async def fetch_one(
            sql: str,
            params: tuple[object, ...],
            *,
            connection: object,
        ) -> tuple[object, ...]:
            """@brief 仅提供 locked Dream 前态 / Supply only the locked Dream pre-state.

            @param sql SQL text / SQL text.
            @param params SQL parameters / SQL parameters.
            @param connection 当前事务 / Current transaction.
            @return processing Dream row / Processing Dream row.
            """

            assert connection is transaction.connection
            assert sql.startswith("SELECT dream_id")
            assert params == (str(DREAM_ID),)
            return _activity_row(claim.activity)

        async def fetch_all(
            sql: str,
            params: tuple[object, ...],
            *,
            connection: object,
        ) -> tuple[tuple[object, ...], ...]:
            """@brief 返回与 claim 一致的 canonical evidence / Return canonical evidence matching the claim.

            @param sql SQL text / SQL text.
            @param params SQL parameters / SQL parameters.
            @param connection 当前事务 / Current transaction.
            @return canonical evidence row / Canonical evidence row.
            """

            assert connection is transaction.connection
            assert "dream_sources" in sql
            return (_evidence_row(_evidence()),)

        monkeypatch.setattr(db, "transaction", lambda: transaction)
        monkeypatch.setattr(db, "fetch_one", fetch_one)
        monkeypatch.setattr(db, "fetch_all", fetch_all)
        monkeypatch.setattr(
            "fogmoe_bot.infrastructure.database.user_profile.store.lock_user_profile",
            lock_user_profile,
        )

        with pytest.raises(StaleDreamClaimError, match="canonical claim evaluation"):
            await PostgresUserProfileStore().complete_dream(
                prepared,
                refresh_after=timedelta(hours=6),
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("settlement", ("retry", "failed_final"))
def test_failure_settlements_cas_the_full_lease_without_incrementing_counters(
    monkeypatch: pytest.MonkeyPatch,
    settlement: str,
) -> None:
    """@brief retry/final settlement 锁定并 CAS 完整 lease，且不推进计数 / Retry and final settlements lock and CAS the full lease without advancing counters."""

    async def scenario() -> None:
        """@brief 执行失败 settlement / Execute a failure settlement."""

        transaction = _Transaction()
        claim = _claim()
        failure = claim.record_failure(
            failed_at=COMPLETED_AT,
            failure=DreamFailure("provider failure"),
        )
        decision = (
            failure.schedule_retry(retry_at=COMPLETED_AT + timedelta(seconds=1))
            if settlement == "retry"
            else failure.fail_final()
        )
        calls: list[str] = []

        async def fetch_one(
            sql: str,
            params: tuple[object, ...],
            *,
            connection: object,
        ) -> tuple[object, ...]:
            """@brief 提供 processing pre-state 与 settlement post-state / Supply processing pre-state and settlement post-state."""

            assert connection is transaction.connection
            if sql.startswith("SELECT dream_id"):
                calls.append("lock")
                return _activity_row(claim.activity)
            calls.append("cas")
            assert "version + 1" not in sql and "attempt_count + 1" not in sql
            assert params[1:3] == (1, 1)
            assert "claim_token = CAST(%s AS UUID)" in sql
            assert "lease_expires_at = %s" in sql
            return _activity_row(decision.activity)

        monkeypatch.setattr(db, "transaction", lambda: transaction)
        monkeypatch.setattr(db, "fetch_one", fetch_one)

        store = PostgresUserProfileStore()
        if settlement == "retry":
            await store.retry_dream(decision)  # type: ignore[arg-type]
        else:
            await store.fail_dream(decision)  # type: ignore[arg-type]

        assert calls == ["lock", "cas"]
        assert (decision.activity.version, decision.activity.attempt_count) == (1, 1)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("max_attempts", "expected_status"),
    ((2, "retry_wait"), (1, "failed_final")),
)
def test_expired_lease_recovery_hydrates_domain_then_cas_without_increment(
    monkeypatch: pytest.MonkeyPatch,
    max_attempts: int,
    expected_status: str,
) -> None:
    """@brief recovery 按预算穷尽转换并 CAS，保留 version/attempt / Recovery transitions exhaustively by budget and CASes while preserving version/attempt.

    @param monkeypatch pytest patch fixture / pytest patch fixture.
    @param max_attempts 最大尝试数 / Maximum attempts.
    @param expected_status 期望 retry 或 final 状态 / Expected retry or final state.
    @return None / None.
    """

    async def scenario() -> None:
        """@brief 执行过期 lease recovery / Execute expired-lease recovery."""

        transaction = _Transaction()
        processing = _claim().activity
        recovered_at = LEASE_EXPIRES_AT + timedelta(seconds=1)
        target = processing.recover_expired_lease(
            DreamLease.restore(processing),
            recovered_at=recovered_at,
            failure=DreamFailure("recovered expired Dream lease"),
            max_attempts=max_attempts,
        ).activity
        assert target.status.value == expected_status
        calls: list[str] = []

        async def fetch_all(
            sql: str,
            params: tuple[object, ...],
            *,
            connection: object,
        ) -> tuple[tuple[object, ...], ...]:
            """@brief 返回已锁过期 processing 行 / Return a locked expired processing row."""

            assert connection is transaction.connection
            calls.append("lock_expired")
            assert "FOR UPDATE SKIP LOCKED" in sql
            assert "ORDER BY lease_expires_at, dream_id" in sql
            assert "LIMIT %s" in sql
            assert params == (recovered_at, 7)
            return (_activity_row(processing),)

        async def fetch_one(
            sql: str,
            params: tuple[object, ...],
            *,
            connection: object,
        ) -> tuple[object, ...]:
            """@brief 返回领域 recovery 的精确后态 / Return the exact domain recovery post-state."""

            assert connection is transaction.connection
            calls.append("recovery_cas")
            assert "version + 1" not in sql and "attempt_count + 1" not in sql
            assert params[1:3] == (1, 1)
            assert params[-2:] == (str(TOKEN), LEASE_EXPIRES_AT)
            return _activity_row(target)

        monkeypatch.setattr(db, "transaction", lambda: transaction)
        monkeypatch.setattr(db, "fetch_all", fetch_all)
        monkeypatch.setattr(db, "fetch_one", fetch_one)

        recovered = await PostgresUserProfileStore().recover_expired_dream_leases(
            now=recovered_at,
            max_attempts=max_attempts,
            limit=7,
        )

        assert recovered == 1
        assert calls == ["lock_expired", "recovery_cas"]
        assert (target.version, target.attempt_count) == (1, 1)

    asyncio.run(scenario())
