"""@brief PostgreSQL User Profile durable 状态机 / PostgreSQL durable state machine for User Profile."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import cast
from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.user_profile.ports import (
    DreamCommitReceipt,
    DreamProfileUnchanged,
    DreamProfileUpdated,
)
from fogmoe_bot.domain.user_profile import (
    DreamActivity,
    DreamActivityDraft,
    DreamClaim,
    DreamCompletionPrepared,
    DreamFailedFinalDecision,
    DreamFailure,
    DreamLease,
    DreamLeaseToken,
    DreamRetryScheduled,
    ProfileBaseline,
    StaleDreamClaimError,
    DreamId,
    ProfileDocument,
    ProfileEvidence,
    UserProfileSnapshot,
)
from fogmoe_bot.domain.temporal import ensure_utc
from fogmoe_bot.infrastructure.database import db

from .locking import lock_user_profile
from .mapping import (
    _DREAM_ACTIVITY_COLUMNS,
    _EVIDENCE_COLUMNS,
    _document_json,
    _dream_identity,
    _evidence_digest,
    _integer,
    _json_object,
    _map_document,
    _map_dream_activity,
    _map_evidence,
    _map_metadata,
    _map_snapshot,
    _metadata_json,
    _patch_json,
    _stored_evidence_semantics,
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
        @note snapshot watermark 是当前 immutable revision 的 provenance，不是可由
            NO_OP Dream 独立推进的 scheduler head cursor。/ The snapshot watermark is
            provenance for the current immutable revision, not the scheduler head cursor that
            a NO_OP Dream may advance independently.
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
        @note snapshot watermark 是 current revision provenance / The snapshot watermark is
            provenance for the current revision.
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

        @param now 调度时间 / Scheduling time.
        @param limit 单轮最大 job 数 / Maximum jobs per pass.
        @param max_events_per_dream 单个 Dream 最大 evidence 数 / Maximum evidence items per Dream.
        @param max_evidence_chars 单个 Dream 最大文本字符数 / Maximum text characters per Dream.
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

        @param user_id Profile owner / Profile owner.
        @param now enqueue 时间 / Enqueue time.
        @param max_events 单个 Dream 最大 evidence 数 / Maximum evidence items per Dream.
        @param max_evidence_chars 单个 Dream 最大文本字符数 / Maximum text characters per Dream.
        @param connection 持有 Profile 行锁的事务 / Transaction holding the Profile row lock.
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
        pending = DreamActivity.enqueue(
            DreamActivityDraft(
                dream_id=dream_id,
                owner_user_id=user_id,
                baseline=ProfileBaseline(
                    revision=base_revision,
                    observed_through_event_id=base_watermark,
                ),
                through_event_id=through_event_id,
                source_count=len(event_ids),
                metadata=_map_metadata(latest_metadata),
                created_at=now,
            )
        )
        row = await db.fetch_one(
            "INSERT INTO user_profile.dreams "
            "(dream_id, user_id, base_revision, base_observed_through_event_id, "
            "through_event_id, source_count, metadata, status, version, attempt_count, "
            "next_attempt_at, result_patch, route_key, last_error, created_at, updated_at, "
            "completed_at, claim_token, lease_expires_at) "
            "VALUES (CAST(%s AS UUID), %s, %s, %s, %s, %s, CAST(%s AS JSONB), "
            "%s, %s, %s, %s, NULL, NULL, NULL, %s, %s, NULL, NULL, NULL) "
            "ON CONFLICT DO NOTHING RETURNING " + _DREAM_ACTIVITY_COLUMNS,
            (
                str(pending.dream_id),
                pending.owner_user_id,
                pending.baseline.revision,
                pending.baseline.observed_through_event_id,
                pending.through_event_id,
                pending.source_count,
                json.dumps(latest_metadata, ensure_ascii=False, sort_keys=True),
                pending.status.value,
                pending.version,
                pending.attempt_count,
                pending.next_attempt_at,
                pending.created_at,
                pending.updated_at,
            ),
            connection=connection,
        )
        if row is None:
            return 0
        if _map_dream_activity(row) != pending:
            raise RuntimeError("Dream enqueue SQL diverged from the domain transition")
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

        @param now 领取时间 / Claim time.
        @param limit 单轮最大领取数 / Maximum claims per pass.
        @param lease_for ownership 租约长度 / Ownership lease duration.
        @return claims / Claims.
        """

        timestamp = ensure_utc(now)
        if not 1 <= limit <= 64 or lease_for <= timedelta():
            raise ValueError("Dream claim bounds are invalid")
        claims: list[DreamClaim] = []
        async with db.transaction() as connection:
            candidates = await db.fetch_all(
                "SELECT " + _DREAM_ACTIVITY_COLUMNS + " FROM user_profile.dreams "
                "WHERE status IN ('pending','retry_wait') AND next_attempt_at <= %s "
                "ORDER BY next_attempt_at, dream_id FOR UPDATE SKIP LOCKED LIMIT %s",
                (timestamp, limit),
                connection=connection,
            )
            for candidate in candidates:
                previous = _map_dream_activity(candidate)
                current_document, evidence = await self._load_dream_workload(
                    previous,
                    connection=connection,
                )
                token = DreamLeaseToken.new()
                claim = previous.claim(
                    token=token,
                    claimed_at=timestamp,
                    lease_expires_at=timestamp + lease_for,
                    current_document=current_document,
                    evidence=evidence,
                )
                target = claim.activity
                row = await db.fetch_one(
                    "UPDATE user_profile.dreams SET status = %s, version = %s, "
                    "attempt_count = %s, next_attempt_at = %s, "
                    "claim_token = CAST(%s AS UUID), lease_expires_at = %s, "
                    "result_patch = NULL, route_key = NULL, last_error = %s, "
                    "completed_at = NULL, updated_at = %s "
                    "WHERE dream_id = CAST(%s AS UUID) AND status = %s "
                    "AND version = %s AND attempt_count = %s "
                    "AND next_attempt_at IS NOT DISTINCT FROM %s AND updated_at = %s "
                    "AND claim_token IS NULL AND lease_expires_at IS NULL RETURNING "
                    + _DREAM_ACTIVITY_COLUMNS,
                    (
                        target.status.value,
                        target.version,
                        target.attempt_count,
                        target.next_attempt_at,
                        str(token),
                        claim.lease_expires_at,
                        target.last_error,
                        target.updated_at,
                        str(previous.dream_id),
                        previous.status.value,
                        previous.version,
                        previous.attempt_count,
                        previous.next_attempt_at,
                        previous.updated_at,
                    ),
                    connection=connection,
                )
                if row is None:
                    raise RuntimeError("Locked Dream candidate was not claimable")
                if _map_dream_activity(row) != target:
                    raise RuntimeError(
                        "Dream claim SQL diverged from the domain transition"
                    )
                claims.append(claim)
        return tuple(claims)

    async def _load_dream_workload(
        self,
        activity: DreamActivity,
        *,
        connection: AsyncConnection,
    ) -> tuple[ProfileDocument, tuple[ProfileEvidence, ...]]:
        """@brief 加载聚合冻结的 Profile 与 evidence / Load the Profile and evidence frozen by an aggregate.

        @param activity 已锁 Dream 聚合 / Locked Dream aggregate.
        @param connection 当前 claim 或 settlement transaction / Current claim or settlement transaction.
        @return 当前文档与有序 evidence / Current document and ordered evidence.
        """

        document = ProfileDocument()
        if activity.baseline.revision > 0:
            revision_row = await db.fetch_one(
                "SELECT document FROM user_profile.profile_revisions "
                "WHERE user_id = %s AND revision = %s",
                (activity.owner_user_id, activity.baseline.revision),
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
            (str(activity.dream_id),),
            connection=connection,
        )
        evidence = tuple(_map_evidence(item) for item in evidence_rows)
        return document, evidence

    async def complete_dream(
        self,
        decision: DreamCompletionPrepared,
        *,
        refresh_after: timedelta,
    ) -> DreamCommitReceipt:
        """@brief 双重 Profile CAS 与 Dream fencing 下提交 completion / Commit completion under dual Profile CAS and Dream fencing.

        @param decision 已验证 patch、时间与 lifecycle 的领域决定 / Domain decision with validated patch, time, and lifecycle.
        @param refresh_after 无 backlog 时的下次 refresh 延迟 / Refresh delay when no backlog remains.
        @return 显式 updated 或 NO_OP durable 回执 / Explicit updated or NO_OP durable receipt.
        @raise StaleDreamClaimError capability、冻结 workload 或 Profile baseline 已失效 /
            Capability, frozen workload, or Profile baseline is stale.
        @raise RuntimeError SQL 后态偏离 canonical 领域决定 / SQL post-state diverged from the canonical domain decision.
        """

        claim = decision.claim
        target = decision.activity
        result = target.result
        timestamp = target.completed_at
        if result is None or timestamp is None:  # pragma: no cover - closed decision.
            raise AssertionError("Dream completion lost result metadata")
        if refresh_after <= timedelta():
            raise ValueError("Profile refresh_after must be positive")
        patch_json = _patch_json(result)
        owner_user_id = claim.activity.owner_user_id
        baseline = claim.activity.baseline
        through_event_id = claim.activity.through_event_id
        next_revision = baseline.revision + int(decision.changed)

        async with db.transaction() as connection:
            await lock_user_profile(connection, owner_user_id)
            current = await self._load_dream_for_update(
                claim.activity.dream_id,
                connection=connection,
            )
            self._require_current_claim(current, claim)
            canonical_document, canonical_evidence = await self._load_dream_workload(
                current,
                connection=connection,
            )
            if (
                canonical_document != claim.current_document
                or canonical_evidence != claim.evidence
            ):
                raise StaleDreamClaimError(
                    "Dream claim workload no longer matches durable sources"
                )
            canonical_decision = claim.prepare_completion(
                result,
                completed_at=timestamp,
            )
            if canonical_decision != decision:
                raise StaleDreamClaimError(
                    "Dream completion does not match its canonical claim evaluation"
                )
            decision = canonical_decision
            target = decision.activity
            profile_row = await db.fetch_one(
                "SELECT COALESCE(current_revision, 0), observed_through_event_id, created_at "
                "FROM user_profile.profiles WHERE user_id = %s FOR UPDATE",
                (owner_user_id,),
                connection=connection,
            )
            if profile_row is None:
                raise StaleDreamClaimError("Dream Profile row no longer exists")
            profile_values = _values(profile_row, 3)
            if (
                _integer(profile_values[0]) != baseline.revision
                or _integer(profile_values[1]) != baseline.observed_through_event_id
            ):
                raise StaleDreamClaimError("Dream Profile baseline was superseded")
            profile_created_at = ensure_utc(cast(datetime, profile_values[2]))

            revision_row: object | None = None
            if decision.changed:
                revision_row = await db.fetch_one(
                    "INSERT INTO user_profile.profile_revisions "
                    "(user_id, revision, document, observed_through_event_id, route_key, "
                    "prompt_version, created_at) VALUES (%s, %s, CAST(%s AS JSONB), %s, %s, %s, %s) "
                    "RETURNING user_id, revision, document, observed_through_event_id, "
                    "created_at, route_key, prompt_version",
                    (
                        owner_user_id,
                        next_revision,
                        json.dumps(
                            _document_json(decision.document),
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        through_event_id,
                        result.route_key,
                        result.prompt_version,
                        timestamp,
                    ),
                    connection=connection,
                )
                if revision_row is None:  # pragma: no cover - INSERT always returns.
                    raise RuntimeError("Dream revision insert returned no post-state")

            more_row = await db.fetch_one(
                "SELECT 1 FROM user_profile.evidence_events "
                "WHERE owner_user_id = %s AND event_id > %s LIMIT 1",
                (owner_user_id, through_event_id),
                connection=connection,
            )
            completion = decision.plan_profile_commit(
                has_backlog=more_row is not None,
                refresh_after=refresh_after,
            )
            if completion.profile_revision != next_revision:  # pragma: no cover
                raise AssertionError("Dream completion revision plan diverged")
            profile_post_row = await db.fetch_one(
                "UPDATE user_profile.profiles SET current_revision = %s, "
                "observed_through_event_id = %s, next_eligible_at = %s, updated_at = %s "
                "WHERE user_id = %s AND COALESCE(current_revision, 0) = %s "
                "AND observed_through_event_id = %s RETURNING user_id, "
                "COALESCE(current_revision, 0), observed_through_event_id, "
                "next_eligible_at, created_at, updated_at",
                (
                    completion.profile_revision
                    if completion.profile_revision > 0
                    else None,
                    completion.observed_through_event_id,
                    completion.next_eligible_at,
                    timestamp,
                    owner_user_id,
                    baseline.revision,
                    baseline.observed_through_event_id,
                ),
                connection=connection,
            )
            if profile_post_row is None:
                raise StaleDreamClaimError("Dream Profile baseline CAS was lost")
            profile_post_values = _values(profile_post_row, 6)
            persisted_profile_post_state = (
                _integer(profile_post_values[0]),
                _integer(profile_post_values[1]),
                _integer(profile_post_values[2]),
                ensure_utc(cast(datetime, profile_post_values[3])),
                ensure_utc(cast(datetime, profile_post_values[4])),
                ensure_utc(cast(datetime, profile_post_values[5])),
            )
            expected_profile_post_state = (
                owner_user_id,
                completion.profile_revision,
                completion.observed_through_event_id,
                completion.next_eligible_at,
                profile_created_at,
                timestamp,
            )
            if persisted_profile_post_state != expected_profile_post_state:
                raise RuntimeError(
                    "Dream Profile UPDATE diverged from the domain commit plan"
                )

            persisted_snapshot: UserProfileSnapshot | None = None
            if revision_row is not None:
                revision_values = _values(revision_row, 7)
                persisted_snapshot = _map_snapshot(
                    (
                        revision_values[0],
                        revision_values[1],
                        revision_values[2],
                        revision_values[3],
                        profile_created_at,
                        revision_values[4],
                        revision_values[5],
                        revision_values[6],
                    )
                )
                expected_snapshot = completion.snapshot(
                    profile_created_at=profile_created_at,
                )
                if persisted_snapshot != expected_snapshot:
                    raise RuntimeError(
                        "Dream revision INSERT diverged from the domain commit plan"
                    )

            dream_row = await db.fetch_one(
                "UPDATE user_profile.dreams SET status = %s, version = %s, "
                "attempt_count = %s, next_attempt_at = NULL, claim_token = NULL, "
                "lease_expires_at = NULL, result_patch = CAST(%s AS JSONB), "
                "route_key = %s, last_error = NULL, completed_at = %s, updated_at = %s "
                "WHERE dream_id = CAST(%s AS UUID) AND status = 'processing' "
                "AND version = %s AND attempt_count = %s AND updated_at = %s "
                "AND claim_token = CAST(%s AS UUID) AND lease_expires_at = %s RETURNING "
                + _DREAM_ACTIVITY_COLUMNS,
                (
                    target.status.value,
                    target.version,
                    target.attempt_count,
                    json.dumps(patch_json, ensure_ascii=False, sort_keys=True),
                    result.route_key,
                    timestamp,
                    target.updated_at,
                    str(current.dream_id),
                    claim.expected_version,
                    claim.activity.attempt_count,
                    claim.activity.updated_at,
                    str(claim.token),
                    claim.lease_expires_at,
                ),
                connection=connection,
            )
            if dream_row is None:
                raise StaleDreamClaimError("Dream completion lost its fencing token")
            if _map_dream_activity(dream_row) != target:
                raise RuntimeError(
                    "Dream completion SQL diverged from the domain transition"
                )
            if persisted_snapshot is not None:
                return DreamProfileUpdated(persisted_snapshot)
            return DreamProfileUnchanged(
                owner_user_id=owner_user_id,
                retained_revision=completion.profile_revision,
                scheduler_head_event_id=completion.observed_through_event_id,
            )

    async def retry_dream(self, decision: DreamRetryScheduled) -> None:
        """@brief fenced 持久化领域 retry 决定 / Persist a domain retry decision under fencing.

        @param decision 已验证 retry settlement / Validated retry settlement.
        @return None / None.
        """

        claim = decision.claim
        target = decision.activity
        retry_at = target.next_attempt_at
        error = target.last_error
        if retry_at is None or error is None:  # pragma: no cover - closed decision.
            raise AssertionError("Dream retry lost its schedule or failure")
        async with db.transaction() as connection:
            current = await self._load_dream_for_update(
                claim.activity.dream_id,
                connection=connection,
            )
            self._require_current_claim(current, claim)
            row = await db.fetch_one(
                "UPDATE user_profile.dreams SET status = %s, version = %s, "
                "attempt_count = %s, next_attempt_at = %s, claim_token = NULL, "
                "lease_expires_at = NULL, result_patch = NULL, route_key = NULL, "
                "last_error = %s, completed_at = NULL, updated_at = %s "
                "WHERE dream_id = CAST(%s AS UUID) AND status = 'processing' "
                "AND version = %s AND attempt_count = %s AND updated_at = %s "
                "AND claim_token = CAST(%s AS UUID) AND lease_expires_at = %s RETURNING "
                + _DREAM_ACTIVITY_COLUMNS,
                (
                    target.status.value,
                    target.version,
                    target.attempt_count,
                    retry_at,
                    error,
                    target.updated_at,
                    str(current.dream_id),
                    claim.expected_version,
                    claim.activity.attempt_count,
                    claim.activity.updated_at,
                    str(claim.token),
                    claim.lease_expires_at,
                ),
                connection=connection,
            )
            if row is None:
                raise StaleDreamClaimError("Dream retry lost its fencing token")
            if _map_dream_activity(row) != target:
                raise RuntimeError(
                    "Dream retry SQL diverged from the domain transition"
                )

    async def fail_dream(self, decision: DreamFailedFinalDecision) -> None:
        """@brief fenced 持久化领域终败决定 / Persist a domain final-failure decision under fencing.

        @param decision 已验证 final-failure settlement / Validated final-failure settlement.
        @return None / None.
        """

        claim = decision.claim
        target = decision.activity
        error = target.last_error
        completed_at = target.completed_at
        if error is None or completed_at is None:  # pragma: no cover - closed decision.
            raise AssertionError("Dream final failure lost terminal metadata")
        async with db.transaction() as connection:
            current = await self._load_dream_for_update(
                claim.activity.dream_id,
                connection=connection,
            )
            self._require_current_claim(current, claim)
            row = await db.fetch_one(
                "UPDATE user_profile.dreams SET status = %s, version = %s, "
                "attempt_count = %s, next_attempt_at = NULL, claim_token = NULL, "
                "lease_expires_at = NULL, result_patch = NULL, route_key = NULL, "
                "last_error = %s, completed_at = %s, updated_at = %s "
                "WHERE dream_id = CAST(%s AS UUID) AND status = 'processing' "
                "AND version = %s AND attempt_count = %s AND updated_at = %s "
                "AND claim_token = CAST(%s AS UUID) AND lease_expires_at = %s RETURNING "
                + _DREAM_ACTIVITY_COLUMNS,
                (
                    target.status.value,
                    target.version,
                    target.attempt_count,
                    error,
                    completed_at,
                    target.updated_at,
                    str(current.dream_id),
                    claim.expected_version,
                    claim.activity.attempt_count,
                    claim.activity.updated_at,
                    str(claim.token),
                    claim.lease_expires_at,
                ),
                connection=connection,
            )
            if row is None:
                raise StaleDreamClaimError("Dream final failure lost its fencing token")
            if _map_dream_activity(row) != target:
                raise RuntimeError(
                    "Dream final-failure SQL diverged from the domain transition"
                )

    async def recover_expired_dream_leases(
        self,
        *,
        now: datetime,
        max_attempts: int,
        limit: int,
    ) -> int:
        """@brief 逐聚合回收过期 lease / Recover expired leases aggregate by aggregate.

        @param now 当前 UTC 时间 / Current UTC time.
        @param max_attempts 包含 crash claim 的最大尝试数 / Maximum attempts including the crashed claim.
        @param limit 单轮最大回收行数 / Maximum rows recovered per pass.
        @return 回收行数 / Recovered row count.
        """

        timestamp = ensure_utc(now)
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or max_attempts < 1
        ):
            raise ValueError("Dream recovery max_attempts must be positive")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 128
        ):
            raise ValueError("Dream recovery limit must be between 1 and 128")
        failure = DreamFailure("recovered expired Dream lease")
        async with db.transaction() as connection:
            rows = await db.fetch_all(
                "SELECT "
                + _DREAM_ACTIVITY_COLUMNS
                + " FROM user_profile.dreams WHERE status = 'processing' "
                "AND lease_expires_at <= %s ORDER BY lease_expires_at, dream_id "
                "LIMIT %s FOR UPDATE SKIP LOCKED",
                (timestamp, limit),
                connection=connection,
            )
            for row in rows:
                previous = _map_dream_activity(row)
                lease = DreamLease.restore(previous)
                recovery = previous.recover_expired_lease(
                    lease,
                    recovered_at=timestamp,
                    failure=failure,
                    max_attempts=max_attempts,
                )
                target = recovery.activity
                recovered_row = await db.fetch_one(
                    "UPDATE user_profile.dreams SET status = %s, version = %s, "
                    "attempt_count = %s, next_attempt_at = %s, claim_token = NULL, "
                    "lease_expires_at = NULL, result_patch = NULL, route_key = NULL, "
                    "last_error = %s, completed_at = %s, updated_at = %s "
                    "WHERE dream_id = CAST(%s AS UUID) AND status = 'processing' "
                    "AND version = %s AND attempt_count = %s AND updated_at = %s "
                    "AND claim_token = CAST(%s AS UUID) AND lease_expires_at = %s RETURNING "
                    + _DREAM_ACTIVITY_COLUMNS,
                    (
                        target.status.value,
                        target.version,
                        target.attempt_count,
                        target.next_attempt_at,
                        target.last_error,
                        target.completed_at,
                        target.updated_at,
                        str(previous.dream_id),
                        previous.version,
                        previous.attempt_count,
                        previous.updated_at,
                        str(lease.token),
                        lease.lease_expires_at,
                    ),
                    connection=connection,
                )
                if recovered_row is None:
                    raise RuntimeError(
                        "Locked expired Dream lease changed unexpectedly"
                    )
                if _map_dream_activity(recovered_row) != target:
                    raise RuntimeError(
                        "Dream lease-recovery SQL diverged from the domain transition"
                    )
            return len(rows)

    @staticmethod
    async def _load_dream_for_update(
        dream_id: DreamId,
        *,
        connection: AsyncConnection,
    ) -> DreamActivity:
        """@brief 锁定并完整恢复 Dream 聚合 / Lock and fully restore a Dream aggregate.

        @param dream_id Dream identity / Dream identity.
        @param connection 当前短事务 / Current short transaction.
        @return 完整聚合 / Complete aggregate.
        """

        row = await db.fetch_one(
            "SELECT "
            + _DREAM_ACTIVITY_COLUMNS
            + " FROM user_profile.dreams WHERE dream_id = CAST(%s AS UUID) FOR UPDATE",
            (str(dream_id),),
            connection=connection,
        )
        if row is None:
            raise StaleDreamClaimError("Dream no longer exists")
        return _map_dream_activity(row)

    @staticmethod
    def _require_current_claim(current: DreamActivity, claim: DreamClaim) -> None:
        """@brief 验证完整 aggregate/version/token/expiry capability / Validate complete aggregate/version/token/expiry capability.

        @param current 已锁 durable Dream 聚合 / Locked durable Dream aggregate.
        @param claim 调用方 settlement capability / Caller settlement capability.
        @return None / None.
        @raise StaleDreamClaimError claim 不再拥有当前聚合 / Claim no longer owns the current aggregate.
        """

        if current != claim.activity or current.version != claim.expected_version:
            raise StaleDreamClaimError("Dream claim is stale")
