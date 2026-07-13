"""@brief PostgreSQL/pgvector 检索与 Conversation 情景来源 adapter / PostgreSQL/pgvector retrieval and episodic-source adapters."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.retrieval import (
    CONVERSATION_TURN_SOURCE_KIND,
    EPISODIC_CORPUS_ID,
    EpisodicTurn,
    PassageVectorClaim,
    StaleVectorClaimError,
)
from fogmoe_bot.domain.temporal import ensure_utc
from fogmoe_bot.domain.retrieval import (
    EmbeddingSpace,
    EmbeddingVector,
    RetrievalEvidence,
    RetrievalPassage,
    RetrievalScope,
    RetrievalScopeKind,
)
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database.retrieval_scope import lock_retrieval_scope


_PASSAGE_COLUMNS = (
    "passage_id, corpus_id, scope_kind, scope_id, source_kind, source_id, ordinal, "
    "format_version, content_text, content_digest, occurred_at"
)
"""@brief RetrievalPassage 映射列 / Columns used to map a RetrievalPassage."""


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
        """

        if isinstance(format_version, bool) or format_version < 1:
            raise ValueError("Episodic format_version must be positive")
        if not 1 <= limit <= 128:
            raise ValueError("Episodic source limit must be between 1 and 128")
        rows = await db_connection.fetch_all(
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
            "AND NOT EXISTS ("
            "SELECT 1 FROM retrieval.source_projections AS projection "
            "WHERE projection.corpus_id = %s "
            "AND projection.source_kind = %s "
            "AND projection.source_id = activity.turn_id "
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
        async with db_connection.transaction() as connection:
            await db_connection.execute(
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
            row = await db_connection.fetch_one(
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
            await db_connection.execute(
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
        async with db_connection.transaction() as connection:
            await lock_retrieval_scope(connection, turn.scope)
            boundary = await db_connection.fetch_one(
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
            await db_connection.execute(
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
            existing = await db_connection.fetch_one(
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

        await db_connection.execute(
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
        row = await db_connection.fetch_one(
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
        await db_connection.execute(
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
        """@brief 使用 SKIP LOCKED 领取待嵌入 passages / Claim passages with SKIP LOCKED.

        @return 当前 fenced claims / Current fenced claims.
        """

        timestamp = ensure_utc(now)
        if not 1 <= limit <= 128 or lease_for <= timedelta():
            raise ValueError("Vector claim bounds are invalid")
        lease_expires_at = timestamp + lease_for
        claims: list[PassageVectorClaim] = []
        async with db_connection.transaction() as connection:
            candidates = await db_connection.fetch_all(
                "SELECT vector.passage_id FROM retrieval.passage_vectors AS vector "
                "WHERE vector.space_id = %s AND vector.status IN ('pending', 'retry_wait') "
                "AND vector.next_attempt_at <= %s ORDER BY vector.next_attempt_at, "
                "vector.passage_id FOR UPDATE SKIP LOCKED LIMIT %s",
                (space.space_id, timestamp, limit),
                connection=connection,
            )
            for candidate in candidates:
                passage_id = _uuid(_row_values(candidate, 1)[0])
                token = uuid4()
                row = await db_connection.fetch_one(
                    "UPDATE retrieval.passage_vectors SET status = 'processing', "
                    "version = version + 1, attempt_count = attempt_count + 1, "
                    "next_attempt_at = NULL, claim_token = CAST(%s AS UUID), "
                    "lease_expires_at = %s, last_error = NULL, updated_at = %s "
                    "WHERE passage_id = CAST(%s AS UUID) AND space_id = %s "
                    "AND status IN ('pending', 'retry_wait') RETURNING attempt_count",
                    (
                        str(token),
                        lease_expires_at,
                        timestamp,
                        str(passage_id),
                        space.space_id,
                    ),
                    connection=connection,
                )
                if row is None:
                    raise RuntimeError("Locked vector candidate was not claimable")
                passage_row = await db_connection.fetch_one(
                    "SELECT " + _PASSAGE_COLUMNS + " FROM retrieval.passages "
                    "WHERE passage_id = CAST(%s AS UUID)",
                    (str(passage_id),),
                    connection=connection,
                )
                if passage_row is None:
                    raise RuntimeError("Claimed vector has no passage")
                claims.append(
                    PassageVectorClaim(
                        passage=_map_passage(passage_row),
                        space=space,
                        claim_token=token,
                        attempt_count=_integer(_row_values(row, 1)[0]),
                    )
                )
        return tuple(claims)

    async def complete_vector(
        self,
        claim: PassageVectorClaim,
        vector: EmbeddingVector,
        *,
        completed_at: datetime,
    ) -> None:
        """@brief fenced 保存完整向量 / Persist a complete vector with fencing.

        @return None / None.
        @raise StaleVectorClaimError claim 过期 / Stale claim.
        """

        vector.require_space(claim.space)
        timestamp = ensure_utc(completed_at)
        rowcount = await db_connection.execute(
            "UPDATE retrieval.passage_vectors SET status = 'completed', "
            "version = version + 1, embedding = CAST(%s AS vector), "
            "claim_token = NULL, lease_expires_at = NULL, completed_at = %s, "
            "updated_at = %s, last_error = NULL WHERE passage_id = CAST(%s AS UUID) "
            "AND space_id = %s AND status = 'processing' "
            "AND claim_token = CAST(%s AS UUID)",
            (
                _encode_vector(vector),
                timestamp,
                timestamp,
                str(claim.passage.passage_id),
                claim.space.space_id,
                str(claim.claim_token),
            ),
        )
        if rowcount != 1:
            raise StaleVectorClaimError(
                f"Stale vector claim {claim.passage.passage_id}"
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

        failure_time = ensure_utc(failed_at)
        retry_time = ensure_utc(retry_at)
        if retry_time <= failure_time:
            raise ValueError("Vector retry_at must follow failed_at")
        await self._finish_failure(
            claim,
            status="retry_wait",
            failed_at=failure_time,
            next_attempt_at=retry_time,
            error=error,
        )

    async def fail_vector(
        self,
        claim: PassageVectorClaim,
        *,
        error: str,
        failed_at: datetime,
    ) -> None:
        """@brief fenced 终结 vector job / Finally fail a vector job with fencing."""

        await self._finish_failure(
            claim,
            status="failed_final",
            failed_at=ensure_utc(failed_at),
            next_attempt_at=None,
            error=error,
        )

    async def _finish_failure(
        self,
        claim: PassageVectorClaim,
        *,
        status: str,
        failed_at: datetime,
        next_attempt_at: datetime | None,
        error: str,
    ) -> None:
        """@brief 写入一种 fenced failure transition / Persist one fenced failure transition."""

        message = error.strip()[:1_000]
        if not message:
            raise ValueError("Vector failure error cannot be blank")
        rowcount = await db_connection.execute(
            "UPDATE retrieval.passage_vectors SET status = %s, version = version + 1, "
            "next_attempt_at = %s, claim_token = NULL, lease_expires_at = NULL, "
            "last_error = %s, updated_at = %s WHERE passage_id = CAST(%s AS UUID) "
            "AND space_id = %s AND status = 'processing' "
            "AND claim_token = CAST(%s AS UUID)",
            (
                status,
                next_attempt_at,
                message,
                failed_at,
                str(claim.passage.passage_id),
                claim.space.space_id,
                str(claim.claim_token),
            ),
        )
        if rowcount != 1:
            raise StaleVectorClaimError(
                f"Stale vector claim {claim.passage.passage_id}"
            )

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
        return await db_connection.execute(
            "UPDATE retrieval.passage_vectors SET status = 'retry_wait', "
            "version = version + 1, next_attempt_at = %s, claim_token = NULL, "
            "lease_expires_at = NULL, updated_at = %s, "
            "last_error = 'recovered expired embedding lease' "
            "WHERE space_id = %s AND status = 'processing' AND lease_expires_at <= %s",
            (timestamp, timestamp, space.space_id, timestamp),
        )

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
        rows = await db_connection.fetch_all(
            "SELECT "
            + ", ".join(
                f"passage.{column.strip()}" for column in _PASSAGE_COLUMNS.split(",")
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
        return tuple(
            RetrievalEvidence(
                passage=_map_passage(_row_values(row, 12)[:11]),
                cosine_distance=_float(_row_values(row, 12)[11]),
            )
            for row in rows
        )


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


def _datetime(value: object) -> datetime:
    """@brief 转换 datetime / Convert a datetime."""

    if not isinstance(value, datetime):
        raise TypeError("Expected retrieval datetime")
    return value


__all__ = ["PostgresEpisodicSource", "PostgresRetrievalStore"]
