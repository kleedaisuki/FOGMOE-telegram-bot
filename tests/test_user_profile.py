"""@brief User Profile 领域、模型 adapter 与 Dreaming worker 测试 / User Profile domain, model-adapter, and Dreaming-worker tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from fogmoe_bot.application.assistant.completion import AssistantCompletion
from fogmoe_bot.application.observability.telemetry import Telemetry, TelemetryBuffer
from fogmoe_bot.application.runtime import UtcClock
from fogmoe_bot.application.user_profile.ports import DreamClaim, DreamResult
from fogmoe_bot.application.user_profile.worker import DreamingWorker
from fogmoe_bot.domain.assistant.routing.models import ProviderRoute
from fogmoe_bot.domain.user_profile.models import (
    DeleteProfileClaim,
    DreamId,
    ProfileClaim,
    ProfileClaimKind,
    ProfileConfidence,
    ProfileDocument,
    ProfileEvidence,
    ProfileMetadata,
    ProfilePatch,
    UpsertProfileClaim,
    apply_profile_patch,
)
from fogmoe_bot.infrastructure.database import connection as db_connection
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
    return DreamClaim(
        dream_id=DreamId(UUID("00000000-0000-0000-0000-000000000099")),
        owner_user_id=42,
        base_revision=0,
        base_observed_through_event_id=0,
        through_event_id=sources[-1].event_id,
        current_document=ProfileDocument(),
        evidence=sources,
        metadata=_metadata(),
        claim_token=UUID("00000000-0000-0000-0000-000000000088"),
        attempt_count=1,
    )


def test_profile_reducer_requires_current_batch_provenance_and_updates_by_stable_key() -> (
    None
):
    """@brief reducer 只接受批内 provenance 且以稳定 key supersede / Reducer accepts only in-batch provenance and supersedes by stable key."""

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
    updated = apply_profile_patch(
        old,
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
        apply_profile_patch(
            old,
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


class _Completion:
    """@brief 返回固定结构化 JSON 的 completion fake / Completion fake returning fixed structured JSON."""

    def __init__(self, content: str) -> None:
        """@brief 保存输出 / Store output."""

        self._content = content
        self.messages: object = None

    async def complete(self, **kwargs: object) -> AssistantCompletion:
        """@brief 记录 request 并返回输出 / Record the request and return output."""

        self.messages = kwargs["messages"]
        return AssistantCompletion(
            self._content,
            {"role": "assistant", "content": self._content},
        )


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
            service_order=("test",),
            profiles={
                "test": ProviderRoute(
                    service_name="test",
                    provider_name="openai",
                    display_name="Test",
                    models=("profile-model",),
                    completion_kwargs={},
                )
            },
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

        monkeypatch.setattr(db_connection, "fetch_all", fake_fetch_all)
        assert await PostgresProfileEvidenceSource().read_unprojected(limit=8) == ()
        assert len(calls) == 1
        sql, params = calls[0]
        assert "turn.source_kind = %s" in sql
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

    async def read_profile(self, user_id: int):
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

    async def claim_dreams(self, *, now: datetime, limit: int, lease_for: timedelta):
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
        claim: DreamClaim,
        result: DreamResult,
        *,
        document: ProfileDocument,
        completed_at: datetime,
        refresh_after: timedelta,
    ):
        """@brief 记录 reducer 结果并停止 / Record the reducer result and stop."""

        assert claim.owner_user_id == 42
        assert result.route_key == "test:model"
        assert completed_at == NOW and refresh_after == timedelta(hours=6)
        self.document = document
        self.stop_event.set()
        return None

    async def retry_dream(self, claim: DreamClaim, **kwargs: object) -> None:
        """@brief 成功场景不允许 retry / Reject retry in the success scenario."""

        raise AssertionError((claim, kwargs))

    async def fail_dream(self, claim: DreamClaim, **kwargs: object) -> None:
        """@brief 成功场景不允许 final failure / Reject final failure in the success scenario."""

        raise AssertionError((claim, kwargs))

    async def recover_expired_dream_leases(self, *, now: datetime) -> int:
        """@brief 验证启动 recovery / Verify startup recovery."""

        assert now == NOW
        return 0


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


def test_worker_has_one_source_owner_and_model_consumers_only_claim_jobs() -> None:
    """@brief 只有 coordinator 扫 source，N consumers 仅 claim jobs / Only the coordinator scans sources while N consumers claim jobs."""

    async def scenario() -> None:
        """@brief 运行完整成功路径 / Run the complete success path."""

        stop_event = asyncio.Event()
        source = _Source()
        store = _Store(stop_event)
        worker = DreamingWorker(
            source=source,
            store=store,  # type: ignore[arg-type]
            model=_Model(),
            telemetry=Telemetry(TelemetryBuffer(64)),
            worker_count=4,
            batch_size=2,
            source_batch_size=4,
            max_events_per_dream=8,
            poll_interval=0.001,
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
