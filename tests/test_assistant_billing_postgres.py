"""@brief Assistant 预留计费的真实 PostgreSQL 契约 / Real-PostgreSQL contracts for Assistant reservation billing."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import os
from pathlib import Path
from uuid import uuid4

import pytest

from fogmoe_bot.application.conversation.reset import ResetConversation
from fogmoe_bot.application.conversation.assistant_ingress import (
    AssistantInsufficientCoins,
    AssistantTurnAccepted,
    AssistantTurnRequest,
)
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    ConversationMessageId,
    DeliveryStreamId,
    OutboundMessageId,
    TurnId,
    TurnSource,
    UpdateId,
)
from fogmoe_bot.domain.conversation.inbox import InboundUpdate
from fogmoe_bot.domain.conversation.message import (
    MessageDraft,
    MessageRole,
)
from fogmoe_bot.domain.conversation.outbox import (
    SEND_TELEGRAM_MESSAGE,
    OutboundDraft,
)
from fogmoe_bot.infrastructure import config
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database import db
from fogmoe_bot.infrastructure.database.assistant_billing import (
    PostgresAssistantBilling,
)
from fogmoe_bot.infrastructure.database.assistant_turn_acceptance import (
    PostgresAssistantTurnAcceptanceUoW,
)
from fogmoe_bot.infrastructure.database.conversation_reset import (
    PostgresConversationResetUoW,
)
from fogmoe_bot.infrastructure.database.conversation_workflow.inbox import (
    PostgresInboxRepository,
)
from fogmoe_bot.infrastructure.database.conversation_workflow.inference import (
    PostgresInferenceRepository,
)
from fogmoe_bot.infrastructure.database.conversation_workflow.outbox import (
    PostgresOutboxRepository,
)
from fogmoe_bot.infrastructure.database.conversation_workflow.turn import (
    PostgresTurnRepository,
)
from fogmoe_dbctl.postgres import read_service, service_sqlalchemy_url


PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""@brief 项目根目录 / Project root."""


def _postgres_url() -> str:
    """@brief 读取显式隔离 DSN 或本地测试 service / Read an explicit isolated DSN or the local test service.

    @return async SQLAlchemy URL / Async SQLAlchemy URL.
    """

    explicit = os.environ.get("FOGMOE_TEST_DATABASE_URL")
    if explicit:
        return explicit
    if os.environ.get("FOGMOE_TEST_POSTGRES") != "1":
        pytest.skip("set FOGMOE_TEST_POSTGRES=1 to run the real PostgreSQL contract")
    config_dir = PROJECT_ROOT / "var/psql"
    if not (config_dir / "pg_service.conf").is_file():
        pytest.skip("local PostgreSQL service configuration is unavailable")
    return service_sqlalchemy_url(read_service(config_dir, "fogmoe_automation"))


def _request(
    *,
    user_id: int,
    update_id: int,
    now: datetime,
    cost: int,
) -> AssistantTurnRequest:
    """@brief 构造一个可计费 durable Assistant 请求 / Build a billable durable Assistant request.

    @param user_id 测试账户 / Test account.
    @param update_id durable Telegram Update / Durable Telegram Update.
    @param now 接收时间 / Receipt time.
    @param cost 费用 / Charge.
    @return acceptance 请求 / Acceptance request.
    """

    conversation_id = ConversationId(f"assistant-user:{user_id}")
    return AssistantTurnRequest(
        update_id=UpdateId(update_id),
        conversation_id=conversation_id,
        received_at=now,
        user_id=user_id,
        username=f"billing_{user_id}",
        display_name="Billing Contract",
        chat_id=user_id,
        is_group=False,
        message_id=update_id % 2_000_000_000 + 1,
        message_thread_id=None,
        delivery_stream_id=DeliveryStreamId(
            f"telegram:primary:chat:{user_id}:thread:0"
        ),
        user_content={
            "text": "billing contract",
            "content_kind": "text",
            "user": {"user_id": user_id},
        },
        coin_cost=cost,
    )


async def _create_account_and_inbound(request: AssistantTurnRequest) -> None:
    """@brief 建立测试账户与 durable inbox 行 / Create the test account and durable inbox row.

    @param request 请求 identity / Request identity.
    @return None / None.
    """

    async with db_connection.transaction() as connection:
        await db_connection.execute(
            "INSERT INTO identity.users "
            "(id, tg_uid, provider, name, coins, coins_paid, user_plan) "
            "VALUES (%s, %s, 'telegram', %s, 2, 5, 'paid')",
            (request.user_id, request.user_id, f"billing_{request.user_id}"),
            connection=connection,
        )
    inbox = PostgresInboxRepository()
    inserted = await inbox.add_inbound(
        InboundUpdate.pending(
            update_id=request.update_id,
            conversation_id=request.conversation_id,
            payload={"update_id": request.update_id.value, "kind": "billing-test"},
            received_at=request.received_at,
        )
    )
    assert inserted is True


async def _accept(
    turns: PostgresTurnRepository,
    request: AssistantTurnRequest,
    *,
    accepted_at: datetime,
) -> TurnId:
    """@brief 接受一次 Turn 并返回 identity / Accept one Turn and return its identity.

    @param repository 真实 workflow adapter / Real workflow adapter.
    @param request acceptance 请求 / Acceptance request.
    @param accepted_at 接受时刻 / Acceptance time.
    @return 新 Turn ID / New Turn ID.
    """

    result = await PostgresAssistantTurnAcceptanceUoW(turns).accept(
        request,
        accepted_at=accepted_at,
    )
    assert isinstance(result, AssistantTurnAccepted)
    assert result.replayed is False
    assert result.acceptance is not None
    return result.acceptance.turn.turn_id


def _completion_drafts(
    request: AssistantTurnRequest,
    turn_id: TurnId,
    *,
    created_at: datetime,
) -> tuple[MessageDraft, OutboundDraft]:
    """@brief 构造 deterministic history 与 outbox / Build deterministic history and outbox drafts.

    @param request 来源请求 / Source request.
    @param turn_id 来源 Turn / Source Turn.
    @param created_at 结果提交时刻 / Result commit time.
    @return assistant message 与 primary outbound / Assistant message and primary outbound.
    """

    return (
        MessageDraft(
            message_id=ConversationMessageId.for_turn(turn_id, "assistant-result"),
            conversation_id=request.conversation_id,
            turn_id=turn_id,
            source_update_id=None,
            role=MessageRole.ASSISTANT,
            content={"text": "done"},
            idempotency_key=f"turn:{turn_id}:assistant-result",
            created_at=created_at,
        ),
        OutboundDraft(
            message_id=OutboundMessageId.for_turn(turn_id, "primary-result"),
            conversation_id=request.conversation_id,
            turn_id=turn_id,
            delivery_stream_id=request.delivery_stream_id,
            kind=SEND_TELEGRAM_MESSAGE,
            payload={"chat_id": request.chat_id, "text": "done"},
            idempotency_key=f"turn:{turn_id}:primary-result",
            created_at=created_at,
        ),
    )


async def _account_and_billing(
    user_id: int,
    turn_id: TurnId,
) -> tuple[tuple[int, int], tuple[object, ...]]:
    """@brief 读取账户与计费事实 / Read account and billing facts.

    @param user_id 账户 ID / Account ID.
    @param turn_id Turn ID / Turn ID.
    @return 余额与 reservation 列 / Balances and reservation columns.
    """

    account = await db_connection.fetch_one(
        "SELECT coins, coins_paid FROM identity.users WHERE id = %s",
        (user_id,),
    )
    billing = await db_connection.fetch_one(
        "SELECT cost, free_reserved, paid_reserved, pool_contribution, status "
        "FROM assistant.billing_reservations WHERE turn_id = CAST(%s AS UUID)",
        (str(turn_id),),
    )
    assert account is not None and billing is not None
    return (int(account[0]), int(account[1])), tuple(billing)


async def _cleanup(user_ids: list[int]) -> None:
    """@brief 删除本测试全部图数据与 posting / Delete all graph data and postings owned by this test.

    @param user_ids 测试账户集合 / Test-account set.
    @return None / None.
    """

    conversation_ids = [f"assistant-user:{user_id}" for user_id in user_ids]
    async with db_connection.transaction() as connection:
        await db_connection.execute(
            "DELETE FROM economy.stake_pool_postings "
            "WHERE idempotency_key LIKE 'assistant-billing:settle:%' "
            "AND split_part(idempotency_key, ':', 3)::UUID IN ("
            "SELECT turn_id FROM conversation.conversation_turns "
            "WHERE conversation_id = ANY(%s))",
            (conversation_ids,),
            connection=connection,
        )
        await db_connection.execute(
            "DELETE FROM conversation.outbound_messages "
            "WHERE conversation_id = ANY(%s)",
            (conversation_ids,),
            connection=connection,
        )
        await db_connection.execute(
            "DELETE FROM assistant.billing_reservations WHERE user_id = ANY(%s)",
            (user_ids,),
            connection=connection,
        )
        await db_connection.execute(
            "DELETE FROM conversation.inference_activities "
            "WHERE conversation_id = ANY(%s)",
            (conversation_ids,),
            connection=connection,
        )
        await db_connection.execute(
            "DELETE FROM conversation.conversation_messages "
            "WHERE conversation_id = ANY(%s)",
            (conversation_ids,),
            connection=connection,
        )
        await db_connection.execute(
            "DELETE FROM conversation.conversation_history_resets "
            "WHERE conversation_id = ANY(%s)",
            (conversation_ids,),
            connection=connection,
        )
        await db_connection.execute(
            "DELETE FROM conversation.conversation_turns "
            "WHERE conversation_id = ANY(%s)",
            (conversation_ids,),
            connection=connection,
        )
        await db_connection.execute(
            "DELETE FROM conversation.inbound_updates WHERE conversation_id = ANY(%s)",
            (conversation_ids,),
            connection=connection,
        )
        await db_connection.execute(
            "DELETE FROM identity.users WHERE id = ANY(%s)",
            (user_ids,),
            connection=connection,
        )


def test_real_postgres_reserve_settle_release_cancel_and_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 真实 PG 验证 reserve→settle/release 全状态与 outbox 边界 / Real PostgreSQL verifies reserve-to-settle-or-release states and the outbox boundary.

    @param monkeypatch 隔离数据库 URL 注入 / Isolated database-URL injection.
    """

    async def scenario() -> None:
        """@brief 执行成功、失败、取消与 reset 场景 / Execute success, failure, cancellation, and reset scenarios."""

        monkeypatch.setattr(config, "SQLALCHEMY_DATABASE_URI", _postgres_url())
        await db.dispose_current_engine()
        discriminator = int(uuid4().hex[:11], 16)
        base_user = 7_000_000_000_000_000_000 + discriminator * 10
        base_update = 4_000_000_000_000_000_000 + discriminator * 10
        user_ids = [base_user + offset for offset in range(4)]
        now = datetime.now(UTC)
        billing = PostgresAssistantBilling()
        inbox = PostgresInboxRepository()
        outbox = PostgresOutboxRepository()
        turns = PostgresTurnRepository(billing=billing)
        inference = PostgresInferenceRepository(billing=billing, outbox=outbox)
        try:
            # 成功后才结算；后续 outbox 最终失败不得退款 / Settle only on inference
            # success; a later final outbox failure must not refund.
            success_request = _request(
                user_id=user_ids[0],
                update_id=base_update,
                now=now,
                cost=4,
            )
            await _create_account_and_inbound(success_request)
            success_turn = await _accept(turns, success_request, accepted_at=now)
            assert await _account_and_billing(user_ids[0], success_turn) == (
                (0, 3),
                (4, 2, 2, Decimal("0.80"), "reserved"),
            )
            before_settle = await db_connection.fetch_one(
                "SELECT COUNT(*) FROM economy.stake_pool_postings "
                "WHERE idempotency_key = %s",
                (f"assistant-billing:settle:{success_turn}",),
            )
            assert before_settle is not None and int(before_settle[0]) == 0
            success_claims = await inference.claim_inference_activities(
                now=now + timedelta(seconds=1),
                limit=32,
                lease_for=timedelta(minutes=1),
            )
            success_claim = next(
                claim
                for claim in success_claims
                if claim.activity.turn_id == success_turn
            )
            completed_at = now + timedelta(seconds=2)
            assistant_message, outbound = _completion_drafts(
                success_request,
                success_turn,
                created_at=completed_at,
            )
            await inference.complete_inference_activity(
                success_claim,
                assistant_message=assistant_message,
                outbound=outbound,
                completed_at=completed_at,
            )
            await inference.complete_inference_activity(
                success_claim,
                assistant_message=assistant_message,
                outbound=outbound,
                completed_at=completed_at,
            )
            assert await _account_and_billing(user_ids[0], success_turn) == (
                (0, 3),
                (4, 2, 2, Decimal("0.80"), "settled"),
            )
            posting = await db_connection.fetch_one(
                "SELECT COUNT(*), SUM(delta) FROM economy.stake_pool_postings "
                "WHERE idempotency_key = %s",
                (f"assistant-billing:settle:{success_turn}",),
            )
            assert posting is not None and tuple(posting) == (1, Decimal("0.80"))
            outbound_claims = await outbox.claim_outbound(
                now=now + timedelta(seconds=3),
                limit=32,
                lease_for=timedelta(minutes=1),
            )
            outbound_claim = next(
                claim
                for claim in outbound_claims
                if claim.message.turn_id == success_turn
            )
            await outbox.fail_outbound(
                outbound_claim,
                failed_at=now + timedelta(seconds=4),
                error="permanent Telegram rejection",
            )
            assert await _account_and_billing(user_ids[0], success_turn) == (
                (0, 3),
                (4, 2, 2, Decimal("0.80"), "settled"),
            )

            # 推理最终失败精确退回 free/paid 原桶且 release 可重放 / Final inference
            # failure refunds the exact original buckets and release is replay-safe.
            failure_request = _request(
                user_id=user_ids[1],
                update_id=base_update + 1,
                now=now,
                cost=4,
            )
            await _create_account_and_inbound(failure_request)
            failure_turn = await _accept(turns, failure_request, accepted_at=now)
            failure_claims = await inference.claim_inference_activities(
                now=now + timedelta(seconds=5),
                limit=32,
                lease_for=timedelta(minutes=1),
            )
            failure_claim = next(
                claim
                for claim in failure_claims
                if claim.activity.turn_id == failure_turn
            )
            await inference.fail_inference_activity(
                failure_claim,
                failed_at=now + timedelta(seconds=6),
                error="provider exhausted",
            )
            async with db_connection.transaction() as connection:
                await billing.release(
                    connection,
                    turn_id=failure_turn,
                    released_at=now + timedelta(seconds=7),
                )
            assert await _account_and_billing(user_ids[1], failure_turn) == (
                (2, 5),
                (4, 2, 2, Decimal("0.80"), "released"),
            )

            # 显式 cancel 共享同一退款状态机 / Explicit cancellation shares the refund state machine.
            cancel_request = _request(
                user_id=user_ids[2],
                update_id=base_update + 2,
                now=now,
                cost=4,
            )
            await _create_account_and_inbound(cancel_request)
            cancel_turn = await _accept(turns, cancel_request, accepted_at=now)
            current = await turns.get_turn(cancel_turn)
            assert current is not None
            await turns.cancel_turn(
                cancel_turn,
                expected_version=current.version,
                cancelled_at=now + timedelta(seconds=8),
            )
            assert await _account_and_billing(user_ids[2], cancel_turn) == (
                (2, 5),
                (4, 2, 2, Decimal("0.80"), "released"),
            )

            # reset 同时 fence 正费用与零费用 activity；仅正费用拥有 reservation / Reset
            # fences both positive- and zero-cost activities; only the positive one is reserved.
            reset_request = _request(
                user_id=user_ids[3],
                update_id=base_update + 3,
                now=now,
                cost=4,
            )
            await _create_account_and_inbound(reset_request)
            reset_turn = await _accept(turns, reset_request, accepted_at=now)
            zero_request = _request(
                user_id=user_ids[3],
                update_id=base_update + 4,
                now=now + timedelta(microseconds=1),
                cost=0,
            )
            await inbox.add_inbound(
                InboundUpdate.pending(
                    update_id=zero_request.update_id,
                    conversation_id=zero_request.conversation_id,
                    payload={"update_id": zero_request.update_id.value},
                    received_at=zero_request.received_at,
                )
            )
            zero_turn = await _accept(
                turns,
                zero_request,
                accepted_at=now + timedelta(microseconds=1),
            )
            reset_update_id = UpdateId(base_update + 5)
            reset_at = now + timedelta(seconds=9)
            await inbox.add_inbound(
                InboundUpdate.pending(
                    update_id=reset_update_id,
                    conversation_id=reset_request.conversation_id,
                    payload={"update_id": reset_update_id.value, "kind": "reset"},
                    received_at=reset_at,
                )
            )
            reset_key = f"update:{reset_update_id.value}:billing-reset-confirmation"
            reset_result = await PostgresConversationResetUoW().reset(
                ResetConversation(
                    source=TurnSource.telegram(reset_update_id),
                    conversation_id=reset_request.conversation_id,
                    confirmation=OutboundDraft(
                        message_id=OutboundMessageId.for_conversation(
                            reset_request.conversation_id,
                            reset_key,
                        ),
                        conversation_id=reset_request.conversation_id,
                        turn_id=None,
                        delivery_stream_id=reset_request.delivery_stream_id,
                        kind=SEND_TELEGRAM_MESSAGE,
                        payload={"chat_id": reset_request.chat_id, "text": "reset"},
                        idempotency_key=reset_key,
                        created_at=reset_at,
                    ),
                    requested_at=reset_at,
                )
            )
            assert reset_result.inserted is True
            assert await _account_and_billing(user_ids[3], reset_turn) == (
                (2, 5),
                (4, 2, 2, Decimal("0.80"), "released"),
            )
            zero_billing = await db_connection.fetch_one(
                "SELECT COUNT(*) FROM assistant.billing_reservations "
                "WHERE turn_id = CAST(%s AS UUID)",
                (str(zero_turn),),
            )
            states = await db_connection.fetch_all(
                "SELECT turn_id, state FROM conversation.conversation_turns "
                "WHERE turn_id IN (CAST(%s AS UUID), CAST(%s AS UUID)) "
                "ORDER BY turn_id",
                (str(reset_turn), str(zero_turn)),
            )
            assert zero_billing is not None and int(zero_billing[0]) == 0
            assert {str(row[1]) for row in states} == {"cancelled"}
        finally:
            await _cleanup(user_ids)
            await db.dispose_current_engine()

    asyncio.run(scenario())


def test_real_postgres_concurrent_acceptance_cannot_overreserve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 同账户并发 acceptance 只能预留一枚可用金币 / Concurrent acceptance against one account can reserve its single available coin only once.

    @param monkeypatch 隔离数据库 URL 注入 / Isolated database-URL injection.
    """

    async def scenario() -> None:
        """@brief 并发执行两个不同 Update / Concurrently execute two distinct Updates."""

        monkeypatch.setattr(config, "SQLALCHEMY_DATABASE_URI", _postgres_url())
        await db.dispose_current_engine()
        discriminator = int(uuid4().hex[:11], 16)
        user_id = 7_500_000_000_000_000_000 + discriminator
        base_update = 4_500_000_000_000_000_000 + discriminator * 2
        now = datetime.now(UTC)
        requests = (
            _request(user_id=user_id, update_id=base_update, now=now, cost=1),
            _request(user_id=user_id, update_id=base_update + 1, now=now, cost=1),
        )
        inbox = PostgresInboxRepository()
        turns = PostgresTurnRepository()
        try:
            async with db_connection.transaction() as connection:
                await db_connection.execute(
                    "INSERT INTO identity.users "
                    "(id, tg_uid, provider, name, coins, coins_paid, user_plan) "
                    "VALUES (%s, %s, 'telegram', %s, 1, 0, 'free')",
                    (user_id, user_id, f"billing_concurrent_{user_id}"),
                    connection=connection,
                )
            for request in requests:
                assert await inbox.add_inbound(
                    InboundUpdate.pending(
                        update_id=request.update_id,
                        conversation_id=request.conversation_id,
                        payload={"update_id": request.update_id.value},
                        received_at=now,
                    )
                )
            acceptance = PostgresAssistantTurnAcceptanceUoW(turns)
            results = await asyncio.gather(
                *(acceptance.accept(request, accepted_at=now) for request in requests)
            )
            assert (
                sum(isinstance(result, AssistantTurnAccepted) for result in results)
                == 1
            )
            assert (
                sum(
                    isinstance(result, AssistantInsufficientCoins) for result in results
                )
                == 1
            )
            account = await db_connection.fetch_one(
                "SELECT coins, coins_paid FROM identity.users WHERE id = %s",
                (user_id,),
            )
            reservations = await db_connection.fetch_one(
                "SELECT COUNT(*), SUM(cost), bool_and(status = 'reserved') "
                "FROM assistant.billing_reservations WHERE user_id = %s",
                (user_id,),
            )
            postings = await db_connection.fetch_one(
                "SELECT COUNT(*) FROM economy.stake_pool_postings "
                "WHERE idempotency_key LIKE 'assistant-billing:settle:%' "
                "AND split_part(idempotency_key, ':', 3)::UUID IN ("
                "SELECT turn_id FROM conversation.conversation_turns "
                "WHERE conversation_id = %s)",
                (str(requests[0].conversation_id),),
            )
            assert account is not None and tuple(account) == (0, 0)
            assert reservations is not None and tuple(reservations) == (1, 1, True)
            assert postings is not None and int(postings[0]) == 0
        finally:
            await _cleanup([user_id])
            await db.dispose_current_engine()

    asyncio.run(scenario())
