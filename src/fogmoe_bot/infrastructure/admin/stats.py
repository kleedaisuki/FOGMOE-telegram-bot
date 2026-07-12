"""PostgreSQL read projection for administrative statistics."""

from __future__ import annotations

import json
from typing import cast

from fogmoe_bot.application.admin.models import (
    AdminStats,
    GroupFeatureStats,
    RecentUser,
)
from fogmoe_bot.infrastructure.database import connection as db_connection


class PostgresAdminStatsProjection:
    """Read one statement-level MVCC snapshot of administrative statistics."""

    async def fetch(self, *, group_limit: int) -> AdminStats:
        """Read a bounded, internally consistent statistics snapshot."""

        if group_limit < 1 or group_limit > 50:
            raise ValueError("Admin group sample limit must be between 1 and 50")
        row = await db_connection.fetch_one(
            """
            SELECT
              (SELECT COUNT(*) FROM identity.users),
              (SELECT COUNT(DISTINCT group_id) FROM moderation.group_keywords),
              (SELECT COUNT(*) FROM moderation.group_verification),
              (SELECT COUNT(*) FROM moderation.group_spam_control WHERE enabled = TRUE),
              (SELECT COUNT(DISTINCT group_id) FROM crypto.group_chart_tokens),
              COALESCE((
                SELECT jsonb_agg(sample.group_id ORDER BY sample.group_id)
                FROM (
                  SELECT DISTINCT group_id
                  FROM moderation.group_keywords
                  ORDER BY group_id
                  LIMIT %s
                ) AS sample
              ), '[]'::JSONB),
              COALESCE((
                SELECT jsonb_agg(sample.group_id ORDER BY sample.group_id)
                FROM (
                  SELECT group_id
                  FROM moderation.group_verification
                  ORDER BY group_id
                  LIMIT %s
                ) AS sample
              ), '[]'::JSONB),
              COALESCE((
                SELECT jsonb_agg(sample.group_id ORDER BY sample.group_id)
                FROM (
                  SELECT group_id
                  FROM moderation.group_spam_control
                  WHERE enabled = TRUE
                  ORDER BY group_id
                  LIMIT %s
                ) AS sample
              ), '[]'::JSONB),
              COALESCE((
                SELECT jsonb_agg(sample.group_id ORDER BY sample.group_id)
                FROM (
                  SELECT DISTINCT group_id
                  FROM crypto.group_chart_tokens
                  ORDER BY group_id
                  LIMIT %s
                ) AS sample
              ), '[]'::JSONB),
              COALESCE((
                SELECT jsonb_agg(
                  jsonb_build_object('user_id', sample.id, 'name', sample.name)
                  ORDER BY sample.id DESC
                )
                FROM (
                  SELECT id, name
                  FROM identity.users
                  ORDER BY id DESC
                  LIMIT 10
                ) AS sample
              ), '[]'::JSONB)
            """,
            (group_limit, group_limit, group_limit, group_limit),
        )
        if row is None:
            raise RuntimeError("Admin statistics query returned no row")
        return AdminStats(
            user_count=_integer(row[0]),
            keywords=GroupFeatureStats(_integer(row[1]), _integer_array(row[5])),
            verification=GroupFeatureStats(_integer(row[2]), _integer_array(row[6])),
            spam_control=GroupFeatureStats(_integer(row[3]), _integer_array(row[7])),
            charts=GroupFeatureStats(_integer(row[4]), _integer_array(row[8])),
            recent_users=_recent_users(row[9]),
        )


def _integer(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("Boolean is not an Admin integer")
    return int(str(value))


def _decoded_array(value: object) -> list[object]:
    decoded = json.loads(value) if isinstance(value, str | bytes) else value
    if not isinstance(decoded, list):
        raise ValueError("Admin projection JSON must be an array")
    return cast(list[object], decoded)


def _integer_array(value: object) -> tuple[int, ...]:
    return tuple(_integer(item) for item in _decoded_array(value))


def _recent_users(value: object) -> tuple[RecentUser, ...]:
    users: list[RecentUser] = []
    for item in _decoded_array(value):
        if not isinstance(item, dict):
            raise ValueError("Recent-user projection item must be an object")
        user_id = item.get("user_id")
        name = item.get("name")
        if not isinstance(name, str):
            raise ValueError("Recent-user projection name must be text")
        users.append(RecentUser(_integer(user_id), name))
    return tuple(users)
