"""PostgreSQL 媒体账户准入与图片预览恢复 / PostgreSQL media admission and picture-preview recovery."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from fogmoe_bot.domain.media.identifiers import UserId
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database.repositories import user_repository

from .common import credit_reward_pool


@dataclass(frozen=True, slots=True)
class MediaUserSnapshot:
    """媒体准入账户快照 / Media-admission account snapshot."""

    registered: bool
    permission: int
    coins: int


class PostgresMediaAccountProfiles:
    """读取账户并推进既有图片预览确认窗口 / Read accounts and advance established picture-preview confirmation windows."""

    async def profile(self, user_id: UserId) -> MediaUserSnapshot:
        """读取准入快照并原子恢复过期预览 / Read admission and atomically recover stale previews."""

        async with db_connection.transaction() as connection:
            account = await user_repository.fetch_user_account(
                int(user_id),
                connection=connection,
                for_update=True,
            )
            if account is None:
                return MediaUserSnapshot(False, 0, 0)
            now = datetime.now(UTC)
            stale = await db_connection.fetch_all(
                "SELECT offer.offer_id, offer.preview_cost, receipt.idempotency_key, outbound.status "
                "FROM media.picture_offers AS offer "
                "LEFT JOIN media.picture_request_receipts AS receipt "
                "ON receipt.offer_id = offer.offer_id "
                "LEFT JOIN conversation.outbound_messages AS outbound "
                "ON outbound.message_id = receipt.outbound_message_id "
                "WHERE offer.requester_id = %s AND offer.state = 'preview_pending' "
                "AND offer.preview_confirm_by <= %s ORDER BY offer.offer_id FOR UPDATE OF offer",
                (int(user_id), now),
                connection=connection,
            )
            refund = 0
            for row in stale:
                offer_id = UUID(str(row[0])).hex
                preview_cost = int(str(row[1]))
                receipt_key = str(row[2]) if row[2] is not None else None
                outbound_status = str(row[3]) if row[3] is not None else None
                if receipt_key is None or outbound_status in {
                    "failed_final",
                    "cancelled",
                }:
                    refund += preview_cost
                    await db_connection.execute(
                        "UPDATE media.picture_offers SET state = 'refunded', "
                        "preview_refunded = TRUE, updated_at = %s "
                        "WHERE offer_id = CAST(%s AS UUID)",
                        (now, offer_id),
                        connection=connection,
                    )
                    continue
                if outbound_status == "delivered":
                    await credit_reward_pool(
                        preview_cost,
                        idempotency_key=f"media:preview:{offer_id}",
                        connection=connection,
                    )
                    await db_connection.execute(
                        "UPDATE media.picture_offers SET state = 'available', updated_at = %s "
                        "WHERE offer_id = CAST(%s AS UUID)",
                        (now, offer_id),
                        connection=connection,
                    )
                    continue
                if outbound_status in {"pending", "processing", "retry_wait"}:
                    continue
                if outbound_status is None:
                    raise RuntimeError(
                        "Picture receipt references a missing outbound message"
                    )
                raise RuntimeError(
                    f"Picture receipt has unknown outbound status {outbound_status}"
                )
            if refund > 0:
                await user_repository.add_free_coins(
                    int(user_id),
                    refund,
                    connection=connection,
                )
            return MediaUserSnapshot(
                True,
                account.permission,
                account.total_coins + refund,
            )
