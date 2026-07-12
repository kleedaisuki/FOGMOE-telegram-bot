"""@brief 跨 workflow adapter 的行解码与租约原语 / Row-decoding and lease primitives shared across workflow adapters."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import cast
from uuid import UUID

from fogmoe_bot.domain.conversation.payloads import JsonObject
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    InferenceActivityId,
    LeaseToken,
    TurnId,
)
from fogmoe_bot.domain.conversation.temporal import ensure_utc
from fogmoe_bot.domain.conversation.inference import (
    InferenceActivity,
    InferenceActivityDraft,
    InferenceActivityStatus,
)
from fogmoe_bot.domain.conversation.errors import (
    IdempotencyConflictError,
    StaleClaimError,
)


_INFERENCE_ACTIVITY_COLUMNS = (
    "activity_id, turn_id, conversation_id, request, status, version, "
    "attempt_count, next_attempt_at, created_at, updated_at, completed_at, "
    "completion_token, last_error"
)
"""@brief acceptance 与 inference 共享的活动列 / Activity columns shared by acceptance and inference."""

_INFERENCE_ACTIVITY_SELECT = (
    "SELECT " + _INFERENCE_ACTIVITY_COLUMNS + " FROM conversation.inference_activities"
)
"""@brief acceptance 与 inference 共享的活动查询前缀 / Activity SELECT shared by acceptance and inference."""


def _map_inference_activity(row: object) -> InferenceActivity:
    """@brief 将数据库行映射为推理活动 / Map a database row to an inference activity."""

    values = _row_values(row, 13)
    draft = InferenceActivityDraft(
        activity_id=InferenceActivityId.parse(_uuid(values[0])),
        turn_id=TurnId.parse(_uuid(values[1])),
        conversation_id=ConversationId(_text(values[2])),
        request=_json_object(values[3]),
        created_at=_datetime(values[8]),
    )
    completion_token = (
        LeaseToken.parse(_uuid(values[11])) if values[11] is not None else None
    )
    return InferenceActivity(
        draft=draft,
        status=InferenceActivityStatus(_text(values[4])),
        version=_integer(values[5]),
        attempt_count=_integer(values[6]),
        next_attempt_at=_optional_datetime(values[7]),
        updated_at=_datetime(values[9]),
        completed_at=_optional_datetime(values[10]),
        completion_token=completion_token,
        last_error=_optional_text(values[12]),
    )


def _validate_inference_activity_idempotency(
    existing: InferenceActivity,
    requested: InferenceActivityDraft,
) -> None:
    """@brief 验证活动意图的幂等语义 / Validate inference-activity idempotency semantics."""

    canonical = existing.draft
    if (
        canonical.activity_id != requested.activity_id
        or canonical.turn_id != requested.turn_id
        or canonical.conversation_id != requested.conversation_id
        or canonical.request != requested.request
    ):
        raise IdempotencyConflictError(
            f"Inference activity {requested.activity_id} or Turn was reused with different semantics"
        )


def _claim_window(
    now: datetime,
    limit: int,
    lease_for: timedelta,
) -> tuple[datetime, datetime]:
    """@brief 校验并计算领取时间窗 / Validate and calculate a claim window."""

    if limit < 0:
        raise ValueError("claim limit cannot be negative")
    if lease_for <= timedelta():
        raise ValueError("lease_for must be positive")
    timestamp = ensure_utc(now)
    return timestamp, timestamp + lease_for


def _require_claim_update(rowcount: int, kind: str, identifier: str) -> None:
    """@brief 确认 fencing 更新命中唯一行 / Require a fencing update to affect exactly one row."""

    if rowcount != 1:
        raise StaleClaimError(f"Stale {kind} claim for {identifier}")


def _required_error(error: str) -> str:
    """@brief 规范化必填错误文本 / Normalize required error text."""

    normalized = error.strip()
    if not normalized:
        raise ValueError("error cannot be empty")
    return normalized[:4000]


def _encode_json(value: JsonObject) -> str:
    """@brief 编码 JSONB 参数 / Encode a JSONB parameter."""

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_object(value: object) -> JsonObject:
    """@brief 解码并校验 JSON 对象 / Decode and validate a JSON object."""

    decoded: object = value
    if isinstance(decoded, bytes):
        decoded = decoded.decode("utf-8", errors="strict")
    if isinstance(decoded, str):
        decoded = json.loads(decoded)
    if not isinstance(decoded, Mapping):
        raise ValueError("Stored payload must be a JSON object")
    return cast(JsonObject, dict(decoded))


def _row_values(row: object, expected: int) -> Sequence[object]:
    """@brief 将 driver 行视为固定长度序列 / View a driver row as a fixed-length sequence."""

    if not isinstance(row, Sequence) or isinstance(row, (str, bytes, bytearray)):
        raise ValueError("Database row is not a positional sequence")
    if len(row) != expected:
        raise ValueError(f"Expected {expected} database columns, got {len(row)}")
    return row


def _text(value: object) -> str:
    """@brief 严格解码文本 / Strictly decode text."""

    if isinstance(value, bytes):
        return value.decode("utf-8", errors="strict")
    return str(value)


def _integer(value: object) -> int:
    """@brief 严格解码整数 / Strictly decode an integer."""

    if isinstance(value, bool):
        raise ValueError("Boolean database value is not an integer identifier")
    return int(_text(value))


def _optional_text(value: object) -> str | None:
    """@brief 解码可选文本 / Decode optional text."""

    return None if value is None else _text(value)


def _uuid(value: object) -> UUID:
    """@brief 严格解码 UUID / Strictly decode a UUID."""

    return value if isinstance(value, UUID) else UUID(_text(value))


def _datetime(value: object) -> datetime:
    """@brief 严格解码 aware datetime / Strictly decode an aware datetime."""

    if not isinstance(value, datetime):
        raise ValueError(f"Expected datetime, got {type(value).__name__}")
    return ensure_utc(value)


def _optional_datetime(value: object) -> datetime | None:
    """@brief 解码可选 aware datetime / Decode an optional aware datetime."""

    return None if value is None else _datetime(value)
