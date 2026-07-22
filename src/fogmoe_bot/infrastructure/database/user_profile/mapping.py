"""@brief User Profile PostgreSQL 映射与规范序列化 / User Profile PostgreSQL mapping and canonical serialization."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol, cast
from uuid import UUID, uuid5

from fogmoe_bot.application.user_profile.ports import DreamResult
from fogmoe_bot.domain.conversation.payloads import JsonObject, JsonValue
from fogmoe_bot.domain.user_profile.models import (
    DreamId,
    ProfileClaim,
    ProfileClaimKind,
    ProfileConfidence,
    ProfileDocument,
    ProfileEvidence,
    ProfileMetadata,
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


class _TupleRow(Protocol):
    """@brief SQLAlchemy Row 的最小 tuple 投影视图 / Minimal tuple projection of a SQLAlchemy Row."""

    def _tuple(self) -> tuple[object, ...]:
        """@brief 返回位置值 / Return positional values."""

        ...


def _dream_identity(
    user_id: int,
    base_revision: int,
    base_watermark: int,
    through_event_id: int,
) -> DreamId:
    """@brief 从冻结 range 派生 job identity / Derive a job identity from its frozen range."""

    return DreamId(
        uuid5(
            _DREAM_NAMESPACE,
            f"{user_id}\x1f{base_revision}\x1f{base_watermark}\x1f{through_event_id}",
        )
    )


def _map_source_evidence(row: object) -> ProfileEvidence:
    """@brief 映射尚未编号的 Conversation source / Map an unnumbered Conversation source."""

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
    """@brief 映射持久化 evidence / Map persisted evidence."""

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
    """@brief 映射 metadata JSON / Map metadata JSON."""

    data = _json_object(value)
    return ProfileMetadata(
        display_name=str(data.get("display_name", "")),
        username=(str(data["username"]) if data.get("username") is not None else None),
        personal_info=str(data.get("personal_info", "")),
        provider=str(data.get("provider", "telegram")),
    )


def _metadata_json(metadata: ProfileMetadata) -> JsonObject:
    """@brief 序列化冻结 metadata / Serialize frozen metadata."""

    return {
        "display_name": metadata.display_name,
        "username": metadata.username,
        "personal_info": metadata.personal_info,
        "provider": metadata.provider,
    }


def _document_json(document: ProfileDocument) -> JsonObject:
    """@brief 序列化 Profile document / Serialize a Profile document."""

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
    """@brief 严格映射 Profile JSON / Strictly map Profile JSON."""

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
    """@brief 映射当前 revision / Map a current revision."""

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


def _patch_json(result: DreamResult) -> JsonObject:
    """@brief 序列化已校验 patch audit / Serialize the validated patch audit."""

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
    """@brief 计算 source 语义 digest / Compute a digest of source semantics."""

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
    """@brief 规范化数据库 evidence 语义 / Normalize stored evidence semantics."""

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
    """@brief 将 driver JSON 值转换为对象 / Convert a driver JSON value to an object."""

    decoded: object = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, Mapping):
        raise TypeError("Expected a JSON object")
    return cast(
        JsonObject, {str(key): cast(JsonValue, item) for key, item in decoded.items()}
    )


def _values(row: object, size: int) -> tuple[object, ...]:
    """@brief 将 SQLAlchemy row 转成定长 tuple / Convert a SQLAlchemy row to a fixed-size tuple."""

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
    """@brief 严格读取 int / Strictly read an integer."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("Expected an integer database value")
    return value


def _uuid(value: object) -> UUID:
    """@brief 严格读取 UUID / Strictly read a UUID."""

    return value if isinstance(value, UUID) else UUID(str(value))
