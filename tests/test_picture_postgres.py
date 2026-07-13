"""图片 workflow 的真实 PostgreSQL 事务测试 / Real-PostgreSQL transaction tests for picture workflows."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import os
from uuid import uuid4

import pytest

from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    OutboundMessageId,
)
from fogmoe_bot.domain.conversation.outbox import (
    SEND_TELEGRAM_PHOTO,
    OutboundDraft,
)
from fogmoe_bot.domain.media.identifiers import ArtifactId, UserId
from fogmoe_bot.domain.media.picture import (
    HdOffer,
    PictureCandidate,
    PictureRating,
    PictureReceiptConflict,
)
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database import db
from fogmoe_bot.infrastructure.database.media.account import (
    PostgresMediaAccountProfiles,
)
from fogmoe_bot.infrastructure.database.media.picture import (
    PostgresPictureRepository,
)


ADMINISTRATOR_ID = 1002288404
"""@brief 测试管理员 Telegram 用户 ID / Test administrator Telegram user ID."""


def _preview_commit(
    offer: HdOffer,
    *,
    now: datetime,
    key: str,
) -> dict[str, object]:
    """@brief 构造原子预览提交参数 / Build atomic preview-commit arguments."""

    conversation_id = ConversationId(f"media-picture-test:{offer.requester_id}")
    outbound_key = f"{key}:photo"
    return {
        "idempotency_key": key,
        "request_fingerprint": "a" * 64,
        "outbound": OutboundDraft(
            message_id=OutboundMessageId.for_conversation(
                conversation_id,
                outbound_key,
            ),
            conversation_id=conversation_id,
            turn_id=None,
            delivery_stream_id=DeliveryStreamId(
                f"telegram:primary:chat:{offer.requester_id}:thread:0"
            ),
            kind=SEND_TELEGRAM_PHOTO,
            payload={
                "chat_id": int(offer.requester_id),
                "photo_url": offer.picture.preview_url,
                "caption": "preview",
                "has_spoiler": False,
            },
            idempotency_key=outbound_key,
            created_at=now,
        ),
    }


def test_picture_charge_claim_and_reward_postings_are_atomic() -> None:
    """@brief 真实 PG 中预览/高清只扣一次且奖励 posting 幂等 / Preview/HD charge once and reward postings are idempotent in real PostgreSQL."""

    if os.environ.get("FOGMOE_TEST_POSTGRES") != "1":
        pytest.skip("set FOGMOE_TEST_POSTGRES=1 to run the real PostgreSQL contract")

    async def scenario() -> None:
        suffix = uuid4().int % 100_000_000
        requester = UserId(8_000_000_000 + suffix)
        first_clicker = UserId(int(requester) + 1)
        second_clicker = UserId(int(requester) + 2)
        offer_id = ArtifactId(uuid4().hex)
        now = datetime.now(UTC)
        repository = PostgresPictureRepository(ADMINISTRATOR_ID)
        posting_keys = (f"media:preview:{offer_id}", f"media:hd:{offer_id}")
        try:
            for user_id in (requester, first_clicker, second_clicker):
                await db_connection.execute(
                    "INSERT INTO identity.users (id, tg_uid, name, coins) VALUES (%s, %s, %s, 100)",
                    (int(user_id), int(user_id), f"media-test-{user_id}"),
                )
            offer = HdOffer(
                offer_id=offer_id,
                picture=PictureCandidate(
                    source_id="pg-picture",
                    sample_url="https://example.test/sample.jpg",
                    file_url="https://example.test/full.jpg",
                    tags="safe",
                    width=100,
                    height=100,
                    file_size=1000,
                    score=1,
                    rating=PictureRating.SAFE,
                ),
                requester_id=requester,
                expires_at=now + timedelta(minutes=30),
            )
            preview = await repository.charge_preview_and_store_offer(
                offer=offer,
                cost=5,
                now=now,
                **_preview_commit(offer, now=now, key=f"media-test:{offer_id}"),
            )
            assert preview.charged
            assert preview.balance == 95
            claims = await asyncio.gather(
                repository.claim_hd(
                    offer_id,
                    user_id=first_clicker,
                    cost=10,
                    now=now,
                    lease_for=timedelta(minutes=1),
                ),
                repository.claim_hd(
                    offer_id,
                    user_id=second_clicker,
                    cost=10,
                    now=now,
                    lease_for=timedelta(minutes=1),
                ),
            )
            assert sorted(claim.code for claim in claims) == ["busy", "claimed"]
            charged = next(claim for claim in claims if claim.code == "claimed")
            assert charged.offer is not None
            recovered = await PostgresPictureRepository(ADMINISTRATOR_ID).claim_hd(
                offer_id,
                user_id=requester,
                cost=10,
                now=now + timedelta(minutes=2),
                lease_for=timedelta(minutes=1),
            )
            assert recovered.code == "claimed"
            assert recovered.offer is not None
            assert recovered.offer.charged_user_id == charged.offer.charged_user_id
            await repository.complete_hd(
                offer_id,
                cost=10,
                now=now + timedelta(minutes=2),
            )
            await repository.complete_hd(
                offer_id,
                cost=10,
                now=now + timedelta(minutes=2),
            )

            balances = await db_connection.fetch_all(
                "SELECT id, coins + coins_paid FROM identity.users WHERE id IN (%s, %s, %s) ORDER BY id",
                (int(requester), int(first_clicker), int(second_clicker)),
            )
            assert int(balances[0][1]) == 95
            assert sorted(int(row[1]) for row in balances[1:]) == [90, 100]
            posting_count = await db_connection.fetch_one(
                "SELECT COUNT(*) FROM economy.stake_pool_postings WHERE idempotency_key IN (%s, %s)",
                posting_keys,
            )
            assert posting_count is not None and int(posting_count[0]) == 2
        finally:
            await db_connection.execute(
                "DELETE FROM media.picture_request_receipts WHERE offer_id = CAST(%s AS UUID)",
                (str(offer_id),),
            )
            await db_connection.execute(
                "DELETE FROM conversation.outbound_messages WHERE conversation_id = %s",
                (f"media-picture-test:{requester}",),
            )
            await db_connection.execute(
                "DELETE FROM media.picture_offers WHERE offer_id = CAST(%s AS UUID)",
                (str(offer_id),),
            )
            await db_connection.execute(
                "DELETE FROM economy.stake_pool_postings WHERE idempotency_key IN (%s, %s)",
                posting_keys,
            )
            await db_connection.execute(
                "DELETE FROM identity.users WHERE id IN (%s, %s, %s)",
                (int(requester), int(first_clicker), int(second_clicker)),
            )
            await db.dispose_current_engine()

    asyncio.run(scenario())


def test_preview_pending_recovers_from_callback_evidence_or_stale_refund() -> None:
    """@brief callback 可确认已见预览，未见预览则租约后幂等退款 / A callback proves preview visibility, while an unseen preview is idempotently refunded after its lease."""

    if os.environ.get("FOGMOE_TEST_POSTGRES") != "1":
        pytest.skip("set FOGMOE_TEST_POSTGRES=1 to run the real PostgreSQL contract")

    async def scenario() -> None:
        """@brief 执行两种 preview_pending 故障恢复分支 / Exercise both preview-pending recovery branches.

        @return None / None.
        """

        suffix = uuid4().int % 100_000_000
        visible_owner = UserId(8_100_000_000 + suffix)
        clicker = UserId(int(visible_owner) + 1)
        stale_owner = UserId(int(visible_owner) + 2)
        visible_offer_id = ArtifactId(uuid4().hex)
        stale_offer_id = ArtifactId(uuid4().hex)
        now = datetime.now(UTC)
        repository = PostgresPictureRepository(ADMINISTRATOR_ID)
        accounts = PostgresMediaAccountProfiles()
        posting_key = f"media:preview:{visible_offer_id}"

        def offer(offer_id: ArtifactId, requester: UserId) -> HdOffer:
            """@brief 构造持久化图片报价 / Build a durable picture offer.

            @param offer_id 报价标识 / Offer identifier.
            @param requester 请求用户 / Requesting user.
            @return 测试报价 / Test offer.
            """

            return HdOffer(
                offer_id=offer_id,
                picture=PictureCandidate(
                    source_id=f"pg-recovery-{offer_id}",
                    sample_url="https://example.test/sample.jpg",
                    file_url="https://example.test/full.jpg",
                    tags="safe",
                    width=100,
                    height=100,
                    file_size=1000,
                    score=1,
                    rating=PictureRating.SAFE,
                ),
                requester_id=requester,
                expires_at=now + timedelta(minutes=30),
            )

        try:
            for user_id in (visible_owner, clicker, stale_owner):
                await db_connection.execute(
                    "INSERT INTO identity.users (id, tg_uid, name, coins) VALUES (%s, %s, %s, 100)",
                    (int(user_id), int(user_id), f"media-recovery-{user_id}"),
                )

            visible_charge = await repository.charge_preview_and_store_offer(
                offer=(visible_offer := offer(visible_offer_id, visible_owner)),
                cost=5,
                now=now,
                **_preview_commit(
                    visible_offer,
                    now=now,
                    key=f"media-recovery:{visible_offer_id}",
                ),
            )
            assert visible_charge.charged
            visible_claim = await repository.claim_hd(
                visible_offer_id,
                user_id=clicker,
                cost=10,
                now=now,
                lease_for=timedelta(minutes=1),
            )
            assert visible_claim.code == "claimed"
            visible_row = await db_connection.fetch_one(
                "SELECT state, preview_refunded FROM media.picture_offers "
                "WHERE offer_id = CAST(%s AS UUID)",
                (str(visible_offer_id),),
            )
            assert visible_row is not None
            assert tuple(visible_row) == ("charged", False)
            posting_count = await db_connection.fetch_one(
                "SELECT COUNT(*) FROM economy.stake_pool_postings WHERE idempotency_key = %s",
                (posting_key,),
            )
            assert posting_count is not None and int(posting_count[0]) == 1

            stale_charge = await repository.charge_preview_and_store_offer(
                offer=(stale_offer := offer(stale_offer_id, stale_owner)),
                cost=5,
                now=now - timedelta(minutes=3),
                **_preview_commit(
                    stale_offer,
                    now=now - timedelta(minutes=3),
                    key=f"media-recovery:{stale_offer_id}",
                ),
            )
            assert stale_charge.charged
            await db_connection.execute(
                "UPDATE conversation.outbound_messages SET status = 'failed_final', "
                "next_attempt_at = NULL, updated_at = %s, last_error = 'test failure' "
                "WHERE message_id = (SELECT outbound_message_id "
                "FROM media.picture_request_receipts WHERE offer_id = CAST(%s AS UUID))",
                (now, str(stale_offer_id)),
            )
            first_profile = await accounts.profile(stale_owner)
            second_profile = await accounts.profile(stale_owner)
            assert first_profile.coins == 100
            assert second_profile.coins == 100
            stale_row = await db_connection.fetch_one(
                "SELECT state, preview_refunded FROM media.picture_offers "
                "WHERE offer_id = CAST(%s AS UUID)",
                (str(stale_offer_id),),
            )
            assert stale_row is not None
            assert tuple(stale_row) == ("refunded", True)
        finally:
            await db_connection.execute(
                "DELETE FROM media.picture_request_receipts "
                "WHERE offer_id IN (CAST(%s AS UUID), CAST(%s AS UUID))",
                (str(visible_offer_id), str(stale_offer_id)),
            )
            await db_connection.execute(
                "DELETE FROM conversation.outbound_messages WHERE conversation_id IN (%s, %s)",
                (
                    f"media-picture-test:{visible_owner}",
                    f"media-picture-test:{stale_owner}",
                ),
            )
            await db_connection.execute(
                "DELETE FROM media.picture_offers WHERE offer_id IN (CAST(%s AS UUID), CAST(%s AS UUID))",
                (str(visible_offer_id), str(stale_offer_id)),
            )
            await db_connection.execute(
                "DELETE FROM economy.stake_pool_postings WHERE idempotency_key = %s",
                (posting_key,),
            )
            await db_connection.execute(
                "DELETE FROM identity.users WHERE id IN (%s, %s, %s)",
                (int(visible_owner), int(clicker), int(stale_owner)),
            )
            await db.dispose_current_engine()

    asyncio.run(scenario())


def test_picture_receipt_serializes_concurrency_and_rolls_back_outbox_failure() -> None:
    """@brief 并发重放只扣一次，outbox 故障回滚所有写 / Concurrent replay charges once and outbox failure rolls every write back."""

    if os.environ.get("FOGMOE_TEST_POSTGRES") != "1":
        pytest.skip("set FOGMOE_TEST_POSTGRES=1 to run the real PostgreSQL contract")

    class FailingOutbox:
        """@brief 在 offer 写入后故障的 outbox 替身 / Outbox double failing after the offer write."""

        async def enqueue_standalone_outbound_in_transaction(
            self,
            connection,
            draft,
        ):
            raise RuntimeError("injected outbox failure")

    async def scenario() -> None:
        suffix = uuid4().int % 100_000_000
        concurrent_user = UserId(8_200_000_000 + suffix)
        failing_user = UserId(int(concurrent_user) + 1)
        first_offer_id = ArtifactId(uuid4().hex)
        second_offer_id = ArtifactId(uuid4().hex)
        failed_offer_id = ArtifactId(uuid4().hex)
        now = datetime.now(UTC)
        key = f"media-concurrent:{uuid4().hex}"
        fingerprint = "a" * 64
        repository = PostgresPictureRepository(ADMINISTRATOR_ID)
        accounts = PostgresMediaAccountProfiles()

        def offer(offer_id: ArtifactId, requester: UserId) -> HdOffer:
            return HdOffer(
                offer_id=offer_id,
                picture=PictureCandidate(
                    source_id=f"source-{offer_id}",
                    sample_url=f"https://example.test/{offer_id}.jpg",
                    file_url=f"https://example.test/{offer_id}-full.jpg",
                    tags="safe",
                    width=100,
                    height=100,
                    file_size=1000,
                    score=1,
                    rating=PictureRating.SAFE,
                ),
                requester_id=requester,
                expires_at=now + timedelta(minutes=30),
            )

        first_offer = offer(first_offer_id, concurrent_user)
        second_offer = offer(second_offer_id, concurrent_user)
        failed_offer = offer(failed_offer_id, failing_user)
        first_commit = _preview_commit(first_offer, now=now, key=key)
        second_commit = _preview_commit(second_offer, now=now, key=key)
        try:
            for user_id in (concurrent_user, failing_user):
                await db_connection.execute(
                    "INSERT INTO identity.users (id, tg_uid, name, coins) "
                    "VALUES (%s, %s, %s, 100)",
                    (int(user_id), int(user_id), f"media-receipt-{user_id}"),
                )

            results = await asyncio.gather(
                repository.charge_preview_and_store_offer(
                    offer=first_offer,
                    cost=5,
                    now=now,
                    **first_commit,
                ),
                repository.charge_preview_and_store_offer(
                    offer=second_offer,
                    cost=5,
                    now=now,
                    **second_commit,
                ),
            )
            assert all(result.charged for result in results)
            assert sum(result.replayed for result in results) == 1
            assert results[0].offer == results[1].offer
            account = await db_connection.fetch_one(
                "SELECT coins + coins_paid FROM identity.users WHERE id = %s",
                (int(concurrent_user),),
            )
            counts = await db_connection.fetch_one(
                "SELECT "
                "(SELECT COUNT(*) FROM media.picture_offers WHERE requester_id = %s), "
                "(SELECT COUNT(*) FROM media.picture_request_receipts WHERE requester_id = %s), "
                "(SELECT COUNT(*) FROM conversation.outbound_messages "
                "WHERE conversation_id = %s)",
                (
                    int(concurrent_user),
                    int(concurrent_user),
                    f"media-picture-test:{concurrent_user}",
                ),
            )
            assert account is not None and int(account[0]) == 95
            assert counts is not None and tuple(int(value) for value in counts) == (
                1,
                1,
                1,
            )
            with pytest.raises(PictureReceiptConflict):
                await repository.load_picture_receipt(
                    idempotency_key=key,
                    user_id=concurrent_user,
                    rating=PictureRating.NSFW,
                    request_fingerprint=fingerprint,
                )

            canonical_offer = results[0].offer
            assert canonical_offer is not None
            await db_connection.execute(
                "UPDATE media.picture_offers SET preview_confirm_by = %s "
                "WHERE offer_id = CAST(%s AS UUID)",
                (now - timedelta(seconds=1), str(canonical_offer.offer_id)),
            )
            await db_connection.execute(
                "UPDATE conversation.outbound_messages SET status = 'delivered', "
                "next_attempt_at = NULL, delivered_at = %s, updated_at = %s, "
                "external_message_id = '42' WHERE message_id = ("
                "SELECT outbound_message_id FROM media.picture_request_receipts "
                "WHERE idempotency_key = %s)",
                (now, now, key),
            )
            settled_profile = await accounts.profile(concurrent_user)
            settled_offer = await db_connection.fetch_one(
                "SELECT state FROM media.picture_offers WHERE offer_id = CAST(%s AS UUID)",
                (str(canonical_offer.offer_id),),
            )
            preview_posting = await db_connection.fetch_one(
                "SELECT COUNT(*) FROM economy.stake_pool_postings "
                "WHERE idempotency_key = %s",
                (f"media:preview:{canonical_offer.offer_id}",),
            )
            assert settled_profile.coins == 95
            assert settled_offer is not None and str(settled_offer[0]) == "available"
            assert preview_posting is not None and int(preview_posting[0]) == 1

            with pytest.raises(RuntimeError, match="injected outbox failure"):
                await PostgresPictureRepository(
                    ADMINISTRATOR_ID, outbox=FailingOutbox()
                ).charge_preview_and_store_offer(
                    offer=failed_offer,
                    cost=5,
                    now=now,
                    **_preview_commit(
                        failed_offer,
                        now=now,
                        key=f"media-failure:{failed_offer_id}",
                    ),
                )
            failed_account = await db_connection.fetch_one(
                "SELECT coins + coins_paid FROM identity.users WHERE id = %s",
                (int(failing_user),),
            )
            failed_counts = await db_connection.fetch_one(
                "SELECT "
                "(SELECT COUNT(*) FROM media.picture_offers WHERE requester_id = %s), "
                "(SELECT COUNT(*) FROM media.picture_request_receipts WHERE requester_id = %s)",
                (int(failing_user), int(failing_user)),
            )
            assert failed_account is not None and int(failed_account[0]) == 100
            assert failed_counts is not None and tuple(
                int(value) for value in failed_counts
            ) == (0, 0)
        finally:
            await db_connection.execute(
                "DELETE FROM media.picture_request_receipts WHERE requester_id IN (%s, %s)",
                (int(concurrent_user), int(failing_user)),
            )
            await db_connection.execute(
                "DELETE FROM conversation.outbound_messages WHERE conversation_id IN (%s, %s)",
                (
                    f"media-picture-test:{concurrent_user}",
                    f"media-picture-test:{failing_user}",
                ),
            )
            await db_connection.execute(
                "DELETE FROM media.picture_offers WHERE requester_id IN (%s, %s)",
                (int(concurrent_user), int(failing_user)),
            )
            await db_connection.execute(
                "DELETE FROM economy.stake_pool_postings "
                "WHERE idempotency_key IN (%s, %s, %s)",
                (
                    f"media:preview:{first_offer_id}",
                    f"media:preview:{second_offer_id}",
                    f"media:preview:{failed_offer_id}",
                ),
            )
            await db_connection.execute(
                "DELETE FROM identity.users WHERE id IN (%s, %s)",
                (int(concurrent_user), int(failing_user)),
            )
            await db.dispose_current_engine()

    asyncio.run(scenario())
