"""@brief durable file artifact store 的故障与竞争测试 / Fault and race tests for the durable file-artifact store."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from fogmoe_bot.domain.media.artifact import ArtifactKind
from fogmoe_bot.infrastructure.media.file_artifact_store import FileArtifactStore


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
        now=created + timedelta(seconds=31),
    )
    assert recovered is not None
    assert recovered.path.read_bytes() == b"image-bytes"

    first_process.complete(stale)
    assert recovered.path.read_bytes() == b"image-bytes"
    restarted.complete(recovered)
    assert not recovered.path.exists()


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
