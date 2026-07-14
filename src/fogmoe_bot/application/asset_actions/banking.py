"""@brief 已确认资产动作到银行用例的显式映射 / Explicit mapping from confirmed asset actions to banking use cases."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fogmoe_bot.application.asset_actions.ports import AssetActionExecutor
from fogmoe_bot.application.banking.json_projection import (
    activity_pot_funding_result_json,
    token_request_result_json,
)
from fogmoe_bot.application.banking.models import (
    FundActivityPot,
    IssueTokens,
    ReviewTokenRequest,
    TokenReviewDecision,
)
from fogmoe_bot.application.banking.service import BankService
from fogmoe_bot.domain.asset_actions.confirmation import (
    AssetActionConfirmation,
    AssetActionKind,
)
from fogmoe_bot.domain.banking.money import TokenAmount, TokenBucket
from fogmoe_bot.domain.conversation.payloads import JsonObject


class BankAssetActionExecutor(AssetActionExecutor):
    """@brief 仅执行已确认的银行资产动作 / Execute only already-confirmed banking asset actions.

    模型永远不能提供 actor ID：每个银行 command 都由 ``confirmation.owner_user_id``
    生成。``BankService`` 在执行点再次检查管理员资格，因此创建确认时的权限检查不是
    唯一防线。/ The model can never provide an actor ID: every banking command is derived from
    ``confirmation.owner_user_id``. ``BankService`` rechecks administrator eligibility at the
    execution point, so proposal-time authorization is not the only defense.
    """

    def __init__(self, *, bank: BankService) -> None:
        """@brief 注入银行应用服务 / Inject the banking application service.

        @param bank 负责最终银行授权的服务 / Service responsible for final bank authorization.
        @return None / None.
        """

        self._bank = bank

    async def execute(
        self,
        confirmation: AssetActionConfirmation,
        *,
        idempotency_key: str,
        executed_at: datetime,
    ) -> JsonObject:
        """@brief 按确认类别调用唯一的银行用例 / Call the sole banking use case for a confirmation kind.

        @param confirmation 已获 owner 同意的确认 / Confirmation approved by its owner.
        @param idempotency_key confirmation 派生稳定键 / Stable confirmation-derived key.
        @param executed_at 执行时间 / Execution time.
        @return JSON 可持久化银行结果 / JSON-persistable banking result.
        @raise ValueError 已持久化参数被篡改或类别未知时抛出 / Raised for tampered persisted arguments or an unknown kind.
        """

        arguments = confirmation.arguments
        match confirmation.kind:
            case AssetActionKind.BANK_REVIEW_TOKEN_REQUEST:
                review_result = await self._bank.review_token_request(
                    ReviewTokenRequest(
                        request_id=_required_uuid(arguments, "request_id"),
                        reviewer_id=confirmation.owner_user_id,
                        decision=TokenReviewDecision(
                            _required_text(arguments, "decision")
                        ),
                        reviewed_at=executed_at,
                        idempotency_key=idempotency_key,
                        note=_optional_text(arguments, "note"),
                    )
                )
                return token_request_result_json(review_result)
            case AssetActionKind.BANK_ISSUE_TOKENS:
                issue_result = await self._bank.issue_tokens(
                    IssueTokens(
                        administrator_id=confirmation.owner_user_id,
                        recipient_id=_required_positive_int(arguments, "recipient_id"),
                        amount=TokenAmount(
                            _required_positive_int(
                                arguments,
                                "amount",
                                maximum=1_000_000,
                            )
                        ),
                        bucket=TokenBucket.FREE,
                        purpose=_required_text(arguments, "purpose"),
                        issued_at=executed_at,
                        idempotency_key=idempotency_key,
                    )
                )
                return token_request_result_json(issue_result)
            case AssetActionKind.BANK_FUND_ACTIVITY_POT:
                funding_result = await self._bank.fund_activity_pot(
                    FundActivityPot(
                        administrator_id=confirmation.owner_user_id,
                        amount=TokenAmount(
                            _required_positive_int(
                                arguments,
                                "amount",
                                maximum=1_000_000,
                            )
                        ),
                        purpose=_required_text(arguments, "purpose"),
                        funded_at=executed_at,
                        idempotency_key=idempotency_key,
                    )
                )
                return activity_pot_funding_result_json(funding_result)
        raise AssertionError("Unhandled confirmed bank asset-action kind")


def _required_positive_int(
    arguments: JsonObject,
    key: str,
    *,
    maximum: int | None = None,
) -> int:
    """@brief 读取严格正整数参数 / Read a strictly positive integer argument.

    @param arguments 已持久化参数对象 / Persisted argument object.
    @param key 参数键 / Argument key.
    @param maximum 可选最大值 / Optional maximum value.
    @return 严格正整数 / Strictly positive integer.
    """

    value = arguments.get(key)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or (maximum is not None and value > maximum)
    ):
        raise ValueError(f"Asset-action argument {key!r} must be a positive integer")
    return value


def _required_text(arguments: JsonObject, key: str) -> str:
    """@brief 读取有界非空文本参数 / Read a bounded non-empty text argument.

    @param arguments 已持久化参数对象 / Persisted argument object.
    @param key 参数键 / Argument key.
    @return 去首尾空白后的文本 / Trimmed text.
    """

    value = arguments.get(key)
    if not isinstance(value, str):
        raise ValueError(f"Asset-action argument {key!r} must be text")
    normalized = value.strip()
    if not 1 <= len(normalized) <= 500:
        raise ValueError(
            f"Asset-action argument {key!r} must contain 1-500 characters"
        )
    return normalized


def _optional_text(arguments: JsonObject, key: str) -> str | None:
    """@brief 读取可选有界文本参数 / Read an optional bounded text argument.

    @param arguments 已持久化参数对象 / Persisted argument object.
    @param key 参数键 / Argument key.
    @return 去首尾空白后的文本或 None / Trimmed text or None.
    """

    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Asset-action argument {key!r} must be text when present")
    normalized = value.strip()
    if len(normalized) > 500:
        raise ValueError(
            f"Asset-action argument {key!r} cannot exceed 500 characters"
        )
    return normalized or None


def _required_uuid(arguments: JsonObject, key: str) -> UUID:
    """@brief 读取 UUID 文本参数 / Read a UUID text argument.

    @param arguments 已持久化参数对象 / Persisted argument object.
    @param key 参数键 / Argument key.
    @return UUID / UUID.
    """

    value = arguments.get(key)
    if not isinstance(value, str):
        raise ValueError(f"Asset-action argument {key!r} must be UUID text")
    try:
        return UUID(value)
    except ValueError as error:
        raise ValueError(f"Asset-action argument {key!r} must be UUID text") from error


__all__ = ["BankAssetActionExecutor"]
