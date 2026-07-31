"""@brief durable artifact 领域状态矩阵与不变量测试 / Durable-artifact domain state matrix and invariant tests."""

import ast
import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from fogmoe_bot.domain.media.artifact import (
    ArtifactClaimCapability,
    ArtifactClaimExpiredDecision,
    ArtifactClaimToken,
    ArtifactClaimed,
    ArtifactCompletionDecision,
    ArtifactKind,
    ArtifactRecord,
    ArtifactRecoveryBlockedDecision,
    ArtifactRecoveryReadyDecision,
)
from fogmoe_bot.domain.media.identifiers import ArtifactId

PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""@brief 静态分层测试的项目根 / Project root for static layering tests."""
SRC_ROOT = PROJECT_ROOT / "src" / "fogmoe_bot"
"""@brief FOGMOE Python 源码根 / FOGMOE Python source root."""
CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)
"""@brief 状态矩阵的固定创建时刻 / Fixed creation instant for the state matrix."""


def _record(*, ttl: timedelta = timedelta(minutes=5)) -> ArtifactRecord:
    """@brief 创建规范测试制品 / Create a canonical test artifact.

    @param ttl 制品存活时间 / Artifact time to live.
    @return available 记录 / Available record.
    """

    return ArtifactRecord.create(
        artifact_id=ArtifactId("a" * 32),
        kind=ArtifactKind.IMAGE,
        filename="image.png",
        mime_type="image/png",
        size_bytes=11,
        created_at=CREATED_AT,
        ttl=ttl,
    )


def _capability(
    *,
    artifact_id: ArtifactId = ArtifactId("a" * 32),
) -> ArtifactClaimCapability:
    """@brief 签发固定测试 capability / Issue a fixed test capability.

    @param artifact_id capability 所属制品 / Artifact owned by the capability.
    @return 三十秒租约 capability / Capability with a thirty-second lease.
    """

    return ArtifactClaimCapability.issue(
        artifact_id=artifact_id,
        token=ArtifactClaimToken("b" * 32),
        claimed_at=CREATED_AT,
        lease_duration=timedelta(seconds=30),
    )


def test_artifact_lifecycle_decisions_preserve_identity() -> None:
    """@brief lifecycle 状态与决策保留聚合身份 / Lifecycle states and decisions preserve aggregate identity."""

    record = _record()
    available = record.available()
    claimed = available.claim(capability=_capability(), claimed_at=CREATED_AT)
    assert isinstance(claimed, ArtifactClaimed)

    active = claimed.recover(CREATED_AT + timedelta(seconds=29))
    recovered = claimed.recover(CREATED_AT + timedelta(seconds=30))
    released = claimed.release()
    completed = claimed.complete(ArtifactClaimToken("c" * 32))

    assert isinstance(active, ArtifactRecoveryBlockedDecision)
    assert active.claim is claimed
    assert active.observed_at == CREATED_AT + timedelta(seconds=29)
    assert isinstance(recovered, ArtifactRecoveryReadyDecision)
    assert recovered.claim is claimed
    assert recovered.observed_at == CREATED_AT + timedelta(seconds=30)
    assert released.record is record
    assert isinstance(completed, ArtifactCompletionDecision)
    assert completed.record is record
    assert str(completed.token) == "c" * 32


def test_claim_at_exact_artifact_expiry_is_an_expired_terminal_decision() -> None:
    """@brief 制品 TTL 边界为左开始过期 / Artifact TTL expires exactly at its upper boundary."""

    record = _record(ttl=timedelta(seconds=30))
    capability = ArtifactClaimCapability.issue(
        artifact_id=record.artifact_id,
        token=ArtifactClaimToken("d" * 32),
        claimed_at=CREATED_AT + timedelta(seconds=30),
        lease_duration=timedelta(seconds=30),
    )

    decision = record.available().claim(
        capability=capability,
        claimed_at=CREATED_AT + timedelta(seconds=30),
    )

    assert isinstance(decision, ArtifactClaimExpiredDecision)
    assert decision.record is record
    assert decision.observed_at == record.expires_at


def test_record_and_capability_normalize_offset_instants_to_utc() -> None:
    """@brief record 与 capability 仅保存规范 UTC 时刻 / Records and capabilities store only canonical UTC instants."""

    offset = timezone(timedelta(hours=8))
    local_created = datetime(2026, 1, 1, 8, tzinfo=offset)
    record = ArtifactRecord.create(
        artifact_id=ArtifactId("e" * 32),
        kind=ArtifactKind.AUDIO,
        filename="voice.ogg",
        mime_type="audio/ogg",
        size_bytes=7,
        created_at=local_created,
        ttl=timedelta(minutes=5),
    )
    capability = ArtifactClaimCapability.issue(
        artifact_id=record.artifact_id,
        token=ArtifactClaimToken("f" * 32),
        claimed_at=local_created,
        lease_duration=timedelta(seconds=30),
    )

    assert record.created_at == CREATED_AT
    assert record.created_at.tzinfo is UTC
    assert record.expires_at == CREATED_AT + timedelta(minutes=5)
    assert record.expires_at.tzinfo is UTC
    assert capability.lease_expires_at == CREATED_AT + timedelta(seconds=30)
    assert capability.lease_expires_at.tzinfo is UTC
    with pytest.raises(FrozenInstanceError):
        record.filename = "mutated.png"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        capability.lease_expires_at = CREATED_AT  # type: ignore[misc]


def test_artifact_invariant_gate_rejects_invalid_identity_metadata_and_time() -> None:
    """@brief 标识、manifest 元数据与时间顺序不能绕过统一门 / Identity, metadata, and time cannot bypass the invariant gate."""

    with pytest.raises(TypeError, match="factory"):
        ArtifactRecord()
    with pytest.raises(TypeError, match="factory"):
        ArtifactClaimCapability()
    with pytest.raises(ValueError, match="32 lowercase hexadecimal"):
        ArtifactId("invalid")
    with pytest.raises(ValueError, match="32 lowercase hexadecimal"):
        ArtifactClaimToken("invalid")
    with pytest.raises(TypeError, match="size_bytes must be an integer"):
        ArtifactRecord.restore(
            artifact_id=ArtifactId("a" * 32),
            kind=ArtifactKind.IMAGE,
            filename="image.png",
            mime_type="image/png",
            size_bytes=True,  # type: ignore[arg-type]
            created_at=CREATED_AT,
            expires_at=CREATED_AT + timedelta(minutes=1),
        )
    with pytest.raises(ValueError, match="after created_at"):
        ArtifactRecord.restore(
            artifact_id=ArtifactId("a" * 32),
            kind=ArtifactKind.IMAGE,
            filename="image.png",
            mime_type="image/png",
            size_bytes=1,
            created_at=CREATED_AT,
            expires_at=CREATED_AT,
        )
    with pytest.raises(ValueError, match="identity does not match"):
        _record().available().claim(
            capability=_capability(artifact_id=ArtifactId("9" * 32)),
            claimed_at=CREATED_AT,
        )
    with pytest.raises(ValueError, match="before its creation"):
        _record().available().claim(
            capability=ArtifactClaimCapability.issue(
                artifact_id=ArtifactId("a" * 32),
                token=ArtifactClaimToken("8" * 32),
                claimed_at=CREATED_AT - timedelta(seconds=1),
                lease_duration=timedelta(seconds=30),
            ),
            claimed_at=CREATED_AT - timedelta(seconds=1),
        )
    with pytest.raises(ValueError, match="after artifact creation"):
        ArtifactClaimed(
            _record(),
            ArtifactClaimCapability.restore(
                artifact_id=ArtifactId("a" * 32),
                token=ArtifactClaimToken("7" * 32),
                lease_expires_at=CREATED_AT,
            ),
        )
    claimed = ArtifactClaimed(_record(), _capability())
    with pytest.raises(ValueError, match="cannot be blocked"):
        ArtifactRecoveryBlockedDecision(
            claimed,
            CREATED_AT + timedelta(seconds=30),
        )
    with pytest.raises(ValueError, match="cannot precede"):
        ArtifactRecoveryReadyDecision(
            claimed,
            CREATED_AT + timedelta(seconds=29),
        )


def test_artifact_id_preserves_persistence_path_and_equality_semantics() -> None:
    """@brief 强类型 ID 保留 JSON、路径与值相等语义 / Strong IDs preserve JSON, path, and value-equality semantics."""

    artifact_id = ArtifactId("1" * 32)

    assert str(artifact_id) == "1" * 32
    assert json.loads(json.dumps({"artifact_id": str(artifact_id)})) == {
        "artifact_id": "1" * 32
    }
    assert Path(f"{artifact_id}.json").name == f"{'1' * 32}.json"
    assert artifact_id == ArtifactId("1" * 32)
    assert {artifact_id: "record"}[ArtifactId("1" * 32)] == "record"


def test_artifact_domain_has_no_filesystem_or_infrastructure_dependency() -> None:
    """@brief 静态保证文件路径与 rename 不泄漏进领域 / Statically keep paths and renames out of the domain."""

    domain_path = SRC_ROOT / "domain" / "media" / "artifact.py"
    adapter_path = SRC_ROOT / "infrastructure" / "media" / "file_artifact_store.py"
    domain_text = domain_path.read_text(encoding="utf-8")
    adapter_text = adapter_path.read_text(encoding="utf-8")
    domain_classes = {
        node.name
        for node in ast.parse(domain_text).body
        if isinstance(node, ast.ClassDef)
    }

    assert {
        "ArtifactAvailable",
        "ArtifactClaimed",
        "ArtifactCompletionDecision",
        "ArtifactRecoveryBlockedDecision",
        "ArtifactRecoveryReadyDecision",
        "ArtifactClaimExpiredDecision",
    } <= domain_classes
    assert "ArtifactLeaseActive" not in domain_classes
    assert "ArtifactLeaseRecoverable" not in domain_classes
    assert "from pathlib" not in domain_text
    assert "os.rename" not in domain_text
    assert "fogmoe_bot.infrastructure" not in domain_text
    assert "@property" not in domain_text
    assert "type ArtifactRecoveryDecision = (" in domain_text
    assert "ArtifactState" not in domain_text
    assert "decide_recovery" not in domain_text
    assert "record.available().claim(" in adapter_text
    assert ".release()" in adapter_text
    assert ".complete(" in adapter_text
    assert ".recover(" in adapter_text
    assert "os.rename" in adapter_text
