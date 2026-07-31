"""@brief durable file artifact store 的故障与竞争测试 / Fault and race tests for the durable file-artifact store."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import fogmoe_bot.infrastructure.media.file_artifact_store as artifact_store_module
from fogmoe_bot.domain.media.artifact import ArtifactKind
from fogmoe_bot.infrastructure.media.file_artifact_store import (
    FileArtifactClaim,
    FileArtifactStore,
)


def _store(root, *, now: datetime | None = None):
    """@brief 创建一条测试制品 / Create one test artifact."""

    return FileArtifactStore(root).store(
        kind=ArtifactKind.IMAGE,
        content=b"image-bytes",
        filename="hello.png",
        mime_type="image/png",
        ttl=timedelta(minutes=5),
        max_bytes=1024,
        now=now,
    )


def test_artifact_survives_store_reconstruction_and_release(tmp_path) -> None:
    """@brief 重建仓储实例不丢失 artifact，release 后可重试 / Reconstructing the store preserves artifacts and release permits retry."""

    record = _store(tmp_path)
    first_process = FileArtifactStore(tmp_path)
    claim = first_process.claim(record.artifact_id, expected_kind=ArtifactKind.IMAGE)
    assert claim is not None
    assert claim.path.read_bytes() == b"image-bytes"
    first_process.release(claim)

    restarted_process = FileArtifactStore(tmp_path)
    retried = restarted_process.claim(
        record.artifact_id,
        expected_kind=ArtifactKind.IMAGE,
    )
    assert retried is not None
    restarted_process.complete(retried)
    assert not retried.path.exists()
    assert (
        restarted_process.claim(
            record.artifact_id,
            expected_kind=ArtifactKind.IMAGE,
        )
        is None
    )


def test_long_nonempty_mime_type_survives_store_and_manifest_restore(tmp_path) -> None:
    """@brief 制品领域不引入 payload 层 MIME 长度上限 / The artifact domain does not import a payload-layer MIME length limit."""

    mime_type = f"application/{'x' * 300}"
    store = FileArtifactStore(tmp_path)
    record = store.store(
        kind=ArtifactKind.IMAGE,
        content=b"image-bytes",
        filename="hello.png",
        mime_type=mime_type,
        ttl=timedelta(minutes=5),
        max_bytes=1024,
    )

    claim = FileArtifactStore(tmp_path).claim(
        record.artifact_id,
        expected_kind=ArtifactKind.IMAGE,
    )

    assert claim is not None
    assert claim.record.mime_type == mime_type
    FileArtifactStore(tmp_path).release(claim)


def test_only_one_concurrent_artifact_claim_wins(tmp_path) -> None:
    """@brief 原子 rename 只允许一个竞争者获胜 / Atomic rename permits exactly one racing claimant."""

    record = _store(tmp_path)

    def claim_once():
        """@brief 用独立 store 实例竞争 / Race with an independent store instance."""

        return FileArtifactStore(tmp_path).claim(
            record.artifact_id,
            expected_kind=ArtifactKind.IMAGE,
        )

    with ThreadPoolExecutor(max_workers=16) as executor:
        claims = list(executor.map(lambda _: claim_once(), range(32)))
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    FileArtifactStore(tmp_path).complete(winners[0])


def test_expired_artifact_is_removed_after_restart(tmp_path) -> None:
    """@brief kill-9 后过期 manifest 仍可被有界回收 / Expired manifests remain collectable after restart."""

    created = datetime(2026, 1, 1, tzinfo=UTC)
    record = _store(tmp_path, now=created)
    restarted = FileArtifactStore(tmp_path)
    assert (
        restarted.claim(
            record.artifact_id,
            expected_kind=ArtifactKind.IMAGE,
            now=created + timedelta(minutes=6),
        )
        is None
    )
    assert not any((tmp_path / "image").iterdir())


def test_kill_after_claim_recovers_only_after_lease_and_fences_stale_owner(
    tmp_path,
) -> None:
    """@brief kill-9 claim 到期后可恢复，旧 owner 不能删除新 claim / An expired kill-9 claim is recoverable and its stale owner cannot delete the new claim."""

    created = datetime(2026, 1, 1, tzinfo=UTC)
    lease = timedelta(seconds=30)
    record = _store(tmp_path, now=created)
    first_process = FileArtifactStore(tmp_path, claim_lease=lease)
    stale = first_process.claim(
        record.artifact_id,
        expected_kind=ArtifactKind.IMAGE,
        now=created,
    )
    assert stale is not None

    restarted = FileArtifactStore(tmp_path, claim_lease=lease)
    assert (
        restarted.claim(
            record.artifact_id,
            expected_kind=ArtifactKind.IMAGE,
            now=created + timedelta(seconds=29),
        )
        is None
    )
    recovered = restarted.claim(
        record.artifact_id,
        expected_kind=ArtifactKind.IMAGE,
        now=created + timedelta(seconds=30),
    )
    assert recovered is not None
    assert stale.token != recovered.token
    assert recovered.path.read_bytes() == b"image-bytes"

    first_process.complete(stale)
    first_process.release(stale)
    assert recovered.claim_path.is_file()
    assert recovered.path.read_bytes() == b"image-bytes"
    restarted.complete(recovered)
    assert not recovered.path.exists()


def test_invalid_expired_claim_manifest_is_cleaned_without_restoration(
    tmp_path,
) -> None:
    """@brief 非法 stale manifest 不能绕过恢复不变量门 / An invalid stale manifest cannot bypass the recovery invariant gate."""

    created = datetime(2026, 1, 1, tzinfo=UTC)
    lease = timedelta(seconds=30)
    record = _store(tmp_path, now=created)
    stale = FileArtifactStore(tmp_path, claim_lease=lease).claim(
        record.artifact_id,
        expected_kind=ArtifactKind.IMAGE,
        now=created,
    )
    assert stale is not None
    payload = json.loads(stale.claim_path.read_text(encoding="utf-8"))
    payload["size_bytes"] = "11"
    stale.claim_path.write_text(json.dumps(payload), encoding="utf-8")

    recovered = FileArtifactStore(tmp_path, claim_lease=lease).claim(
        record.artifact_id,
        expected_kind=ArtifactKind.IMAGE,
        now=created + lease,
    )

    assert recovered is None
    assert not stale.claim_path.exists()


def test_recovery_read_rename_race_is_a_normal_failed_claim(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 恢复读取后丢失 rename 竞争只返回 None / Losing the rename race after recovery read returns only None."""

    created = datetime(2026, 1, 1, tzinfo=UTC)
    lease = timedelta(seconds=30)
    record = _store(tmp_path, now=created)
    stale = FileArtifactStore(tmp_path, claim_lease=lease).claim(
        record.artifact_id,
        expected_kind=ArtifactKind.IMAGE,
        now=created,
    )
    assert stale is not None
    real_rename = artifact_store_module.os.rename

    def lose_recovery_race(source: Path, destination: Path) -> None:
        """@brief 模拟另一进程先移走 stale claim / Simulate another process moving the stale claim first.

        @param source rename 源 / Rename source.
        @param destination rename 目标 / Rename destination.
        @return None / None.
        """

        if source == stale.claim_path:
            stale.claim_path.unlink()
            raise FileNotFoundError(source)
        real_rename(source, destination)

    monkeypatch.setattr(artifact_store_module.os, "rename", lose_recovery_race)

    assert (
        FileArtifactStore(tmp_path, claim_lease=lease).claim(
            record.artifact_id,
            expected_kind=ArtifactKind.IMAGE,
            now=created + lease,
        )
        is None
    )
    assert not stale.claim_path.exists()
    assert stale.path.is_file()


def test_cleanup_recovers_and_removes_expired_claim_after_restart(tmp_path) -> None:
    """@brief cleanup 可回收同时过 claim lease 与 artifact TTL 的遗留项 / Cleanup reclaims an item whose claim lease and artifact TTL both expired."""

    created = datetime(2026, 1, 1, tzinfo=UTC)
    lease = timedelta(seconds=30)
    record = _store(tmp_path, now=created)
    claimed = FileArtifactStore(tmp_path, claim_lease=lease).claim(
        record.artifact_id,
        expected_kind=ArtifactKind.IMAGE,
        now=created,
    )
    assert claimed is not None

    restarted = FileArtifactStore(tmp_path, claim_lease=lease)
    assert restarted.cleanup_expired(now=created + timedelta(minutes=6)) == 1
    assert not any((tmp_path / "image").iterdir())


def test_restart_finalizes_completion_tombstone_without_redelivery(tmp_path) -> None:
    """@brief complete 在 fencing rename 后被 kill，重启会清理且不重复投递 / If complete is killed after its fencing rename, restart cleans up without redelivery."""

    record = _store(tmp_path)
    first_process = FileArtifactStore(tmp_path)
    claim = first_process.claim(record.artifact_id, expected_kind=ArtifactKind.IMAGE)
    assert claim is not None
    completion = claim.claim_path.parent / (
        f"{record.artifact_id}.complete.{'b' * 32}.json"
    )
    claim.claim_path.rename(completion)

    restarted = FileArtifactStore(tmp_path)
    assert (
        restarted.claim(
            record.artifact_id,
            expected_kind=ArtifactKind.IMAGE,
        )
        is None
    )
    assert not claim.path.exists()
    assert not completion.exists()


def test_claim_handle_matches_filename_lease_and_release_state(tmp_path) -> None:
    """@brief handle capability 与 claim 文件名一致，release 仅恢复 available / Handle matches its claim filename and release only restores available."""

    created = datetime(2026, 1, 1, tzinfo=UTC)
    lease = timedelta(seconds=30)
    record = _store(tmp_path, now=created)
    store = FileArtifactStore(tmp_path, claim_lease=lease)

    claim = store.claim(
        record.artifact_id,
        expected_kind=ArtifactKind.IMAGE,
        now=created,
    )

    assert claim is not None
    assert str(claim.token) in claim.claim_path.name
    assert claim.lease_expires_at == created + lease
    assert not (tmp_path / "image" / f"{record.artifact_id}.json").exists()
    store.release(claim)
    assert not claim.claim_path.exists()
    assert claim.path.is_file()
    assert (tmp_path / "image" / f"{record.artifact_id}.json").is_file()


def test_handle_cannot_be_forged_or_used_by_another_store(tmp_path) -> None:
    """@brief 路径 capability 不能公开伪造或跨 store 使用 / Path capability cannot be publicly forged or used across stores."""

    left_root = tmp_path / "left"
    right_root = tmp_path / "right"
    record = _store(left_root)
    left = FileArtifactStore(left_root)
    claim = left.claim(record.artifact_id, expected_kind=ArtifactKind.IMAGE)
    assert claim is not None

    with pytest.raises(TypeError):
        FileArtifactClaim()
    with pytest.raises(ValueError, match="different store"):
        FileArtifactStore(right_root).complete(claim)

    assert claim.claim_path.is_file()
    assert claim.path.is_file()
    left.release(claim)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("artifact_id", "f" * 32),
        ("kind", "audio"),
        ("size_bytes", "11"),
        ("created_at", 123),
    ),
)
def test_illegal_manifest_state_is_never_restored(
    tmp_path,
    field: str,
    value: object,
) -> None:
    """@brief manifest 身份、目录类型与字段类型必须共同合法 / Manifest identity, directory kind, and field types must all be valid."""

    created = datetime(2026, 1, 1, tzinfo=UTC)
    record = _store(tmp_path, now=created)
    manifest = tmp_path / "image" / f"{record.artifact_id}.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload[field] = value
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    claim = FileArtifactStore(tmp_path).claim(
        record.artifact_id,
        expected_kind=ArtifactKind.IMAGE,
        now=created,
    )

    assert claim is None
    assert not manifest.exists()
    assert not list((tmp_path / "image").glob(f"{record.artifact_id}.claim.*.json"))


def test_malformed_claim_filename_does_not_poison_expired_claim_recovery(
    tmp_path,
) -> None:
    """@brief 非法 claim 文件名不能永久阻塞合法 kill-9 恢复 / A malformed claim filename cannot permanently block valid kill-9 recovery."""

    created = datetime(2026, 1, 1, tzinfo=UTC)
    lease = timedelta(seconds=30)
    record = _store(tmp_path, now=created)
    first = FileArtifactStore(tmp_path, claim_lease=lease).claim(
        record.artifact_id,
        expected_kind=ArtifactKind.IMAGE,
        now=created,
    )
    assert first is not None
    malformed = first.claim_path.parent / (
        f"{record.artifact_id}.claim.not-a-lease.{'f' * 32}.json"
    )
    malformed.write_bytes(first.claim_path.read_bytes())

    recovered = FileArtifactStore(tmp_path, claim_lease=lease).claim(
        record.artifact_id,
        expected_kind=ArtifactKind.IMAGE,
        now=created + timedelta(seconds=31),
    )

    assert recovered is not None
    assert recovered.token != first.token
    assert recovered.path.read_bytes() == b"image-bytes"
    FileArtifactStore(tmp_path, claim_lease=lease).complete(recovered)
