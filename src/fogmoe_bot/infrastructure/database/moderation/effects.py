"""@brief 治理 effect 与警告窗口 PostgreSQL adapter / PostgreSQL adapter for moderation effects and warning windows."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.moderation.ports import ModerationEffectRepository
from fogmoe_bot.domain.moderation.aggregate import StaleModerationVersion
from fogmoe_bot.domain.moderation.effects import (
    KeywordReplyPlan,
    ModerationEffect,
    ModerationEffectId,
    ModerationEffectKind,
    ModerationEffectPlan,
    ModerationEffectStatus,
    SpamEnforcementPlan,
)
from fogmoe_bot.domain.moderation.models import (
    ChatId,
    EnforcementFailureMode,
    MessageId,
    RuleKind,
    UserId,
)
from fogmoe_bot.infrastructure.database import db

_EFFECT_SELECT = """
SELECT effect_id, source_update_id, kind, chat_id, user_id, message_id,
       status, warning_count, payload, version, last_error, updated_at
FROM moderation.effects
"""
"""@brief effect 聚合规范 SELECT / Canonical effect-aggregate SELECT."""


class PostgresModerationEffectRepository(ModerationEffectRepository):
    """@brief effect 状态机与警告计数的原子仓储 / Atomic repository for effect state and warning counts."""

    async def load_effect(
        self,
        effect_id: ModerationEffectId,
    ) -> ModerationEffect | None:
        """@brief 读取治理 effect / Load a moderation effect."""

        row = await db.fetch_one(
            _EFFECT_SELECT + " WHERE effect_id = CAST(%s AS UUID)",
            (str(effect_id),),
        )
        return _effect_from_row(row) if row is not None else None

    async def reserve_effect(
        self,
        plan: ModerationEffectPlan,
        *,
        now: datetime,
        warning_window: timedelta,
    ) -> ModerationEffect:
        """@brief 幂等创建 effect，并且仅对新垃圾意图计数一次 / Idempotently create an effect and count each new spam intent once."""

        timestamp = _utc(now)
        payload = _plan_payload(plan)
        kind = _plan_kind(plan)
        async with db.transaction() as connection:
            inserted = await db.fetch_one(
                """
                INSERT INTO moderation.effects
                  (effect_id, source_update_id, kind, chat_id, user_id, message_id,
                   status, warning_count, payload, version, last_error, created_at, updated_at)
                VALUES
                  (CAST(%s AS UUID), %s, %s, %s, %s, %s,
                   'pending', NULL, CAST(%s AS JSONB), 0, NULL, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING effect_id
                """,
                (
                    str(plan.effect_id),
                    plan.update_id,
                    kind.value,
                    int(plan.chat_id),
                    int(plan.user_id),
                    int(plan.message_id),
                    json.dumps(payload, ensure_ascii=False),
                    timestamp,
                    timestamp,
                ),
                connection=connection,
            )
            if inserted is not None and isinstance(plan, SpamEnforcementPlan):
                warning_count = await self._increment_warning(
                    connection,
                    plan,
                    now=timestamp,
                    warning_window=warning_window,
                )
                await db.execute(
                    "UPDATE moderation.effects SET warning_count = %s "
                    "WHERE effect_id = CAST(%s AS UUID)",
                    (warning_count, str(plan.effect_id)),
                    connection=connection,
                )
            row = await db.fetch_one(
                _EFFECT_SELECT + " WHERE effect_id = CAST(%s AS UUID) FOR UPDATE",
                (str(plan.effect_id),),
                connection=connection,
            )
            if row is None:
                raise RuntimeError("Reserved moderation effect disappeared")
            effect = _effect_from_row(row)
            if effect.plan != plan:
                raise RuntimeError(
                    "Moderation effect ID was reused for a different intent"
                )
            return effect

    async def save_effect(
        self,
        effect: ModerationEffect,
        *,
        expected_version: int,
    ) -> None:
        """@brief 以 OCC 保存 effect 阶段 / Save an effect stage with OCC."""

        if effect.version != expected_version + 1:
            raise ValueError("Saved moderation effect must advance exactly one version")
        changed = await db.execute(
            "UPDATE moderation.effects SET status = %s, version = %s, last_error = %s, "
            "updated_at = %s WHERE effect_id = CAST(%s AS UUID) AND version = %s",
            (
                effect.status.value,
                effect.version,
                effect.last_error,
                _utc(effect.updated_at),
                str(effect.effect_id),
                expected_version,
            ),
        )
        if changed != 1:
            raise StaleModerationVersion(
                f"Moderation effect {effect.effect_id} is no longer version {expected_version}"
            )

    async def _increment_warning(
        self,
        connection: AsyncConnection,
        plan: SpamEnforcementPlan,
        *,
        now: datetime,
        warning_window: timedelta,
    ) -> int:
        """@brief 原子增加或重置成员警告窗口 / Atomically increment or reset a member warning window."""

        row = await db.fetch_one(
            """
            INSERT INTO moderation.member_warning_windows
              (chat_id, user_id, window_started_at, warning_count, version, updated_at)
            VALUES (%s, %s, %s, 1, 0, %s)
            ON CONFLICT (chat_id, user_id) DO UPDATE SET
              window_started_at = CASE
                WHEN moderation.member_warning_windows.window_started_at <= %s
                THEN EXCLUDED.window_started_at
                ELSE moderation.member_warning_windows.window_started_at
              END,
              warning_count = CASE
                WHEN moderation.member_warning_windows.window_started_at <= %s
                THEN 1
                ELSE moderation.member_warning_windows.warning_count + 1
              END,
              version = moderation.member_warning_windows.version + 1,
              updated_at = EXCLUDED.updated_at
            RETURNING warning_count
            """,
            (
                int(plan.chat_id),
                int(plan.user_id),
                now,
                now,
                now - warning_window,
                now - warning_window,
            ),
            connection=connection,
        )
        if row is None:
            raise RuntimeError("Warning increment did not return a count")
        return int(row[0])


def _plan_kind(plan: ModerationEffectPlan) -> ModerationEffectKind:
    """@brief 返回意图类别 / Return an intent's kind."""

    return (
        ModerationEffectKind.SPAM_ENFORCEMENT
        if isinstance(plan, SpamEnforcementPlan)
        else ModerationEffectKind.KEYWORD_REPLY
    )


def _plan_payload(plan: ModerationEffectPlan) -> dict[str, object]:
    """@brief 序列化 effect 特有载荷 / Serialize effect-specific payload."""

    if isinstance(plan, SpamEnforcementPlan):
        return {
            "matched_text": plan.matched_text,
            "rule_kind": plan.rule_kind.name,
            "failure_mode": plan.failure_mode.name,
        }
    return {
        "keyword": plan.keyword,
        "response": plan.response,
    }


def _effect_from_row(row: Any) -> ModerationEffect:
    """@brief 从数据库行重建 effect / Reconstitute an effect from a database row."""

    effect_id = ModerationEffectId.parse(row[0])
    update_id = int(row[1])
    kind = ModerationEffectKind(str(row[2]))
    chat_id = ChatId(int(row[3]))
    user_id = UserId(int(row[4]))
    message_id = MessageId(int(row[5]))
    payload_values = _json_object(row[8])
    if kind is ModerationEffectKind.SPAM_ENFORCEMENT:
        plan: ModerationEffectPlan = SpamEnforcementPlan(
            effect_id=effect_id,
            update_id=update_id,
            chat_id=chat_id,
            user_id=user_id,
            message_id=message_id,
            matched_text=str(payload_values["matched_text"]),
            rule_kind=RuleKind[str(payload_values["rule_kind"])],
            failure_mode=EnforcementFailureMode[str(payload_values["failure_mode"])],
        )
    else:
        plan = KeywordReplyPlan(
            effect_id=effect_id,
            update_id=update_id,
            chat_id=chat_id,
            user_id=user_id,
            message_id=message_id,
            keyword=str(payload_values["keyword"]),
            response=str(payload_values["response"]),
        )
    return ModerationEffect(
        plan=plan,
        status=ModerationEffectStatus(str(row[6])),
        warning_count=int(row[7]) if row[7] is not None else None,
        version=int(row[9]),
        last_error=str(row[10]) if row[10] is not None else None,
        updated_at=_utc(cast(datetime, row[11])),
    )


def _json_object(value: object) -> dict[str, object]:
    """@brief 解码 JSONB 对象 / Decode a JSONB object."""

    decoded: object = json.loads(value) if isinstance(value, str | bytes) else value
    if not isinstance(decoded, dict):
        raise ValueError("Moderation effect payload must be an object")
    return cast(dict[str, object], decoded)


def _utc(value: datetime) -> datetime:
    """@brief 规范为 UTC，并拒绝 naive 时间 / Normalize to UTC and reject naive timestamps."""

    if value.tzinfo is None:
        raise ValueError("Moderation timestamps must be timezone-aware")
    return value.astimezone(UTC)
