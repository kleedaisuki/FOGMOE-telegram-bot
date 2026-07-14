"""@brief 银行结果到受限 JSON 的投影 / Banking-result projections to bounded JSON."""

from __future__ import annotations

from fogmoe_bot.application.banking.models import (
    ActivityPotFundingResult,
    BankOverview,
    PendingTokenRequestsResult,
    TokenRequestResult,
)
from fogmoe_bot.domain.banking.requests import TokenRequest
from fogmoe_bot.domain.conversation.payloads import JsonObject


def bank_overview_json(overview: BankOverview | None) -> JsonObject | None:
    """@brief 将可选钱包概览投影为 JSON / Project an optional wallet overview into JSON.

    @param overview 已验证的银行钱包概览 / Validated bank-wallet overview.
    @return JSON 概览或 ``None`` / JSON overview or ``None``.
    """

    if overview is None:
        return None
    return {
        "user_id": overview.user_id,
        "free": overview.free.value,
        "paid": overview.paid.value,
        "total": overview.total,
    }


def token_request_json(request: TokenRequest | None) -> JsonObject | None:
    """@brief 将可选代币申请投影为 JSON / Project an optional token request into JSON.

    @param request 已验证的代币申请 / Validated token request.
    @return JSON 申请或 ``None`` / JSON request or ``None``.
    """

    if request is None:
        return None
    return {
        "request_id": str(request.request_id),
        "requester_id": request.requester_id,
        "requested_amount": request.requested_amount.value,
        "requested_bucket": request.requested_bucket.value,
        "purpose": request.purpose,
        "status": request.status.value,
        "requested_at": request.requested_at.isoformat(),
        "reviewed_at": (
            request.reviewed_at.isoformat()
            if request.reviewed_at is not None
            else None
        ),
        "reviewer_id": request.reviewer_id,
        "review_note": request.review_note,
        "ledger_entry_id": (
            str(request.ledger_entry_id)
            if request.ledger_entry_id is not None
            else None
        ),
    }


def token_request_result_json(result: TokenRequestResult) -> JsonObject:
    """@brief 将代币请求结果投影为持久 JSON / Project a token-request result into persistent JSON.

    @param result 银行 token-request 用例结果 / Banking token-request use-case result.
    @return JSON 可持久化结果 / JSON-persistable result.
    """

    return {
        "code": result.code.value,
        "request": token_request_json(result.request),
        "overview": bank_overview_json(result.overview),
        "replayed": result.replayed,
    }


def pending_token_requests_result_json(
    result: PendingTokenRequestsResult,
) -> JsonObject:
    """@brief 将待审申请列表投影为 JSON / Project pending-token requests into JSON.

    @param result 银行待审申请列表结果 / Banking pending-request-list result.
    @return JSON 可持久化结果 / JSON-persistable result.
    """

    return {
        "code": result.code.value,
        "requests": [
            request_json
            for request in result.requests
            if (request_json := token_request_json(request)) is not None
        ],
    }


def activity_pot_funding_result_json(
    result: ActivityPotFundingResult,
) -> JsonObject:
    """@brief 将活动奖池注资结果投影为 JSON / Project activity-pot funding result into JSON.

    @param result 银行奖池注资用例结果 / Banking activity-pot funding use-case result.
    @return JSON 可持久化结果 / JSON-persistable result.
    """

    return {
        "code": result.code.value,
        "amount": result.amount.value if result.amount is not None else None,
        "activity_pot_balance": result.activity_pot_balance,
        "ledger_entry_id": (
            str(result.ledger_entry_id) if result.ledger_entry_id is not None else None
        ),
        "replayed": result.replayed,
    }


__all__ = [
    "activity_pot_funding_result_json",
    "bank_overview_json",
    "pending_token_requests_result_json",
    "token_request_json",
    "token_request_result_json",
]
