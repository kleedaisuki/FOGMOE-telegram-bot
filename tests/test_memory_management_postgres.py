"""@brief Memory/Profile 管理命令的真实 PostgreSQL 契约 / Real-PostgreSQL contract for Memory/Profile management commands."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
import os
from uuid import UUID, uuid4

import pytest

from fogmoe_bot.application.memory import ForgetMemory
from fogmoe_bot.application.retrieval import EpisodicPassageRenderer
from fogmoe_bot.application.retrieval.ports import EpisodicTurn
from fogmoe_bot.application.telegram import GroupAdministratorDecision
from fogmoe_bot.application.user_profile import (
    ClearUserProfile,
    RequestUserProfileRegeneration,
)
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    OutboundMessageId,
    TurnSource,
    UpdateId,
)
from fogmoe_bot.domain.conversation.inbox import InboundUpdate
from fogmoe_bot.domain.conversation.outbox import SEND_TELEGRAM_MESSAGE, OutboundDraft
from fogmoe_bot.domain.retrieval import EmbeddingSpace, RetrievalScope
from fogmoe_bot.domain.user_profile.models import ProfileEvidence, ProfileMetadata
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database import db
from fogmoe_bot.infrastructure.database.conversation_workflow.inbox import (
    PostgresInboxRepository,
)
from fogmoe_bot.infrastructure.database.memory_management import (
    PostgresMemoryForgetUoW,
)
from fogmoe_bot.infrastructure.database.retrieval import PostgresRetrievalStore
from fogmoe_bot.infrastructure.database.retrieval import PostgresEpisodicSource
from fogmoe_bot.infrastructure.database.telegram_authorization import (
    PostgresGroupAdministratorDecisionStore,
)
from fogmoe_bot.infrastructure.database.user_profile.management import (
    PostgresUserProfileManagementUoW,
)
from fogmoe_bot.infrastructure.database.user_profile.source import (
    PostgresProfileEvidenceSource,
)
from fogmoe_bot.infrastructure.database.user_profile.store import (
    PostgresUserProfileStore,
)
from postgres_test_support import configure_bot_database


def _postgres_url() -> str:
    """@brief 读取显式 DSN / Read an explicit DSN.

    @return SQLAlchemy URL / SQLAlchemy URL.
    """

    explicit = os.environ.get("FOGMOE_TEST_DATABASE_URL")
    if explicit:
        return explicit
    pytest.skip("set FOGMOE_TEST_DATABASE_URL to run the real PostgreSQL contract")


def _draft(
    *,
    update_id: UpdateId,
    conversation_id: ConversationId,
    created_at: datetime,
) -> OutboundDraft:
    """@brief 构造确定性命令确认 / Build a deterministic command confirmation.

    @param update_id 来源 Update / Source Update.
    @param conversation_id Conversation / Conversation.
    @param created_at 命令时刻 / Command time.
    @return standalone outbox draft / Standalone outbox draft.
    """

    key = f"update:{int(update_id)}:test-memory-management-response"
    return OutboundDraft(
        message_id=OutboundMessageId.for_conversation(conversation_id, key),
        conversation_id=conversation_id,
        turn_id=None,
        delivery_stream_id=DeliveryStreamId("telegram:primary:chat:42"),
        kind=SEND_TELEGRAM_MESSAGE,
        payload={"chat_id": 42, "text": "done"},
        idempotency_key=key,
        created_at=created_at,
    )


async def _add_inbound(
    update_id: UpdateId,
    conversation_id: ConversationId,
    received_at: datetime,
) -> None:
    """@brief 插入命令 durable source / Insert a durable command source.

    @param update_id Update ID / Update ID.
    @param conversation_id Conversation / Conversation.
    @param received_at 接收时刻 / Receipt time.
    @return None / None.
    """

    await PostgresInboxRepository().add_inbound(
        InboundUpdate.pending(
            update_id=update_id,
            conversation_id=conversation_id,
            payload={"update_id": int(update_id)},
            received_at=received_at,
        )
    )


async def _insert_profile_turn(
    *,
    turn_id: str,
    conversation_id: ConversationId,
    occurred_at: datetime,
    source_key: str,
) -> None:
    """@brief 插入 Profile evidence 所需 canonical Turn / Insert the canonical Turn required by Profile evidence.

    @param turn_id Turn UUID / Turn UUID.
    @param conversation_id Conversation / Conversation.
    @param occurred_at 完成时刻 / Completion time.
    @param source_key 唯一来源键 / Unique source key.
    @return None / None.
    """

    await db_connection.execute(
        "INSERT INTO conversation.conversation_turns "
        "(turn_id, conversation_id, state, created_at, updated_at, completed_at, "
        "source_kind, source_key) VALUES (CAST(%s AS UUID), %s, 'delivered', "
        "%s, %s, %s, 'scheduled.prompt', %s)",
        (
            turn_id,
            str(conversation_id),
            occurred_at,
            occurred_at,
            occurred_at,
            source_key,
        ),
    )


async def _insert_delayed_completed_turn(
    *,
    turn_id: str,
    update_id: UpdateId,
    conversation_id: ConversationId,
    user_id: int,
    occurred_at: datetime,
) -> None:
    """@brief 在遗忘后插入一个较早完成的延迟来源 / Insert an old completed source after forgetting.

    @param turn_id Turn UUID / Turn UUID.
    @param update_id Telegram source Update / Telegram source Update.
    @param conversation_id Conversation / Conversation.
    @param user_id 用户 ID / User identifier.
    @param occurred_at 早于遗忘边界的完成时刻 / Completion time preceding the forgetting boundary.
    @return None / None.
    """

    activity_id = str(uuid4())
    request = {
        "task_kind": "assistant",
        "user": {
            "user_id": user_id,
            "display_name": "Klee",
            "username": "klee",
            "personal_info": "delayed metadata",
        },
        "scope": {"is_group": False, "group_id": None},
    }
    async with db_connection.transaction() as connection:
        await db_connection.execute(
            "INSERT INTO conversation.conversation_turns "
            "(turn_id, conversation_id, source_update_id, state, created_at, updated_at, "
            "completed_at, source_kind, source_key) VALUES (CAST(%s AS UUID), %s, %s, "
            "'delivered', %s, %s, %s, 'telegram.update', %s)",
            (
                turn_id,
                str(conversation_id),
                int(update_id),
                occurred_at,
                occurred_at,
                occurred_at,
                str(int(update_id)),
            ),
            connection=connection,
        )
        await db_connection.execute(
            "INSERT INTO conversation.inference_activities "
            "(activity_id, turn_id, conversation_id, request, status, version, "
            "attempt_count, next_attempt_at, claim_token, lease_expires_at, "
            "completion_token, created_at, updated_at, completed_at, traceparent) "
            "VALUES (CAST(%s AS UUID), CAST(%s AS UUID), %s, CAST(%s AS JSONB), "
            "'completed', 1, 1, NULL, NULL, NULL, CAST(%s AS UUID), %s, %s, %s, %s)",
            (
                activity_id,
                turn_id,
                str(conversation_id),
                json.dumps(request),
                str(uuid4()),
                occurred_at,
                occurred_at,
                occurred_at,
                "00-11111111111111111111111111111111-2222222222222222-01",
            ),
            connection=connection,
        )
        for sequence, (role, text) in enumerate(
            (("user", "delayed old fact"), ("assistant", "delayed old answer")),
            start=1,
        ):
            await db_connection.execute(
                "INSERT INTO conversation.conversation_messages "
                "(message_id, conversation_id, sequence, turn_id, role, content, "
                "idempotency_key, created_at) VALUES (CAST(%s AS UUID), %s, %s, "
                "CAST(%s AS UUID), %s, CAST(%s AS JSONB), %s, %s)",
                (
                    str(uuid4()),
                    str(conversation_id),
                    sequence,
                    turn_id,
                    role,
                    json.dumps({"text": text}),
                    f"management-delayed:{turn_id}:{role}",
                    occurred_at + timedelta(microseconds=sequence),
                ),
                connection=connection,
            )


def test_real_postgres_forgetting_boundaries_prevent_resurrection_and_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 实库验证旧数据不复活、重放不误删新数据 / Verify old data cannot resurrect and replay cannot delete newer data."""

    async def scenario() -> None:
        """@brief 执行 Retrieval 与 Profile 管理场景 / Execute Retrieval and Profile management scenarios.

        @return None / None.
        """

        await db.dispose_current_engine()
        configure_bot_database(_postgres_url())
        suffix = uuid4().hex
        user_id = 6_500_000_000_000_000_000 + int(suffix[:10], 16)
        group_id = -(user_id + 100)
        base_update = 5_000_000_000_000_000_000 + int(suffix[:10], 16) * 4
        conversation_id = ConversationId(f"assistant-user:{user_id}")
        cutoff = datetime(2038, 5, 6, 7, 8, tzinfo=UTC)
        updates = tuple(UpdateId(base_update + index) for index in range(4))
        space = EmbeddingSpace(
            space_id=f"test.manage.{suffix[:12]}",
            model="test/embedding",
            dimensions=1024,
            query_instruction="Retrieve test memory.",
            passage_format_version=int(suffix[:7], 16) + 2,
        )
        retrieval = PostgresRetrievalStore()
        renderer = EpisodicPassageRenderer(format_version=space.passage_format_version)
        profile_store = PostgresUserProfileStore()
        profile_management = PostgresUserProfileManagementUoW()
        memory_management = PostgresMemoryForgetUoW()
        profile_turns = tuple(str(uuid4()) for _ in range(3))
        retrieval_turns = (uuid4(), uuid4())
        delayed_turn = str(uuid4())
        try:
            await db_connection.execute(
                "INSERT INTO identity.users (id, tg_uid, provider, name) "
                "VALUES (%s, %s, 'telegram', %s)",
                (user_id, user_id, f"management-{suffix}"),
            )
            for update_id in updates:
                await _add_inbound(update_id, conversation_id, cutoff)
            authorization_store = PostgresGroupAdministratorDecisionStore()
            allowed = await authorization_store.freeze(
                GroupAdministratorDecision(
                    update_id=updates[1],
                    chat_id=group_id,
                    actor_user_id=user_id,
                    allowed=True,
                    observed_at=cutoff,
                )
            )
            replayed_decision = await authorization_store.freeze(
                GroupAdministratorDecision(
                    update_id=updates[1],
                    chat_id=group_id,
                    actor_user_id=user_id,
                    allowed=False,
                    observed_at=cutoff + timedelta(seconds=1),
                )
            )
            assert allowed.allowed is True and replayed_decision == allowed
            await retrieval.ensure_space(space)

            personal_scope = RetrievalScope("personal", user_id)
            group_scope = RetrievalScope("group", group_id)
            old_personal = EpisodicTurn(
                retrieval_turns[0],
                personal_scope,
                "old personal fact",
                "old personal answer",
                cutoff - timedelta(seconds=1),
            )
            old_group = EpisodicTurn(
                retrieval_turns[1],
                group_scope,
                "old group fact",
                "old group answer",
                cutoff - timedelta(seconds=1),
            )
            for index, turn in enumerate((old_personal, old_group)):
                await _insert_profile_turn(
                    turn_id=str(turn.turn_id),
                    conversation_id=conversation_id,
                    occurred_at=turn.occurred_at,
                    source_key=f"management-retrieval:{suffix}:{index}",
                )
            for turn in (old_personal, old_group):
                await retrieval.project_turn(
                    turn,
                    renderer.render(turn),
                    space=space,
                    projected_at=cutoff,
                )

            personal_command = ForgetMemory(
                source=TurnSource.telegram(updates[0]),
                conversation_id=conversation_id,
                scope=personal_scope,
                confirmation=_draft(
                    update_id=updates[0],
                    conversation_id=conversation_id,
                    created_at=cutoff,
                ),
                requested_at=cutoff,
            )
            first = await memory_management.forget(personal_command)
            assert first.applied is True and first.deleted_passages == 1
            personal_markers = await db_connection.fetch_one(
                "SELECT COUNT(*) FROM retrieval.source_projections "
                "WHERE scope_kind = 'personal' AND scope_id = %s",
                (user_id,),
            )
            assert personal_markers is not None and int(personal_markers[0]) == 0
            await retrieval.project_turn(
                old_personal,
                renderer.render(old_personal),
                space=space,
                projected_at=cutoff + timedelta(seconds=1),
            )
            new_personal = EpisodicTurn(
                uuid4(),
                personal_scope,
                "new personal fact",
                "new personal answer",
                cutoff + timedelta(seconds=1),
            )
            await retrieval.project_turn(
                new_personal,
                renderer.render(new_personal),
                space=space,
                projected_at=cutoff + timedelta(seconds=1),
            )
            replay = await memory_management.forget(personal_command)
            assert replay.applied is False and replay.deleted_passages == 0
            counts = await db_connection.fetch_all(
                "SELECT scope_kind, COUNT(*) FROM retrieval.passages "
                "WHERE (scope_kind = 'personal' AND scope_id = %s) "
                "OR (scope_kind = 'group' AND scope_id = %s) "
                "GROUP BY scope_kind ORDER BY scope_kind",
                (user_id, group_id),
            )
            assert [(str(row[0]), int(row[1])) for row in counts] == [
                ("group", 1),
                ("personal", 1),
            ]

            group_result = await memory_management.forget(
                ForgetMemory(
                    source=TurnSource.telegram(updates[1]),
                    conversation_id=conversation_id,
                    scope=group_scope,
                    confirmation=_draft(
                        update_id=updates[1],
                        conversation_id=conversation_id,
                        created_at=cutoff,
                    ),
                    requested_at=cutoff,
                )
            )
            assert group_result.deleted_passages == 1

            for index, turn_id in enumerate(profile_turns):
                await _insert_profile_turn(
                    turn_id=turn_id,
                    conversation_id=conversation_id,
                    occurred_at=cutoff + timedelta(seconds=index - 2),
                    source_key=f"management:{suffix}:{index}",
                )
            old_evidence = ProfileEvidence(
                event_id=0,
                source_turn_id=UUID(profile_turns[0]),
                owner_user_id=user_id,
                user_text="old profile fact",
                assistant_text="old profile answer",
                occurred_at=cutoff - timedelta(seconds=2),
                metadata=ProfileMetadata("Klee", None, "old metadata"),
            )
            await profile_store.project_evidence(old_evidence, projected_at=cutoff)
            event_row = await db_connection.fetch_one(
                "SELECT event_id FROM user_profile.evidence_events "
                "WHERE source_turn_id = CAST(%s AS UUID)",
                (profile_turns[0],),
            )
            assert event_row is not None
            event_id = int(event_row[0])
            async with db_connection.transaction() as connection:
                await db_connection.execute(
                    "INSERT INTO user_profile.profile_revisions "
                    "(user_id, revision, document, observed_through_event_id, route_key, "
                    "prompt_version, created_at) VALUES (%s, 1, CAST(%s AS JSONB), %s, "
                    "'test:model', 1, %s)",
                    (user_id, json.dumps({"claims": []}), event_id, cutoff),
                    connection=connection,
                )
                await db_connection.execute(
                    "UPDATE user_profile.profiles SET current_revision = 1, "
                    "observed_through_event_id = %s, updated_at = %s WHERE user_id = %s",
                    (event_id, cutoff, user_id),
                    connection=connection,
                )

            clear_command = ClearUserProfile(
                source=TurnSource.telegram(updates[2]),
                conversation_id=conversation_id,
                user_id=user_id,
                confirmation=_draft(
                    update_id=updates[2],
                    conversation_id=conversation_id,
                    created_at=cutoff,
                ),
                requested_at=cutoff,
            )
            cleared = await profile_management.clear(clear_command)
            assert cleared.applied is True
            assert await profile_store.read_profile(user_id) is None
            await _insert_delayed_completed_turn(
                turn_id=delayed_turn,
                update_id=updates[0],
                conversation_id=conversation_id,
                user_id=user_id,
                occurred_at=datetime(1990, 1, 1, tzinfo=UTC),
            )
            delayed_retrieval = await PostgresEpisodicSource().read_unprojected(
                format_version=space.passage_format_version,
                limit=128,
            )
            delayed_profiles = await PostgresProfileEvidenceSource().read_unprojected(
                limit=128
            )
            assert delayed_turn not in {str(item.turn_id) for item in delayed_retrieval}
            assert delayed_turn not in {
                str(item.source_turn_id) for item in delayed_profiles
            }
            old_late = ProfileEvidence(
                event_id=0,
                source_turn_id=UUID(profile_turns[1]),
                owner_user_id=user_id,
                user_text="late old profile fact",
                assistant_text="late old profile answer",
                occurred_at=cutoff - timedelta(seconds=1),
                metadata=ProfileMetadata("Klee", None, "old metadata"),
            )
            await profile_store.project_evidence(
                old_late,
                projected_at=cutoff + timedelta(seconds=1),
            )
            new_evidence = ProfileEvidence(
                event_id=0,
                source_turn_id=UUID(profile_turns[2]),
                owner_user_id=user_id,
                user_text="new profile fact",
                assistant_text="new profile answer",
                occurred_at=cutoff + timedelta(seconds=1),
                metadata=ProfileMetadata("Klee", None, "new metadata"),
            )
            await profile_store.project_evidence(
                new_evidence,
                projected_at=cutoff + timedelta(seconds=1),
            )
            replay_clear = await profile_management.clear(clear_command)
            assert replay_clear.applied is False
            evidence_rows = await db_connection.fetch_all(
                "SELECT user_text FROM user_profile.evidence_events "
                "WHERE owner_user_id = %s ORDER BY event_id",
                (user_id,),
            )
            assert [str(row[0]) for row in evidence_rows] == ["new profile fact"]

            regeneration = RequestUserProfileRegeneration(
                source=TurnSource.telegram(updates[3]),
                conversation_id=conversation_id,
                user_id=user_id,
                confirmation=_draft(
                    update_id=updates[3],
                    conversation_id=conversation_id,
                    created_at=cutoff,
                ),
                requested_at=cutoff,
            )
            requested = await profile_management.request_regeneration(regeneration)
            replay_requested = await profile_management.request_regeneration(
                regeneration
            )
            assert requested.applied is True and replay_requested.applied is False
            due = await db_connection.fetch_one(
                "SELECT next_eligible_at, forgotten_through "
                "FROM user_profile.profiles WHERE user_id = %s",
                (user_id,),
            )
            assert due is not None and due[0] == cutoff and due[1] == cutoff
        finally:
            await db_connection.execute(
                "DELETE FROM conversation.inference_activities "
                "WHERE conversation_id = %s",
                (str(conversation_id),),
            )
            await db_connection.execute(
                "DELETE FROM conversation.conversation_messages "
                "WHERE conversation_id = %s",
                (str(conversation_id),),
            )
            await db_connection.execute(
                "DELETE FROM conversation.outbound_messages WHERE conversation_id = %s",
                (str(conversation_id),),
            )
            await db_connection.execute(
                "DELETE FROM identity.users WHERE id = %s",
                (user_id,),
            )
            await db_connection.execute(
                "DELETE FROM conversation.conversation_turns "
                "WHERE turn_id = ANY(CAST(%s AS UUID[]))",
                (
                    [
                        *profile_turns,
                        delayed_turn,
                        *(str(item) for item in retrieval_turns),
                    ],
                ),
            )
            await db_connection.execute(
                "DELETE FROM conversation.inbound_updates "
                "WHERE update_id = ANY(CAST(%s AS BIGINT[]))",
                (list(int(item) for item in updates),),
            )
            await db_connection.execute(
                "DELETE FROM retrieval.source_projections "
                "WHERE scope_kind = 'group' AND scope_id = %s",
                (group_id,),
            )
            await db_connection.execute(
                "DELETE FROM retrieval.scope_forgetting_boundaries "
                "WHERE scope_kind = 'group' AND scope_id = %s",
                (group_id,),
            )
            await db_connection.execute(
                "DELETE FROM retrieval.embedding_spaces WHERE space_id = %s",
                (space.space_id,),
            )
            await db.dispose_current_engine()

    asyncio.run(scenario())
