"""@brief 以原子 sidecar manifest 持久化生成媒体 / Persist generated media with atomic sidecar manifests."""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fogmoe_bot.domain.media.artifact import (
    ArtifactClaimExpiredDecision,
    ArtifactClaimCapability,
    ArtifactClaimed,
    ArtifactClaimToken,
    ArtifactKind,
    ArtifactRecord,
    ArtifactRecoveryBlockedDecision,
    ArtifactRecoveryReadyDecision,
)
from fogmoe_bot.domain.media.identifiers import ArtifactId

_SAFE_EXTENSION = re.compile(r"^\.[a-z0-9]{1,8}$")
"""@brief 允许的文件扩展名 grammar / Allowed filename-extension grammar."""

_AVAILABLE_MANIFEST = re.compile(r"^(?P<artifact>[0-9a-f]{32})\.json$")
"""@brief available manifest grammar / Available-manifest grammar."""

_CLAIM_MANIFEST = re.compile(
    r"^(?P<artifact>[0-9a-f]{32})\.claim\."
    r"(?P<expires>[0-9]{1,20})\.(?P<token>[0-9a-f]{32})\.json$"
)
"""@brief 带显式租约的 claim manifest grammar / Claim-manifest grammar with an explicit lease."""

_COMPLETION_MANIFEST = re.compile(
    r"^(?P<artifact>[0-9a-f]{32})\.complete\.(?P<token>[0-9a-f]{32})\.json$"
)
"""@brief 投递完成 tombstone grammar / Delivery-completion tombstone grammar."""

_DEFAULT_CLAIM_LEASE = timedelta(minutes=5)
"""@brief 默认投递 claim 租约 / Default delivery-claim lease."""


@dataclass(frozen=True, slots=True, init=False)
class FileArtifactClaim:
    """@brief 绑定领域 capability 与仓储路径的不可伪造 handle / Unforgeable handle binding domain capability to store paths.

    构造器不公开；只有在 available→claim rename 获胜且 manifest/data 验证
    通过后，store 才能创建 handle。/ Its constructor is not public; the store may
    create a handle only after winning the available→claim rename and validating manifest/data.
    """

    _state: ArtifactClaimed
    """@brief 领域 claimed 状态 / Domain claimed state."""
    _path: Path
    """@brief 受验证 payload 路径 / Validated payload path."""
    _claim_path: Path
    """@brief 独占 claim manifest 路径 / Exclusive claim-manifest path."""
    _store_root: Path
    """@brief 签发 handle 的 store 根 / Store root that issued the handle."""

    def __init__(self) -> None:
        """@brief 禁止公开伪造文件 capability / Prevent public forgery of a file capability.

        @raise TypeError 始终抛出 / Always raised.
        """

        raise TypeError("FileArtifactClaim can only be issued by FileArtifactStore")

    @classmethod
    def _create(
        cls,
        *,
        state: ArtifactClaimed,
        path: Path,
        claim_path: Path,
        store_root: Path,
    ) -> FileArtifactClaim:
        """@brief 经路径与 capability 一致性门创建 handle / Create a handle through path/capability consistency checks.

        @param state 领域 claimed 状态 / Domain claimed state.
        @param path payload 路径 / Payload path.
        @param claim_path claim manifest 路径 / Claim-manifest path.
        @param store_root 签发 store 根 / Issuing store root.
        @return 已验证文件 capability / Validated file capability.
        """

        if not isinstance(state, ArtifactClaimed):
            raise TypeError("file artifact claim requires ArtifactClaimed")
        if not all(isinstance(value, Path) for value in (path, claim_path, store_root)):
            raise TypeError("file artifact claim paths must be Path values")
        expected_directory = store_root / state.record.kind.value
        if path.parent != expected_directory or claim_path.parent != expected_directory:
            raise ValueError("file artifact claim paths are outside their store")
        capability = _claim_capability_from_path(claim_path)
        if capability != state.capability:
            raise ValueError("file artifact claim path does not match its capability")
        expected_prefix = f"{state.record.artifact_id}."
        if not path.name.startswith(expected_prefix):
            raise ValueError("file artifact payload path does not match its record")
        instance = object.__new__(cls)
        object.__setattr__(instance, "_state", state)
        object.__setattr__(instance, "_path", path)
        object.__setattr__(instance, "_claim_path", claim_path)
        object.__setattr__(instance, "_store_root", store_root)
        return instance

    @property
    def record(self) -> ArtifactRecord:
        """@brief 返回受领取制品元数据 / Return claimed artifact metadata."""

        return self._state.record

    @property
    def path(self) -> Path:
        """@brief 返回只读 payload 路径 / Return the read-only payload path."""

        return self._path

    @property
    def claim_path(self) -> Path:
        """@brief 返回独占 claim manifest 路径 / Return the exclusive claim-manifest path."""

        return self._claim_path

    @property
    def token(self) -> ArtifactClaimToken:
        """@brief 返回 fencing token / Return the fencing token."""

        return self._state.capability.token

    @property
    def lease_expires_at(self) -> datetime:
        """@brief 返回 UTC 租约截止 / Return the UTC lease deadline."""

        return self._state.capability.lease_expires_at


class FileArtifactStore:
    """@brief 无进程内索引的 crash-resilient 文件制品仓储 / Crash-resilient file-artifact store without an in-memory index."""

    def __init__(
        self,
        root: Path,
        *,
        claim_lease: timedelta = _DEFAULT_CLAIM_LEASE,
    ) -> None:
        """@brief 创建仓储 / Create the store.

        @param root 仓储根目录 / Store root directory.
        @param claim_lease kill-9 后允许其他进程恢复的租约 / Lease before another process may recover a kill-9 claim.
        @raise ValueError claim 租约非正时抛出 / Raised for a non-positive claim lease.
        """

        if claim_lease <= timedelta(0):
            raise ValueError("claim_lease must be positive")
        self._root = root
        self._claim_lease = claim_lease
        """@brief 投递 claim 故障恢复租约 / Delivery-claim recovery lease."""

    def store(
        self,
        *,
        kind: ArtifactKind,
        content: bytes,
        filename: str,
        mime_type: str,
        ttl: timedelta,
        max_bytes: int,
        now: datetime | None = None,
    ) -> ArtifactRecord:
        """@brief 原子写入数据与 durable manifest / Atomically write data and its durable manifest.

        @param kind 媒体类型 / Media kind.
        @param content 媒体字节 / Media bytes.
        @param filename 用户可见文件名 / User-visible filename.
        @param mime_type MIME 类型 / MIME type.
        @param ttl 制品 TTL / Artifact TTL.
        @param max_bytes 最大字节数 / Maximum bytes.
        @param now 可测试创建时间 / Testable creation instant.
        @return 持久化元数据 / Persisted metadata.
        @raise ValueError 空、过大或 TTL 非正时抛出 / Raised for empty/oversized content or non-positive TTL.
        """

        if not content or len(content) > max_bytes:
            raise ValueError(f"artifact content must be 1..{max_bytes} bytes")
        if ttl <= timedelta(0):
            raise ValueError("artifact ttl must be positive")
        created_at = _utc(now or datetime.now(UTC))
        artifact_id = ArtifactId(uuid.uuid4().hex)
        extension = Path(filename).suffix.casefold()
        if _SAFE_EXTENSION.fullmatch(extension) is None:
            extension = ".bin"
        directory = self._root / kind.value
        directory.mkdir(parents=True, exist_ok=True)
        data_path = directory / f"{artifact_id}{extension}"
        manifest_path = directory / f"{artifact_id}.json"
        data_tmp = directory / f".{artifact_id}.{uuid.uuid4().hex}.data.tmp"
        manifest_tmp = directory / f".{artifact_id}.{uuid.uuid4().hex}.manifest.tmp"
        record = ArtifactRecord.create(
            artifact_id=artifact_id,
            kind=kind,
            filename=_safe_filename(filename, fallback=f"artifact{extension}"),
            mime_type=mime_type,
            size_bytes=len(content),
            created_at=created_at,
            ttl=ttl,
        )
        try:
            _write_durable(data_tmp, content)
            os.replace(data_tmp, data_path)
            manifest = {
                "artifact_id": str(record.artifact_id),
                "kind": record.kind.value,
                "filename": record.filename,
                "mime_type": record.mime_type,
                "size_bytes": record.size_bytes,
                "created_at": record.created_at.isoformat(),
                "expires_at": record.expires_at.isoformat(),
                "data_file": data_path.name,
            }
            _write_durable(
                manifest_tmp,
                json.dumps(
                    manifest, ensure_ascii=False, separators=(",", ":")
                ).encode(),
            )
            os.replace(manifest_tmp, manifest_path)
            _fsync_directory(directory)
        except BaseException:
            data_tmp.unlink(missing_ok=True)
            manifest_tmp.unlink(missing_ok=True)
            if not manifest_path.exists():
                data_path.unlink(missing_ok=True)
            raise
        return record

    def claim(
        self,
        artifact_id: ArtifactId,
        *,
        expected_kind: ArtifactKind,
        now: datetime | None = None,
    ) -> FileArtifactClaim | None:
        """@brief 以原子 rename 领取一次投递 / Claim one delivery with an atomic rename.

        @param artifact_id 制品标识 / Artifact identifier.
        @param expected_kind 期望媒体类型 / Expected media kind.
        @param now 可测试当前时间 / Testable current instant.
        @return 独占 claim；缺失、过期或已领取时为 None / Exclusive claim, or None when missing, expired, or claimed.
        """

        current = _utc(now or datetime.now(UTC))
        directory = self._root / expected_kind.value
        self._finalize_completions(directory, artifact_id)
        claimed = self._claim_available(
            directory,
            artifact_id,
            expected_kind=expected_kind,
            current=current,
        )
        if claimed is not None:
            return claimed
        if not self._recover_expired_claim(directory, artifact_id, current=current):
            return None
        return self._claim_available(
            directory,
            artifact_id,
            expected_kind=expected_kind,
            current=current,
        )

    def _claim_available(
        self,
        directory: Path,
        artifact_id: ArtifactId,
        *,
        expected_kind: ArtifactKind,
        current: datetime,
    ) -> FileArtifactClaim | None:
        """@brief 原子领取当前 available manifest / Atomically claim the current available manifest.

        @param directory 类型目录 / Kind directory.
        @param artifact_id 制品标识 / Artifact identifier.
        @param expected_kind 期望媒体类型 / Expected media kind.
        @param current 当前 UTC 时间 / Current UTC instant.
        @return 独占 claim 或 None / Exclusive claim or None.
        """

        manifest_path = directory / f"{artifact_id}.json"
        capability = ArtifactClaimCapability.issue(
            artifact_id=artifact_id,
            token=ArtifactClaimToken(uuid.uuid4().hex),
            claimed_at=current,
            lease_duration=self._claim_lease,
        )
        lease_micros = int(capability.lease_expires_at.timestamp() * 1_000_000)
        claim_path = directory / (
            f"{artifact_id}.claim.{lease_micros}.{capability.token}.json"
        )
        try:
            os.rename(manifest_path, claim_path)
        except FileNotFoundError:
            return None
        _fsync_directory(directory)
        try:
            record, data_path = _read_manifest(claim_path, directory)
            if record.artifact_id != artifact_id or record.kind is not expected_kind:
                data_path.unlink(missing_ok=True)
                claim_path.unlink(missing_ok=True)
                _fsync_directory(directory)
                return None
            state = record.available().claim(
                capability=capability,
                claimed_at=current,
            )
            if isinstance(state, ArtifactClaimExpiredDecision):
                data_path.unlink(missing_ok=True)
                claim_path.unlink(missing_ok=True)
                _fsync_directory(directory)
                return None
            if not data_path.is_file() or data_path.stat().st_size != record.size_bytes:
                data_path.unlink(missing_ok=True)
                claim_path.unlink(missing_ok=True)
                _fsync_directory(directory)
                return None
            return FileArtifactClaim._create(
                state=state,
                path=data_path,
                claim_path=claim_path,
                store_root=self._root,
            )
        except OSError, ValueError, KeyError, json.JSONDecodeError:
            claim_path.unlink(missing_ok=True)
            _fsync_directory(directory)
            return None

    def _recover_expired_claim(
        self,
        directory: Path,
        artifact_id: ArtifactId,
        *,
        current: datetime,
    ) -> bool:
        """@brief 将 kill-9 遗留且租约到期的 claim 恢复为 available / Recover a lease-expired kill-9 claim to available.

        @param directory 类型目录 / Kind directory.
        @param artifact_id 制品标识 / Artifact identifier.
        @param current 当前 UTC 时间 / Current UTC instant.
        @return 是否成功恢复一个 manifest / Whether one manifest was recovered.
        """

        recoverable: list[tuple[datetime, Path, ArtifactRecoveryReadyDecision]] = []
        for claim_path in sorted(directory.glob(f"{artifact_id}.claim.*.json")):
            capability = _claim_capability_from_path(claim_path)
            if capability is None or capability.artifact_id != artifact_id:
                continue
            try:
                record, _ = _read_manifest(claim_path, directory)
                claimed = ArtifactClaimed(record, capability)
            except FileNotFoundError:
                continue
            except OSError, ValueError, KeyError, json.JSONDecodeError:
                claim_path.unlink(missing_ok=True)
                _fsync_directory(directory)
                return False
            decision = claimed.recover(current)
            if isinstance(decision, ArtifactRecoveryBlockedDecision):
                return False
            recoverable.append((capability.lease_expires_at, claim_path, decision))
        if not recoverable:
            return False
        available = directory / f"{artifact_id}.json"
        if available.exists():
            return True
        for _, claim_path, recovered in sorted(
            recoverable,
            key=lambda item: item[0],
        ):
            recovered_path = directory / f"{recovered.claim.record.artifact_id}.json"
            try:
                os.rename(claim_path, recovered_path)
            except FileNotFoundError:
                continue
            _fsync_directory(directory)
            return True
        return False

    def _finalize_completions(self, directory: Path, artifact_id: ArtifactId) -> int:
        """@brief 完成 kill-9 遗留的 delivery tombstone / Finalize delivery tombstones left by kill-9.

        @param directory 类型目录 / Kind directory.
        @param artifact_id 制品标识 / Artifact identifier.
        @return 已完成 tombstone 数 / Number of finalized tombstones.
        """

        finalized = 0
        for completion in directory.glob(f"{artifact_id}.complete.*.json"):
            if _COMPLETION_MANIFEST.fullmatch(completion.name) is None:
                continue
            try:
                record, data_path = _read_manifest(completion, directory)
                if record.artifact_id == artifact_id:
                    data_path.unlink(missing_ok=True)
            except OSError, ValueError, KeyError, json.JSONDecodeError:
                pass
            completion.unlink(missing_ok=True)
            finalized += 1
        if finalized:
            _fsync_directory(directory)
        return finalized

    def release(self, claim: FileArtifactClaim) -> None:
        """@brief 可重试失败后释放 claim / Release a claim after a retryable failure.

        @param claim 独占 claim / Exclusive claim.
        @return None / None.
        """

        state = self._claim_state(claim)
        available_state = state.release()
        available = (
            claim.claim_path.parent / f"{available_state.record.artifact_id}.json"
        )
        if not claim.path.is_file():
            claim.claim_path.unlink(missing_ok=True)
            _fsync_directory(claim.claim_path.parent)
            return
        try:
            os.rename(claim.claim_path, available)
        except FileNotFoundError:
            return
        except FileExistsError:
            claim.claim_path.unlink(missing_ok=True)
        _fsync_directory(claim.claim_path.parent)

    def complete(self, claim: FileArtifactClaim) -> None:
        """@brief 投递成功后删除数据与 claim / Delete data and claim after successful delivery.

        @param claim 独占 claim / Exclusive claim.
        @return None / None.
        """

        state = self._claim_state(claim)
        completed = state.complete(ArtifactClaimToken(uuid.uuid4().hex))
        directory = claim.claim_path.parent
        completion = directory / (
            f"{completed.record.artifact_id}.complete.{completed.token}.json"
        )
        try:
            os.rename(claim.claim_path, completion)
        except FileNotFoundError:
            return
        _fsync_directory(directory)
        claim.path.unlink(missing_ok=True)
        completion.unlink(missing_ok=True)
        _fsync_directory(directory)

    def _claim_state(self, claim: FileArtifactClaim) -> ArtifactClaimed:
        """@brief 验证 handle 由同一 store 根签发 / Verify that a handle was issued for the same store root.

        @param claim 待验证文件 capability / File capability to validate.
        @return 领域 claimed 状态 / Domain claimed state.
        @raise TypeError 值不是 ``FileArtifactClaim`` 时抛出 /
            Raised when the value is not a ``FileArtifactClaim``.
        @raise ValueError handle 属于其他 store 根时抛出 /
            Raised when the handle belongs to another store root.
        """

        if not isinstance(claim, FileArtifactClaim):
            raise TypeError("artifact transition requires a FileArtifactClaim")
        if claim._store_root != self._root:
            raise ValueError("artifact claim belongs to a different store")
        return claim._state

    def cleanup_expired(
        self,
        *,
        now: datetime | None = None,
        scan_limit: int = 1000,
    ) -> int:
        """@brief 有界清理过期且未领取制品 / Bounded cleanup of expired unclaimed artifacts.

        @param now 可测试当前时间 / Testable current instant.
        @param scan_limit 单次最大 manifest 数 / Maximum manifests per run.
        @return 删除制品数 / Number of removed artifacts.
        """

        if scan_limit <= 0:
            raise ValueError("scan_limit must be positive")
        current = _utc(now or datetime.now(UTC))
        removed = 0
        scanned = 0
        for kind in ArtifactKind:
            directory = self._root / kind.value
            if not directory.exists():
                continue
            manifests = list(directory.glob("*.json"))
            for manifest in manifests:
                scanned += 1
                if scanned > scan_limit:
                    return removed
                completion_match = _COMPLETION_MANIFEST.fullmatch(manifest.name)
                if completion_match is not None:
                    artifact_id = ArtifactId(completion_match.group("artifact"))
                    removed += self._finalize_completions(directory, artifact_id)
                    continue
                claim_match = _CLAIM_MANIFEST.fullmatch(manifest.name)
                if claim_match is not None:
                    artifact_id = ArtifactId(claim_match.group("artifact"))
                    self._recover_expired_claim(
                        directory,
                        artifact_id,
                        current=current,
                    )
                    continue
                try:
                    record, _ = _read_manifest(manifest, directory)
                except OSError, ValueError, KeyError, json.JSONDecodeError:
                    manifest.unlink(missing_ok=True)
                    continue
                if record.expires_at > current:
                    continue
                claim = self.claim(
                    record.artifact_id,
                    expected_kind=kind,
                    now=record.created_at,
                )
                if claim is None:
                    continue
                self.complete(claim)
                removed += 1
            for manifest in directory.glob("[0-9a-f]*.json"):
                if (
                    scanned >= scan_limit
                    or ".claim." in manifest.name
                    or ".complete." in manifest.name
                ):
                    continue
                try:
                    record, _ = _read_manifest(manifest, directory)
                except OSError, ValueError, KeyError, json.JSONDecodeError:
                    continue
                if record.expires_at > current:
                    continue
                claim = self.claim(
                    record.artifact_id,
                    expected_kind=kind,
                    now=record.created_at,
                )
                if claim is None:
                    continue
                self.complete(claim)
                removed += 1
        return removed


def _read_manifest(path: Path, directory: Path) -> tuple[ArtifactRecord, Path]:
    """@brief 读取并校验 manifest / Read and validate a manifest.

    @param path manifest 路径 / Manifest path.
    @param directory 预期父目录 / Expected parent directory.
    @return 元数据与安全数据路径 / Metadata and safe data path.
    """

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("artifact manifest must be an object")
    expected_keys = {
        "artifact_id",
        "kind",
        "filename",
        "mime_type",
        "size_bytes",
        "created_at",
        "expires_at",
        "data_file",
    }
    if set(payload) != expected_keys:
        raise ValueError("artifact manifest fields do not match the schema")
    artifact_id = ArtifactId(_manifest_string(payload, "artifact_id"))
    kind = ArtifactKind(_manifest_string(payload, "kind"))
    filename = _manifest_string(payload, "filename")
    mime_type = _manifest_string(payload, "mime_type")
    size_bytes = payload["size_bytes"]
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int):
        raise ValueError("artifact manifest size_bytes must be an integer")
    data_file = _manifest_string(payload, "data_file")
    data_path = directory / data_file
    if data_path.parent != directory or Path(data_file).name != data_file:
        raise ValueError("artifact data path escapes its directory")
    record = ArtifactRecord.restore(
        artifact_id=artifact_id,
        kind=kind,
        filename=filename,
        mime_type=mime_type,
        size_bytes=size_bytes,
        created_at=_parse_datetime(payload["created_at"]),
        expires_at=_parse_datetime(payload["expires_at"]),
    )
    manifest_artifact_id = _manifest_artifact_id(path)
    if manifest_artifact_id != record.artifact_id:
        raise ValueError("artifact manifest filename does not match its record")
    try:
        directory_kind = ArtifactKind(directory.name)
    except ValueError as error:
        raise ValueError("artifact manifest directory has an invalid kind") from error
    if directory_kind is not record.kind:
        raise ValueError("artifact manifest directory does not match its kind")
    extension = data_file.removeprefix(str(record.artifact_id))
    if (
        not data_file.startswith(str(record.artifact_id))
        or _SAFE_EXTENSION.fullmatch(extension) is None
    ):
        raise ValueError("artifact data filename does not match its manifest")
    return record, data_path


def _manifest_string(payload: dict[object, object], key: str) -> str:
    """@brief 严格读取 manifest 字符串 / Strictly read a manifest string.

    @param payload 已解析 JSON object / Parsed JSON object.
    @param key 字段名 / Field name.
    @return 未强制转换的字符串 / String without coercion.
    @raise ValueError 字段不是字符串时抛出 / Raised when the field is not a string.
    """

    value = payload[key]
    if not isinstance(value, str):
        raise ValueError(f"artifact manifest {key} must be a string")
    return value


def _manifest_artifact_id(path: Path) -> ArtifactId:
    """@brief 从三种合法 manifest 文件名解析身份 / Parse identity from any legal manifest filename.

    @param path available、claim 或 completion manifest 路径 / Available, claim, or completion manifest path.
    @return 文件名制品标识 / Filename artifact identifier.
    @raise ValueError 文件名不是合法状态时抛出 /
        Raised when the filename is not a legal state representation.
    """

    for pattern in (_AVAILABLE_MANIFEST, _CLAIM_MANIFEST, _COMPLETION_MANIFEST):
        matched = pattern.fullmatch(path.name)
        if matched is not None:
            return ArtifactId(matched.group("artifact"))
    raise ValueError("artifact manifest filename has an invalid state")


def _claim_capability_from_path(path: Path) -> ArtifactClaimCapability | None:
    """@brief 从合法 claim 文件名恢复 capability / Restore a capability from a legal claim filename.

    @param path 候选 claim manifest 路径 / Candidate claim-manifest path.
    @return 已验证 capability；非法文件名则为 None / Validated capability, or None for an invalid filename.
    """

    matched = _CLAIM_MANIFEST.fullmatch(path.name)
    if matched is None:
        return None
    try:
        return ArtifactClaimCapability.restore(
            artifact_id=ArtifactId(matched.group("artifact")),
            token=ArtifactClaimToken(matched.group("token")),
            lease_expires_at=datetime.fromtimestamp(
                int(matched.group("expires")) / 1_000_000,
                tz=UTC,
            ),
        )
    except OSError, OverflowError, ValueError:
        return None


def _parse_datetime(value: object) -> datetime:
    """@brief 解析 aware ISO8601 时间 / Parse an aware ISO8601 instant.

    @param value JSON 值 / JSON value.
    @return aware UTC 时间 / Aware UTC instant.
    """

    if not isinstance(value, str):
        raise ValueError("artifact manifest timestamp must be a string")
    return _utc(datetime.fromisoformat(value))


def _safe_filename(value: str, *, fallback: str) -> str:
    """@brief 移除路径和控制字符 / Remove path and control characters.

    @param value 原始文件名 / Raw filename.
    @param fallback 空名称 fallback / Fallback for an empty name.
    @return 安全 basename / Safe basename.
    """

    basename = Path(value).name.replace("\x00", "").strip()
    return (basename or fallback)[:200]


def _write_durable(path: Path, content: bytes) -> None:
    """@brief 写入并 fsync 临时文件 / Write and fsync a temporary file.

    @param path 临时路径 / Temporary path.
    @param content 字节 / Bytes.
    @return None / None.
    """

    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    """@brief 尽力 fsync 目录 rename / Best-effort fsync directory renames.

    @param path 目录 / Directory.
    @return None / None.
    """

    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _utc(value: datetime) -> datetime:
    """@brief 规范化 aware UTC 时间 / Normalize an aware UTC instant.

    @param value 时间 / Instant.
    @return aware UTC 时间 / Aware UTC instant.
    """

    if value.tzinfo is None:
        raise ValueError("artifact store requires aware datetimes")
    return value.astimezone(UTC)
