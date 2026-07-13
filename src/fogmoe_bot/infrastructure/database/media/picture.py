"""PostgreSQL 图片报价、回执与扣退款适配器 / PostgreSQL picture offer, receipt, charge, and refund adapter."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.domain.conversation.outbox import (
    SEND_TELEGRAM_PHOTO,
    OutboundDraft,
)
from fogmoe_bot.domain.media.identifiers import ArtifactId, UserId
from fogmoe_bot.domain.media.picture import (
    HdOffer,
    HdOfferState,
    PictureCandidate,
    PictureRating,
    PictureReceiptConflict,
)
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database.conversation_workflow.outbox import (
    PostgresOutboxRepository,
    StandaloneOutboxWriter,
)
from fogmoe_bot.infrastructure.database.repositories import user_repository

from .common import credit_reward_pool, spend, utc


_PREVIEW_CONFIRM_LEASE = timedelta(minutes=2)


@dataclass(frozen=True, slots=True)
class PreviewChargeResult:
    """@brief 预览扣费结果 / Preview-charge result.

    @param charged 是否成功 / Whether charging succeeded.
    @param balance 事务观察余额 / Balance observed by the transaction.
    """

    charged: bool
    balance: int
    offer: HdOffer | None = None
    replayed: bool = False
    cost: int | None = None


@dataclass(frozen=True, slots=True)
class HdClaimResult:
    """@brief 高清领取结果 / HD-claim result.

    @param code 稳定结果代码 / Stable outcome code.
    @param offer 已领取报价 / Claimed offer.
    @param balance 金币不足时余额 / Balance when insufficient.
    """

    code: str
    offer: HdOffer | None = None
    balance: int | None = None


class PostgresPictureRepository:
    """以短事务持久化图片 callback 与精确一次扣退款 / Persist picture callbacks and exactly-once charges/refunds with short transactions."""

    def __init__(
        self,
        administrator_id: int,
        outbox: StandaloneOutboxWriter | None = None,
    ) -> None:
        """@brief 注入管理员身份与共享 outbox 原语 / Inject administrator identity and shared transactional outbox primitive.

        @param administrator_id 管理员 Telegram 用户 ID / Administrator Telegram user ID.
        @param outbox 可选的同事务 outbox writer / Optional same-transaction outbox writer.
        @return None / None.
        @raise TypeError 管理员 ID 不是严格整数时抛出 /
            Raised when the administrator ID is not a strict integer.
        """

        if isinstance(administrator_id, bool) or not isinstance(administrator_id, int):
            raise TypeError("administrator_id must be an integer")
        self._administrator_id = administrator_id
        """@brief 用于套餐判定的管理员 ID / Administrator ID used for plan selection."""
        self._outbox = outbox or PostgresOutboxRepository()

    async def charge_preview_and_store_offer(
        self,
        *,
        offer: HdOffer,
        cost: int,
        now: datetime,
        idempotency_key: str,
        request_fingerprint: str,
        outbound: OutboundDraft,
    ) -> PreviewChargeResult:
        """@brief 原子扣除预览费用并保存报价 / Atomically charge the preview and persist the offer.

        @param offer 报价 / Offer.
        @param cost 金币成本 / Coin cost.
        @param now 当前时间 / Current instant.
        @return 扣费结果 / Charge result.
        """

        timestamp = utc(now)
        _validate_picture_request_key(idempotency_key, request_fingerprint)
        async with db_connection.transaction() as connection:
            await _lock_picture_receipt(idempotency_key, connection)
            replay = await _load_picture_receipt(
                idempotency_key=idempotency_key,
                user_id=offer.requester_id,
                rating=offer.picture.rating,
                request_fingerprint=request_fingerprint,
                connection=connection,
            )
            if replay is not None:
                return replay
            account = await user_repository.fetch_user_account(
                int(offer.requester_id),
                connection=connection,
                for_update=True,
            )
            balance = account.total_coins if account is not None else 0
            if account is None or balance < cost:
                return PreviewChargeResult(False, balance, cost=cost)
            await spend(
                account,
                cost=cost,
                connection=connection,
                administrator_id=self._administrator_id,
            )
            picture = offer.picture
            await db_connection.execute(
                "INSERT INTO media.picture_offers "
                "(offer_id, source_id, sample_url, file_url, tags, width, height, file_size, score, "
                "rating, requester_id, expires_at, state, charged_user_id, claim_expires_at, "
                "preview_cost, hd_cost, preview_confirm_by, preview_refunded, hd_refunded, created_at, updated_at) "
                "VALUES (CAST(%s AS UUID), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "'preview_pending', NULL, NULL, %s, NULL, %s, FALSE, FALSE, %s, %s)",
                (
                    str(offer.offer_id),
                    picture.source_id,
                    picture.sample_url,
                    picture.file_url,
                    picture.tags,
                    picture.width,
                    picture.height,
                    picture.file_size,
                    picture.score,
                    picture.rating.value,
                    int(offer.requester_id),
                    utc(offer.expires_at),
                    cost,
                    timestamp + _PREVIEW_CONFIRM_LEASE,
                    timestamp,
                    timestamp,
                ),
                connection=connection,
            )
            await self._outbox.enqueue_standalone_outbound_in_transaction(
                connection,
                outbound,
            )
            result = PreviewChargeResult(
                True,
                balance - cost,
                offer=offer,
                cost=cost,
            )
            await _save_picture_receipt(
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
                result=result,
                outbound=outbound,
                connection=connection,
            )
            await db_connection.execute(
                "WITH stale AS ("
                "SELECT offer_id FROM media.picture_offers WHERE requester_id = %s "
                "AND (state IN ('delivered', 'refunded') OR "
                "(state IN ('preview_pending', 'available') AND expires_at <= %s)) "
                "ORDER BY created_at DESC OFFSET 100) "
                "DELETE FROM media.picture_offers WHERE offer_id IN (SELECT offer_id FROM stale)",
                (int(offer.requester_id), timestamp),
                connection=connection,
            )
            return result

    async def load_picture_receipt(
        self,
        *,
        idempotency_key: str,
        user_id: UserId,
        rating: PictureRating,
        request_fingerprint: str,
    ) -> PreviewChargeResult | None:
        """@brief 读取并校验首次已提交预览 / Load and validate the first committed preview."""

        _validate_picture_request_key(idempotency_key, request_fingerprint)
        return await _load_picture_receipt(
            idempotency_key=idempotency_key,
            user_id=user_id,
            rating=rating,
            request_fingerprint=request_fingerprint,
            connection=None,
        )

    async def claim_hd(
        self,
        offer_id: ArtifactId,
        *,
        user_id: UserId,
        cost: int,
        now: datetime,
        lease_for: timedelta,
    ) -> HdClaimResult:
        """@brief 原子领取高清报价与扣费 / Atomically claim and charge an HD offer.

        @param offer_id 报价标识 / Offer identifier.
        @param user_id 点击用户 / Clicking user.
        @param cost 高清成本 / HD cost.
        @param now 当前时间 / Current instant.
        @param lease_for 故障恢复租约 / Failure-recovery lease.
        @return 领取结果 / Claim result.
        """

        timestamp = utc(now)
        async with db_connection.transaction() as connection:
            account = await user_repository.fetch_user_account(
                int(user_id),
                connection=connection,
                for_update=True,
            )
            row = await _offer_row(offer_id, connection=connection, for_update=True)
            if row is None:
                return HdClaimResult("missing")
            offer = _offer_from_row(row)
            claim_expires_at = cast(datetime | None, row[14])
            if offer.state is HdOfferState.DELIVERED:
                return HdClaimResult("delivered")
            if offer.state is HdOfferState.REFUNDED:
                return HdClaimResult("missing")
            if offer.state is HdOfferState.PREVIEW_PENDING:
                if offer.expires_at <= timestamp:
                    return HdClaimResult("expired")
                await credit_reward_pool(
                    int(str(row[18])),
                    idempotency_key=f"media:preview:{offer_id}",
                    connection=connection,
                )
                await db_connection.execute(
                    "UPDATE media.picture_offers SET state = 'available', updated_at = %s "
                    "WHERE offer_id = CAST(%s AS UUID)",
                    (timestamp, str(offer_id)),
                    connection=connection,
                )
                offer = HdOffer(
                    offer_id=offer.offer_id,
                    picture=offer.picture,
                    requester_id=offer.requester_id,
                    expires_at=offer.expires_at,
                    state=HdOfferState.AVAILABLE,
                )
            if offer.state is HdOfferState.CHARGED:
                if claim_expires_at is not None and utc(claim_expires_at) > timestamp:
                    return HdClaimResult("busy")
                await db_connection.execute(
                    "UPDATE media.picture_offers SET claim_expires_at = %s, updated_at = %s "
                    "WHERE offer_id = CAST(%s AS UUID)",
                    (timestamp + lease_for, timestamp, str(offer_id)),
                    connection=connection,
                )
                return HdClaimResult("claimed", offer)
            if offer.expires_at <= timestamp:
                return HdClaimResult("expired")

            balance = account.total_coins if account is not None else 0
            if account is None or balance < cost:
                return HdClaimResult("insufficient", balance=balance)
            await spend(
                account,
                cost=cost,
                connection=connection,
                administrator_id=self._administrator_id,
            )
            await db_connection.execute(
                "UPDATE media.picture_offers SET state = 'charged', charged_user_id = %s, hd_cost = %s, "
                "claim_expires_at = %s, updated_at = %s WHERE offer_id = CAST(%s AS UUID)",
                (
                    int(user_id),
                    cost,
                    timestamp + lease_for,
                    timestamp,
                    str(offer_id),
                ),
                connection=connection,
            )
            return HdClaimResult(
                "claimed",
                HdOffer(
                    offer_id=offer.offer_id,
                    picture=offer.picture,
                    requester_id=offer.requester_id,
                    expires_at=offer.expires_at,
                    state=HdOfferState.CHARGED,
                    charged_user_id=user_id,
                ),
            )

    async def complete_hd(
        self,
        offer_id: ArtifactId,
        *,
        cost: int,
        now: datetime,
    ) -> None:
        """@brief 幂等确认高清投递 / Idempotently confirm HD delivery.

        @param offer_id 报价标识 / Offer identifier.
        @param cost 已收金币 / Charged coins.
        @param now 完成时间 / Completion instant.
        @return None / None.
        """

        async with db_connection.transaction() as connection:
            row = await _offer_row(offer_id, connection=connection, for_update=True)
            if row is None:
                raise LookupError("picture offer does not exist")
            offer = _offer_from_row(row)
            if offer.state is HdOfferState.DELIVERED:
                return
            if offer.state is not HdOfferState.CHARGED:
                raise RuntimeError(f"cannot complete HD from {offer.state.value}")
            persisted_cost = int(str(row[19]))
            await credit_reward_pool(
                persisted_cost,
                idempotency_key=f"media:hd:{offer_id}",
                connection=connection,
            )
            await db_connection.execute(
                "UPDATE media.picture_offers SET state = 'delivered', claim_expires_at = NULL, "
                "updated_at = %s WHERE offer_id = CAST(%s AS UUID)",
                (utc(now), str(offer_id)),
                connection=connection,
            )

    async def refund_hd(
        self,
        offer_id: ArtifactId,
        *,
        cost: int,
        now: datetime,
    ) -> None:
        """@brief 幂等退款高清领取 / Idempotently refund an HD claim.

        @param offer_id 报价标识 / Offer identifier.
        @param cost 退款金币 / Refunded coins.
        @param now 当前时间 / Current instant.
        @return None / None.
        """

        owner = await db_connection.fetch_one(
            "SELECT charged_user_id FROM media.picture_offers "
            "WHERE offer_id = CAST(%s AS UUID)",
            (str(offer_id),),
        )
        if owner is None or owner[0] is None:
            return
        charged_user_id = int(str(owner[0]))
        async with db_connection.transaction() as connection:
            await user_repository.fetch_user_account(
                charged_user_id,
                connection=connection,
                for_update=True,
            )
            row = await _offer_row(offer_id, connection=connection, for_update=True)
            if row is None:
                return
            offer = _offer_from_row(row)
            hd_refunded = bool(row[17])
            if hd_refunded or offer.state is HdOfferState.REFUNDED:
                return
            if offer.state is not HdOfferState.CHARGED or offer.charged_user_id is None:
                return
            persisted_cost = int(str(row[19]))
            await user_repository.add_free_coins(
                charged_user_id,
                persisted_cost,
                connection=connection,
            )
            await db_connection.execute(
                "UPDATE media.picture_offers SET state = 'refunded', hd_refunded = TRUE, "
                "claim_expires_at = NULL, updated_at = %s WHERE offer_id = CAST(%s AS UUID)",
                (utc(now), str(offer_id)),
                connection=connection,
            )


def _validate_picture_request_key(key: str, fingerprint: str) -> None:
    """@brief 校验图片 source receipt 键 / Validate picture source-receipt keys."""

    if not key.strip() or len(key) > 200:
        raise ValueError("Picture idempotency key must be 1..200 characters")
    if len(fingerprint) != 64 or any(
        character not in "0123456789abcdef" for character in fingerprint
    ):
        raise ValueError("Picture request fingerprint must be lowercase SHA-256")


async def _lock_picture_receipt(
    key: str,
    connection: AsyncConnection,
) -> None:
    """@brief 串行化同一图片 source key / Serialize one picture source key."""

    await db_connection.fetch_one(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"media-picture-receipt:{key}",),
        connection=connection,
    )


async def _load_picture_receipt(
    *,
    idempotency_key: str,
    user_id: UserId,
    rating: PictureRating,
    request_fingerprint: str,
    connection: AsyncConnection | None,
) -> PreviewChargeResult | None:
    """@brief 读取并校验图片请求回执 / Load and validate a picture-request receipt."""

    row = await db_connection.fetch_one(
        "SELECT requester_id, rating, request_fingerprint, offer_id, result "
        "FROM media.picture_request_receipts WHERE idempotency_key = %s",
        (idempotency_key,),
        connection=connection,
    )
    if row is None:
        return None
    if (
        int(str(row[0])) != int(user_id)
        or str(row[1]) != rating.value
        or str(row[2]) != request_fingerprint
    ):
        raise PictureReceiptConflict(
            "Picture idempotency key changed ownership, rating, or delivery semantics"
        )
    raw_result: object = row[4]
    decoded: object = (
        json.loads(raw_result) if isinstance(raw_result, str | bytes) else raw_result
    )
    if not isinstance(decoded, Mapping):
        raise ValueError("Invalid picture-request receipt result")
    result = cast(Mapping[str, object], decoded)
    if set(result) != {"balance", "cost", "offer"}:
        raise ValueError("Invalid picture-request receipt fields")
    balance = _required_nonnegative_int(result["balance"], "balance")
    cost = _required_positive_int(result["cost"], "cost")
    offer = _offer_from_receipt(result["offer"])
    if str(offer.offer_id) != UUID(str(row[3])).hex:
        raise ValueError("Picture receipt offer identity is inconsistent")
    if offer.requester_id != user_id or offer.picture.rating is not rating:
        raise ValueError("Picture receipt offer semantics are inconsistent")
    return PreviewChargeResult(
        True,
        balance,
        offer=offer,
        replayed=True,
        cost=cost,
    )


async def _save_picture_receipt(
    *,
    idempotency_key: str,
    request_fingerprint: str,
    result: PreviewChargeResult,
    outbound: OutboundDraft,
    connection: AsyncConnection,
) -> None:
    """@brief 在扣费与 outbox 同事务保存规范结果 / Save the canonical result with the charge and outbox."""

    offer = result.offer
    if not result.charged or offer is None or result.cost is None:
        raise ValueError("Only a successful picture charge can be receipted")
    if outbound.turn_id is not None or outbound.kind != SEND_TELEGRAM_PHOTO:
        raise ValueError("Picture receipts require a standalone photo outbound")
    payload = {
        "balance": result.balance,
        "cost": result.cost,
        "offer": _offer_receipt_mapping(offer),
    }
    await db_connection.execute(
        "INSERT INTO media.picture_request_receipts "
        "(idempotency_key, requester_id, rating, request_fingerprint, offer_id, "
        "outbound_message_id, result, created_at) "
        "VALUES (%s, %s, %s, %s, CAST(%s AS UUID), CAST(%s AS UUID), "
        "CAST(%s AS JSONB), %s)",
        (
            idempotency_key,
            int(offer.requester_id),
            offer.picture.rating.value,
            request_fingerprint,
            str(offer.offer_id),
            str(outbound.message_id),
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            outbound.created_at,
        ),
        connection=connection,
    )


def _offer_receipt_mapping(offer: HdOffer) -> Mapping[str, object]:
    """@brief 序列化可脱离可清理 offer 行重放的快照 / Serialize a replayable snapshot independent of offer retention."""

    picture = offer.picture
    return {
        "offer_id": str(offer.offer_id),
        "requester_id": int(offer.requester_id),
        "expires_at": utc(offer.expires_at).isoformat(),
        "state": offer.state.value,
        "charged_user_id": (
            int(offer.charged_user_id) if offer.charged_user_id is not None else None
        ),
        "picture": {
            "source_id": picture.source_id,
            "sample_url": picture.sample_url,
            "file_url": picture.file_url,
            "tags": picture.tags,
            "width": picture.width,
            "height": picture.height,
            "file_size": picture.file_size,
            "score": picture.score,
            "rating": picture.rating.value,
        },
    }


def _offer_from_receipt(value: object) -> HdOffer:
    """@brief 从严格 JSON 快照恢复报价 / Restore an offer from a strict JSON snapshot."""

    if not isinstance(value, Mapping):
        raise ValueError("Invalid picture receipt offer")
    offer = cast(Mapping[str, object], value)
    expected_offer_keys = {
        "offer_id",
        "requester_id",
        "expires_at",
        "state",
        "charged_user_id",
        "picture",
    }
    if set(offer) != expected_offer_keys:
        raise ValueError("Invalid picture receipt offer fields")
    raw_picture = offer["picture"]
    if not isinstance(raw_picture, Mapping):
        raise ValueError("Invalid picture receipt picture")
    picture = cast(Mapping[str, object], raw_picture)
    expected_picture_keys = {
        "source_id",
        "sample_url",
        "file_url",
        "tags",
        "width",
        "height",
        "file_size",
        "score",
        "rating",
    }
    if set(picture) != expected_picture_keys:
        raise ValueError("Invalid picture receipt picture fields")
    source_id = _required_text(picture["source_id"], "source_id")
    tags = _required_text(picture["tags"], "tags", allow_empty=True)
    sample_url = _optional_text(picture["sample_url"], "sample_url")
    file_url = _optional_text(picture["file_url"], "file_url")
    expires_at_raw = _required_text(offer["expires_at"], "expires_at")
    try:
        expires_at = utc(datetime.fromisoformat(expires_at_raw))
        state = HdOfferState(_required_text(offer["state"], "state"))
        picture_rating = PictureRating(_required_text(picture["rating"], "rating"))
        offer_id = ArtifactId(UUID(_required_text(offer["offer_id"], "offer_id")).hex)
    except (ValueError, TypeError) as error:
        raise ValueError("Invalid picture receipt enum, UUID, or time") from error
    requester_id = UserId(_required_positive_int(offer["requester_id"], "requester_id"))
    charged_raw = offer["charged_user_id"]
    charged_user_id = (
        UserId(_required_positive_int(charged_raw, "charged_user_id"))
        if charged_raw is not None
        else None
    )
    return HdOffer(
        offer_id=offer_id,
        picture=PictureCandidate(
            source_id=source_id,
            sample_url=sample_url,
            file_url=file_url,
            tags=tags,
            width=_optional_positive_int(picture["width"], "width"),
            height=_optional_positive_int(picture["height"], "height"),
            file_size=_optional_positive_int(picture["file_size"], "file_size"),
            score=_optional_int(picture["score"], "score"),
            rating=picture_rating,
        ),
        requester_id=requester_id,
        expires_at=expires_at,
        state=state,
        charged_user_id=charged_user_id,
    )


def _required_text(value: object, field: str, *, allow_empty: bool = False) -> str:
    """@brief 校验 receipt 文本 / Validate receipt text."""

    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError(f"Picture receipt {field} must be text")
    return value


def _optional_text(value: object, field: str) -> str | None:
    """@brief 校验可选 receipt 文本 / Validate optional receipt text."""

    if value is None:
        return None
    return _required_text(value, field)


def _required_nonnegative_int(value: object, field: str) -> int:
    """@brief 校验非负整数 / Validate a non-negative integer."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Picture receipt {field} must be a non-negative integer")
    return value


def _required_positive_int(value: object, field: str) -> int:
    """@brief 校验正整数 / Validate a positive integer."""

    parsed = _required_nonnegative_int(value, field)
    if parsed < 1:
        raise ValueError(f"Picture receipt {field} must be positive")
    return parsed


def _optional_positive_int(value: object, field: str) -> int | None:
    """@brief 校验可选正整数 / Validate an optional positive integer."""

    return None if value is None else _required_positive_int(value, field)


def _optional_int(value: object, field: str) -> int | None:
    """@brief 校验可选整数 / Validate an optional integer."""

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Picture receipt {field} must be an integer")
    return value


async def _offer_row(
    offer_id: ArtifactId,
    *,
    connection: AsyncConnection,
    for_update: bool,
) -> tuple[object, ...] | None:
    """@brief 读取完整报价行 / Read a complete offer row.

    @param offer_id 报价标识 / Offer identifier.
    @param connection 事务连接 / Transaction connection.
    @param for_update 是否行锁 / Whether to row-lock.
    @return 行或 None / Row or None.
    """

    lock = " FOR UPDATE" if for_update else ""
    row = await db_connection.fetch_one(
        "SELECT offer_id, source_id, sample_url, file_url, tags, width, height, file_size, score, "
        "rating, requester_id, expires_at, state, charged_user_id, claim_expires_at, "
        "created_at, preview_refunded, hd_refunded, preview_cost, hd_cost, preview_confirm_by "
        "FROM media.picture_offers "
        f"WHERE offer_id = CAST(%s AS UUID){lock}",
        (str(offer_id),),
        connection=connection,
    )
    return tuple(row) if row is not None else None


def _offer_from_row(row: tuple[object, ...]) -> HdOffer:
    """@brief 数据库行转报价 / Convert a database row to an offer.

    @param row 完整报价行 / Complete offer row.
    @return 领域报价 / Domain offer.
    """

    return HdOffer(
        offer_id=ArtifactId(UUID(str(row[0])).hex),
        picture=PictureCandidate(
            source_id=str(row[1]),
            sample_url=str(row[2]) if row[2] is not None else None,
            file_url=str(row[3]) if row[3] is not None else None,
            tags=str(row[4] or ""),
            width=int(str(row[5])) if row[5] is not None else None,
            height=int(str(row[6])) if row[6] is not None else None,
            file_size=int(str(row[7])) if row[7] is not None else None,
            score=int(str(row[8])) if row[8] is not None else None,
            rating=PictureRating(str(row[9])),
        ),
        requester_id=UserId(int(str(row[10]))),
        expires_at=utc(cast(datetime, row[11])),
        state=HdOfferState(str(row[12])),
        charged_user_id=UserId(int(str(row[13]))) if row[13] is not None else None,
    )
