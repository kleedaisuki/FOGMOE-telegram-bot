"""@brief PostgreSQL Assistant 扣费与 Conversation acceptance UoW / PostgreSQL Assistant charge-and-Conversation-acceptance UoW."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.conversation.assistant_ingress import (
    AssistantAccountContext,
    AssistantInsufficientCoins,
    AssistantTurnAcceptanceResult,
    AssistantTurnAccepted,
    AssistantTurnRequest,
    AssistantUserNotRegistered,
    assistant_pool_contribution,
    normalize_assistant_impression,
    normalize_assistant_personal_info,
)
from fogmoe_bot.application.conversation.workflow import (
    ConversationWorkflow,
)
from fogmoe_bot.domain.conversation.identity import (
    TurnId,
    TurnSource,
)
from fogmoe_bot.domain.conversation.temporal import ensure_utc
from fogmoe_bot.domain.conversation.errors import IdempotencyConflictError
from fogmoe_bot.domain.economy import (
    AssistantBillingReservation,
    AssistantBillingStatus,
)
from fogmoe_bot.infrastructure import config
from fogmoe_bot.infrastructure.database.assistant_billing import (
    AssistantBillingTransactions,
    PostgresAssistantBilling,
)
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database.repositories import (
    conversation_repository,
    user_repository,
)
from fogmoe_bot.infrastructure.database.conversation_workflow.turn import (
    PostgresTurnRepository,
)


class PostgresAssistantTurnAcceptanceUoW:
    """@brief 以 inbox/account 行锁串行化扣费与 Turn acceptance / Serialize charging and Turn acceptance with inbox/account row locks."""

    def __init__(
        self,
        workflow_repository: PostgresTurnRepository | None = None,
        billing: AssistantBillingTransactions | None = None,
    ) -> None:
        """@brief 注入 connection-bound workflow 与计费原语 / Inject connection-bound workflow and billing primitives.

        @param workflow_repository Conversation workflow adapter / Conversation workflow adapter.
        @param billing reserve/settle/release 计费原语 / Reserve/settle/release billing primitive.
        """

        self._workflow_repository = (
            workflow_repository
            if workflow_repository is not None
            else PostgresTurnRepository()
        )
        """@brief 同事务 acceptance primitive / Same-transaction acceptance primitive."""
        self._billing = billing or PostgresAssistantBilling()
        """@brief 同事务计费预留原语 / Same-transaction billing-reservation primitive."""

    async def accept(
        self,
        request: AssistantTurnRequest,
        *,
        accepted_at: datetime,
    ) -> AssistantTurnAcceptanceResult:
        """@brief 在单个短事务内校验、预留费用并接受 Turn / Validate, reserve the charge, and accept the Turn in one short transaction.

        @param request 已预检入口请求 / Preflighted ingress request.
        @param accepted_at acceptance 时间 / Acceptance time.
        @return 接受、幂等 replay 或无写入业务拒绝 / Acceptance, idempotent replay, or a no-write business rejection.
        @note 事务内不调用 Telegram、LLM、HTTP、文件下载或 sleep。/
            The transaction performs no Telegram, LLM, HTTP, file-download, or sleep call.
        @note 仅 ``message/activity`` 同时新建才预留；同时已存在是 replay，异或是必须
            回滚的数据库不变量冲突。/ Reserving occurs only when message and activity are both
            inserted; both existing is a replay, while an exclusive-or is a database-invariant
            conflict that must roll back.
        @note acceptance 不向奖池 posting；只有推理结果与 history/outbox 原子提交时结算。/
            Acceptance does not post to the pool; settlement occurs only when the inference result,
            history, and outbox commit atomically.
        """

        timestamp = ensure_utc(accepted_at)
        turn_id = TurnId.for_source(TurnSource.telegram(request.update_id))
        contribution = (
            assistant_pool_contribution(request.coin_cost)
            if request.coin_cost > 0
            else None
        )
        async with db_connection.transaction() as connection:
            await self._lock_and_validate_inbound(request, connection=connection)
            await db_connection.fetch_one(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (str(request.conversation_id),),
                connection=connection,
            )
            existing_state = await self._existing_turn_state(
                request,
                turn_id=turn_id,
                connection=connection,
            )
            if existing_state is not None and existing_state != "received":
                await self._billing.validate_expected(
                    connection,
                    turn_id=turn_id,
                    user_id=request.user_id,
                    cost=request.coin_cost,
                    pool_contribution=contribution,
                )
                return AssistantTurnAccepted(acceptance=None, replayed=True)

            account = await user_repository.fetch_user_account(
                request.user_id,
                connection=connection,
                for_update=True,
            )
            if account is None:
                return AssistantUserNotRegistered()
            if account.total_coins < request.coin_cost:
                return AssistantInsufficientCoins(
                    available=account.total_coins,
                    required=request.coin_cost,
                )

            new_free, new_paid = _deduct_balances(
                free=account.coins,
                paid=account.coins_paid,
                amount=request.coin_cost,
            )
            account_context = AssistantAccountContext(
                coins=new_free + new_paid,
                plan=_resolve_plan(request.user_id, new_paid),
                permission=account.permission,
                impression=normalize_assistant_impression(
                    await user_repository.fetch_impression(
                        request.user_id,
                        connection=connection,
                    )
                ),
                personal_info=normalize_assistant_personal_info(account.info),
                diary_exists=await conversation_repository.user_diary_exists(
                    request.user_id,
                    connection=connection,
                ),
            )
            prepared = ConversationWorkflow.prepare(
                request.to_accept_turn(account_context, accepted_at=timestamp)
            )
            acceptance = (
                await self._workflow_repository.create_and_accept_turn_in_transaction(
                    connection,
                    prepared.turn,
                    message=prepared.message,
                    activity=prepared.activity,
                    accepted_at=prepared.accepted_at,
                )
            )
            message_inserted = acceptance.user_message.inserted
            activity_inserted = acceptance.inference_activity.inserted
            if message_inserted != activity_inserted:
                raise IdempotencyConflictError(
                    "Assistant acceptance found a partial durable effect set: "
                    f"user_message_inserted={message_inserted}, "
                    f"inference_activity_inserted={activity_inserted}"
                )
            if not message_inserted:
                await self._billing.validate_expected(
                    connection,
                    turn_id=turn_id,
                    user_id=request.user_id,
                    cost=request.coin_cost,
                    pool_contribution=contribution,
                )
                return AssistantTurnAccepted(acceptance=None, replayed=True)

            if request.coin_cost > 0:
                plan = _resolve_plan(request.user_id, new_paid)
                await user_repository.set_coin_balances_and_plan(
                    request.user_id,
                    new_free,
                    new_paid,
                    plan,
                    connection=connection,
                )
                if contribution is None:
                    raise RuntimeError(
                        "Positive Assistant cost has no pool contribution"
                    )
                await self._billing.reserve(
                    connection,
                    AssistantBillingReservation(
                        turn_id=turn_id,
                        user_id=request.user_id,
                        cost=request.coin_cost,
                        free_reserved=account.coins - new_free,
                        paid_reserved=account.coins_paid - new_paid,
                        pool_contribution=contribution,
                        status=AssistantBillingStatus.RESERVED,
                        reserved_at=timestamp,
                    ),
                )
            else:
                await self._billing.validate_expected(
                    connection,
                    turn_id=turn_id,
                    user_id=request.user_id,
                    cost=0,
                    pool_contribution=None,
                )
            return AssistantTurnAccepted(acceptance=acceptance, replayed=False)

    @staticmethod
    async def _lock_and_validate_inbound(
        request: AssistantTurnRequest,
        *,
        connection: AsyncConnection,
    ) -> None:
        """@brief 锁定 durable Update 作为幂等 mutex / Lock the durable Update as the idempotency mutex.

        @param request 入口请求 / Ingress request.
        @param connection 当前事务 / Current transaction.
        @return None / None.
        """

        row = await db_connection.fetch_one(
            "SELECT conversation_id FROM conversation.inbound_updates "
            "WHERE update_id = %s FOR UPDATE",
            (request.update_id.value,),
            connection=connection,
        )
        if row is None:
            raise IdempotencyConflictError(
                f"Inbound Update {request.update_id.value} does not exist"
            )
        if str(row[0]) != str(request.conversation_id):
            raise IdempotencyConflictError(
                f"Inbound Update {request.update_id.value} changed conversation identity"
            )

    @staticmethod
    async def _existing_turn_state(
        request: AssistantTurnRequest,
        *,
        turn_id: TurnId,
        connection: AsyncConnection,
    ) -> str | None:
        """@brief 锁定并验证可能存在的规范 Turn / Lock and validate an existing canonical Turn.

        @param request 入口请求 / Ingress request.
        @param turn_id 确定性 Turn ID / Deterministic Turn ID.
        @param connection 当前事务 / Current transaction.
        @return 状态；不存在为 None / State, or None when absent.
        """

        row = await db_connection.fetch_one(
            "SELECT turn_id, conversation_id, state FROM conversation.conversation_turns "
            "WHERE source_update_id = %s FOR UPDATE",
            (request.update_id.value,),
            connection=connection,
        )
        if row is None:
            return None
        if str(row[0]) != str(turn_id) or str(row[1]) != str(request.conversation_id):
            raise IdempotencyConflictError(
                f"Update {request.update_id.value} already belongs to another Turn"
            )
        return str(row[2])


def _deduct_balances(*, free: int, paid: int, amount: int) -> tuple[int, int]:
    """@brief 保持旧语义，先扣免费余额再扣付费余额 / Preserve legacy semantics by spending free coins before paid coins.

    @param free 免费余额 / Free balance.
    @param paid 付费余额 / Paid balance.
    @param amount 非负扣费额 / Non-negative charge.
    @return 新免费与付费余额 / New free and paid balances.
    """

    if min(free, paid, amount) < 0 or free + paid < amount:
        raise ValueError("Coin deduction requires sufficient non-negative balances")
    if amount == 0:
        return free, paid
    if free >= amount:
        return free - amount, paid
    return 0, paid - (amount - free)


def _resolve_plan(user_id: int, paid: int) -> str:
    """@brief 按旧产品规则解析扣费后计划 / Resolve the post-charge plan using legacy product rules.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param paid 扣费后付费余额 / Paid balance after charging.
    @return ``admin``、``paid`` 或 ``free`` / ``admin``, ``paid``, or ``free``.
    """

    if user_id == config.ADMIN_USER_ID:
        return "admin"
    return "paid" if paid > 0 else "free"


__all__ = ["PostgresAssistantTurnAcceptanceUoW"]
