"""@brief PostgreSQL/pgvector 检索与 Conversation 情景来源 adapter / PostgreSQL/pgvector retrieval and episodic-source adapters."""

from __future__ import annotations

import hashlib
import json
import struct
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.retrieval import (
    CONVERSATION_TURN_SOURCE_KIND,
    EPISODIC_CORPUS_ID,
    EpisodicTurn,
    RetrievalIOError,
    StaleVectorClaimError,
)
from fogmoe_bot.domain.retrieval import (
    AwaitingPassageVector,
    CompletedPassageVector,
    EmbeddingSpace,
    EmbeddingVector,
    FailedPassageVector,
    PassageVectorClaim,
    PassageVectorClaimed,
    PassageVectorFailure,
    PassageVectorJob,
    PassageVectorJobKey,
    PassageVectorLeaseRecovered,
    PassageVectorStatus,
    ProcessingPassageVector,
    RetrievalEvidence,
    RetrievalPassage,
    RetrievalScope,
    RetrievalScopeKind,
    WaitingPassageVectorRetry,
)
from fogmoe_bot.domain.temporal import ensure_utc
from fogmoe_bot.infrastructure.database import db
from fogmoe_bot.infrastructure.database.retrieval_scope import lock_retrieval_scope

_PASSAGE_COLUMNS = (
    "passage_id, corpus_id, scope_kind, scope_id, source_kind, source_id, ordinal, "
    "format_version, content_text, content_digest, occurred_at"
)
"""@brief RetrievalPassage 映射列 / Columns used to map a RetrievalPassage."""

_VECTOR_COLUMN_NAMES = (
    "passage_id",
    "space_id",
    "status",
    "version",
    "attempt_count",
    "next_attempt_at",
    "claim_token",
    "lease_expires_at",
    "embedding",
    "last_error",
    "created_at",
    "updated_at",
    "completed_at",
)
"""@brief PassageVectorJob 映射列 / Columns used to map a PassageVectorJob."""

_VECTOR_TRANSITION_BATCH_SIZE = 128
"""@brief 单次向量状态批量 CAS 上限 / Maximum vector-state CAS batch size."""


class PostgresEpisodicSource:
    """@brief 从完整 Assistant Turn 发现个人/群聊隔离的情景来源 / Discover personal/group-isolated episodes from complete Assistant turns."""

    async def read_unprojected(
        self,
        *,
        format_version: int,
        limit: int,
    ) -> tuple[EpisodicTurn, ...]:
        """@brief 读取尚无指定格式 marker 的 Turn / Read turns without a marker for the requested format.

        @return 按完成时间稳定排序的 Turn / Turns stably ordered by completion time.
        @raise ValueError 参数越界 / Invalid arguments.
        @note ``source_projections.source_id`` 是非空复合主键成员；非相关 ``NOT IN``
            允许 PostgreSQL 一次构建 hashed SubPlan，避免对每个候选 Turn 重扫投影表。/
            ``source_projections.source_id`` is a non-null composite-primary-key member;
            the uncorrelated ``NOT IN`` lets PostgreSQL build one hashed SubPlan instead of
            rescanning the projection table for every candidate turn.
        """

        if isinstance(format_version, bool) or format_version < 1:
            raise ValueError("Episodic format_version must be positive")
        if not 1 <= limit <= 128:
            raise ValueError("Episodic source limit must be between 1 and 128")
        rows = await db.fetch_all(
            "WITH candidates AS ("
            "SELECT activity.turn_id, "
            "CASE WHEN COALESCE(activity.request #>> '{scope,is_group}', 'false') = 'true' "
            "THEN 'group' ELSE 'personal' END AS scope_kind, "
            "CASE WHEN COALESCE(activity.request #>> '{scope,is_group}', 'false') = 'true' "
            "THEN CAST(activity.request #>> '{scope,group_id}' AS BIGINT) "
            "ELSE CAST(activity.request #>> '{user,user_id}' AS BIGINT) END AS scope_id, "
            "turn.created_at AS occurred_at, "
            "activity.completed_at "
            "FROM conversation.inference_activities AS activity "
            "JOIN conversation.conversation_turns AS turn ON turn.turn_id = activity.turn_id "
            "WHERE activity.status = 'completed' "
            "AND COALESCE(activity.request ->> 'task_kind', 'assistant') = 'assistant' "
            "AND NOT EXISTS (SELECT 1 FROM conversation.conversation_messages AS excluded_message "
            "WHERE excluded_message.turn_id = activity.turn_id "
            "AND excluded_message.content @> jsonb_build_object('exclude_from_assistant', TRUE)) "
            "AND NOT EXISTS (SELECT 1 FROM conversation.conversation_messages AS attachment_message "
            "WHERE attachment_message.turn_id = activity.turn_id "
            "AND attachment_message.content ? 'workspace_attachment' "
            "AND ((jsonb_typeof(attachment_message.content -> 'workspace_attachment') = 'object' "
            "AND jsonb_typeof(attachment_message.content #> '{workspace_attachment,version}') = 'number' "
            "AND attachment_message.content #>> '{workspace_attachment,version}' = '1' "
            "AND jsonb_typeof(attachment_message.content #> '{workspace_attachment,state}') = 'string' "
            "AND attachment_message.content #>> '{workspace_attachment,state}' = 'imported') IS NOT TRUE)) "
            "AND activity.request #>> '{user,user_id}' ~ '^[1-9][0-9]*$' "
            "AND (COALESCE(activity.request #>> '{scope,is_group}', 'false') = 'false' "
            "OR activity.request #>> '{scope,group_id}' ~ '^-?[1-9][0-9]*$') "
            "AND NOT EXISTS ("
            "SELECT 1 FROM retrieval.scope_forgetting_boundaries AS boundary "
            "WHERE boundary.scope_kind = CASE WHEN COALESCE("
            "activity.request #>> '{scope,is_group}', 'false') = 'true' "
            "THEN 'group' ELSE 'personal' END "
            "AND boundary.scope_id = CASE WHEN COALESCE("
            "activity.request #>> '{scope,is_group}', 'false') = 'true' "
            "THEN CAST(activity.request #>> '{scope,group_id}' AS BIGINT) "
            "ELSE CAST(activity.request #>> '{user,user_id}' AS BIGINT) END "
            "AND turn.created_at <= boundary.forgotten_through"
            ") "
            "AND EXISTS (SELECT 1 FROM conversation.conversation_messages AS source_message "
            "WHERE source_message.turn_id = activity.turn_id "
            "AND source_message.role = 'user' "
            "AND jsonb_typeof(source_message.content -> 'text') = 'string') "
            "AND EXISTS (SELECT 1 FROM conversation.conversation_messages AS source_message "
            "WHERE source_message.turn_id = activity.turn_id "
            "AND source_message.role = 'assistant' "
            "AND jsonb_typeof(source_message.content -> 'text') = 'string') "
            "AND activity.turn_id NOT IN ("
            "SELECT projection.source_id FROM retrieval.source_projections AS projection "
            "WHERE projection.corpus_id = %s "
            "AND projection.source_kind = %s "
            "AND projection.format_version = %s"
            ") ORDER BY activity.completed_at, activity.turn_id LIMIT %s"
            ") SELECT candidate.turn_id, candidate.scope_kind, candidate.scope_id, "
            "user_messages.content_text, assistant_messages.content_text, "
            "candidate.occurred_at "
            "FROM candidates AS candidate "
            "CROSS JOIN LATERAL ("
            "SELECT string_agg(message.content ->> 'text', E'\\n' ORDER BY message.sequence) "
            "AS content_text FROM conversation.conversation_messages AS message "
            "WHERE message.turn_id = candidate.turn_id AND message.role = 'user' "
            "AND jsonb_typeof(message.content -> 'text') = 'string' "
            "AND NOT EXISTS (SELECT 1 FROM conversation.conversation_messages AS excluded_message "
            "WHERE excluded_message.turn_id = candidate.turn_id "
            "AND excluded_message.content @> jsonb_build_object('exclude_from_assistant', TRUE)) "
            "AND NOT EXISTS (SELECT 1 FROM conversation.conversation_messages AS attachment_message "
            "WHERE attachment_message.turn_id = candidate.turn_id "
            "AND attachment_message.content ? 'workspace_attachment' "
            "AND ((jsonb_typeof(attachment_message.content -> 'workspace_attachment') = 'object' "
            "AND jsonb_typeof(attachment_message.content #> '{workspace_attachment,version}') = 'number' "
            "AND attachment_message.content #>> '{workspace_attachment,version}' = '1' "
            "AND jsonb_typeof(attachment_message.content #> '{workspace_attachment,state}') = 'string' "
            "AND attachment_message.content #>> '{workspace_attachment,state}' = 'imported') IS NOT TRUE))"
            ") AS user_messages "
            "CROSS JOIN LATERAL ("
            "SELECT string_agg(message.content ->> 'text', E'\\n' ORDER BY message.sequence) "
            "AS content_text FROM conversation.conversation_messages AS message "
            "WHERE message.turn_id = candidate.turn_id AND message.role = 'assistant' "
            "AND jsonb_typeof(message.content -> 'text') = 'string' "
            "AND NOT EXISTS (SELECT 1 FROM conversation.conversation_messages AS excluded_message "
            "WHERE excluded_message.turn_id = candidate.turn_id "
            "AND excluded_message.content @> jsonb_build_object('exclude_from_assistant', TRUE)) "
            "AND NOT EXISTS (SELECT 1 FROM conversation.conversation_messages AS attachment_message "
            "WHERE attachment_message.turn_id = candidate.turn_id "
            "AND attachment_message.content ? 'workspace_attachment' "
            "AND ((jsonb_typeof(attachment_message.content -> 'workspace_attachment') = 'object' "
            "AND jsonb_typeof(attachment_message.content #> '{workspace_attachment,version}') = 'number' "
            "AND attachment_message.content #>> '{workspace_attachment,version}' = '1' "
            "AND jsonb_typeof(attachment_message.content #> '{workspace_attachment,state}') = 'string' "
            "AND attachment_message.content #>> '{workspace_attachment,state}' = 'imported') IS NOT TRUE))"
            ") AS assistant_messages "
            "WHERE user_messages.content_text IS NOT NULL "
            "AND assistant_messages.content_text IS NOT NULL "
            "ORDER BY candidate.completed_at, candidate.turn_id",
            (
                EPISODIC_CORPUS_ID,
                CONVERSATION_TURN_SOURCE_KIND,
                format_version,
                limit,
            ),
        )
        return tuple(_map_episode(row) for row in rows)


class PostgresRetrievalStore:
    """@brief pgvector passage workflow 与精确检索 store / pgvector passage workflow and exact-retrieval store."""

    async def ensure_space(self, space: EmbeddingSpace) -> None:
        """@brief 幂等创建并严格验证 embedding space / Idempotently create and strictly verify an embedding space.

        @return None / None.
        @raise RuntimeError 相同 ID 的协议漂移 / Contract drift under the same identity.
        """

        if space.dimensions != 1024:
            raise ValueError("PostgreSQL retrieval schema v1 requires 1024 dimensions")
        async with db.transaction() as connection:
            await db.execute(
                "INSERT INTO retrieval.embedding_spaces "
                "(space_id, model, dimensions, distance_metric, query_instruction, "
                "passage_format_version) VALUES (%s, %s, %s, 'cosine', %s, %s) "
                "ON CONFLICT (space_id) DO NOTHING",
                (
                    space.space_id,
                    space.model,
                    space.dimensions,
                    space.query_instruction,
                    space.passage_format_version,
                ),
                connection=connection,
            )
            row = await db.fetch_one(
                "SELECT model, dimensions, distance_metric, query_instruction, "
                "passage_format_version FROM retrieval.embedding_spaces "
                "WHERE space_id = %s FOR UPDATE",
                (space.space_id,),
                connection=connection,
            )
            expected = (
                space.model,
                space.dimensions,
                "cosine",
                space.query_instruction,
                space.passage_format_version,
            )
            if row is None or tuple(row) != expected:
                raise RuntimeError(
                    f"Embedding space contract drifted: {space.space_id}"
                )
            await db.execute(
                "INSERT INTO retrieval.passage_vectors "
                "(passage_id, space_id, status, version, attempt_count, next_attempt_at, "
                "created_at, updated_at) SELECT passage.passage_id, %s, 'pending', 0, 0, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP "
                "FROM retrieval.passages AS passage WHERE passage.format_version = %s "
                "ON CONFLICT (passage_id, space_id) DO NOTHING",
                (space.space_id, space.passage_format_version),
                connection=connection,
            )

    async def project_turn(
        self,
        turn: EpisodicTurn,
        passages: Sequence[RetrievalPassage],
        *,
        space: EmbeddingSpace,
        projected_at: datetime,
    ) -> None:
        """@brief 原子写入 source marker、passages 与 vector intents / Atomically write a source marker, passages, and vector intents.

        @return None / None.
        @raise RuntimeError 同来源 projection 漂移 / Projection drift for the same source.
        """

        timestamp = ensure_utc(projected_at)
        canonical = tuple(passages)
        if not canonical:
            raise ValueError("Episodic projection requires at least one passage")
        _validate_projection(turn, canonical, space)
        source_digest = _projection_digest(canonical)
        async with db.transaction() as connection:
            await lock_retrieval_scope(connection, turn.scope)
            boundary = await db.fetch_one(
                "SELECT forgotten_through "
                "FROM retrieval.scope_forgetting_boundaries "
                "WHERE scope_kind = %s AND scope_id = %s",
                (turn.scope.kind, turn.scope.scope_id),
                connection=connection,
            )
            if boundary is not None:
                forgotten_through = boundary[0]
                if not isinstance(forgotten_through, datetime):
                    raise TypeError("Retrieval forgetting boundary must be a datetime")
                if turn.occurred_at <= ensure_utc(forgotten_through):
                    return
            await db.execute(
                "INSERT INTO retrieval.source_projections "
                "(corpus_id, scope_kind, scope_id, personal_user_id, source_kind, "
                "source_id, format_version, source_digest, projected_at) "
                "VALUES (%s, %s, %s, %s, %s, CAST(%s AS UUID), %s, %s, %s) ON CONFLICT "
                "(corpus_id, source_kind, source_id, format_version) DO NOTHING",
                (
                    EPISODIC_CORPUS_ID,
                    turn.scope.kind,
                    turn.scope.scope_id,
                    _personal_user_id(turn.scope),
                    CONVERSATION_TURN_SOURCE_KIND,
                    str(turn.turn_id),
                    space.passage_format_version,
                    source_digest,
                    timestamp,
                ),
                connection=connection,
            )
            existing = await db.fetch_one(
                "SELECT scope_kind, scope_id, personal_user_id, source_digest "
                "FROM retrieval.source_projections "
                "WHERE corpus_id = %s AND source_kind = %s "
                "AND source_id = CAST(%s AS UUID) AND format_version = %s",
                (
                    EPISODIC_CORPUS_ID,
                    CONVERSATION_TURN_SOURCE_KIND,
                    str(turn.turn_id),
                    space.passage_format_version,
                ),
                connection=connection,
            )
            if existing is None or tuple(existing) != (
                turn.scope.kind,
                turn.scope.scope_id,
                _personal_user_id(turn.scope),
                source_digest,
            ):
                raise RuntimeError(
                    f"Episodic projection drifted for turn {turn.turn_id}"
                )
            for passage in canonical:
                await self._insert_passage(
                    passage,
                    space=space,
                    created_at=timestamp,
                    connection=connection,
                )

    async def _insert_passage(
        self,
        passage: RetrievalPassage,
        *,
        space: EmbeddingSpace,
        created_at: datetime,
        connection: AsyncConnection,
    ) -> None:
        """@brief 插入 passage 与该空间的 pending vector / Insert a passage and its pending vector.

        @return None / None.
        """

        await db.execute(
            "INSERT INTO retrieval.passages "
            "(passage_id, corpus_id, scope_kind, scope_id, personal_user_id, source_kind, "
            "source_id, ordinal, format_version, content_text, content_digest, occurred_at, "
            "created_at) VALUES (CAST(%s AS UUID), %s, %s, %s, %s, %s, "
            "CAST(%s AS UUID), %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (passage_id) DO NOTHING",
            (
                str(passage.passage_id),
                passage.corpus_id,
                passage.scope.kind,
                passage.scope.scope_id,
                _personal_user_id(passage.scope),
                passage.source_kind,
                str(passage.source_id),
                passage.ordinal,
                passage.format_version,
                passage.text,
                passage.content_digest,
                passage.occurred_at,
                created_at,
            ),
            connection=connection,
        )
        row = await db.fetch_one(
            "SELECT corpus_id, scope_kind, scope_id, source_kind, source_id, ordinal, "
            "format_version, content_text, content_digest, occurred_at "
            "FROM retrieval.passages WHERE passage_id = CAST(%s AS UUID)",
            (str(passage.passage_id),),
            connection=connection,
        )
        if row is None or _passage_semantics(row) != _passage_semantics_from_model(
            passage
        ):
            raise RuntimeError(f"Retrieval passage drifted: {passage.passage_id}")
        await db.execute(
            "INSERT INTO retrieval.passage_vectors "
            "(passage_id, space_id, status, version, attempt_count, next_attempt_at, "
            "created_at, updated_at) VALUES (CAST(%s AS UUID), %s, 'pending', 0, 0, "
            "%s, %s, %s) ON CONFLICT (passage_id, space_id) DO NOTHING",
            (
                str(passage.passage_id),
                space.space_id,
                created_at,
                created_at,
                created_at,
            ),
            connection=connection,
        )

    async def claim_vectors(
        self,
        *,
        space: EmbeddingSpace,
        now: datetime,
        limit: int,
        lease_for: timedelta,
    ) -> tuple[PassageVectorClaim, ...]:
        """@brief 以领域决策和批量 CAS 领取待 embedding Passage / Claim passages through domain decisions and batched CAS.

        @param space 当前 embedding space / Active embedding space.
        @param now 领取判定时刻 / Claim decision instant.
        @param limit 最大领取数 / Maximum number of claims.
        @param lease_for crash recovery 租期 / Crash-recovery lease duration.
        @return 按 ready 顺序排列的 sealed claims / Sealed claims in ready order.
        """

        timestamp = ensure_utc(now)
        if not 1 <= limit <= _VECTOR_TRANSITION_BATCH_SIZE:
            raise ValueError("Vector claim limit must be between 1 and 128")
        if not isinstance(lease_for, timedelta) or lease_for <= timedelta():
            raise ValueError("Vector claim lease_for must be positive")

        async with db.transaction() as connection:
            rows = await db.fetch_all(
                "SELECT "
                + _vector_columns("vector")
                + ", "
                + ", ".join(
                    f"passage.{column.strip()}"
                    for column in _PASSAGE_COLUMNS.split(",")
                )
                + " FROM retrieval.passage_vectors AS vector "
                "JOIN retrieval.passages AS passage "
                "ON passage.passage_id = vector.passage_id "
                "WHERE vector.space_id = %s "
                "AND vector.status IN ('pending', 'retry_wait') "
                "AND vector.next_attempt_at <= %s "
                "ORDER BY vector.next_attempt_at, vector.passage_id "
                "LIMIT %s FOR UPDATE OF vector SKIP LOCKED",
                (space.space_id, timestamp, limit),
                connection=connection,
            )
            decisions: list[PassageVectorClaimed] = []
            for row in rows:
                values = _row_values(row, len(_VECTOR_COLUMN_NAMES) + 11)
                previous = _map_vector_job(values[: len(_VECTOR_COLUMN_NAMES)])
                passage = _map_passage(values[len(_VECTOR_COLUMN_NAMES) :])
                decisions.append(
                    previous.claim(
                        passage=passage,
                        space=space,
                        claim_token=uuid4(),
                        claimed_at=timestamp,
                        lease_for=lease_for,
                    )
                )
            if not decisions:
                return ()

            persisted = await _persist_claim_decisions(
                decisions,
                space=space,
                connection=connection,
            )
            for decision in decisions:
                actual = persisted.get(decision.job.key)
                if actual is None or actual != decision.job:
                    raise RuntimeError(
                        "Passage-vector claim SQL diverged from the domain decision"
                    )
            return tuple(decision.claim for decision in decisions)

    async def complete_vector(
        self,
        claim: PassageVectorClaim,
        vector: EmbeddingVector,
        *,
        completed_at: datetime,
    ) -> None:
        """@brief fenced 保存完整向量 / Persist a complete vector with fencing.

        @return None / None.
        @raise StaleVectorClaimError recovery/reclaim 后 claim token 已非当前 owner /
            Claim token no longer identifies the current owner after recovery or reclaim.
        """

        decision = claim.job.complete(claim, vector, completed_at=completed_at)
        target = decision.job
        completed = _completed_vector_state(target)
        processing = _processing_vector_state(claim.job)
        async with db.transaction() as connection:
            row = await db.fetch_one(
                "UPDATE retrieval.passage_vectors AS vector "
                "SET status = 'completed', version = %s, next_attempt_at = NULL, "
                "claim_token = NULL, lease_expires_at = NULL, "
                "embedding = CAST(%s AS vector), last_error = NULL, "
                "updated_at = %s, completed_at = %s "
                "WHERE vector.passage_id = CAST(%s AS UUID) "
                "AND vector.space_id = %s AND vector.status = 'processing' "
                "AND vector.version = %s "
                "AND vector.claim_token = CAST(%s AS UUID) RETURNING "
                + _vector_columns("vector"),
                (
                    target.version,
                    _encode_vector(completed.vector),
                    target.updated_at,
                    completed.completed_at,
                    str(target.key.passage_id),
                    target.key.space_id,
                    claim.job.version,
                    str(processing.claim_token),
                ),
                connection=connection,
            )
            if row is None:
                raise StaleVectorClaimError(
                    f"Stale vector claim {claim.passage.passage_id}"
                )
            persisted = _map_vector_job(row)
            if not _jobs_equal_at_pgvector_precision(target, persisted):
                raise RuntimeError(
                    "Passage-vector completion SQL diverged from the domain decision"
                )

    async def retry_vector(
        self,
        claim: PassageVectorClaim,
        *,
        retry_at: datetime,
        error: str,
        failed_at: datetime,
    ) -> None:
        """@brief fenced 安排 retry / Schedule a retry with fencing."""

        decision = claim.job.schedule_retry(
            claim,
            retry_at=retry_at,
            failure=PassageVectorFailure(error),
            failed_at=failed_at,
        )
        target = decision.job
        retrying = _retrying_vector_state(target)
        processing = _processing_vector_state(claim.job)
        async with db.transaction() as connection:
            row = await db.fetch_one(
                "UPDATE retrieval.passage_vectors AS vector "
                "SET status = 'retry_wait', version = %s, next_attempt_at = %s, "
                "claim_token = NULL, lease_expires_at = NULL, embedding = NULL, "
                "last_error = %s, updated_at = %s, completed_at = NULL "
                "WHERE vector.passage_id = CAST(%s AS UUID) "
                "AND vector.space_id = %s AND vector.status = 'processing' "
                "AND vector.version = %s "
                "AND vector.claim_token = CAST(%s AS UUID) RETURNING "
                + _vector_columns("vector"),
                (
                    target.version,
                    retrying.next_attempt_at,
                    retrying.failure.summary,
                    target.updated_at,
                    str(target.key.passage_id),
                    target.key.space_id,
                    claim.job.version,
                    str(processing.claim_token),
                ),
                connection=connection,
            )
            _require_persisted_settlement(row, target, claim)

    async def fail_vector(
        self,
        claim: PassageVectorClaim,
        *,
        error: str,
        failed_at: datetime,
    ) -> None:
        """@brief fenced 终结 vector job / Finally fail a vector job with fencing."""

        decision = claim.job.fail(
            claim,
            failure=PassageVectorFailure(error),
            failed_at=failed_at,
        )
        target = decision.job
        failed = _failed_vector_state(target)
        processing = _processing_vector_state(claim.job)
        async with db.transaction() as connection:
            row = await db.fetch_one(
                "UPDATE retrieval.passage_vectors AS vector "
                "SET status = 'failed_final', version = %s, next_attempt_at = NULL, "
                "claim_token = NULL, lease_expires_at = NULL, embedding = NULL, "
                "last_error = %s, updated_at = %s, completed_at = NULL "
                "WHERE vector.passage_id = CAST(%s AS UUID) "
                "AND vector.space_id = %s AND vector.status = 'processing' "
                "AND vector.version = %s "
                "AND vector.claim_token = CAST(%s AS UUID) RETURNING "
                + _vector_columns("vector"),
                (
                    target.version,
                    failed.failure.summary,
                    target.updated_at,
                    str(target.key.passage_id),
                    target.key.space_id,
                    claim.job.version,
                    str(processing.claim_token),
                ),
                connection=connection,
            )
            _require_persisted_settlement(row, target, claim)

    async def recover_expired_vector_leases(
        self,
        *,
        space: EmbeddingSpace,
        now: datetime,
    ) -> int:
        """@brief 回收当前空间过期 leases / Recover expired leases for one space.

        @return 回收行数 / Number of recovered rows.
        """

        timestamp = ensure_utc(now)
        recovered = 0
        async with db.transaction() as connection:
            while True:
                rows = await db.fetch_all(
                    "SELECT "
                    + _vector_columns("vector")
                    + " FROM retrieval.passage_vectors AS vector "
                    "WHERE vector.space_id = %s AND vector.status = 'processing' "
                    "AND vector.lease_expires_at <= %s "
                    "ORDER BY vector.lease_expires_at, vector.passage_id "
                    "LIMIT %s FOR UPDATE OF vector",
                    (space.space_id, timestamp, _VECTOR_TRANSITION_BATCH_SIZE),
                    connection=connection,
                )
                decisions = tuple(
                    _map_vector_job(row).recover_expired(recovered_at=timestamp)
                    for row in rows
                )
                if decisions:
                    persisted = await _persist_recovery_decisions(
                        decisions,
                        space=space,
                        connection=connection,
                    )
                    for decision in decisions:
                        actual = persisted.get(decision.job.key)
                        if actual is None or actual != decision.job:
                            raise RuntimeError(
                                "Passage-vector recovery SQL diverged from the domain decision"
                            )
                recovered += len(decisions)
                if len(decisions) < _VECTOR_TRANSITION_BATCH_SIZE:
                    return recovered

    async def search(
        self,
        *,
        scope: RetrievalScope,
        corpus_id: str,
        space: EmbeddingSpace,
        query_vector: EmbeddingVector,
        limit: int,
    ) -> tuple[RetrievalEvidence, ...]:
        """@brief 先做强租户过滤再精确 cosine 排序 / Apply strong tenant filtering before exact cosine ordering.

        @return 距离升序证据 / Evidence in ascending distance order.
        """

        if not 1 <= limit <= 384:
            raise ValueError("Retrieval limit must be between 1 and 384")
        query_vector.require_space(space)
        try:
            rows = await db.fetch_all(
                "SELECT "
                + ", ".join(
                    f"passage.{column.strip()}"
                    for column in _PASSAGE_COLUMNS.split(",")
                )
                + ", vector.embedding <=> CAST(%s AS vector) AS cosine_distance "
                "FROM retrieval.passage_vectors AS vector "
                "JOIN retrieval.passages AS passage ON passage.passage_id = vector.passage_id "
                "WHERE vector.space_id = %s AND vector.status = 'completed' "
                "AND passage.scope_kind = %s AND passage.scope_id = %s "
                "AND passage.corpus_id = %s "
                "AND passage.format_version = %s "
                "ORDER BY vector.embedding <=> CAST(%s AS vector), passage.occurred_at DESC, "
                "passage.passage_id LIMIT %s",
                (
                    _encode_vector(query_vector),
                    space.space_id,
                    scope.kind,
                    scope.scope_id,
                    corpus_id,
                    space.passage_format_version,
                    _encode_vector(query_vector),
                    limit,
                ),
            )
        except SQLAlchemyError as error:
            raise RetrievalIOError("Semantic retrieval store is unavailable") from error
        return tuple(
            RetrievalEvidence(
                passage=_map_passage(_row_values(row, 12)[:11]),
                cosine_distance=_float(_row_values(row, 12)[11]),
            )
            for row in rows
        )


def _vector_columns(alias: str) -> str:
    """@brief 构造完整 PassageVectorJob SQL 投影 / Build the complete PassageVectorJob SQL projection.

    @param alias 已知安全的 SQL table alias / Known-safe SQL table alias.
    @return 逗号分隔投影 / Comma-separated projection.
    """

    return ", ".join(
        f"{alias}.embedding::text" if column == "embedding" else f"{alias}.{column}"
        for column in _VECTOR_COLUMN_NAMES
    )


def _map_vector_job(row: object) -> PassageVectorJob:
    """@brief 从完整数据库 row 恢复向量聚合 / Restore a vector aggregate from a complete database row.

    @param row 十三个持久化字段 / Thirteen persisted fields.
    @return 已验证领域聚合 / Validated domain aggregate.
    """

    values = _row_values(row, len(_VECTOR_COLUMN_NAMES))
    return PassageVectorJob.restore(
        key=PassageVectorJobKey(
            passage_id=_uuid(values[0]),
            space_id=_text(values[1]),
        ),
        status=PassageVectorStatus(_text(values[2])),
        version=_integer(values[3]),
        attempt_count=_integer(values[4]),
        next_attempt_at=_optional_datetime(values[5]),
        claim_token=_optional_uuid(values[6]),
        lease_expires_at=_optional_datetime(values[7]),
        vector=_optional_vector(values[8]),
        last_error=_optional_text(values[9]),
        created_at=_datetime(values[10]),
        updated_at=_datetime(values[11]),
        completed_at=_optional_datetime(values[12]),
    )


async def _persist_claim_decisions(
    decisions: Sequence[PassageVectorClaimed],
    *,
    space: EmbeddingSpace,
    connection: AsyncConnection,
) -> dict[PassageVectorJobKey, PassageVectorJob]:
    """@brief 批量 CAS 持久化已经计算的 claim 决策 / Persist already-computed claim decisions through batched CAS.

    @param decisions 非空、有序领域决策 / Non-empty ordered domain decisions.
    @param space 当前 embedding space / Active embedding space.
    @param connection 持有 candidate locks 的事务连接 / Transaction holding candidate locks.
    @return 按聚合 key 索引的 RETURNING 状态 / RETURNING states indexed by aggregate key.
    """

    if not decisions:
        raise ValueError("Passage-vector claim persistence requires decisions")
    value_sql = ", ".join(
        "(CAST(%s AS UUID), CAST(%s AS TEXT), CAST(%s AS BIGINT), "
        "CAST(%s AS BIGINT), CAST(%s AS INTEGER), CAST(%s AS UUID))"
        for _ in decisions
    )
    params: list[object] = []
    first = decisions[0]
    first_processing = _processing_vector_state(first.job)
    for decision in decisions:
        processing = _processing_vector_state(decision.job)
        if (
            decision.job.key.space_id != space.space_id
            or decision.job.updated_at != first.job.updated_at
            or processing.lease_expires_at != first_processing.lease_expires_at
        ):
            raise RuntimeError("Passage-vector claim batch is not homogeneous")
        params.extend(
            (
                str(decision.previous.key.passage_id),
                _vector_status(decision.previous).value,
                decision.previous.version,
                decision.job.version,
                decision.job.attempt_count,
                str(processing.claim_token),
            )
        )
    params.extend(
        (
            first_processing.lease_expires_at,
            first.job.updated_at,
            space.space_id,
        )
    )
    rows = await db.fetch_all(
        "WITH decisions (passage_id, expected_status, expected_version, "
        "new_version, new_attempt_count, new_claim_token) AS (VALUES "
        + value_sql
        + ") UPDATE retrieval.passage_vectors AS vector "
        "SET status = 'processing', version = decision.new_version, "
        "attempt_count = decision.new_attempt_count, next_attempt_at = NULL, "
        "claim_token = decision.new_claim_token, lease_expires_at = %s, "
        "embedding = NULL, last_error = NULL, updated_at = %s, completed_at = NULL "
        "FROM decisions AS decision "
        "WHERE vector.passage_id = decision.passage_id AND vector.space_id = %s "
        "AND vector.status = decision.expected_status "
        "AND vector.version = decision.expected_version RETURNING "
        + _vector_columns("vector"),
        params,
        connection=connection,
    )
    if len(rows) != len(decisions):
        raise RuntimeError("Passage-vector claim CAS did not update every candidate")
    return _index_vector_jobs(rows)


async def _persist_recovery_decisions(
    decisions: Sequence[PassageVectorLeaseRecovered],
    *,
    space: EmbeddingSpace,
    connection: AsyncConnection,
) -> dict[PassageVectorJobKey, PassageVectorJob]:
    """@brief 批量 CAS 持久化过期 lease 恢复决策 / Persist expired-lease recovery decisions through batched CAS.

    @param decisions 非空恢复决策 / Non-empty recovery decisions.
    @param space 当前 embedding space / Active embedding space.
    @param connection 持有过期行锁的事务连接 / Transaction holding expired-row locks.
    @return 按聚合 key 索引的 RETURNING 状态 / RETURNING states indexed by aggregate key.
    """

    if not decisions:
        raise ValueError("Passage-vector recovery persistence requires decisions")
    value_sql = ", ".join(
        "(CAST(%s AS UUID), CAST(%s AS BIGINT), CAST(%s AS UUID), CAST(%s AS BIGINT))"
        for _ in decisions
    )
    params: list[object] = []
    first = decisions[0]
    first_retry = _retrying_vector_state(first.job)
    for decision in decisions:
        previous = _processing_vector_state(decision.previous)
        retrying = _retrying_vector_state(decision.job)
        if (
            decision.job.key.space_id != space.space_id
            or decision.job.updated_at != first.job.updated_at
            or retrying != first_retry
        ):
            raise RuntimeError("Passage-vector recovery batch is not homogeneous")
        params.extend(
            (
                str(decision.previous.key.passage_id),
                decision.previous.version,
                str(previous.claim_token),
                decision.job.version,
            )
        )
    params.extend(
        (
            first_retry.next_attempt_at,
            first_retry.failure.summary,
            first.job.updated_at,
            space.space_id,
        )
    )
    rows = await db.fetch_all(
        "WITH decisions (passage_id, expected_version, expected_claim_token, "
        "new_version) AS (VALUES "
        + value_sql
        + ") UPDATE retrieval.passage_vectors AS vector "
        "SET status = 'retry_wait', version = decision.new_version, "
        "next_attempt_at = %s, claim_token = NULL, lease_expires_at = NULL, "
        "embedding = NULL, last_error = %s, updated_at = %s, completed_at = NULL "
        "FROM decisions AS decision "
        "WHERE vector.passage_id = decision.passage_id AND vector.space_id = %s "
        "AND vector.status = 'processing' "
        "AND vector.version = decision.expected_version "
        "AND vector.claim_token = decision.expected_claim_token RETURNING "
        + _vector_columns("vector"),
        params,
        connection=connection,
    )
    if len(rows) != len(decisions):
        raise RuntimeError("Passage-vector recovery CAS did not update every candidate")
    return _index_vector_jobs(rows)


def _index_vector_jobs(
    rows: Sequence[object],
) -> dict[PassageVectorJobKey, PassageVectorJob]:
    """@brief 按复合 key 索引 RETURNING 聚合 / Index RETURNING aggregates by composite key.

    @param rows 完整向量状态 rows / Complete vector-state rows.
    @return 唯一 key 到聚合的映射 / Mapping from unique keys to aggregates.
    """

    indexed: dict[PassageVectorJobKey, PassageVectorJob] = {}
    for row in rows:
        job = _map_vector_job(row)
        if job.key in indexed:
            raise RuntimeError("Passage-vector RETURNING contained a duplicate key")
        indexed[job.key] = job
    return indexed


def _require_persisted_settlement(
    row: object | None,
    target: PassageVectorJob,
    claim: PassageVectorClaim,
) -> None:
    """@brief 验证 fenced settlement 的 RETURNING 状态 / Validate a fenced settlement's RETURNING state.

    @param row 可选 RETURNING row / Optional RETURNING row.
    @param target 领域目标聚合 / Domain target aggregate.
    @param claim 当前 sealed claim / Current sealed claim.
    @return None / None.
    @raise StaleVectorClaimError CAS 未命中 / CAS did not match.
    @raise RuntimeError SQL post-state 与领域决策不同 / SQL post-state differs from the domain decision.
    """

    if row is None:
        raise StaleVectorClaimError(f"Stale vector claim {claim.passage.passage_id}")
    if _map_vector_job(row) != target:
        raise RuntimeError(
            "Passage-vector settlement SQL diverged from the domain decision"
        )


def _vector_status(job: PassageVectorJob) -> PassageVectorStatus:
    """@brief 把穷尽领域状态映射为持久化枚举 / Map an exhaustive domain state to its persisted enum.

    @param job 向量聚合 / Vector aggregate.
    @return 持久化状态 / Persisted status.
    """

    if isinstance(job.state, AwaitingPassageVector):
        return PassageVectorStatus.PENDING
    if isinstance(job.state, WaitingPassageVectorRetry):
        return PassageVectorStatus.RETRY_WAIT
    if isinstance(job.state, ProcessingPassageVector):
        return PassageVectorStatus.PROCESSING
    if isinstance(job.state, CompletedPassageVector):
        return PassageVectorStatus.COMPLETED
    return PassageVectorStatus.FAILED_FINAL


def _processing_vector_state(job: PassageVectorJob) -> ProcessingPassageVector:
    """@brief 提取 processing 状态或暴露内部错误 / Extract processing state or expose an internal error.

    @param job 预期 processing 聚合 / Expected processing aggregate.
    @return Processing 状态 / Processing state.
    """

    if not isinstance(job.state, ProcessingPassageVector):
        raise RuntimeError("Passage-vector job is not processing")
    return job.state


def _completed_vector_state(job: PassageVectorJob) -> CompletedPassageVector:
    """@brief 提取 completed 状态 / Extract completed state.

    @param job 预期 completed 聚合 / Expected completed aggregate.
    @return Completed 状态 / Completed state.
    """

    if not isinstance(job.state, CompletedPassageVector):
        raise RuntimeError("Passage-vector job is not completed")
    return job.state


def _retrying_vector_state(job: PassageVectorJob) -> WaitingPassageVectorRetry:
    """@brief 提取 retry-wait 状态 / Extract retry-wait state.

    @param job 预期 retry-wait 聚合 / Expected retry-wait aggregate.
    @return Retry-wait 状态 / Retry-wait state.
    """

    if not isinstance(job.state, WaitingPassageVectorRetry):
        raise RuntimeError("Passage-vector job is not waiting for retry")
    return job.state


def _failed_vector_state(job: PassageVectorJob) -> FailedPassageVector:
    """@brief 提取 failed-final 状态 / Extract failed-final state.

    @param job 预期 failed-final 聚合 / Expected failed-final aggregate.
    @return Failed-final 状态 / Failed-final state.
    """

    if not isinstance(job.state, FailedPassageVector):
        raise RuntimeError("Passage-vector job is not finally failed")
    return job.state


def _jobs_equal_at_pgvector_precision(
    expected: PassageVectorJob,
    persisted: PassageVectorJob,
) -> bool:
    """@brief 按 pgvector float32 语义比较完成态 / Compare completed states using pgvector float32 semantics.

    @param expected Provider 精度的领域目标 / Domain target at provider precision.
    @param persisted PostgreSQL RETURNING 状态 / PostgreSQL RETURNING state.
    @return 除向量量化外是否完全相等 / Whether states are equal aside from vector quantization.
    @note pgvector ``vector`` 坐标是 IEEE-754 单精度；该适配器比较不得污染通用领域向量。/
        pgvector ``vector`` coordinates are IEEE-754 single precision; this adapter-specific
        comparison must not leak that storage choice into the generic domain vector.
    """

    if expected == persisted:
        return True
    if not isinstance(expected.state, CompletedPassageVector) or not isinstance(
        persisted.state, CompletedPassageVector
    ):
        return False
    return (
        expected.key == persisted.key
        and expected.version == persisted.version
        and expected.attempt_count == persisted.attempt_count
        and expected.created_at == persisted.created_at
        and expected.updated_at == persisted.updated_at
        and expected.state.completed_at == persisted.state.completed_at
        and tuple(_float32(value) for value in expected.state.vector.values)
        == tuple(_float32(value) for value in persisted.state.vector.values)
    )


def _float32(value: float) -> float:
    """@brief 按 PostgreSQL pgvector 的单精度往返一个坐标 / Round-trip one coordinate at PostgreSQL pgvector precision.

    @param value Python 双精度坐标 / Python double-precision coordinate.
    @return IEEE-754 单精度值 / IEEE-754 single-precision value.
    """

    return float(struct.unpack("!f", struct.pack("!f", value))[0])


def _validate_projection(
    turn: EpisodicTurn,
    passages: Sequence[RetrievalPassage],
    space: EmbeddingSpace,
) -> None:
    """@brief 验证 source、passages 与 space 一致 / Validate source, passages, and space consistency."""

    for ordinal, passage in enumerate(passages):
        if (
            passage.corpus_id != EPISODIC_CORPUS_ID
            or passage.scope != turn.scope
            or passage.source_kind != CONVERSATION_TURN_SOURCE_KIND
            or passage.source_id != turn.turn_id
            or passage.ordinal != ordinal
            or passage.format_version != space.passage_format_version
        ):
            raise ValueError("Episodic passage does not match its source and space")


def _projection_digest(passages: Sequence[RetrievalPassage]) -> str:
    """@brief 对有序 passage digest 再摘要 / Hash the ordered passage digests.

    @return Source projection SHA-256 / Source-projection SHA-256.
    """

    payload = "\x1f".join(passage.content_digest for passage in passages)
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def _map_episode(row: object) -> EpisodicTurn:
    """@brief 映射数据库情景 Turn / Map a database episodic turn."""

    values = _row_values(row, 6)
    return EpisodicTurn(
        turn_id=_uuid(values[0]),
        scope=_retrieval_scope(values[1], values[2]),
        user_text=_text(values[3]),
        assistant_text=_text(values[4]),
        occurred_at=_datetime(values[5]),
    )


def _map_passage(row: object) -> RetrievalPassage:
    """@brief 映射数据库 passage / Map a database passage."""

    values = _row_values(row, 11)
    return RetrievalPassage(
        passage_id=_uuid(values[0]),
        corpus_id=_text(values[1]),
        scope=_retrieval_scope(values[2], values[3]),
        source_kind=_text(values[4]),
        source_id=_uuid(values[5]),
        ordinal=_integer(values[6]),
        format_version=_integer(values[7]),
        text=_text(values[8]),
        content_digest=_text(values[9]),
        occurred_at=_datetime(values[10]),
    )


def _passage_semantics(row: object) -> tuple[object, ...]:
    """@brief 规范数据库 passage 语义 tuple / Normalize database-passage semantics."""

    values = _row_values(row, 10)
    return (
        _text(values[0]),
        _text(values[1]),
        _integer(values[2]),
        _text(values[3]),
        _uuid(values[4]),
        _integer(values[5]),
        _integer(values[6]),
        _text(values[7]),
        _text(values[8]),
        _datetime(values[9]),
    )


def _passage_semantics_from_model(passage: RetrievalPassage) -> tuple[object, ...]:
    """@brief 规范领域 passage 语义 tuple / Normalize domain-passage semantics."""

    return (
        passage.corpus_id,
        passage.scope.kind,
        passage.scope.scope_id,
        passage.source_kind,
        passage.source_id,
        passage.ordinal,
        passage.format_version,
        passage.text,
        passage.content_digest,
        passage.occurred_at,
    )


def _retrieval_scope(kind: object, scope_id: object) -> RetrievalScope:
    """@brief 映射并验证持久化隔离域 / Map and validate a persisted isolation scope.

    @param kind 持久化类别 / Persisted kind.
    @param scope_id 持久化主体 ID / Persisted principal identifier.
    @return 强类型检索域 / Strongly typed retrieval scope.
    """

    scope_kind = _text(kind)
    if scope_kind not in {"personal", "group"}:
        raise ValueError(f"Unknown retrieval scope kind: {scope_kind}")
    return RetrievalScope(cast(RetrievalScopeKind, scope_kind), _integer(scope_id))


def _personal_user_id(scope: RetrievalScope) -> int | None:
    """@brief 返回个人域的级联删除锚点 / Return the cascade-deletion anchor for a personal scope.

    @param scope 检索隔离域 / Retrieval isolation scope.
    @return 个人 user ID；群域为 None / Personal user ID, or None for a group scope.
    """

    return scope.scope_id if scope.kind == "personal" else None


def _encode_vector(vector: EmbeddingVector) -> str:
    """@brief 编码 pgvector literal / Encode a pgvector literal.

    @return 无 NaN 的 JSON array / JSON array without NaN.
    """

    return json.dumps(vector.values, allow_nan=False, separators=(",", ":"))


def _row_values(row: object, expected: int) -> Sequence[object]:
    """@brief 校验数据库 row 宽度 / Validate database-row width."""

    if not isinstance(row, Sequence) or isinstance(row, str) or len(row) != expected:
        raise TypeError(f"Expected a {expected}-column retrieval row")
    return cast(Sequence[object], row)


def _uuid(value: object) -> UUID:
    """@brief 转换 UUID / Convert a UUID."""

    return value if isinstance(value, UUID) else UUID(str(value))


def _optional_uuid(value: object) -> UUID | None:
    """@brief 转换 nullable UUID / Convert a nullable UUID.

    @param value 数据库标量 / Database scalar.
    @return UUID 或 None / UUID or None.
    """

    return None if value is None else _uuid(value)


def _integer(value: object) -> int:
    """@brief 转换整数 / Convert an integer."""

    return int(str(value))


def _float(value: object) -> float:
    """@brief 转换浮点数 / Convert a float."""

    return float(str(value))


def _text(value: object) -> str:
    """@brief 转换非空文本 / Convert non-empty text."""

    if not isinstance(value, str):
        raise TypeError("Expected retrieval text")
    return value


def _optional_text(value: object) -> str | None:
    """@brief 转换 nullable 文本 / Convert nullable text.

    @param value 数据库标量 / Database scalar.
    @return 文本或 None / Text or None.
    """

    return None if value is None else _text(value)


def _datetime(value: object) -> datetime:
    """@brief 转换 datetime / Convert a datetime."""

    if not isinstance(value, datetime):
        raise TypeError("Expected retrieval datetime")
    return value


def _optional_datetime(value: object) -> datetime | None:
    """@brief 转换 nullable datetime / Convert a nullable datetime.

    @param value 数据库标量 / Database scalar.
    @return UTC datetime 或 None / UTC datetime or None.
    """

    return None if value is None else _datetime(value)


def _optional_vector(value: object) -> EmbeddingVector | None:
    """@brief 严格解析 nullable pgvector 文本 / Strictly parse nullable pgvector text.

    @param value ``embedding::text`` 数据库标量 / ``embedding::text`` database scalar.
    @return 已验证领域向量或 None / Validated domain vector or None.
    @raise TypeError pgvector 文本不是数值数组 / pgvector text is not a numeric array.
    """

    if value is None:
        return None
    payload = json.loads(_text(value))
    if not isinstance(payload, list) or not payload:
        raise TypeError("Expected pgvector text to contain a non-empty array")
    if any(
        isinstance(item, bool) or not isinstance(item, int | float) for item in payload
    ):
        raise TypeError("Expected pgvector text to contain only numeric coordinates")
    return EmbeddingVector(tuple(float(item) for item in payload))


__all__ = ["PostgresEpisodicSource", "PostgresRetrievalStore"]
