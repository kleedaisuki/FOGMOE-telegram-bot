"""@brief 银行申请通知的同事务 outbox 写入器 / Same-transaction outbox writer for bank-request notifications."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.domain.banking.requests import TokenRequest, TokenRequestStatus
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    OutboundMessageId,
)
from fogmoe_bot.domain.conversation.outbox import (
    SEND_TELEGRAM_MESSAGE,
    OutboundDraft,
)
from fogmoe_bot.infrastructure.database.conversation_workflow.outbox import (
    StandaloneOutboxWriter,
)


class BankTokenRequestNotificationWriter(Protocol):
    """@brief 银行申请状态与 outbox 同事务落盘端口 / Port for persisting bank-request state and outbox intents in one transaction."""

    async def enqueue_request_created(
        self,
        request: TokenRequest,
        *,
        connection: AsyncConnection,
    ) -> None:
        """@brief 写入待审核申请的管理员提醒 / Persist an administrator reminder for a pending request.

        @param request 规范待审核申请 / Canonical pending token request.
        @param connection 调用方拥有的活动事务 / Caller-owned active transaction.
        @return None / None.
        """

        ...

    async def enqueue_request_reviewed(
        self,
        request: TokenRequest,
        *,
        connection: AsyncConnection,
    ) -> None:
        """@brief 写入已决申请的申请人回执 / Persist a requester receipt for a resolved request.

        @param request 规范审核终态申请 / Canonical terminal token request.
        @param connection 调用方拥有的活动事务 / Caller-owned active transaction.
        @return None / None.
        """

        ...


class PostgresBankTokenRequestNotificationWriter:
    """@brief 使用既有 transactional outbox 投递银行通知 / Use the existing transactional outbox for bank notifications."""

    def __init__(
        self,
        *,
        outbox: StandaloneOutboxWriter,
        administrator_id: int,
    ) -> None:
        """@brief 注入同事务 outbox 与唯一银行管理员 / Inject the same-transaction outbox and sole bank administrator.

        @param outbox 可在调用方 connection 内写入的 outbox 端口 /
            Outbox port writable inside the caller-owned connection.
        @param administrator_id 管理员 Telegram 用户 ID / Administrator Telegram user ID.
        @return None / None.
        @raise ValueError 管理员 ID 非法时抛出 / Raised when the administrator ID is invalid.
        """

        if isinstance(administrator_id, bool) or administrator_id <= 0:
            raise ValueError("Bank notification administrator must be positive")
        self._outbox = outbox
        """@brief 同事务 standalone outbox 原语 / Same-transaction standalone-outbox primitive."""
        self._administrator_id = administrator_id
        """@brief 唯一银行管理员身份 / Sole bank-administrator identity."""

    async def enqueue_request_created(
        self,
        request: TokenRequest,
        *,
        connection: AsyncConnection,
    ) -> None:
        """@brief 在申请创建事务中写入管理员提醒 / Write the administrator reminder in the request-creation transaction.

        @param request 待审核申请 / Pending token request.
        @param connection 调用方拥有的活动事务 / Caller-owned active transaction.
        @return None / None.
        @raise ValueError 申请不是待审核状态时抛出 / Raised when the request is not pending.
        @note 此方法绝不调用 Telegram Bot API；事务提交后由 outbox worker 异步投递。
            / This method never calls the Telegram Bot API; an outbox worker delivers it after
            the transaction commits.
        """

        if request.status is not TokenRequestStatus.PENDING:
            raise ValueError("Administrator notification requires a pending token request")
        idempotency_key = (
            f"request:{request.request_id}:administrator-review-notification"
        )
        await self._enqueue(
            recipient_id=self._administrator_id,
            request=request,
            text=_administrator_request_notification_text(request),
            idempotency_key=idempotency_key,
            created_at=request.requested_at,
            connection=connection,
        )

    async def enqueue_request_reviewed(
        self,
        request: TokenRequest,
        *,
        connection: AsyncConnection,
    ) -> None:
        """@brief 在审核事务中写入申请人终态回执 / Write the requester receipt in the review transaction.

        @param request 已批准或已拒绝的申请 / Approved or rejected token request.
        @param connection 调用方拥有的活动事务 / Caller-owned active transaction.
        @return None / None.
        @raise ValueError 申请不是可通知审核终态时抛出 / Raised when the request is not a notifiable review terminal state.
        """

        if request.status not in {
            TokenRequestStatus.APPROVED,
            TokenRequestStatus.REJECTED,
        }:
            raise ValueError("Requester notification requires an approved or rejected request")
        if request.reviewed_at is None:
            raise ValueError("Requester notification requires a review timestamp")
        idempotency_key = (
            f"decision:{request.request_id}:v{request.version}:"
            "requester-review-notification"
        )
        await self._enqueue(
            recipient_id=request.requester_id,
            request=request,
            text=_requester_review_notification_text(request),
            idempotency_key=idempotency_key,
            created_at=request.reviewed_at,
            connection=connection,
        )

    async def _enqueue(
        self,
        *,
        recipient_id: int,
        request: TokenRequest,
        text: str,
        idempotency_key: str,
        created_at: datetime,
        connection: AsyncConnection,
    ) -> None:
        """@brief 以申请专属幂等域写入一条 Telegram 消息 / Persist one Telegram message in the request-specific idempotency scope.

        @param recipient_id 收件人 Telegram 用户 ID / Recipient Telegram user ID.
        @param request 关联申请 / Associated token request.
        @param text 已渲染的用户可见文本 / Rendered user-facing text.
        @param idempotency_key 申请事件派生的稳定副作用键 / Stable effect key derived from the request event.
        @param created_at 业务事件时刻 / Business-event timestamp.
        @param connection 调用方拥有的活动事务 / Caller-owned active transaction.
        @return None / None.
        """

        if isinstance(recipient_id, bool) or recipient_id <= 0:
            raise ValueError("Bank notification recipient must be positive")
        conversation_id = _notification_conversation_id(request)
        await self._outbox.enqueue_standalone_outbound_in_transaction(
            connection,
            OutboundDraft(
                message_id=OutboundMessageId.for_conversation(
                    conversation_id,
                    idempotency_key,
                ),
                conversation_id=conversation_id,
                turn_id=None,
                delivery_stream_id=_private_delivery_stream(recipient_id),
                kind=SEND_TELEGRAM_MESSAGE,
                payload={
                    "chat_id": recipient_id,
                    "text": text,
                    "disable_web_page_preview": True,
                },
                idempotency_key=idempotency_key,
                created_at=created_at,
            ),
        )


def _notification_conversation_id(request: TokenRequest) -> ConversationId:
    """@brief 获取一份申请专属的通知幂等域 / Get the notification idempotency scope dedicated to one request.

    @param request 关联银行申请 / Associated bank token request.
    @return 申请专属 conversation ID / Request-specific conversation ID.
    """

    return ConversationId(f"bank-token-request:{request.request_id}")


def _private_delivery_stream(user_id: int) -> DeliveryStreamId:
    """@brief 构造 Telegram 私聊有序投递流 / Construct a Telegram private-chat ordered delivery stream.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @return 私聊 delivery-stream ID / Private-chat delivery-stream ID.
    """

    return DeliveryStreamId(f"telegram:primary:chat:{user_id}:thread:0")


def _administrator_request_notification_text(request: TokenRequest) -> str:
    """@brief 渲染管理员待审核提醒 / Render the administrator pending-request notification.

    @param request 待审核申请 / Pending token request.
    @return 不含自动执行动作的 Telegram 文本 / Telegram text containing no automatic action.
    """

    purpose = " ".join(request.purpose.split())
    request_id = str(request.request_id)
    return (
        "🏦 新的免费金币申请待审核\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"申请 ID：{request_id}\n"
        f"申请人：{request.requester_id}\n"
        f"数量：{request.requested_amount.value} Free\n"
        f"用途：{purpose}\n\n"
        "请先核对详情与 /bank_pending 的最新状态；本提醒不会自动发行金币。\n"
        "确认批准：\n"
        f"/bank_review {request_id} approve <说明>\n"
        "拒绝申请：\n"
        f"/bank_review {request_id} reject <说明>"
    )


def _requester_review_notification_text(request: TokenRequest) -> str:
    """@brief 渲染申请人审核终态回执 / Render the requester review-terminal receipt.

    @param request 已批准或已拒绝的申请 / Approved or rejected token request.
    @return Telegram 私聊回执文本 / Telegram private-chat receipt text.
    @raise ValueError 批准申请缺少账本分录时抛出 / Raised when an approved request lacks its ledger entry.
    """

    if request.status is TokenRequestStatus.APPROVED:
        if request.ledger_entry_id is None:
            raise ValueError("Approved token request notification requires a ledger entry")
        return (
            "🏦 你的免费金币申请已批准\n"
            f"申请 ID：{request.request_id}\n"
            f"已发行：{request.requested_amount.value} Free\n"
            f"账本分录：{request.ledger_entry_id}\n"
            "可使用 /bank 查看当前余额。"
        )
    if request.status is TokenRequestStatus.REJECTED:
        note = (
            f"\n审核说明：{' '.join(request.review_note.split())}"
            if request.review_note
            else ""
        )
        return (
            "🏦 你的免费金币申请未获批准\n"
            f"申请 ID：{request.request_id}{note}\n"
            "如需补充用途说明，请重新提交 /request_tokens。"
        )
    raise ValueError("Requester notification requires an approved or rejected request")


__all__ = [
    "BankTokenRequestNotificationWriter",
    "PostgresBankTokenRequestNotificationWriter",
]
