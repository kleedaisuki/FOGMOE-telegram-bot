"""@brief User Profile 领域、模型 adapter 与 Dreaming worker 测试 / User Profile domain, model-adapter, and Dreaming-worker tests."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
import time
from uuid import UUID

import pytest

from fogmoe_bot.application.assistant.completion import AssistantCompletion
from fogmoe_bot.application.observability.telemetry import Telemetry, TelemetryBuffer
from fogmoe_bot.application.runtime import AdaptivePollingPolicy, UtcClock
from fogmoe_bot.application.user_profile.ports import (
    DreamCommitReceipt,
    DreamProfileUnchanged,
    DreamProfileUpdated,
    DreamingModel,
    RetryableDreamingError,
)
from fogmoe_bot.application.user_profile.worker import DreamingWorker
from fogmoe_bot.domain.assistant.messages import text_message
from fogmoe_bot.domain.assistant.routing.models import (
    ProviderAuth,
    ProviderRoute,
    RouteModel,
)
from fogmoe_bot.domain.conversation.message import MessageRole
from fogmoe_bot.domain.user_profile import (
    DreamActivity,
    DreamActivityDraft,
    DreamClaim,
    DreamCompletionPrepared,
    DreamFailedFinalDecision,
    DreamFailure,
    DreamLeaseToken,
    DreamResult,
    DreamRetryScheduled,
    DeleteProfileClaim,
    DreamId,
    ProfileBaseline,
    ProfileClaim,
    ProfileClaimKind,
    ProfileConfidence,
    ProfileDocument,
    ProfileEvidence,
    ProfileMetadata,
    ProfilePatch,
    UpsertProfileClaim,
    UserProfileSnapshot,
)
from fogmoe_bot.infrastructure.database import db
from fogmoe_bot.infrastructure.database.user_profile.source import (
    PostgresProfileEvidenceSource,
)
from fogmoe_bot.infrastructure.user_profile.dreaming_model import ProviderDreamingModel

NOW = datetime(2035, 1, 2, 3, 4, tzinfo=UTC)
"""@brief 固定测试时间 / Fixed test time."""


class _Clock(UtcClock):
    """@brief 固定 UTC clock / Fixed UTC clock."""

    def now(self) -> datetime:
        """@brief 返回固定时间 / Return the fixed time."""

        return NOW


class _ClockAt(UtcClock):
    """@brief 返回注入时刻的测试 UTC clock / Test UTC clock returning an injected instant."""

    def __init__(self, instant: datetime) -> None:
        """@brief 保存固定时刻 / Store the fixed instant.

        @param instant 每次返回的 UTC 时刻 / UTC instant returned on every read.
        @return None / None.
        """

        self._instant = instant

    def now(self) -> datetime:
        """@brief 返回注入时刻 / Return the injected instant.

        @return 固定时刻 / Fixed instant.
        """

        return self._instant


def _metadata() -> ProfileMetadata:
    """@brief 构造冻结用户元信息 / Build frozen user metadata."""

    return ProfileMetadata("Klee", "klee", "CS researcher")


def _evidence(
    event_id: int,
    text: str = "I prefer tea",
    *,
    assistant_text: str = "Understood",
) -> ProfileEvidence:
    """@brief 构造一条 Profile evidence / Build one Profile evidence item."""

    return ProfileEvidence(
        event_id=event_id,
        source_turn_id=UUID(f"00000000-0000-0000-0000-{event_id:012d}"),
        owner_user_id=42,
        user_text=text,
        assistant_text=assistant_text,
        occurred_at=NOW + timedelta(seconds=event_id),
        metadata=_metadata(),
    )


def _claim(*, evidence: tuple[ProfileEvidence, ...] | None = None) -> DreamClaim:
    """@brief 构造 processing Dream claim / Build a processing Dream claim."""

    sources = evidence or (_evidence(1),)
    pending = DreamActivity.enqueue(
        DreamActivityDraft(
            dream_id=DreamId(UUID("00000000-0000-0000-0000-000000000099")),
            owner_user_id=42,
            baseline=ProfileBaseline(
                revision=0,
                observed_through_event_id=0,
            ),
            through_event_id=sources[-1].event_id,
            source_count=len(sources),
            metadata=_metadata(),
            created_at=NOW - timedelta(seconds=1),
        )
    )
    return pending.claim(
        token=DreamLeaseToken.parse(UUID("00000000-0000-0000-0000-000000000088")),
        claimed_at=NOW,
        lease_expires_at=NOW + timedelta(seconds=30),
        current_document=ProfileDocument(),
        evidence=sources,
    )


def test_profile_reducer_requires_current_batch_provenance_and_updates_by_stable_key() -> (
    None
):
    """@brief document 只接受批内 provenance 且以稳定 key supersede / The document accepts only in-batch provenance and supersedes by stable key.

    @return None / None.
    """

    old = ProfileDocument(
        (
            ProfileClaim(
                key="drink.preference",
                kind=ProfileClaimKind.PREFERENCE,
                statement="偏好咖啡",
                confidence=ProfileConfidence.EXPLICIT,
                evidence_event_ids=(1,),
                observed_at=NOW,
            ),
        )
    )
    new_evidence = (_evidence(2, "I now prefer tea, not coffee"),)
    updated = old.apply(
        ProfilePatch(
            (
                UpsertProfileClaim(
                    key="drink.preference",
                    kind=ProfileClaimKind.PREFERENCE,
                    statement="现在偏好茶而非咖啡",
                    confidence=ProfileConfidence.EXPLICIT,
                    evidence_event_ids=(2,),
                ),
            )
        ),
        evidence=new_evidence,
    )

    assert len(updated.claims) == 1
    assert updated.claims[0].statement == "现在偏好茶而非咖啡"
    assert updated.claims[0].evidence_event_ids == (2,)
    with pytest.raises(ValueError, match="outside the current batch"):
        old.apply(
            ProfilePatch(
                (
                    DeleteProfileClaim(
                        key="drink.preference",
                        evidence_event_ids=(1,),
                    ),
                )
            ),
            evidence=new_evidence,
        )


def test_profile_patch_freezes_operations_and_operation_provenance() -> None:
    """@brief patch 与 operation 切断调用方可变别名 / A patch and its operations sever caller-owned mutable aliases.

    @return None / None.
    """

    evidence_ids = [1]
    operation = UpsertProfileClaim(
        key="drink.preference",
        kind=ProfileClaimKind.PREFERENCE,
        statement="偏好茶",
        confidence=ProfileConfidence.EXPLICIT,
        evidence_event_ids=evidence_ids,  # type: ignore[arg-type]
    )
    operations = [operation]
    patch = ProfilePatch(operations)  # type: ignore[arg-type]

    evidence_ids.clear()
    operations.clear()

    assert operation.evidence_event_ids == (1,)
    assert patch.operations == (operation,)
    with pytest.raises(TypeError, match="unknown operation"):
        ProfilePatch((object(),))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "evidence_ids",
    (
        pytest.param((1, True), id="integer-then-boolean"),
        pytest.param((True, 1), id="boolean-then-integer"),
        pytest.param((1, 1.0), id="integer-then-float"),
        pytest.param((1.0, 1), id="float-then-integer"),
    ),
)
def test_operation_validates_types_before_deduplicating_equal_values(
    evidence_ids: tuple[object, ...],
) -> None:
    """@brief provenance 在 Python 数值相等去重前校验实际类型 / Provenance validates actual types before Python numeric-equality deduplication.

    @param evidence_ids 含伪整数的候选 IDs / Candidate IDs containing a pseudo-integer.
    @return None / None.
    """

    with pytest.raises(ValueError, match="positive integer"):
        DeleteProfileClaim(
            key="drink.preference",
            evidence_event_ids=evidence_ids,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        pytest.param("event_id", 1.0, id="floating-event-id"),
        pytest.param("owner_user_id", 42.0, id="floating-owner-id"),
        pytest.param("event_id", True, id="boolean-event-id"),
        pytest.param("owner_user_id", True, id="boolean-owner-id"),
    ),
)
def test_profile_evidence_rejects_values_that_are_not_actual_integers(
    field: str,
    value: object,
) -> None:
    """@brief evidence 在运行时拒绝 float/bool 伪整数 / Evidence rejects float and bool pseudo-integers at runtime.

    @param field 被破坏的 identity 域 / Identity field under test.
    @param value 非整数运行时值 / Non-integer runtime value.
    @return None / None.
    """

    values: dict[str, object] = {
        "event_id": 1,
        "source_turn_id": UUID("00000000-0000-0000-0000-000000000001"),
        "owner_user_id": 42,
        "user_text": "I prefer tea",
        "assistant_text": "Understood",
        "occurred_at": NOW,
        "metadata": _metadata(),
    }
    values[field] = value

    with pytest.raises(ValueError):
        ProfileEvidence(**values)  # type: ignore[arg-type]


def test_profile_metadata_exposes_a_canonical_provider_key() -> None:
    """@brief provider identity 在进入聚合前规范化为受限 key / Provider identity is normalized to a bounded key before entering the aggregate.

    @return None / None.
    """

    assert ProfileMetadata("Klee", provider=" TELEGRAM ").provider == "telegram"
    with pytest.raises(ValueError, match="canonical provider key"):
        ProfileMetadata("Klee", provider="ß" * 32)


class _Completion:
    """@brief 返回固定结构化 JSON 的 completion fake / Completion fake returning fixed structured JSON."""

    def __init__(self, content: str) -> None:
        """@brief 保存输出 / Store output."""

        self._content = content
        self.messages: object = None

    async def complete(self, **kwargs: object) -> AssistantCompletion:
        """@brief 记录 request 并返回输出 / Record the request and return output."""

        self.messages = kwargs["messages"]
        return AssistantCompletion(text_message(MessageRole.ASSISTANT, self._content))


def test_provider_dreaming_model_requires_strict_json_and_preserves_route_provenance() -> (
    None
):
    """@brief adapter 验证 JSON schema 并记录实际 route / Adapter validates JSON schema and records the actual route."""

    async def scenario() -> None:
        """@brief 执行 provider adapter / Execute the provider adapter."""

        completion = _Completion(
            '{"operations":[{"op":"upsert","key":"drink.preference",'
            '"kind":"preference","statement":"偏好茶","confidence":"explicit",'
            '"evidence_event_ids":[1]}]}'
        )
        model = ProviderDreamingModel(
            completion=completion,
            routes=(
                ProviderRoute(
                    route_id="test",
                    provider_id="openai",
                    provider_label="Test",
                    style="openai",
                    endpoint="https://api.example.test/v1/chat/completions",
                    auth=ProviderAuth(),
                    models=(RouteModel("profile-model"),),
                    supports_tools=False,
                ),
            ),
            request_timeout_seconds=10,
            telemetry=Telemetry(TelemetryBuffer(32)),
        )

        result = await model.dream(
            _claim(evidence=(_evidence(1, assistant_text="x" * 5_000),))
        )

        assert result.route_key == "test:profile-model"
        assert result.prompt_version == 1
        operation = result.patch.operations[0]
        assert isinstance(operation, UpsertProfileClaim)
        assert operation.evidence_event_ids == (1,)
        assert "<new_evidence_json>" in str(completion.messages)
        assert "x" * 4_001 not in str(completion.messages)
        assert ("x" * 3_999) + "…" in str(completion.messages)

    asyncio.run(scenario())


def test_evidence_discovery_accepts_only_real_telegram_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 系统生成的 scheduled prompt 不得成为用户画像证据 / System-generated scheduled prompts cannot become profile evidence."""

    async def scenario() -> None:
        """@brief 审计 source SQL 的身份边界 / Audit the source SQL identity boundary."""

        calls: list[tuple[str, tuple[object, ...]]] = []

        async def fake_fetch_all(
            sql: str,
            params: tuple[object, ...],
        ) -> tuple[object, ...]:
            """@brief 捕获查询 / Capture the query."""

            calls.append((sql, params))
            return ()

        monkeypatch.setattr(db, "fetch_all", fake_fetch_all)
        assert await PostgresProfileEvidenceSource().read_unprojected(limit=8) == ()
        assert len(calls) == 1
        sql, params = calls[0]
        assert "turn.source_kind = %s" in sql
        assert sql.count("(") == sql.count(")")
        assert (
            sql.count("workspace_attachment,state}' = 'imported') IS NOT TRUE))") == 3
        )
        assert params == ("telegram.update", 8)

    asyncio.run(scenario())


class _Source:
    """@brief 记录 source reader task 的一次性 source / One-shot source recording its reader task."""

    def __init__(self) -> None:
        """@brief 初始化状态 / Initialize state."""

        self.returned = False
        self.reader_tasks: list[str] = []

    async def read_unprojected(self, *, limit: int) -> tuple[ProfileEvidence, ...]:
        """@brief coordinator 第一次读取来源 / Let the coordinator read one source once."""

        assert limit == 4
        task = asyncio.current_task()
        self.reader_tasks.append(task.get_name() if task is not None else "")
        if self.returned:
            return ()
        self.returned = True
        return (_evidence(0),)


class _Store:
    """@brief Dreaming worker 的内存 store fake / In-memory store fake for Dreaming worker."""

    def __init__(self, stop_event: asyncio.Event) -> None:
        """@brief 初始化状态 / Initialize state."""

        self.stop_event = stop_event
        self.projected = False
        self.enqueued = False
        self.claimed = False
        self.claim_tasks: list[str] = []
        self.document: ProfileDocument | None = None

    async def read_profile(self, user_id: int) -> UserProfileSnapshot | None:
        """@brief 本测试不读取 acceptance Profile / This test does not read an acceptance Profile."""

        raise AssertionError(user_id)

    async def project_evidence(
        self, evidence: ProfileEvidence, *, projected_at: datetime
    ) -> None:
        """@brief 记录 projection / Record projection."""

        assert evidence.event_id == 0 and projected_at == NOW
        self.projected = True

    async def enqueue_eligible(
        self,
        *,
        now: datetime,
        limit: int,
        max_events_per_dream: int,
        max_evidence_chars: int,
    ) -> int:
        """@brief projection 后建立一次 job / Enqueue one job after projection."""

        assert now == NOW and limit == 2 and max_events_per_dream == 8
        assert max_evidence_chars == 60_000
        if not self.projected or self.enqueued:
            return 0
        self.enqueued = True
        return 1

    async def claim_dreams(
        self,
        *,
        now: datetime,
        limit: int,
        lease_for: timedelta,
    ) -> tuple[DreamClaim, ...]:
        """@brief durable job 只被领取一次 / Claim the durable job once."""

        assert now == NOW and limit == 1 and lease_for == timedelta(seconds=30)
        task = asyncio.current_task()
        self.claim_tasks.append(task.get_name() if task is not None else "")
        if not self.enqueued or self.claimed:
            return ()
        self.claimed = True
        return (_claim(),)

    async def complete_dream(
        self,
        decision: DreamCompletionPrepared,
        *,
        refresh_after: timedelta,
    ) -> DreamCommitReceipt:
        """@brief 记录 reducer 结果并停止 / Record the reducer result and stop."""

        assert decision.claim.activity.owner_user_id == 42
        assert decision.activity.result is not None
        assert decision.activity.result.route_key == "test:model"
        assert decision.activity.completed_at == NOW
        assert refresh_after == timedelta(hours=6)
        self.document = decision.document
        self.stop_event.set()
        snapshot = decision.plan_profile_commit(
            has_backlog=False,
            refresh_after=refresh_after,
        ).snapshot(profile_created_at=NOW)
        if snapshot is None:  # pragma: no cover - fixed model always changes.
            raise AssertionError("Changed test completion did not create a snapshot")
        return DreamProfileUpdated(snapshot)

    async def retry_dream(self, decision: DreamRetryScheduled) -> None:
        """@brief 成功场景不允许 retry / Reject retry in the success scenario."""

        raise AssertionError(decision)

    async def fail_dream(self, decision: DreamFailedFinalDecision) -> None:
        """@brief 成功场景不允许 final failure / Reject final failure in the success scenario."""

        raise AssertionError(decision)

    async def recover_expired_dream_leases(
        self,
        *,
        now: datetime,
        max_attempts: int,
        limit: int,
    ) -> int:
        """@brief 验证启动 recovery / Verify startup recovery.

        @param now recovery 截止时间 / Recovery cutoff time.
        @param max_attempts 最大尝试数 / Maximum attempts.
        @param limit 单轮上限 / Per-pass limit.
        @return 零个 recovered leases / Zero recovered leases.
        """

        assert now == NOW and max_attempts == 5 and limit == 2
        return 0


class _NoClaimStore(_Store):
    """@brief 永不领取 Dream 的 store 替身 / Store double that never claims a Dream.

    该替身让 coordinator 的下一轮负责停止测试，避免 consumer 在断言前完成工作。
    This double lets the coordinator's next pass stop the test, rather than letting a
    consumer complete work before the assertion.
    """

    async def claim_dreams(
        self,
        *,
        now: datetime,
        limit: int,
        lease_for: timedelta,
    ) -> tuple[DreamClaim, ...]:
        """@brief 始终返回空 claim 批次 / Always return an empty claim batch.

        @param now 当前时间 / Current time.
        @param limit claim 上限 / Claim limit.
        @param lease_for claim 租约 / Claim lease.
        @return 空 claim 批次 / Empty claim batch.
        """

        assert now == NOW and limit == 1 and lease_for == timedelta(seconds=30)
        return ()


class _Model:
    """@brief 固定返回 UPSERT 的 Dreaming model fake / Dreaming-model fake returning a fixed UPSERT."""

    async def dream(self, claim: DreamClaim) -> DreamResult:
        """@brief 返回带本批 provenance 的 patch / Return a patch with batch provenance."""

        return DreamResult(
            ProfilePatch(
                (
                    UpsertProfileClaim(
                        key="drink.preference",
                        kind=ProfileClaimKind.PREFERENCE,
                        statement="偏好茶",
                        confidence=ProfileConfidence.EXPLICIT,
                        evidence_event_ids=(claim.evidence[0].event_id,),
                    ),
                )
            ),
            "test:model",
            1,
        )


class _FailOnceCoordinatorSource:
    """@brief 首轮 coordinator 读取失败的 source 替身 / Source double whose first coordinator read fails."""

    def __init__(self, stop_event: asyncio.Event) -> None:
        """@brief 保存停止信号与调用次数 / Store the stop signal and invocation count."""

        self._stop_event = stop_event
        self.calls = 0

    async def read_unprojected(self, *, limit: int) -> tuple[ProfileEvidence, ...]:
        """@brief 注入一次临时数据库轮询失败 / Inject one transient database-poll failure.

        @param limit 读取上限 / Read limit.
        @return 空 evidence 批次 / Empty evidence batch.
        """

        assert limit == 4
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary profile-source database failure")
        self._stop_event.set()
        return ()


class _OneEvidenceThenStopSource:
    """@brief 首轮返回 evidence、第二轮停止的 source 替身 / Source double returning evidence once then stopping."""

    def __init__(self, stop_event: asyncio.Event) -> None:
        """@brief 保存停止信号与调用次数 / Store the stop signal and invocation count."""

        self._stop_event = stop_event
        self.calls = 0

    async def read_unprojected(self, *, limit: int) -> tuple[ProfileEvidence, ...]:
        """@brief 首轮返回 evidence，后续请求停止 / Return evidence once, then request stop.

        @param limit 读取上限 / Read limit.
        @return 第一轮 evidence，之后为空 / Evidence on the first pass, then empty.
        """

        assert limit == 4
        self.calls += 1
        if self.calls == 1:
            return (_evidence(0),)
        self._stop_event.set()
        return ()


class _BlockingCoordinatorSource:
    """@brief 阻塞直至取消的 coordinator source 替身 / Coordinator-source double that blocks until cancellation."""

    def __init__(self) -> None:
        """@brief 初始化开始同步点 / Initialize the start synchronization point."""

        self.started = asyncio.Event()

    async def read_unprojected(self, *, limit: int) -> tuple[ProfileEvidence, ...]:
        """@brief 等待外部取消 / Wait for external cancellation.

        @param limit 读取上限 / Read limit.
        @return 永不返回 / Never returns.
        """

        assert limit == 4
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("blocking coordinator source unexpectedly resumed")


class _IdleRecoverySource:
    """@brief 保持 coordinator 空闲以隔离 recovery cadence / Keep the coordinator idle to isolate recovery cadence."""

    def __init__(self) -> None:
        """@brief 初始化业务轮询计数 / Initialize the business-poll count."""

        self.calls = 0

    async def read_unprojected(self, *, limit: int) -> tuple[ProfileEvidence, ...]:
        """@brief 返回空 evidence，使 coordinator 进入长退避 / Return no evidence so the coordinator enters a long backoff.

        @param limit 读取上限 / Read limit.
        @return 始终为空的 evidence 批次 / Always-empty evidence batch.
        """

        assert limit == 4
        self.calls += 1
        return ()


class _LeaseRecoveryStore(_Store):
    """@brief 记录运行期 lease recovery 时刻的 store / Store recording runtime lease-recovery instants."""

    def __init__(
        self,
        stop_event: asyncio.Event,
        *,
        fail_first: bool = False,
    ) -> None:
        """@brief 初始化记录与可选首次故障 / Initialize recording and an optional first-pass fault."""

        super().__init__(stop_event)
        self.recovery_times: list[float] = []
        self.recovery_tasks: list[str] = []
        self._fail_first = fail_first

    async def enqueue_eligible(
        self,
        *,
        now: datetime,
        limit: int,
        max_events_per_dream: int,
        max_evidence_chars: int,
    ) -> int:
        """@brief 保持 coordinator 空闲 / Keep the coordinator idle."""

        assert now == NOW and limit == 2 and max_events_per_dream == 8
        assert max_evidence_chars == 60_000
        return 0

    async def claim_dreams(
        self,
        *,
        now: datetime,
        limit: int,
        lease_for: timedelta,
    ) -> tuple[DreamClaim, ...]:
        """@brief 不提供 job，使测试仅观察 recovery / Offer no jobs so the test observes only recovery."""

        assert now == NOW and limit == 1
        assert lease_for == timedelta(milliseconds=200)
        return ()

    async def recover_expired_dream_leases(
        self,
        *,
        now: datetime,
        max_attempts: int,
        limit: int,
    ) -> int:
        """@brief 用真实 monotonic 时间记录单 owner 回收 / Record single-owner recovery using real monotonic time.

        @param now recovery 截止时间 / Recovery cutoff time.
        @param max_attempts 最大尝试数 / Maximum attempts.
        @param limit 单轮上限 / Per-pass limit.
        @return 零个 recovered leases / Zero recovered leases.
        """

        assert now == NOW and max_attempts == 5 and limit == 2
        self.recovery_times.append(time.monotonic())
        task = asyncio.current_task()
        self.recovery_tasks.append(task.get_name() if task is not None else "")
        if len(self.recovery_times) == 2:
            self.stop_event.set()
        if self._fail_first and len(self.recovery_times) == 1:
            raise RuntimeError("temporary Dream lease-recovery failure")
        return 0


class _FailOnceProfileTelemetry(Telemetry):
    """@brief 第一次 counter 失败的 telemetry 替身 / Telemetry double whose first counter fails."""

    def __init__(self) -> None:
        """@brief 初始化基础缓冲与失败开关 / Initialize the base buffer and failure switch."""

        super().__init__(TelemetryBuffer(64))
        self._fail_next_counter = True

    def counter(
        self,
        name: str,
        value: float = 1.0,
        *,
        unit: str = "{event}",
        attributes: Mapping[str, object] | None = None,
    ) -> bool:
        """@brief 模拟一次 telemetry 发射失败 / Simulate one telemetry-emission failure.

        @param name metric 名称 / Metric name.
        @param value metric 值 / Metric value.
        @param unit metric 单位 / Metric unit.
        @param attributes metric 属性 / Metric attributes.
        @return 缓冲接收结果 / Buffer acceptance result.
        """

        if self._fail_next_counter:
            self._fail_next_counter = False
            raise ValueError("Span duration cannot be negative")
        return super().counter(name, value, unit=unit, attributes=attributes)


class _ExitFailingTelemetryClock:
    """@brief 仅在 span 退出读取时失败的 telemetry clock / Telemetry clock failing only when a span exits."""

    def __init__(self) -> None:
        """@brief 初始化 monotonic 读取次数 / Initialize the monotonic-read count.

        @return None / None.
        """

        self.monotonic_calls = 0

    def now(self) -> datetime:
        """@brief 返回固定 telemetry 墙钟 / Return a fixed telemetry wall clock.

        @return 固定 UTC 时刻 / Fixed UTC instant.
        """

        return NOW

    def monotonic_ns(self) -> int:
        """@brief 首次进入成功、退出时失败 / Succeed on entry and fail on exit.

        @return 首次读取的单调值 / Monotonic value on the first read.
        @raise RuntimeError span 退出故障 / Span-exit fault.
        """

        self.monotonic_calls += 1
        if self.monotonic_calls > 1:
            raise RuntimeError("telemetry span exit failed")
        return 1


class _EntryFailingTelemetryClock:
    """@brief span 进入即失败的 telemetry clock / Telemetry clock failing when a span enters."""

    def now(self) -> datetime:
        """@brief 模拟 span 进入故障 / Simulate a span-entry fault.

        @return 不返回 / Does not return.
        @raise RuntimeError span 进入故障 / Span-entry fault.
        """

        raise RuntimeError("telemetry span entry failed")

    def monotonic_ns(self) -> int:
        """@brief 提供未使用的单调时钟值 / Provide an unused monotonic-clock value.

        @return 固定单调值 / Fixed monotonic value.
        """

        return 1


class _CountingDreamingModel:
    """@brief 记录 provider 调用次数的 Dreaming model / Dreaming model recording provider-call count."""

    def __init__(self) -> None:
        """@brief 初始化调用计数 / Initialize call count.

        @return None / None.
        """

        self.calls = 0

    async def dream(self, claim: DreamClaim) -> DreamResult:
        """@brief 记录调用并委托固定模型 / Record the call and delegate to the fixed model.

        @param claim 冻结 Dream claim / Frozen Dream claim.
        @return 合法 changed result / Valid changed result.
        """

        self.calls += 1
        return await _Model().dream(claim)


class _RetryableDreamingModel:
    """@brief 总是报告可重试 provider 故障的模型 / Model always reporting a retryable provider fault."""

    def __init__(self) -> None:
        """@brief 初始化调用计数 / Initialize call count.

        @return None / None.
        """

        self.calls = 0

    async def dream(self, claim: DreamClaim) -> DreamResult:
        """@brief 记录调用并抛出可重试故障 / Record the call and raise a retryable fault.

        @param claim 冻结 Dream claim / Frozen Dream claim.
        @return 不返回 / Does not return.
        @raise RetryableDreamingError 模拟 provider 暂时不可用 /
            Simulated transient provider unavailability.
        """

        del claim
        self.calls += 1
        raise RetryableDreamingError("provider unavailable")


class _SettlementBoundaryStore(_Store):
    """@brief 记录所有 Dream settlement 的边界 store / Boundary store recording every Dream settlement."""

    def __init__(
        self,
        *,
        completion_mode: str = "valid",
        retry_mode: str = "valid",
        fail_mode: str = "valid",
    ) -> None:
        """@brief 配置各 settlement acknowledgment 行为 / Configure each settlement-acknowledgment behavior.

        @param completion_mode ``valid``、``unknown``、``wrong_receipt`` 或 ``blocked`` /
            ``valid``, ``unknown``, ``wrong_receipt``, or ``blocked``.
        @param retry_mode ``valid`` 或 ``unknown`` / ``valid`` or ``unknown``.
        @param fail_mode ``valid`` 或 ``unknown`` / ``valid`` or ``unknown``.
        @return None / None.
        @raise ValueError 任一 settlement mode 非法 / Any settlement mode is invalid.
        """

        if completion_mode not in {"valid", "unknown", "wrong_receipt", "blocked"}:
            raise ValueError("Unknown settlement-boundary test mode")
        if retry_mode not in {"valid", "unknown"}:
            raise ValueError("Unknown retry-settlement test mode")
        if fail_mode not in {"valid", "unknown"}:
            raise ValueError("Unknown final-settlement test mode")
        super().__init__(asyncio.Event())
        self.completion_mode = completion_mode
        self.retry_mode = retry_mode
        self.fail_mode = fail_mode
        self.complete_calls = 0
        self.retry_calls = 0
        self.fail_calls = 0
        self.completion_started = asyncio.Event()
        self.completion_release = asyncio.Event()
        self.retry_decision: DreamRetryScheduled | None = None
        self.final_decision: DreamFailedFinalDecision | None = None

    async def complete_dream(
        self,
        decision: DreamCompletionPrepared,
        *,
        refresh_after: timedelta,
    ) -> DreamCommitReceipt:
        """@brief 模拟 ACK unknown、错误回执或正常提交 / Simulate unknown ACK, wrong receipt, or normal commit.

        @param decision completion 决定 / Completion decision.
        @param refresh_after refresh delay / Refresh delay.
        @return 配置的 durable receipt / Configured durable receipt.
        @raise RuntimeError 模拟 commit outcome unknown / Simulated unknown commit outcome.
        """

        self.complete_calls += 1
        self.completion_started.set()
        if self.completion_mode == "blocked":
            await self.completion_release.wait()
        if self.completion_mode == "unknown":
            self.document = decision.document
            raise RuntimeError("commit acknowledgment was lost")
        receipt = await super().complete_dream(
            decision,
            refresh_after=refresh_after,
        )
        if self.completion_mode == "wrong_receipt":
            return DreamProfileUnchanged(
                owner_user_id=decision.claim.activity.owner_user_id,
                retained_revision=decision.claim.activity.baseline.revision,
                scheduler_head_event_id=decision.claim.activity.through_event_id,
            )
        return receipt

    async def retry_dream(self, decision: DreamRetryScheduled) -> None:
        """@brief 记录 retry commit 并可选丢失 ACK / Record the retry commit and optionally lose its ACK.

        @param decision retry 决定 / Retry decision.
        @return None / None.
        @raise RuntimeError 模拟 commit 后 acknowledgment 丢失 /
            Simulated acknowledgment loss after the commit.
        """

        self.retry_calls += 1
        self.retry_decision = decision
        if self.retry_mode == "unknown":
            raise RuntimeError("retry commit acknowledgment was lost")

    async def fail_dream(self, decision: DreamFailedFinalDecision) -> None:
        """@brief 记录 final commit 并可选丢失 ACK / Record the final commit and optionally lose its ACK.

        @param decision final-failure 决定 / Final-failure decision.
        @return None / None.
        @raise RuntimeError 模拟 commit 后 acknowledgment 丢失 /
            Simulated acknowledgment loss after the commit.
        """

        self.fail_calls += 1
        self.final_decision = decision
        if self.fail_mode == "unknown":
            raise RuntimeError("final commit acknowledgment was lost")


def _boundary_worker(
    *,
    store: _SettlementBoundaryStore,
    model: DreamingModel,
    telemetry: Telemetry,
    max_attempts: int = 5,
    clock: UtcClock | None = None,
) -> DreamingWorker:
    """@brief 构造直接执行单 claim 的边界 worker / Build a boundary worker that processes one claim directly.

    @param store settlement recorder / Settlement recorder.
    @param model provider-call recorder / Provider-call recorder.
    @param telemetry observability recorder / Observability recorder.
    @param max_attempts 最大尝试数 / Maximum attempts.
    @param clock 可替换业务时钟 / Replaceable business clock.
    @return 配置好的 worker / Configured worker.
    """

    return DreamingWorker(
        source=_Source(),
        store=store,
        model=model,
        telemetry=telemetry,
        polling_policy=AdaptivePollingPolicy(0.001, 0.004, jitter_ratio=0.0),
        worker_count=1,
        batch_size=2,
        source_batch_size=4,
        max_events_per_dream=8,
        refresh_after=timedelta(hours=6),
        attempt_timeout=timedelta(seconds=20),
        lease_for=timedelta(seconds=30),
        max_attempts=max_attempts,
        clock=clock or _Clock(),
    )


def _resilient_worker(
    *,
    source: object,
    store: _Store,
    telemetry: Telemetry,
) -> DreamingWorker:
    """@brief 构造只验证故障隔离的 Dreaming worker / Build a Dreaming worker used only for fault-isolation checks.

    @param source 测试 evidence source / Test evidence source.
    @param store 测试 Profile store / Test Profile store.
    @param telemetry 测试 telemetry recorder / Test telemetry recorder.
    @return 配置好的 Dreaming worker / Configured Dreaming worker.
    """

    return DreamingWorker(
        source=source,  # type: ignore[arg-type]
        store=store,
        model=_Model(),
        telemetry=telemetry,
        worker_count=1,
        batch_size=2,
        source_batch_size=4,
        max_events_per_dream=8,
        polling_policy=AdaptivePollingPolicy(0.001, 0.004, jitter_ratio=0.0),
        refresh_after=timedelta(hours=6),
        attempt_timeout=timedelta(seconds=20),
        lease_for=timedelta(seconds=30),
        clock=_Clock(),
    )


def test_worker_has_one_source_owner_and_model_consumers_only_claim_jobs() -> None:
    """@brief 只有 coordinator 扫 source，N consumers 仅 claim jobs / Only the coordinator scans sources while N consumers claim jobs."""

    async def scenario() -> None:
        """@brief 运行完整成功路径 / Run the complete success path."""

        stop_event = asyncio.Event()
        source = _Source()
        store = _Store(stop_event)
        worker = DreamingWorker(
            source=source,
            store=store,
            model=_Model(),
            telemetry=Telemetry(TelemetryBuffer(64)),
            worker_count=4,
            batch_size=2,
            source_batch_size=4,
            max_events_per_dream=8,
            polling_policy=AdaptivePollingPolicy(0.001, 0.004, jitter_ratio=0.0),
            refresh_after=timedelta(hours=6),
            attempt_timeout=timedelta(seconds=20),
            lease_for=timedelta(seconds=30),
            clock=_Clock(),
        )

        await asyncio.wait_for(worker.run(stop_event), timeout=1)

        assert set(source.reader_tasks) == {"dreaming-coordinator"}
        assert store.claim_tasks
        assert all(name.startswith("dreaming-model:") for name in store.claim_tasks)
        assert store.document is not None
        assert store.document.claims[0].statement == "偏好茶"

    asyncio.run(scenario())


def test_transient_coordinator_poll_failure_does_not_escape_dreaming_task_group() -> (
    None
):
    """@brief 单次 coordinator 轮询错误不会终止 Dreaming TaskGroup / One coordinator-poll error does not terminate the Dreaming TaskGroup."""

    async def scenario() -> None:
        """@brief 验证第二轮仍会运行并干净停止 / Verify the next pass runs and stops cleanly."""

        stop_event = asyncio.Event()
        source = _FailOnceCoordinatorSource(stop_event)
        worker = _resilient_worker(
            source=source,
            store=_Store(stop_event),
            telemetry=Telemetry(TelemetryBuffer(64)),
        )

        await asyncio.wait_for(worker.run(stop_event), timeout=1)

        assert source.calls >= 2

    asyncio.run(scenario())


def test_telemetry_failure_does_not_escape_dreaming_task_group() -> None:
    """@brief 单次 telemetry 错误不会终止 Dreaming TaskGroup / One telemetry error does not terminate the Dreaming TaskGroup."""

    async def scenario() -> None:
        """@brief 在下一轮停止，验证首轮错误已被隔离 / Stop on the next pass, proving the first fault was isolated."""

        stop_event = asyncio.Event()
        source = _OneEvidenceThenStopSource(stop_event)
        worker = _resilient_worker(
            source=source,
            store=_NoClaimStore(stop_event),
            telemetry=_FailOnceProfileTelemetry(),
        )

        await asyncio.wait_for(worker.run(stop_event), timeout=1)

        assert source.calls >= 2

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "fault",
    ("settlement_unknown", "wrong_receipt", "counter", "span_exit"),
)
def test_settlement_and_post_commit_faults_never_resettle_the_claim(
    fault: str,
) -> None:
    """@brief settlement outcome unknown 与提交后故障不得二次 retry/fail / Unknown settlement outcomes and post-commit faults must never retry or fail the claim again.

    @param fault 注入的边界故障 / Injected boundary fault.
    @return None / None.
    """

    async def scenario() -> None:
        """@brief 处理单 claim 并检查只发生一次 completion settlement / Process one claim and verify a single completion settlement.

        @return None / None.
        """

        mode = {
            "settlement_unknown": "unknown",
            "wrong_receipt": "wrong_receipt",
        }.get(fault, "valid")
        store = _SettlementBoundaryStore(completion_mode=mode)
        model = _CountingDreamingModel()
        if fault == "counter":
            telemetry = _FailOnceProfileTelemetry()
        elif fault == "span_exit":
            telemetry = Telemetry(
                TelemetryBuffer(64),
                clock=_ExitFailingTelemetryClock(),
            )
        else:
            telemetry = Telemetry(TelemetryBuffer(64))
        worker = _boundary_worker(
            store=store,
            model=model,
            telemetry=telemetry,
        )

        await worker._process(_claim())

        assert model.calls == 1
        assert store.complete_calls == 1
        assert store.retry_calls == 0
        assert store.fail_calls == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("settlement", "max_attempts"),
    (
        pytest.param("retry", 5, id="retry"),
        pytest.param("fail", 1, id="final-failure"),
    ),
)
@pytest.mark.parametrize(
    "post_commit_fault",
    ("ack_unknown", "counter"),
)
def test_failure_settlement_post_commit_faults_never_resettle_the_claim(
    settlement: str,
    max_attempts: int,
    post_commit_fault: str,
) -> None:
    """@brief retry/final commit 后故障不得对同一 claim 再结算 / Faults after retry/final commit must not resettle the same claim.

    @param settlement 预期的 failure settlement 类型 / Expected failure-settlement kind.
    @param max_attempts 决定 retry 或 final 的最大尝试数 / Attempt limit selecting retry or final.
    @param post_commit_fault commit 后注入的 ACK 或 telemetry 故障 /
        ACK or telemetry fault injected after commit.
    @return None / None.
    """

    async def scenario() -> None:
        """@brief 触发 failure settlement 并验证唯一写边界 / Trigger failure settlement and verify the single write boundary.

        @return None / None.
        """

        unknown_ack = post_commit_fault == "ack_unknown"
        store = _SettlementBoundaryStore(
            retry_mode="unknown" if unknown_ack and settlement == "retry" else "valid",
            fail_mode="unknown" if unknown_ack and settlement == "fail" else "valid",
        )
        telemetry = (
            _FailOnceProfileTelemetry()
            if post_commit_fault == "counter"
            else Telemetry(TelemetryBuffer(64))
        )
        worker = _boundary_worker(
            store=store,
            model=_RetryableDreamingModel(),
            telemetry=telemetry,
            max_attempts=max_attempts,
        )
        expected_error = RuntimeError if unknown_ack else ValueError

        with pytest.raises(expected_error):
            await worker._process(_claim())

        assert store.complete_calls == 0
        assert store.retry_calls == (settlement == "retry")
        assert store.fail_calls == (settlement == "fail")
        assert (store.retry_decision is not None) == (settlement == "retry")
        assert (store.final_decision is not None) == (settlement == "fail")

    asyncio.run(scenario())


def test_completion_cancellation_propagates_without_resettling_the_claim() -> None:
    """@brief completion 阻塞期取消必须传播且不得 retry/fail / Cancellation during blocked completion must propagate without retry/fail.

    @return None / None.
    """

    async def scenario() -> None:
        """@brief 在 store completion 内取消任务并验证 settlement 计数 / Cancel inside store completion and verify settlement counts.

        @return None / None.
        """

        store = _SettlementBoundaryStore(completion_mode="blocked")
        model = _CountingDreamingModel()
        worker = _boundary_worker(
            store=store,
            model=model,
            telemetry=Telemetry(TelemetryBuffer(64)),
        )
        task = asyncio.create_task(worker._process(_claim()))
        await asyncio.wait_for(store.completion_started.wait(), timeout=1)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert model.calls == 1
        assert store.complete_calls == 1
        assert store.retry_calls == 0
        assert store.fail_calls == 0

    asyncio.run(scenario())


def test_span_entry_failure_does_not_block_dream_business_processing() -> None:
    """@brief span 进入失败仍执行并完成 Dream / A span-entry failure still executes and completes the Dream.

    @return None / None.
    """

    async def scenario() -> None:
        """@brief 注入 span-entry fault 并验证业务 settlement / Inject a span-entry fault and verify business settlement.

        @return None / None.
        """

        store = _SettlementBoundaryStore()
        model = _CountingDreamingModel()
        worker = _boundary_worker(
            store=store,
            model=model,
            telemetry=Telemetry(
                TelemetryBuffer(64),
                clock=_EntryFailingTelemetryClock(),
            ),
        )

        await worker._process(_claim())

        assert model.calls == 1
        assert store.complete_calls == 1
        assert store.retry_calls == 0
        assert store.fail_calls == 0

    asyncio.run(scenario())


def test_span_exit_failure_preserves_retryable_business_error() -> None:
    """@brief span 退出错误不得覆盖 provider 可重试错误 / A span-exit fault must not mask a retryable provider error.

    @return None / None.
    """

    async def scenario() -> None:
        """@brief 同时注入 provider 与 span-exit 故障并验证 retry / Inject provider and span-exit faults and verify retry.

        @return None / None.
        """

        store = _SettlementBoundaryStore()
        model = _RetryableDreamingModel()
        worker = _boundary_worker(
            store=store,
            model=model,
            telemetry=Telemetry(
                TelemetryBuffer(64),
                clock=_ExitFailingTelemetryClock(),
            ),
        )

        await worker._process(_claim())

        assert model.calls == 1
        assert store.complete_calls == 0
        assert store.retry_calls == 1
        assert store.fail_calls == 0

    asyncio.run(scenario())


def test_claim_beyond_attempt_budget_fails_without_calling_provider() -> None:
    """@brief 已超预算 claim 防御性终败且不再调用 provider / A claim beyond its attempt budget fails defensively without another provider call.

    @return None / None.
    """

    async def scenario() -> None:
        """@brief 构造第二次 claim 并以 max_attempts=1 处理 / Build a second claim and process it with max_attempts=1.

        @return None / None.
        """

        first = _claim()
        second_claimed_at = NOW + timedelta(seconds=1)
        retry = first.record_failure(
            failed_at=NOW,
            failure=DreamFailure("simulated crash recovery"),
        ).schedule_retry(retry_at=second_claimed_at)
        over_budget = retry.activity.claim(
            token=DreamLeaseToken.parse(UUID("00000000-0000-0000-0000-000000000087")),
            claimed_at=second_claimed_at,
            lease_expires_at=second_claimed_at + timedelta(seconds=30),
            current_document=first.current_document,
            evidence=first.evidence,
        )
        assert over_budget.activity.attempt_count == 2
        store = _SettlementBoundaryStore()
        model = _CountingDreamingModel()
        worker = _boundary_worker(
            store=store,
            model=model,
            telemetry=Telemetry(TelemetryBuffer(64)),
            max_attempts=1,
            clock=_ClockAt(second_claimed_at),
        )

        await worker._process(over_budget)

        assert model.calls == 0
        assert store.complete_calls == 0
        assert store.retry_calls == 0
        assert store.fail_calls == 1
        assert store.final_decision is not None
        assert store.final_decision.activity.status.value == "failed_final"

    asyncio.run(scenario())


@pytest.mark.parametrize("fail_first", [False, True], ids=["steady", "retry"])
def test_dreaming_recovery_cadence_is_independent_of_business_polling(
    fail_first: bool,
) -> None:
    """@brief Dreaming 的单 owner recovery 不受长业务退避影响 / Dreaming's single-owner recovery is independent of long business backoff.

    @param fail_first 首次恢复是否注入故障 / Whether the first recovery attempt fails.
    @return None / None.
    """

    async def scenario() -> None:
        """@brief 用真实 monotonic 时间观察立即恢复与 100ms cadence / Observe immediate recovery and a 100 ms cadence using real monotonic time."""

        stop_event = asyncio.Event()
        source = _IdleRecoverySource()
        store = _LeaseRecoveryStore(stop_event, fail_first=fail_first)
        worker = DreamingWorker(
            source=source,
            store=store,
            model=_Model(),
            telemetry=Telemetry(TelemetryBuffer(64)),
            worker_count=1,
            batch_size=2,
            source_batch_size=4,
            max_events_per_dream=8,
            polling_policy=AdaptivePollingPolicy(2.0, 2.0, jitter_ratio=0.0),
            refresh_after=timedelta(hours=6),
            attempt_timeout=timedelta(milliseconds=50),
            lease_for=timedelta(milliseconds=200),
            clock=_Clock(),
        )

        started = time.monotonic()
        await asyncio.wait_for(worker.run(stop_event), timeout=1)

        assert source.calls >= 1
        assert len(store.recovery_times) == 2
        assert store.recovery_times[0] - started < 0.5
        recovery_gap = store.recovery_times[1] - store.recovery_times[0]
        assert 0.075 <= recovery_gap < 0.5
        assert set(store.recovery_tasks) == {"dreaming-recovery"}

    asyncio.run(scenario())


def test_dreaming_poll_cancellation_still_propagates() -> None:
    """@brief Dreaming 轮询取消不得被故障隔离吞掉 / Dreaming-poll cancellation must not be swallowed by fault isolation."""

    async def scenario() -> None:
        """@brief 取消阻塞的 coordinator 并验证 CancelledError / Cancel a blocked coordinator and verify CancelledError."""

        stop_event = asyncio.Event()
        source = _BlockingCoordinatorSource()
        worker = _resilient_worker(
            source=source,
            store=_Store(stop_event),
            telemetry=Telemetry(TelemetryBuffer(64)),
        )
        task = asyncio.create_task(worker.run(stop_event))
        await asyncio.wait_for(source.started.wait(), timeout=1)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
