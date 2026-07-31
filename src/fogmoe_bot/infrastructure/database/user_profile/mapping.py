"""@brief User Profile PostgreSQL 映射与规范序列化 / User Profile PostgreSQL mapping and canonical serialization."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol, cast
from uuid import UUID, uuid5

from fogmoe_bot.domain.conversation.payloads import JsonObject, JsonValue
from fogmoe_bot.domain.temporal import ensure_utc
from fogmoe_bot.domain.user_profile import (
    DreamActivity,
    DreamActivityDraft,
    DreamActivityStatus,
    DreamLeaseToken,
    DreamResult,
    ProfileBaseline,
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
    UserProfileSnapshot,
)

_DREAM_NAMESPACE = UUID("4235ec26-caad-57c8-a12b-cba708cffc23")
"""@brief Dream job 确定性 UUIDv5 namespace / Deterministic UUIDv5 namespace for Dream jobs."""

_EVIDENCE_COLUMNS = (
    "evidence.event_id, evidence.source_turn_id, evidence.owner_user_id, "
    "evidence.user_text, evidence.assistant_text, evidence.occurred_at, evidence.metadata"
)
"""@brief ProfileEvidence 规范列 / Canonical ProfileEvidence columns."""

_DREAM_ACTIVITY_COLUMNS = (
    "dream_id, user_id, base_revision, base_observed_through_event_id, "
    "through_event_id, source_count, metadata, status, version, attempt_count, "
    "next_attempt_at, result_patch, route_key, last_error, created_at, updated_at, "
    "completed_at, claim_token, lease_expires_at"
)
"""@brief Dream 聚合的规范持久化投影 / Canonical persistence projection for Dream aggregates."""


class _TupleRow(Protocol):
    """@brief SQLAlchemy Row 的最小 tuple 投影视图 / Minimal tuple projection of a SQLAlchemy Row."""

    def _tuple(self) -> tuple[object, ...]:
        """@brief 返回位置值 / Return positional values.

        @return 数据库行的位置投影 / Positional projection of the database row.
        """

        ...


def _dream_identity(
    user_id: int,
    base_revision: int,
    base_watermark: int,
    through_event_id: int,
) -> DreamId:
    """@brief 从冻结 range 派生 job identity / Derive a job identity from its frozen range.

    @param user_id Profile owner / Profile owner.
    @param base_revision 冻结 Profile revision / Frozen Profile revision.
    @param base_watermark 冻结 scheduler cursor / Frozen scheduler cursor.
    @param through_event_id 本批最后 evidence 标识 / Final evidence identity in the batch.
    @return 确定性 Dream identity / Deterministic Dream identity.
    """

    return DreamId(
        uuid5(
            _DREAM_NAMESPACE,
            f"{user_id}\x1f{base_revision}\x1f{base_watermark}\x1f{through_event_id}",
        )
    )


def _map_source_evidence(row: object) -> ProfileEvidence:
    """@brief 映射尚未编号的 Conversation source / Map an unnumbered Conversation source.

    @param row 八列来源投影 / Eight-column source projection.
    @return event_id 为零的来源 evidence / Source evidence with event_id zero.
    """

    values = _values(row, 8)
    return ProfileEvidence(
        event_id=0,
        source_turn_id=_uuid(values[0]),
        owner_user_id=_integer(values[1]),
        user_text=str(values[2]),
        assistant_text=str(values[3]),
        occurred_at=cast(datetime, values[4]),
        metadata=ProfileMetadata(
            display_name=str(values[5]),
            username=str(values[6]) if values[6] is not None else None,
            personal_info=str(values[7] or ""),
        ),
    )


def _map_evidence(row: object) -> ProfileEvidence:
    """@brief 映射持久化 evidence / Map persisted evidence.

    @param row 七列 evidence 投影 / Seven-column evidence projection.
    @return 严格映射的 evidence / Strictly mapped evidence.
    """

    values = _values(row, 7)
    return ProfileEvidence(
        event_id=_integer(values[0]),
        source_turn_id=_uuid(values[1]),
        owner_user_id=_integer(values[2]),
        user_text=str(values[3]),
        assistant_text=str(values[4]),
        occurred_at=cast(datetime, values[5]),
        metadata=_map_metadata(values[6]),
    )


def _map_metadata(value: object) -> ProfileMetadata:
    """@brief 映射 metadata JSON / Map metadata JSON.

    @param value driver 返回的 JSON 对象 / JSON object returned by the driver.
    @return 冻结 Profile metadata / Frozen Profile metadata.
    """

    data = _json_object(value)
    return ProfileMetadata(
        display_name=str(data.get("display_name", "")),
        username=(str(data["username"]) if data.get("username") is not None else None),
        personal_info=str(data.get("personal_info", "")),
        provider=str(data.get("provider", "telegram")),
    )


def _metadata_json(metadata: ProfileMetadata) -> JsonObject:
    """@brief 序列化冻结 metadata / Serialize frozen metadata.

    @param metadata 领域 metadata / Domain metadata.
    @return 规范 JSON 对象 / Canonical JSON object.
    """

    return {
        "display_name": metadata.display_name,
        "username": metadata.username,
        "personal_info": metadata.personal_info,
        "provider": metadata.provider,
    }


def _document_json(document: ProfileDocument) -> JsonObject:
    """@brief 序列化 Profile document / Serialize a Profile document.

    @param document 领域 Profile 文档 / Domain Profile document.
    @return 规范 JSON 对象 / Canonical JSON object.
    """

    return {
        "claims": [
            {
                "key": claim.key,
                "kind": claim.kind.value,
                "statement": claim.statement,
                "confidence": claim.confidence.value,
                "evidence_event_ids": list(claim.evidence_event_ids),
                "observed_at": claim.observed_at.isoformat(),
            }
            for claim in document.claims
        ]
    }


def _map_document(value: object) -> ProfileDocument:
    """@brief 严格映射 Profile JSON / Strictly map Profile JSON.

    @param value driver 返回的 JSON 值 / JSON value returned by the driver.
    @return 严格恢复的 Profile 文档 / Strictly restored Profile document.
    """

    data = _json_object(value)
    raw_claims = data.get("claims", [])
    if not isinstance(raw_claims, Sequence) or isinstance(raw_claims, str | bytes):
        raise TypeError("Stored Profile claims must be an array")
    claims: list[ProfileClaim] = []
    for raw in raw_claims:
        if not isinstance(raw, Mapping):
            raise TypeError("Stored Profile claim must be an object")
        raw_ids = raw.get("evidence_event_ids")
        if not isinstance(raw_ids, Sequence) or isinstance(raw_ids, str | bytes):
            raise TypeError("Stored Profile evidence IDs must be an array")
        claims.append(
            ProfileClaim(
                key=str(raw.get("key", "")),
                kind=ProfileClaimKind(str(raw.get("kind", ""))),
                statement=str(raw.get("statement", "")),
                confidence=ProfileConfidence(str(raw.get("confidence", ""))),
                evidence_event_ids=tuple(_integer(item) for item in raw_ids),
                observed_at=datetime.fromisoformat(str(raw.get("observed_at", ""))),
            )
        )
    return ProfileDocument(tuple(claims))


def _map_snapshot(row: object) -> UserProfileSnapshot:
    """@brief 映射当前 revision / Map a current revision.

    @param row 八列 revision 投影 / Eight-column revision projection.
    @return 可冻结的 Profile snapshot / Pinnable Profile snapshot.
    """

    values = _values(row, 8)
    return UserProfileSnapshot(
        user_id=_integer(values[0]),
        revision=_integer(values[1]),
        document=_map_document(values[2]),
        observed_through_event_id=_integer(values[3]),
        created_at=cast(datetime, values[4]),
        updated_at=cast(datetime, values[5]),
        route_key=str(values[6]),
        prompt_version=_integer(values[7]),
    )


def _map_dream_activity(row: object) -> DreamActivity:
    """@brief 将固定数据库投影恢复为 Dream 聚合 / Restore a Dream aggregate from a fixed database projection.

    @param row 十九列 Dream 行 / Nineteen-column Dream row.
    @return 严格恢复的聚合 / Strictly restored aggregate.
    """

    values = _values(row, 19)
    if (values[11] is None) != (values[12] is None):
        raise ValueError("Stored Dream patch and route_key must be present together")
    result = (
        _map_dream_result(values[11], str(values[12]))
        if values[11] is not None and values[12] is not None
        else None
    )
    return DreamActivity.restore(
        draft=DreamActivityDraft(
            dream_id=DreamId(_uuid(values[0])),
            owner_user_id=_integer(values[1]),
            baseline=ProfileBaseline(
                revision=_integer(values[2]),
                observed_through_event_id=_integer(values[3]),
            ),
            through_event_id=_integer(values[4]),
            source_count=_integer(values[5]),
            metadata=_map_metadata(values[6]),
            created_at=_datetime(values[14]),
        ),
        status=DreamActivityStatus(str(values[7])),
        version=_integer(values[8]),
        attempt_count=_integer(values[9]),
        next_attempt_at=_optional_datetime(values[10]),
        claim_token=(
            DreamLeaseToken.parse(_uuid(values[17])) if values[17] is not None else None
        ),
        lease_expires_at=_optional_datetime(values[18]),
        result=result,
        last_error=str(values[13]) if values[13] is not None else None,
        updated_at=_datetime(values[15]),
        completed_at=_optional_datetime(values[16]),
    )


def _map_dream_result(value: object, route_key: str) -> DreamResult:
    """@brief 恢复 completed Dream 的 patch audit / Restore the patch audit of a completed Dream.

    @param value result_patch JSON / Result-patch JSON.
    @param route_key provider/model provenance / Provider/model provenance.
    @return 已验证 Dream result / Validated Dream result.
    """

    data = _json_object(value)
    raw_operations = data.get("operations")
    if not isinstance(raw_operations, Sequence) or isinstance(
        raw_operations,
        str | bytes,
    ):
        raise TypeError("Stored Dream operations must be an array")
    operations: list[UpsertProfileClaim | DeleteProfileClaim] = []
    for raw in raw_operations:
        if not isinstance(raw, Mapping):
            raise TypeError("Stored Dream operation must be an object")
        raw_ids = raw.get("evidence_event_ids")
        if not isinstance(raw_ids, Sequence) or isinstance(raw_ids, str | bytes):
            raise TypeError("Stored Dream operation evidence IDs must be an array")
        evidence_ids = tuple(_integer(item) for item in raw_ids)
        if str(raw.get("op", "")) == "delete":
            operations.append(
                DeleteProfileClaim(
                    key=str(raw.get("key", "")),
                    evidence_event_ids=evidence_ids,
                )
            )
            continue
        if str(raw.get("op", "")) != "upsert":
            raise ValueError("Stored Dream operation has an unknown discriminator")
        operations.append(
            UpsertProfileClaim(
                key=str(raw.get("key", "")),
                kind=ProfileClaimKind(str(raw.get("kind", ""))),
                statement=str(raw.get("statement", "")),
                confidence=ProfileConfidence(str(raw.get("confidence", ""))),
                evidence_event_ids=evidence_ids,
            )
        )
    return DreamResult(
        patch=ProfilePatch(tuple(operations)),
        route_key=route_key,
        prompt_version=_integer(data.get("prompt_version")),
    )


def _patch_json(result: DreamResult) -> JsonObject:
    """@brief 序列化已校验 patch audit / Serialize the validated patch audit.

    @param result 带 provenance 的领域结果 / Domain result with provenance.
    @return 规范 audit JSON / Canonical audit JSON.
    """

    operations: list[JsonObject] = []
    for operation in result.patch.operations:
        item: JsonObject = {
            "op": "upsert" if isinstance(operation, UpsertProfileClaim) else "delete",
            "key": operation.key,
            "evidence_event_ids": list(operation.evidence_event_ids),
        }
        if isinstance(operation, UpsertProfileClaim):
            item.update(
                {
                    "kind": operation.kind.value,
                    "statement": operation.statement,
                    "confidence": operation.confidence.value,
                }
            )
        operations.append(item)
    return {
        "prompt_version": result.prompt_version,
        "operations": cast(JsonValue, operations),
    }


def _evidence_digest(evidence: ProfileEvidence) -> str:
    """@brief 计算 source 语义 digest / Compute a digest of source semantics.

    @param evidence 待摘要 evidence / Evidence to digest.
    @return 小写 SHA-256 十六进制摘要 / Lowercase SHA-256 hexadecimal digest.
    """

    payload = {
        "owner_user_id": evidence.owner_user_id,
        "user_text": evidence.user_text,
        "assistant_text": evidence.assistant_text,
        "occurred_at": evidence.occurred_at.isoformat(),
        "metadata": _metadata_json(evidence.metadata),
    }
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _stored_evidence_semantics(row: object) -> tuple[object, ...]:
    """@brief 规范化数据库 evidence 语义 / Normalize stored evidence semantics.

    @param row 六列持久化 evidence 投影 / Six-column persisted-evidence projection.
    @return 可直接比较的语义 tuple / Directly comparable semantic tuple.
    """

    values = _values(row, 6)
    return (
        _integer(values[0]),
        str(values[1]),
        str(values[2]),
        values[3],
        _json_object(values[4]),
        str(values[5]),
    )


def _json_object(value: object) -> JsonObject:
    """@brief 将 driver JSON 值转换为对象 / Convert a driver JSON value to an object.

    @param value driver JSON 值 / Driver JSON value.
    @return 字符串键 JSON 对象 / String-keyed JSON object.
    """

    decoded: object = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, Mapping):
        raise TypeError("Expected a JSON object")
    return cast(
        JsonObject, {str(key): cast(JsonValue, item) for key, item in decoded.items()}
    )


def _values(row: object, size: int) -> tuple[object, ...]:
    """@brief 将 SQLAlchemy row 转成定长 tuple / Convert a SQLAlchemy row to a fixed-size tuple.

    @param row driver 行值 / Driver row value.
    @param size 预期列数 / Expected column count.
    @return 定长位置 tuple / Fixed-size positional tuple.
    """

    if hasattr(row, "_tuple"):
        values = cast(_TupleRow, row)._tuple()
    elif isinstance(row, Sequence) and not isinstance(row, str | bytes):
        values = tuple(row)
    else:
        raise TypeError("Database row is not sequence-like")
    if len(values) != size:
        raise RuntimeError(f"Expected {size} columns, received {len(values)}")
    return values


def _integer(value: object) -> int:
    """@brief 严格读取 int / Strictly read an integer.

    @param value driver 标量 / Driver scalar.
    @return 非 bool 整数 / Non-boolean integer.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("Expected an integer database value")
    return value


def _datetime(value: object) -> datetime:
    """@brief 严格读取 UTC datetime / Strictly read a UTC datetime.

    @param value 数据库值 / Database value.
    @return aware UTC datetime / Aware UTC datetime.
    """

    if not isinstance(value, datetime):
        raise TypeError("Expected a datetime database value")
    return ensure_utc(value)


def _optional_datetime(value: object) -> datetime | None:
    """@brief 读取可选 UTC datetime / Read an optional UTC datetime.

    @param value 数据库值 / Database value.
    @return datetime 或 None / Datetime or None.
    """

    return None if value is None else _datetime(value)


def _uuid(value: object) -> UUID:
    """@brief 严格读取 UUID / Strictly read a UUID.

    @param value UUID 或其文本形式 / UUID or its textual form.
    @return UUID 值 / UUID value.
    """

    return value if isinstance(value, UUID) else UUID(str(value))
