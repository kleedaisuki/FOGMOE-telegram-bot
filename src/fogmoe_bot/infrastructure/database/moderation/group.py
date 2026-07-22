"""@brief 群组治理聚合 PostgreSQL adapter / PostgreSQL adapter for group-moderation aggregates."""

from __future__ import annotations

import json
from typing import cast

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.moderation.ports import GroupModerationRepository
from fogmoe_bot.domain.moderation.aggregate import (
    GroupModeration,
    StaleModerationVersion,
)
from fogmoe_bot.domain.moderation.models import (
    ChatId,
    EnforcementFailureMode,
    GroupModerationPolicy,
    KeywordReply,
    ModerationRule,
    ModerationToggleResult,
    RuleKind,
    RuleMergeMode,
    RuleScope,
    UserId,
)
from fogmoe_bot.infrastructure.database import db
from fogmoe_bot.infrastructure.database.toggle_command_receipts import (
    load_toggle_receipt,
    lock_toggle_command,
    lock_toggle_scope,
    save_toggle_receipt,
)

_SPAM_TOGGLE_OPERATION = "spam_control"
"""@brief 垃圾过滤总开关的持久操作名 / Persisted spam-control toggle operation."""


class PostgresModerationGroupRepository(GroupModerationRepository):
    """@brief 群组 policy、规则与 toggle receipt 的原子仓储 / Atomic repository for group policy, rules, and toggle receipts."""

    async def load_group(self, chat_id: ChatId) -> GroupModeration:
        """@brief 以单 SQL 快照读取完整聚合 / Load the full aggregate in one SQL snapshot."""

        return await self._load_group(chat_id)

    async def toggle_group(
        self,
        chat_id: ChatId,
        *,
        actor_id: int,
        idempotency_key: str,
    ) -> ModerationToggleResult:
        """@brief 原子切换垃圾过滤并保存 source receipt / Atomically toggle spam control and save its source receipt."""

        request_payload: dict[str, object] = {}
        async with db.transaction() as connection:
            await lock_toggle_command(
                idempotency_key=idempotency_key,
                operation_kind=_SPAM_TOGGLE_OPERATION,
                chat_id=int(chat_id),
                connection=connection,
            )
            replay = await load_toggle_receipt(
                idempotency_key=idempotency_key,
                operation_kind=_SPAM_TOGGLE_OPERATION,
                chat_id=int(chat_id),
                actor_id=actor_id,
                request_payload=request_payload,
                connection=connection,
            )
            if replay is not None:
                return replay

            current = await self._load_group(chat_id, connection=connection)
            updated = current.toggle(UserId(actor_id))
            changed = await self._update_control(
                connection,
                updated,
                expected_version=current.version,
                actor_id=actor_id,
            )
            if changed == 0 and current.version == 0:
                changed = await self._insert_control(
                    connection,
                    updated,
                    actor_id=actor_id,
                )
            if changed != 1:
                raise StaleModerationVersion(
                    f"Moderation group {int(chat_id)} is no longer version {current.version}"
                )
            result = ModerationToggleResult(enabled=updated.policy.enabled)
            await save_toggle_receipt(
                idempotency_key=idempotency_key,
                operation_kind=_SPAM_TOGGLE_OPERATION,
                chat_id=int(chat_id),
                actor_id=actor_id,
                request_payload=request_payload,
                enabled=result.enabled,
                connection=connection,
            )
            return result

    async def save_group(
        self,
        aggregate: GroupModeration,
        *,
        expected_version: int,
        actor_id: int,
    ) -> None:
        """@brief 以 OCC 原子保存 policy、垃圾规则与关键词回复 / Atomically save policy, spam rules, and keyword replies with OCC."""

        if aggregate.version != expected_version + 1:
            raise ValueError(
                "Saved moderation aggregate must advance exactly one version"
            )
        async with db.transaction() as connection:
            await lock_toggle_scope(
                operation_kind=_SPAM_TOGGLE_OPERATION,
                chat_id=int(aggregate.chat_id),
                connection=connection,
            )
            changed = await self._update_control(
                connection,
                aggregate,
                expected_version=expected_version,
                actor_id=actor_id,
            )
            if changed == 0 and expected_version == 0:
                changed = await self._insert_control(
                    connection,
                    aggregate,
                    actor_id=actor_id,
                )
            if changed != 1:
                raise StaleModerationVersion(
                    f"Moderation group {int(aggregate.chat_id)} is no longer version {expected_version}"
                )
            await db.execute(
                "DELETE FROM moderation.group_spam_keywords WHERE group_id = %s",
                (int(aggregate.chat_id),),
                connection=connection,
            )
            for rule in aggregate.spam_rules:
                await db.execute(
                    "INSERT INTO moderation.group_spam_keywords "
                    "(group_id, keyword, is_regex, created_by) VALUES (%s, %s, %s, %s)",
                    (
                        int(aggregate.chat_id),
                        rule.pattern,
                        rule.kind is RuleKind.REGEX,
                        actor_id,
                    ),
                    connection=connection,
                )
            await db.execute(
                "DELETE FROM moderation.group_keywords WHERE group_id = %s",
                (int(aggregate.chat_id),),
                connection=connection,
            )
            for reply in aggregate.keyword_replies:
                await db.execute(
                    "INSERT INTO moderation.group_keywords "
                    "(group_id, keyword, response, created_by) VALUES (%s, %s, %s, %s)",
                    (
                        int(aggregate.chat_id),
                        reply.keyword,
                        reply.response,
                        actor_id,
                    ),
                    connection=connection,
                )

    async def _load_group(
        self,
        chat_id: ChatId,
        *,
        connection: AsyncConnection | None = None,
    ) -> GroupModeration:
        """@brief 在可选事务中读取完整聚合 / Load the aggregate inside an optional transaction."""

        row = await db.fetch_one(
            """
            SELECT
              COALESCE(control.enabled, FALSE),
              COALESCE(control.block_links, FALSE),
              COALESCE(control.block_mentions, FALSE),
              COALESCE(control.exempt_administrators, TRUE),
              COALESCE(control.rule_merge_mode, 'override_global'),
              COALESCE(control.failure_mode, 'fail_closed'),
              COALESCE(control.version, 0),
              COALESCE(
                (
                  SELECT jsonb_agg(
                    jsonb_build_object(
                      'pattern', spam.keyword,
                      'is_regex', spam.is_regex
                    ) ORDER BY spam.id
                  )
                  FROM moderation.group_spam_keywords AS spam
                  WHERE spam.group_id = requested.group_id
                ),
                '[]'::jsonb
              ),
              COALESCE(
                (
                  SELECT jsonb_agg(
                    jsonb_build_object(
                      'keyword', reply.keyword,
                      'response', reply.response
                    ) ORDER BY reply.id
                  )
                  FROM moderation.group_keywords AS reply
                  WHERE reply.group_id = requested.group_id
                ),
                '[]'::jsonb
              )
            FROM (SELECT CAST(%s AS BIGINT) AS group_id) AS requested
            LEFT JOIN moderation.group_spam_control AS control
              ON control.group_id = requested.group_id
            """,
            (int(chat_id),),
            connection=connection,
        )
        if row is None:
            raise RuntimeError("Moderation aggregate query did not return a row")
        version = int(row[6])
        policy = GroupModerationPolicy(
            chat_id=chat_id,
            enabled=bool(row[0]),
            block_links=bool(row[1]),
            block_mentions=bool(row[2]),
            exempt_administrators=bool(row[3]),
            rule_merge_mode=RuleMergeMode(str(row[4])),
            failure_mode=EnforcementFailureMode(str(row[5])),
            version=version,
        )
        spam_values = _json_array(row[7])
        reply_values = _json_array(row[8])
        return GroupModeration(
            policy=policy,
            spam_rules=tuple(_spam_rule(item) for item in spam_values),
            keyword_replies=tuple(_keyword_reply(item) for item in reply_values),
            version=version,
        )

    async def _update_control(
        self,
        connection: AsyncConnection,
        aggregate: GroupModeration,
        *,
        expected_version: int,
        actor_id: int,
    ) -> int:
        """@brief 更新已存在控制行 / Update an existing control row."""

        policy = aggregate.policy
        return await db.execute(
            "UPDATE moderation.group_spam_control SET enabled = %s, block_links = %s, "
            "block_mentions = %s, exempt_administrators = %s, rule_merge_mode = %s, "
            "failure_mode = %s, version = %s, enabled_by = %s, updated_at = CURRENT_TIMESTAMP "
            "WHERE group_id = %s AND version = %s",
            (
                policy.enabled,
                policy.block_links,
                policy.block_mentions,
                policy.exempt_administrators,
                policy.rule_merge_mode.value,
                policy.failure_mode.value,
                aggregate.version,
                actor_id,
                int(aggregate.chat_id),
                expected_version,
            ),
            connection=connection,
        )

    async def _insert_control(
        self,
        connection: AsyncConnection,
        aggregate: GroupModeration,
        *,
        actor_id: int,
    ) -> int:
        """@brief 创建首个控制行 / Create the initial control row."""

        policy = aggregate.policy
        return await db.execute(
            "INSERT INTO moderation.group_spam_control "
            "(group_id, enabled, block_links, block_mentions, exempt_administrators, "
            "rule_merge_mode, failure_mode, version, enabled_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (group_id) DO NOTHING",
            (
                int(aggregate.chat_id),
                policy.enabled,
                policy.block_links,
                policy.block_mentions,
                policy.exempt_administrators,
                policy.rule_merge_mode.value,
                policy.failure_mode.value,
                aggregate.version,
                actor_id,
            ),
            connection=connection,
        )


def _json_array(value: object) -> list[dict[str, object]]:
    """@brief 解码 JSONB 对象数组 / Decode a JSONB object array."""

    decoded: object = json.loads(value) if isinstance(value, str | bytes) else value
    if not isinstance(decoded, list) or not all(
        isinstance(item, dict) for item in decoded
    ):
        raise ValueError("Moderation JSONB projection must be an object array")
    return cast(list[dict[str, object]], decoded)


def _spam_rule(value: dict[str, object]) -> ModerationRule:
    """@brief 解码垃圾规则投影 / Decode a spam-rule projection."""

    return ModerationRule(
        pattern=str(value["pattern"]),
        kind=RuleKind.REGEX if bool(value["is_regex"]) else RuleKind.LITERAL,
        scope=RuleScope.GROUP,
    )


def _keyword_reply(value: dict[str, object]) -> KeywordReply:
    """@brief 解码关键词回复投影 / Decode a keyword-reply projection."""

    return KeywordReply(
        keyword=str(value["keyword"]),
        response=str(value["response"]),
    )
