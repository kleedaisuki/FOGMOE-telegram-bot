"""@brief PostgreSQL User Profile durable 状态机 / PostgreSQL durable state machine for User Profile."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.user_profile.ports import (
    DreamClaim,
    DreamResult,
    StaleDreamClaimError,
)
from fogmoe_bot.domain.temporal import ensure_utc
from fogmoe_bot.domain.user_profile.models import (
    DreamId,
    ProfileDocument,
    ProfileEvidence,
    UserProfileSnapshot,
)
from fogmoe_bot.infrastructure.database import db

from .locking import lock_user_profile
from .mapping import (
    _EVIDENCE_COLUMNS,
    _document_json,
    _dream_identity,
    _evidence_digest,
    _integer,
    _json_object,
    _map_document,
    _map_evidence,
    _map_metadata,
    _map_snapshot,
    _metadata_json,
    _patch_json,
    _stored_evidence_semantics,
    _uuid,
    _values,
)


class PostgresUserProfileStore:
    """@brief Profile evidence log、durable Dream queue 与 revision store / Profile evidence log, durable Dream queue, and revision store."""

    async def read_profile(
        self,
        user_id: int,
    ) -> UserProfileSnapshot | None:
        """@brief 读取当前 Profile revision / Read the current Profile revision.

        @param user_id Profile owner / Profile owner.
        @return 当前 snapshot 或 None / Current snapshot or None.
        """

        if isinstance(user_id, bool) or user_id <= 0:
            raise ValueError("Profile user_id must be positive")
        row = await db.fetch_one(
            "SELECT profile.user_id, revision.revision, revision.document, "
            "revision.observed_through_event_id, profile.created_at, "
            "revision.created_at, revision.route_key, revision.prompt_version "
            "FROM user_profile.profiles AS profile "
            "JOIN user_profile.profile_revisions AS revision "
            "ON revision.user_id = profile.user_id "
            "AND revision.revision = profile.current_revision "
            "WHERE profile.user_id = %s",
            (user_id,),
        )
        return _map_snapshot(row) if row is not None else None

    async def read_profile_in_transaction(
        self,
        user_id: int,
        *,
        connection: AsyncConnection,
    ) -> UserProfileSnapshot | None:
        """@brief 在 acceptance transaction 内读取 snapshot / Read a snapshot inside an acceptance transaction.

        @param user_id Profile owner / Profile owner.
        @param connection acceptance transaction / Acceptance transaction.
        @return 当前 snapshot 或 None / Current snapshot or None.
        """

        row = await db.fetch_one(
            "SELECT profile.user_id, revision.revision, revision.document, "
            "revision.observed_through_event_id, profile.created_at, "
            "revision.created_at, revision.route_key, revision.prompt_version "
            "FROM user_profile.profiles AS profile "
            "JOIN user_profile.profile_revisions AS revision "
            "ON revision.user_id = profile.user_id "
            "AND revision.revision = profile.current_revision "
            "WHERE profile.user_id = %s",
            (user_id,),
            connection=connection,
        )
        return _map_snapshot(row) if row is not None else None

    async def project_evidence(
        self,
        evidence: ProfileEvidence,
        *,
        projected_at: datetime,
    ) -> None:
        """@brief 幂等写入 evidence 并 materialize Profile 调度行 / Idempotently write evidence and materialize the Profile scheduling row.

        @param evidence event_id=0 的来源证据 / Source evidence with event_id zero.
        @param projected_at 投影时间 / Projection time.
        @return None / None.
        @raise RuntimeError 同 Turn 语义漂移 / Semantic drift under the same Turn.
        """

        if evidence.event_id != 0:
            raise ValueError("Source Profile evidence must use event_id zero")
        timestamp = ensure_utc(projected_at)
        metadata = _metadata_json(evidence.metadata)
        digest = _evidence_digest(evidence)
        async with db.transaction() as connection:
            await lock_user_profile(connection, evidence.owner_user_id)
            boundary = await db.fetch_one(
                "SELECT forgotten_through FROM user_profile.profiles "
                "WHERE user_id = %s",
                (evidence.owner_user_id,),
                connection=connection,
            )
            if boundary is not None and boundary[0] is not None:
                forgotten_through = boundary[0]
                if not isinstance(forgotten_through, datetime):
                    raise TypeError("Profile forgetting boundary must be a datetime")
                if evidence.occurred_at <= ensure_utc(forgotten_through):
                    return
            await db.execute(
                "INSERT INTO user_profile.evidence_events "
                "(source_turn_id, owner_user_id, user_text, assistant_text, occurred_at, "
                "metadata, source_digest, projected_at) "
                "VALUES (CAST(%s AS UUID), %s, %s, %s, %s, CAST(%s AS JSONB), %s, %s) "
                "ON CONFLICT (source_turn_id) DO NOTHING",
                (
                    str(evidence.source_turn_id),
                    evidence.owner_user_id,
                    evidence.user_text,
                    evidence.assistant_text,
                    evidence.occurred_at,
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    digest,
                    timestamp,
                ),
                connection=connection,
            )
            row = await db.fetch_one(
                "SELECT owner_user_id, user_text, assistant_text, occurred_at, metadata, "
                "source_digest FROM user_profile.evidence_events "
                "WHERE source_turn_id = CAST(%s AS UUID)",
                (str(evidence.source_turn_id),),
                connection=connection,
            )
            if row is None or _stored_evidence_semantics(row) != (
                evidence.owner_user_id,
                evidence.user_text,
                evidence.assistant_text,
                evidence.occurred_at,
                metadata,
                digest,
            ):
                raise RuntimeError(
                    f"Profile evidence projection drifted for Turn {evidence.source_turn_id}"
                )
            await db.execute(
                "INSERT INTO user_profile.profiles "
                "(user_id, current_revision, observed_through_event_id, next_eligible_at, "
                "forgotten_through, created_at, updated_at) "
                "VALUES (%s, NULL, 0, %s, NULL, %s, %s) "
                "ON CONFLICT (user_id) DO UPDATE SET next_eligible_at = "
                "LEAST(COALESCE(user_profile.profiles.next_eligible_at, EXCLUDED.next_eligible_at), "
                "EXCLUDED.next_eligible_at) WHERE user_profile.profiles.current_revision IS NULL",
                (evidence.owner_user_id, timestamp, timestamp, timestamp),
                connection=connection,
            )

    async def enqueue_eligible(
        self,
        *,
        now: datetime,
        limit: int,
        max_events_per_dream: int,
        max_evidence_chars: int,
    ) -> int:
        """@brief 为到期 Profile 建立精确 source set 的 durable jobs / Enqueue durable jobs with exact source sets for due Profiles.

        @return 新建 job 数 / Number of inserted jobs.
        """

        timestamp = ensure_utc(now)
        if not 1 <= limit <= 64:
            raise ValueError("Dream enqueue limit must be between 1 and 64")
        if not 1 <= max_events_per_dream <= 256:
            raise ValueError("Dream event limit must be between 1 and 256")
        if not 4_096 <= max_evidence_chars <= 1_000_000:
            raise ValueError("Dream evidence character budget is invalid")
        inserted = 0
        async with db.transaction() as connection:
            candidates = await db.fetch_all(
                "SELECT profile.user_id FROM user_profile.profiles AS profile "
                "WHERE profile.next_eligible_at <= %s "
                "AND EXISTS (SELECT 1 FROM user_profile.evidence_events AS evidence "
                "WHERE evidence.owner_user_id = profile.user_id "
                "AND evidence.event_id > profile.observed_through_event_id) "
                "AND NOT EXISTS (SELECT 1 FROM user_profile.dreams AS dream "
                "WHERE dream.user_id = profile.user_id "
                "AND dream.status IN ('pending','retry_wait','processing','failed_final')) "
                "ORDER BY profile.next_eligible_at, profile.user_id "
                "FOR UPDATE OF profile SKIP LOCKED LIMIT %s",
                (timestamp, limit),
                connection=connection,
            )
            for candidate in candidates:
                user_id = _integer(_values(candidate, 1)[0])
                inserted += await self._enqueue_user(
                    user_id,
                    now=timestamp,
                    max_events=max_events_per_dream,
                    max_evidence_chars=max_evidence_chars,
                    connection=connection,
                )
        return inserted

    async def _enqueue_user(
        self,
        user_id: int,
        *,
        now: datetime,
        max_events: int,
        max_evidence_chars: int,
        connection: AsyncConnection,
    ) -> int:
        """@brief 在已锁 Profile 行上形成一个 job / Form one job while its Profile row is locked.

        @return 插入为 1，竞态收敛为 0 / One when inserted, zero when a race converged.
        """

        profile_row = await db.fetch_one(
            "SELECT COALESCE(current_revision, 0), observed_through_event_id "
            "FROM user_profile.profiles WHERE user_id = %s",
            (user_id,),
            connection=connection,
        )
        if profile_row is None:
            return 0
        base_revision, base_watermark = (
            _integer(value) for value in _values(profile_row, 2)
        )
        event_rows = await db.fetch_all(
            "SELECT event_id, metadata, char_length(user_text) + "
            "least(char_length(assistant_text), 4000) "
            "FROM user_profile.evidence_events "
            "WHERE owner_user_id = %s AND event_id > %s "
            "ORDER BY event_id LIMIT %s",
            (user_id, base_watermark, max_events),
            connection=connection,
        )
        if not event_rows:
            return 0
        selected_rows: list[object] = []
        selected_chars = 0
        for event_row in event_rows:
            values = _values(event_row, 3)
            event_chars = _integer(values[2])
            if selected_rows and selected_chars + event_chars > max_evidence_chars:
                break
            selected_rows.append(event_row)
            selected_chars += event_chars
        event_ids = tuple(_integer(_values(row, 3)[0]) for row in selected_rows)
        latest_metadata = _json_object(_values(selected_rows[-1], 3)[1])
        through_event_id = event_ids[-1]
        dream_id = _dream_identity(
            user_id, base_revision, base_watermark, through_event_id
        )
        row = await db.fetch_one(
            "INSERT INTO user_profile.dreams "
            "(dream_id, user_id, base_revision, base_observed_through_event_id, "
            "through_event_id, source_count, metadata, status, version, attempt_count, "
            "next_attempt_at, created_at, updated_at) "
            "VALUES (CAST(%s AS UUID), %s, %s, %s, %s, %s, CAST(%s AS JSONB), "
            "'pending', 0, 0, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING dream_id",
            (
                str(dream_id),
                user_id,
                base_revision,
                base_watermark,
                through_event_id,
                len(event_ids),
                json.dumps(latest_metadata, ensure_ascii=False, sort_keys=True),
                now,
                now,
                now,
            ),
            connection=connection,
        )
        if row is None:
            return 0
        for ordinal, event_id in enumerate(event_ids):
            await db.execute(
                "INSERT INTO user_profile.dream_sources (dream_id, ordinal, event_id) "
                "VALUES (CAST(%s AS UUID), %s, %s)",
                (str(dream_id), ordinal, event_id),
                connection=connection,
            )
        await db.execute(
            "UPDATE user_profile.profiles SET next_eligible_at = NULL, updated_at = %s "
            "WHERE user_id = %s",
            (now, user_id),
            connection=connection,
        )
        return 1

    async def claim_dreams(
        self,
        *,
        now: datetime,
        limit: int,
        lease_for: timedelta,
    ) -> tuple[DreamClaim, ...]:
        """@brief 使用 SKIP LOCKED 领取并冻结 jobs / Claim and freeze jobs using SKIP LOCKED.

        @return claims / Claims.
        """

        timestamp = ensure_utc(now)
        if not 1 <= limit <= 64 or lease_for <= timedelta():
            raise ValueError("Dream claim bounds are invalid")
        claims: list[DreamClaim] = []
        async with db.transaction() as connection:
            candidates = await db.fetch_all(
                "SELECT dream_id FROM user_profile.dreams "
                "WHERE status IN ('pending','retry_wait') AND next_attempt_at <= %s "
                "ORDER BY next_attempt_at, dream_id FOR UPDATE SKIP LOCKED LIMIT %s",
                (timestamp, limit),
                connection=connection,
            )
            for candidate in candidates:
                dream_id = _uuid(_values(candidate, 1)[0])
                token = uuid4()
                row = await db.fetch_one(
                    "UPDATE user_profile.dreams SET status = 'processing', "
                    "version = version + 1, attempt_count = attempt_count + 1, "
                    "next_attempt_at = NULL, claim_token = CAST(%s AS UUID), "
                    "lease_expires_at = %s, last_error = NULL, updated_at = %s "
                    "WHERE dream_id = CAST(%s AS UUID) "
                    "AND status IN ('pending','retry_wait') RETURNING "
                    "user_id, base_revision, base_observed_through_event_id, "
                    "through_event_id, metadata, attempt_count",
                    (
                        str(token),
                        timestamp + lease_for,
                        timestamp,
                        str(dream_id),
                    ),
                    connection=connection,
                )
                if row is None:
                    raise RuntimeError("Locked Dream candidate was not claimable")
                claims.append(
                    await self._load_claim(
                        DreamId(dream_id),
                        token,
                        row,
                        connection=connection,
                    )
                )
        return tuple(claims)

    async def _load_claim(
        self,
        dream_id: DreamId,
        token: UUID,
        row: object,
        *,
        connection: AsyncConnection,
    ) -> DreamClaim:
        """@brief 从 processing 行加载冻结 Profile 与 evidence / Load the frozen Profile and evidence from a processing row."""

        values = _values(row, 6)
        user_id = _integer(values[0])
        base_revision = _integer(values[1])
        base_watermark = _integer(values[2])
        through_event_id = _integer(values[3])
        metadata = _map_metadata(values[4])
        attempt_count = _integer(values[5])
        document = ProfileDocument()
        if base_revision > 0:
            revision_row = await db.fetch_one(
                "SELECT document FROM user_profile.profile_revisions "
                "WHERE user_id = %s AND revision = %s",
                (user_id, base_revision),
                connection=connection,
            )
            if revision_row is None:
                raise RuntimeError("Dream base Profile revision does not exist")
            document = _map_document(_values(revision_row, 1)[0])
        evidence_rows = await db.fetch_all(
            "SELECT "
            + _EVIDENCE_COLUMNS
            + " FROM user_profile.dream_sources AS source "
            "JOIN user_profile.evidence_events AS evidence USING (event_id) "
            "WHERE source.dream_id = CAST(%s AS UUID) ORDER BY source.ordinal",
            (str(dream_id),),
            connection=connection,
        )
        evidence = tuple(_map_evidence(item) for item in evidence_rows)
        return DreamClaim(
            dream_id=dream_id,
            owner_user_id=user_id,
            base_revision=base_revision,
            base_observed_through_event_id=base_watermark,
            through_event_id=through_event_id,
            current_document=document,
            evidence=evidence,
            metadata=metadata,
            claim_token=token,
            attempt_count=attempt_count,
        )

    async def complete_dream(
        self,
        claim: DreamClaim,
        result: DreamResult,
        *,
        document: ProfileDocument,
        completed_at: datetime,
        refresh_after: timedelta,
    ) -> UserProfileSnapshot | None:
        """@brief CAS/Fencing 提交 Profile revision 与 watermark / Commit a Profile revision and watermark using CAS and fencing.

        @return 新 revision；文档未变为 None / New revision, or None when the document did not change.
        """

        timestamp = ensure_utc(completed_at)
        if refresh_after <= timedelta():
            raise ValueError("Profile refresh_after must be positive")
        patch_json = _patch_json(result)
        async with db.transaction() as connection:
            await lock_user_profile(connection, claim.owner_user_id)
            await self._lock_claim(claim, connection=connection)
            profile_row = await db.fetch_one(
                "SELECT COALESCE(current_revision, 0), observed_through_event_id, created_at "
                "FROM user_profile.profiles WHERE user_id = %s FOR UPDATE",
                (claim.owner_user_id,),
                connection=connection,
            )
            if profile_row is None:
                raise StaleDreamClaimError("Dream Profile row no longer exists")
            profile_values = _values(profile_row, 3)
            if (
                _integer(profile_values[0]) != claim.base_revision
                or _integer(profile_values[1]) != claim.base_observed_through_event_id
            ):
                raise StaleDreamClaimError("Dream base Profile revision was superseded")
            changed = document != claim.current_document
            snapshot: UserProfileSnapshot | None = None
            next_revision = claim.base_revision
            if changed:
                next_revision += 1
                await db.execute(
                    "INSERT INTO user_profile.profile_revisions "
                    "(user_id, revision, document, observed_through_event_id, route_key, "
                    "prompt_version, created_at) VALUES (%s, %s, CAST(%s AS JSONB), %s, %s, %s, %s)",
                    (
                        claim.owner_user_id,
                        next_revision,
                        json.dumps(
                            _document_json(document), ensure_ascii=False, sort_keys=True
                        ),
                        claim.through_event_id,
                        result.route_key,
                        result.prompt_version,
                        timestamp,
                    ),
                    connection=connection,
                )
            more_row = await db.fetch_one(
                "SELECT 1 FROM user_profile.evidence_events "
                "WHERE owner_user_id = %s AND event_id > %s LIMIT 1",
                (claim.owner_user_id, claim.through_event_id),
                connection=connection,
            )
            next_eligible_at = (
                timestamp if more_row is not None else timestamp + refresh_after
            )
            await db.execute(
                "UPDATE user_profile.profiles SET current_revision = %s, "
                "observed_through_event_id = %s, next_eligible_at = %s, updated_at = %s "
                "WHERE user_id = %s",
                (
                    next_revision if next_revision > 0 else None,
                    claim.through_event_id,
                    next_eligible_at,
                    timestamp,
                    claim.owner_user_id,
                ),
                connection=connection,
            )
            row = await db.fetch_one(
                "UPDATE user_profile.dreams SET status = 'completed', claim_token = NULL, "
                "lease_expires_at = NULL, result_patch = CAST(%s AS JSONB), route_key = %s, "
                "completed_at = %s, updated_at = %s WHERE dream_id = CAST(%s AS UUID) "
                "AND status = 'processing' AND claim_token = CAST(%s AS UUID) RETURNING dream_id",
                (
                    json.dumps(patch_json, ensure_ascii=False, sort_keys=True),
                    result.route_key,
                    timestamp,
                    timestamp,
                    str(claim.dream_id),
                    str(claim.claim_token),
                ),
                connection=connection,
            )
            if row is None:
                raise StaleDreamClaimError("Dream completion lost its fencing token")
            if changed:
                snapshot = UserProfileSnapshot(
                    user_id=claim.owner_user_id,
                    revision=next_revision,
                    document=document,
                    observed_through_event_id=claim.through_event_id,
                    created_at=cast(datetime, profile_values[2]),
                    updated_at=timestamp,
                    route_key=result.route_key,
                    prompt_version=result.prompt_version,
                )
            return snapshot

    async def retry_dream(
        self,
        claim: DreamClaim,
        *,
        failed_at: datetime,
        retry_at: datetime,
        error: str,
    ) -> None:
        """@brief fenced 安排 retry / Schedule a fenced retry."""

        failure_time = ensure_utc(failed_at)
        retry_time = ensure_utc(retry_at)
        if retry_time <= failure_time:
            raise ValueError("Dream retry_at must follow failed_at")
        row = await db.fetch_one(
            "UPDATE user_profile.dreams SET status = 'retry_wait', next_attempt_at = %s, "
            "claim_token = NULL, lease_expires_at = NULL, last_error = %s, updated_at = %s "
            "WHERE dream_id = CAST(%s AS UUID) AND status = 'processing' "
            "AND claim_token = CAST(%s AS UUID) RETURNING dream_id",
            (
                retry_time,
                error[:1000],
                failure_time,
                str(claim.dream_id),
                str(claim.claim_token),
            ),
        )
        if row is None:
            raise StaleDreamClaimError("Dream retry lost its fencing token")

    async def fail_dream(
        self,
        claim: DreamClaim,
        *,
        failed_at: datetime,
        error: str,
    ) -> None:
        """@brief fenced 终结 job / Finally fail a job."""

        timestamp = ensure_utc(failed_at)
        row = await db.fetch_one(
            "UPDATE user_profile.dreams SET status = 'failed_final', next_attempt_at = NULL, "
            "claim_token = NULL, lease_expires_at = NULL, last_error = %s, "
            "completed_at = %s, updated_at = %s WHERE dream_id = CAST(%s AS UUID) "
            "AND status = 'processing' AND claim_token = CAST(%s AS UUID) RETURNING dream_id",
            (
                error[:1000],
                timestamp,
                timestamp,
                str(claim.dream_id),
                str(claim.claim_token),
            ),
        )
        if row is None:
            raise StaleDreamClaimError("Dream final failure lost its fencing token")

    async def recover_expired_dream_leases(self, *, now: datetime) -> int:
        """@brief 回收过期 lease / Recover expired leases.

        @return 回收行数 / Recovered row count.
        """

        timestamp = ensure_utc(now)
        return await db.execute(
            "UPDATE user_profile.dreams SET status = 'retry_wait', next_attempt_at = %s, "
            "claim_token = NULL, lease_expires_at = NULL, "
            "last_error = COALESCE(last_error, 'recovered expired Dream lease'), "
            "updated_at = %s WHERE status = 'processing' AND lease_expires_at <= %s",
            (timestamp, timestamp, timestamp),
        )

    @staticmethod
    async def _lock_claim(
        claim: DreamClaim,
        *,
        connection: AsyncConnection,
    ) -> None:
        """@brief 锁定并验证 processing/fencing 状态 / Lock and validate processing/fencing state."""

        row = await db.fetch_one(
            "SELECT status, claim_token FROM user_profile.dreams "
            "WHERE dream_id = CAST(%s AS UUID) FOR UPDATE",
            (str(claim.dream_id),),
            connection=connection,
        )
        if row is None:
            raise StaleDreamClaimError("Dream no longer exists")
        status, token = _values(row, 2)
        if str(status) != "processing" or _uuid(token) != claim.claim_token:
            raise StaleDreamClaimError("Dream claim is stale")
