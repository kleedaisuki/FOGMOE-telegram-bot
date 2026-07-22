"""@brief Durable translation acceptance 的真实 PostgreSQL 契约 / Real-PostgreSQL contract for durable translation acceptance."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
import os
from uuid import uuid4

import pytest

from fogmoe_bot.application.conversation.assistant_ingress import AssistantTurnAccepted
from fogmoe_bot.application.conversation.translation_ingress import (
    TranslationReplyTarget,
    TranslationTurnRequest,
)
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    UpdateId,
)
from fogmoe_bot.domain.conversation.inbox import InboundUpdate
from fogmoe_bot.domain.accounts.plan import AccountPlanPolicy
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database import db
from fogmoe_bot.infrastructure.database.assistant_turn_acceptance import (
    PostgresAssistantTurnAcceptanceUoW,
)
from fogmoe_bot.infrastructure.database.account_plan import PostgresAccountPlanResolver
from fogmoe_bot.infrastructure.database.conversation_workflow.inbox import (
    PostgresInboxRepository,
)
from fogmoe_bot.infrastructure.database.conversation_workflow.turn import (
    PostgresTurnRepository,
)
from postgres_test_support import configure_bot_database

def _postgres_url() -> str:
    """@brief 读取真实 PostgreSQL DSN / Read a real PostgreSQL DSN.

    @return async SQLAlchemy URL / Async SQLAlchemy URL.
    """

    explicit = os.environ.get("FOGMOE_TEST_DATABASE_URL")
    if explicit:
        return explicit
    pytest.skip("set FOGMOE_TEST_DATABASE_URL to run the real PostgreSQL contract")


def test_translation_turn_and_activity_replay_atomically_without_billing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 翻译、Turn、隔离消息和 activity 原子且只发生一次，不产生计费 / Translation, Turn, isolated message, and activity are atomic and occur once without billing.

    @param monkeypatch 临时数据库配置 / Temporary database configuration.
    """

    async def scenario() -> None:
        """@brief 执行真实 acceptance 与重放 / Execute real acceptance and replay."""

        await db.dispose_current_engine()
        configure_bot_database(_postgres_url())
        suffix = uuid4().hex
        discriminator = int(suffix[:12], 16)
        user_id = 6_000_000_000_000_000_000 + discriminator
        update_id = 5_000_000_000_000_000_000 + discriminator
        now = datetime.now(UTC)
        conversation_id = ConversationId(f"assistant-user:{user_id}")
        inbound = InboundUpdate.pending(
            update_id=UpdateId(update_id),
            conversation_id=conversation_id,
            payload={"update_id": update_id, "kind": "translation-test"},
            received_at=now,
        )
        inbox = PostgresInboxRepository()
        turns = PostgresTurnRepository()
        target = TranslationReplyTarget(
            update_id=inbound.update_id,
            conversation_id=conversation_id,
            received_at=now,
            chat_id=user_id,
            chat_type="private",
            message_id=77,
            message_thread_id=None,
            delivery_stream_id=DeliveryStreamId(
                f"telegram:primary:chat:{user_id}:thread:0"
            ),
        )
        request = TranslationTurnRequest(
            target=target,
            user_id=user_id,
            username=f"translation_{suffix}",
            display_name="Translation Test",
            is_group=False,
            text="x" * 501,
        ).to_assistant_request()
        try:
            async with db_connection.transaction() as connection:
                await db_connection.execute(
                    "INSERT INTO identity.users "
                    "(id, tg_uid, provider, name) "
                    "VALUES (%s, %s, 'telegram', %s)",
                    (user_id, user_id, f"translation_{suffix}"),
                    connection=connection,
                )
            assert await inbox.add_inbound(inbound) is True
            acceptance = PostgresAssistantTurnAcceptanceUoW(
                turns,
                plans=PostgresAccountPlanResolver(
                    AccountPlanPolicy(administrator_id=1)
                ),
            )
            first = await acceptance.accept(request, accepted_at=now)
            replay = await acceptance.accept(request, accepted_at=now)

            assert isinstance(first, AssistantTurnAccepted) and not first.replayed
            assert isinstance(replay, AssistantTurnAccepted) and replay.replayed
            ledger_entries = await db_connection.fetch_one(
                "SELECT count(*) FROM bank.ledger_entries WHERE actor_id = %s",
                (user_id,),
            )
            assert ledger_entries is not None and ledger_entries[0] == 0
            facts = await db_connection.fetch_one(
                "SELECT turn.state, message.content, activity.request "
                "FROM conversation.conversation_turns AS turn "
                "JOIN conversation.conversation_messages AS message "
                "ON message.turn_id = turn.turn_id AND message.role = 'user' "
                "JOIN conversation.inference_activities AS activity "
                "ON activity.turn_id = turn.turn_id "
                "WHERE turn.source_update_id = %s",
                (update_id,),
            )
            assert facts is not None
            assert facts[0] == "waiting_inference"
            assert facts[1]["exclude_from_assistant"] is True
            assert "coin_cost" not in facts[1]
            assert facts[2]["task_kind"] == "translation"
            assert facts[2]["translation_input"] == "x" * 501
            assert first.acceptance is not None
            current_messages = await turns.read_conversation_messages(
                conversation_id,
                through_turn_id=first.acceptance.turn.turn_id,
                limit=128,
            )
            assert len(current_messages) == 1
            assert current_messages[0].draft.content["exclude_from_assistant"] is True

            later_update_id = update_id + 1
            later_inbound = InboundUpdate.pending(
                update_id=UpdateId(later_update_id),
                conversation_id=conversation_id,
                payload={"update_id": later_update_id, "kind": "assistant-test"},
                received_at=now,
            )
            assert await inbox.add_inbound(later_inbound) is True
            later_request = replace(
                request,
                update_id=later_inbound.update_id,
                message_id=78,
                user_content={"text": "ordinary follow-up"},
                task_kind="assistant",
                translation_input=None,
            )
            later = await acceptance.accept(later_request, accepted_at=now)
            assert isinstance(later, AssistantTurnAccepted) and not later.replayed
            assert later.acceptance is not None
            future_messages = await turns.read_conversation_messages(
                conversation_id,
                through_turn_id=later.acceptance.turn.turn_id,
                limit=128,
            )
            assert [message.draft.content["text"] for message in future_messages] == [
                "ordinary follow-up"
            ]
        finally:
            async with db_connection.transaction() as connection:
                await db_connection.execute(
                    "DELETE FROM conversation.inference_activities "
                    "WHERE conversation_id = %s",
                    (str(conversation_id),),
                    connection=connection,
                )
                await db_connection.execute(
                    "DELETE FROM conversation.conversation_messages "
                    "WHERE conversation_id = %s",
                    (str(conversation_id),),
                    connection=connection,
                )
                await db_connection.execute(
                    "DELETE FROM conversation.conversation_turns "
                    "WHERE conversation_id = %s",
                    (str(conversation_id),),
                    connection=connection,
                )
                await db_connection.execute(
                    "DELETE FROM conversation.inbound_updates "
                    "WHERE update_id IN (%s, %s)",
                    (update_id, update_id + 1),
                    connection=connection,
                )
                await db_connection.execute(
                    "DELETE FROM identity.users WHERE id = %s",
                    (user_id,),
                    connection=connection,
                )
            await db.dispose_current_engine()

    asyncio.run(scenario())
