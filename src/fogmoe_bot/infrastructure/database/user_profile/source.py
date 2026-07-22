"""@brief PostgreSQL User Profile 证据发现 / PostgreSQL User Profile evidence discovery."""

from __future__ import annotations

from fogmoe_bot.domain.conversation.identity import TELEGRAM_UPDATE_SOURCE_KIND
from fogmoe_bot.domain.user_profile.models import ProfileEvidence
from fogmoe_bot.infrastructure.database import db

from .mapping import _map_source_evidence


class PostgresProfileEvidenceSource:
    """@brief 从完整私聊 Assistant Turn 发现未投影 Profile evidence / Discover unprojected Profile evidence from complete private Assistant turns."""

    async def read_unprojected(self, *, limit: int) -> tuple[ProfileEvidence, ...]:
        """@brief 读取无 evidence marker 的完整 Turn / Read complete turns without an evidence marker.

        @param limit 最大 Turn 数 / Maximum turns.
        @return event_id=0 的来源 evidence / Source evidence with event_id zero.
        @raise ValueError limit 越界 / Invalid limit.
        """

        if not 1 <= limit <= 128:
            raise ValueError("Profile source limit must be between 1 and 128")
        rows = await db.fetch_all(
            "WITH candidates AS ("
            "SELECT activity.turn_id, "
            "CAST(activity.request #>> '{user,user_id}' AS BIGINT) AS owner_user_id, "
            "activity.request #>> '{user,display_name}' AS display_name, "
            "activity.request #>> '{user,username}' AS username, "
            "COALESCE(activity.request #>> '{user,personal_info}', '') AS personal_info, "
            "turn.created_at AS occurred_at, "
            "activity.completed_at "
            "FROM conversation.inference_activities AS activity "
            "JOIN conversation.conversation_turns AS turn ON turn.turn_id = activity.turn_id "
            "WHERE activity.status = 'completed' "
            "AND turn.source_kind = %s "
            "AND COALESCE(activity.request ->> 'task_kind', 'assistant') = 'assistant' "
            "AND COALESCE(activity.request #>> '{scope,is_group}', 'false') = 'false' "
            "AND activity.request #>> '{user,user_id}' ~ '^[1-9][0-9]*$' "
            "AND activity.request #>> '{user,display_name}' IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM user_profile.profiles AS profile "
            "WHERE profile.user_id = CAST(activity.request #>> '{user,user_id}' AS BIGINT) "
            "AND profile.forgotten_through IS NOT NULL "
            "AND turn.created_at <= profile.forgotten_through) "
            "AND EXISTS (SELECT 1 FROM conversation.conversation_messages AS source_user "
            "WHERE source_user.turn_id = activity.turn_id AND source_user.role = 'user' "
            "AND jsonb_typeof(source_user.content -> 'text') = 'string' "
            "AND char_length(btrim(source_user.content ->> 'text')) > 0) "
            "AND EXISTS (SELECT 1 FROM conversation.conversation_messages AS source_assistant "
            "WHERE source_assistant.turn_id = activity.turn_id "
            "AND source_assistant.role = 'assistant' "
            "AND jsonb_typeof(source_assistant.content -> 'text') = 'string' "
            "AND char_length(btrim(source_assistant.content ->> 'text')) > 0) "
            "AND NOT EXISTS (SELECT 1 FROM user_profile.evidence_events AS evidence "
            "WHERE evidence.source_turn_id = activity.turn_id) "
            "ORDER BY activity.completed_at, activity.turn_id LIMIT %s"
            ") SELECT candidate.turn_id, candidate.owner_user_id, "
            "user_messages.content_text, assistant_messages.content_text, "
            "candidate.occurred_at, candidate.display_name, candidate.username, "
            "candidate.personal_info "
            "FROM candidates AS candidate "
            "CROSS JOIN LATERAL ("
            "SELECT string_agg(message.content ->> 'text', E'\\n' ORDER BY message.sequence) "
            "AS content_text FROM conversation.conversation_messages AS message "
            "WHERE message.turn_id = candidate.turn_id AND message.role = 'user' "
            "AND jsonb_typeof(message.content -> 'text') = 'string'"
            ") AS user_messages "
            "CROSS JOIN LATERAL ("
            "SELECT string_agg(message.content ->> 'text', E'\\n' ORDER BY message.sequence) "
            "AS content_text FROM conversation.conversation_messages AS message "
            "WHERE message.turn_id = candidate.turn_id AND message.role = 'assistant' "
            "AND jsonb_typeof(message.content -> 'text') = 'string'"
            ") AS assistant_messages "
            "WHERE user_messages.content_text IS NOT NULL "
            "AND assistant_messages.content_text IS NOT NULL "
            "ORDER BY candidate.completed_at, candidate.turn_id",
            (TELEGRAM_UPDATE_SOURCE_KIND, limit),
        )
        return tuple(_map_source_evidence(row) for row in rows)
