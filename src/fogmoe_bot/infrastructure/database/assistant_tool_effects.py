"""@brief PostgreSQL Assistant tool checkpoint/receipt adapter / PostgreSQL Assistant 工具 checkpoint/receipt 适配器."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol, cast

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.assistant.completion import (
    AgentCheckpointConflictError,
    AgentStepCheckpoint,
    AssistantCompletion,
)
from fogmoe_bot.application.assistant.tool_runtime import (
    PersistedToolResult,
    ToolEffectBusyError,
    ToolEffectConflictError,
    ToolEffectRequest,
)
from fogmoe_bot.application.workspace.errors import WorkspaceRuntimeUnavailableError
from fogmoe_bot.domain.assistant.messages import (
    CanonicalMessage,
    CanonicalMessageError,
)
from fogmoe_bot.domain.conversation.errors import StaleClaimError
from fogmoe_bot.domain.conversation.identity import TurnId
from fogmoe_bot.domain.conversation.inference import InferenceGenerationFence
from fogmoe_bot.domain.conversation.payloads import JsonObject, JsonValue
from fogmoe_bot.infrastructure.database import db

logger = logging.getLogger(__name__)


class ToolTransactionMode(StrEnum):
    """@brief operation 的事务语义 / Transaction semantics of an operation."""

    OUTSIDE_TRANSACTION = "outside_transaction"
    """@brief 在 receipt claim 与 finalize 之间、数据库事务外执行 / Execute outside database transactions between receipt claim and finalization."""

    ATOMIC_MUTATION = "atomic_mutation"
    """@brief 与业务写及 receipt finalization 共用短事务 / Share one short transaction with business writes and receipt finalization."""


class AssistantToolOperations(Protocol):
    """@brief receipt adapter 调用的基础设施 operation port / Infrastructure operation port called by the receipt adapter."""

    def transaction_mode(self, request: ToolEffectRequest) -> ToolTransactionMode:
        """@brief 返回 operation 执行模式 / Return operation execution mode.

        @param request 工具请求 / Tool request.
        @return 事务模式 / Transaction mode.
        """

        ...

    async def execute(
        self,
        request: ToolEffectRequest,
        *,
        connection: AsyncConnection | None,
    ) -> JsonValue:
        """@brief 执行 operation / Execute the operation.

        @param request 工具请求 / Tool request.
        @param connection 原子 mutation 的活动事务；其他模式为 None / Active transaction for atomic mutations; None otherwise.
        @return JSON 结果 / JSON result.
        """

        ...

    async def finalize(
        self,
        request: ToolEffectRequest,
        result: JsonValue,
        *,
        connection: AsyncConnection,
    ) -> None:
        """@brief 在 succeeded receipt 同一事务写入 durable downstream effects / Persist durable downstream effects in the succeeded-receipt transaction.

        @param request 工具请求 / Tool request.
        @param result operation 结果 / Operation result.
        @param connection 活动事务 / Active transaction.
        @return None / None.
        """

        ...


type UtcNow = Callable[[], datetime]
"""@brief 可注入 UTC 时钟 / Injectable UTC clock."""

type AfterOperationHook = Callable[[ToolEffectRequest, JsonValue], Awaitable[None]]
"""@brief 测试故障注入 hook / Test fault-injection hook."""


def _utc_now() -> datetime:
    """@brief 返回系统 UTC / Return system UTC.

    @return aware UTC / Aware UTC.
    """

    return datetime.now(UTC)


async def _noop_hook(request: ToolEffectRequest, result: JsonValue) -> None:
    """@brief 默认无故障 hook / Default no-fault hook.

    @param request 工具请求 / Tool request.
    @param result operation 结果 / Operation result.
    @return None / None.
    """

    del request, result


class PostgresAssistantToolStore:
    """@brief 以 checkpoint + leased receipt 实现可恢复工具状态机 / Implement a resumable tool state machine with checkpoints and leased receipts."""

    def __init__(
        self,
        *,
        operations: AssistantToolOperations,
        lease_for: timedelta = timedelta(minutes=2),
        now: UtcNow = _utc_now,
        after_operation: AfterOperationHook = _noop_hook,
    ) -> None:
        """@brief 创建 store / Create the store.

        @param operations 类型化 operation adapter / Typed operation adapter.
        @param lease_for kill-9 恢复租约 / Kill-9 recovery lease.
        @param now 可测试时钟 / Testable clock.
        @param after_operation mutation 与 receipt 之间的故障注入点 / Fault point between mutation and receipt.
        """

        if lease_for <= timedelta(0):
            raise ValueError("lease_for must be positive")
        self._operations = operations
        self._lease_for = lease_for
        self._now = now
        self._after_operation = after_operation
        """@brief fault hook / Fault hook."""

    async def load_step(
        self,
        turn_id: TurnId,
        step_no: int,
        *,
        generation: int = 0,
    ) -> AgentStepCheckpoint | None:
        """@brief 读取 Agent step checkpoint / Load an Agent-step checkpoint.

        @param turn_id Turn ID / Turn identifier.
        @param step_no step 序号 / Step number.
        @param generation input-revision effect generation / Input-revision effect generation.
        @return checkpoint 或 None / Checkpoint or None.
        """

        if step_no < 0 or generation < 0:
            raise ValueError("step_no and generation must be non-negative")
        row = await db.fetch_one(
            "SELECT request_hash, route_key, response FROM assistant.tool_agent_steps "
            "WHERE turn_id = CAST(%s AS UUID) AND generation = %s AND step_no = %s",
            (str(turn_id), generation, step_no),
        )
        return (
            None
            if row is None
            else _checkpoint(turn_id, generation, step_no, row)
        )

    async def save_step(self, checkpoint: AgentStepCheckpoint) -> AgentStepCheckpoint:
        """@brief 幂等保存 Agent step / Idempotently persist an Agent step.

        @param checkpoint 待保存 checkpoint / Checkpoint to persist.
        @return 规范 checkpoint / Canonical checkpoint.
        """

        if checkpoint.step_no < 0:
            raise ValueError("step_no must be non-negative")
        payload = _encode_completion(checkpoint.completion)
        async with db.transaction() as connection:
            if checkpoint.generation_fence is not None:
                await _assert_current_generation(
                    checkpoint.generation_fence,
                    connection=connection,
                )
            await db.execute(
                "INSERT INTO assistant.tool_agent_steps "
                "(turn_id, generation, step_no, request_hash, route_key, response) "
                "VALUES (CAST(%s AS UUID), %s, %s, %s, %s, CAST(%s AS JSONB)) "
                "ON CONFLICT (turn_id, generation, step_no) DO NOTHING",
                (
                    str(checkpoint.turn_id),
                    checkpoint.generation,
                    checkpoint.step_no,
                    checkpoint.request_hash,
                    checkpoint.route_key,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                ),
                connection=connection,
            )
            row = await db.fetch_one(
                "SELECT request_hash, route_key, response FROM assistant.tool_agent_steps "
                "WHERE turn_id = CAST(%s AS UUID) AND generation = %s "
                "AND step_no = %s FOR UPDATE",
                (
                    str(checkpoint.turn_id),
                    checkpoint.generation,
                    checkpoint.step_no,
                ),
                connection=connection,
            )
            if row is None:
                raise RuntimeError("Agent checkpoint insert returned no row")
            canonical = _checkpoint(
                checkpoint.turn_id,
                checkpoint.generation,
                checkpoint.step_no,
                row,
            )
            if (
                canonical.request_hash != checkpoint.request_hash
                or canonical.route_key != checkpoint.route_key
                or canonical.completion != checkpoint.completion
            ):
                raise AgentCheckpointConflictError(
                    f"Conflicting Agent checkpoint at step {checkpoint.step_no}"
                )
            return canonical

    async def assert_current_generation(
        self,
        fence: InferenceGenerationFence,
    ) -> None:
        """@brief 验证 processing claim/revision 仍是当前 generation / Verify the processing claim and revision are still current.

        @param fence worker 传入的跨进程 generation fence / Cross-process generation fence supplied by the worker.
        @return None / None.
        @raise StaleClaimError claim 已被 steer、恢复或完成时抛出 /
            Raised when the claim was steered, recovered, or completed.
        """

        await _assert_current_generation(fence)

    async def execute(self, request: ToolEffectRequest) -> PersistedToolResult:
        """@brief 领取、执行并终结一个工具 receipt / Claim, execute, and finalize one tool receipt.

        @param request 已校验请求 / Validated request.
        @return 规范 receipt 结果 / Canonical receipt result.
        """

        fence = request.context.generation_fence
        if fence is not None:
            await self.assert_current_generation(fence)
        if not request.result_cacheable:
            if request.mutating:
                raise ValueError(
                    "A mutating tool result cannot bypass durable receipts"
                )
            if (
                self._operations.transaction_mode(request)
                is not ToolTransactionMode.OUTSIDE_TRANSACTION
            ):
                raise ValueError("A non-cacheable tool must run outside transactions")
            result = await self._operations.execute(request, connection=None)
            await self._after_operation(request, result)
            return PersistedToolResult(result, False)

        token = uuid.uuid4()
        replay = await self._claim(request, token=token)
        if replay is not None:
            return PersistedToolResult(replay, True)
        mode = self._operations.transaction_mode(request)
        try:
            if mode is ToolTransactionMode.ATOMIC_MUTATION:
                return await self._execute_atomic(request, token=token)
            if fence is not None:
                # This is the narrowest possible preflight before an out-of-transaction
                # operation. External mutations must additionally use a destination
                # idempotency key or create a durable downstream activity, because no
                # PostgreSQL row lock can span the remote effect safely.
                await self.assert_current_generation(fence)
            result = await self._operations.execute(request, connection=None)
            await self._after_operation(request, result)
            return await self._finalize(request, token=token, result=result)
        except asyncio.CancelledError:
            # Cancellation may not stop an offloaded SDK call.  Keep the fencing
            # lease so a second process cannot immediately duplicate the effect.
            raise
        except Exception as error:
            await self._release_failed_claim(request, token=token, error=error)
            raise

    async def _claim(
        self,
        request: ToolEffectRequest,
        *,
        token: uuid.UUID,
    ) -> JsonValue | None:
        """@brief 领取 receipt 或读取成功结果 / Claim a receipt or load its successful result.

        @param request 工具请求 / Tool request.
        @param token 新 fencing token / New fencing token.
        @return replay 结果；新 claim 为 None / Replay result, or None for a new claim.
        """

        now = _aware(self._now())
        async with db.transaction() as connection:
            if request.context.generation_fence is not None:
                await _assert_current_generation(
                    request.context.generation_fence,
                    connection=connection,
                )
            await db.execute(
                "INSERT INTO assistant.tool_effect_receipts "
                "(turn_id, invocation_id, effect_kind, tool_name, provider_call_id, "
                "request_hash, request, mutating, status) VALUES "
                "(CAST(%s AS UUID), %s, %s, %s, %s, %s, CAST(%s AS JSONB), %s, 'pending') "
                "ON CONFLICT (turn_id, invocation_id, effect_kind) DO NOTHING",
                (
                    str(request.context.turn_id),
                    request.invocation_id,
                    request.effect_kind,
                    request.tool_name,
                    request.provider_call_id,
                    request.request_hash,
                    json.dumps(
                        request.arguments, ensure_ascii=False, separators=(",", ":")
                    ),
                    request.mutating,
                ),
                connection=connection,
            )
            row = await db.fetch_one(
                "SELECT request_hash, status, result, claim_token, lease_expires_at "
                "FROM assistant.tool_effect_receipts WHERE turn_id = CAST(%s AS UUID) "
                "AND invocation_id = %s AND effect_kind = %s FOR UPDATE",
                (
                    str(request.context.turn_id),
                    request.invocation_id,
                    request.effect_kind,
                ),
                connection=connection,
            )
            if row is None:
                raise RuntimeError("Tool receipt insert returned no row")
            if str(row[0]) != request.request_hash:
                raise ToolEffectConflictError(
                    f"Tool receipt request conflict for {request.invocation_id}"
                )
            status = str(row[1])
            if status == "succeeded":
                return _json_value(row[2])
            lease_expires_at = cast(datetime | None, row[4])
            if (
                status == "processing"
                and lease_expires_at is not None
                and _aware(lease_expires_at) > now
            ):
                raise ToolEffectBusyError(
                    f"Tool receipt {request.invocation_id} is already processing"
                )
            rowcount = await db.execute(
                "UPDATE assistant.tool_effect_receipts SET status = 'processing', "
                "claim_token = CAST(%s AS UUID), lease_expires_at = %s, "
                "attempt_count = attempt_count + 1, last_error = NULL, updated_at = %s "
                "WHERE turn_id = CAST(%s AS UUID) AND invocation_id = %s AND effect_kind = %s",
                (
                    str(token),
                    now + self._lease_for,
                    now,
                    str(request.context.turn_id),
                    request.invocation_id,
                    request.effect_kind,
                ),
                connection=connection,
            )
            if rowcount != 1:
                raise RuntimeError("Could not claim tool receipt")
            return None

    async def _execute_atomic(
        self,
        request: ToolEffectRequest,
        *,
        token: uuid.UUID,
    ) -> PersistedToolResult:
        """@brief 在 mutation 同一事务终结 receipt / Finalize the receipt in the mutation transaction.

        @param request 工具请求 / Tool request.
        @param token fencing token / Fencing token.
        @return 新结果 / New result.
        """

        async with db.transaction() as connection:
            await _lock_claim(request, token=token, connection=connection)
            if request.context.generation_fence is not None:
                await _assert_current_generation(
                    request.context.generation_fence,
                    connection=connection,
                )
            result = await self._operations.execute(request, connection=connection)
            await self._after_operation(request, result)
            await self._operations.finalize(request, result, connection=connection)
            await _mark_succeeded(
                request,
                token=token,
                result=result,
                now=_aware(self._now()),
                connection=connection,
            )
        return PersistedToolResult(result, False)

    async def _finalize(
        self,
        request: ToolEffectRequest,
        *,
        token: uuid.UUID,
        result: JsonValue,
    ) -> PersistedToolResult:
        """@brief 在短事务写 downstream effect 与 succeeded receipt / Persist downstream effects and the succeeded receipt in a short transaction.

        @param request 工具请求 / Tool request.
        @param token fencing token / Fencing token.
        @param result operation 结果 / Operation result.
        @return 新结果 / New result.
        """

        async with db.transaction() as connection:
            await _lock_claim(request, token=token, connection=connection)
            if request.context.generation_fence is not None:
                await _assert_current_generation(
                    request.context.generation_fence,
                    connection=connection,
                )
            await self._operations.finalize(request, result, connection=connection)
            await _mark_succeeded(
                request,
                token=token,
                result=result,
                now=_aware(self._now()),
                connection=connection,
            )
        return PersistedToolResult(result, False)

    async def _release_failed_claim(
        self,
        request: ToolEffectRequest,
        *,
        token: uuid.UUID,
        error: Exception,
    ) -> None:
        """@brief 释放显式失败 claim；kill-9 由 lease 回收 / Release an explicit failure; leases recover kill-9.

        @param request 工具请求 / Tool request.
        @param token fencing token / Fencing token.
        @param error operation 抛出的失败 / Failure raised by the operation.
        @return None / None.
        """

        failure_detail = _safe_failure_detail(error)
        try:
            await db.execute(
                "UPDATE assistant.tool_effect_receipts SET status = 'pending', claim_token = NULL, "
                "lease_expires_at = NULL, updated_at = %s, last_error = %s "
                "WHERE turn_id = CAST(%s AS UUID) AND invocation_id = %s AND effect_kind = %s "
                "AND status = 'processing' AND claim_token = CAST(%s AS UUID)",
                (
                    _aware(self._now()),
                    failure_detail,
                    str(request.context.turn_id),
                    request.invocation_id,
                    request.effect_kind,
                    str(token),
                ),
            )
        except Exception:
            logger.exception(
                "Tool receipt failure release failed; lease recovery will retry: "
                "turn_id=%s invocation_id=%s effect_kind=%s",
                request.context.turn_id,
                request.invocation_id,
                request.effect_kind,
            )
            return


async def _lock_claim(
    request: ToolEffectRequest,
    *,
    token: uuid.UUID,
    connection: AsyncConnection,
) -> None:
    """@brief 用 fencing token 锁定活动 receipt / Lock an active receipt with its fencing token.

    @param request 工具请求 / Tool request.
    @param token fencing token / Fencing token.
    @param connection 活动事务 / Active transaction.
    @return None / None.
    """

    row = await db.fetch_one(
        "SELECT 1 FROM assistant.tool_effect_receipts WHERE turn_id = CAST(%s AS UUID) "
        "AND invocation_id = %s AND effect_kind = %s AND status = 'processing' "
        "AND claim_token = CAST(%s AS UUID) FOR UPDATE",
        (
            str(request.context.turn_id),
            request.invocation_id,
            request.effect_kind,
            str(token),
        ),
        connection=connection,
    )
    if row is None:
        raise ToolEffectBusyError(
            f"Stale tool receipt claim for {request.invocation_id}"
        )


async def _mark_succeeded(
    request: ToolEffectRequest,
    *,
    token: uuid.UUID,
    result: JsonValue,
    now: datetime,
    connection: AsyncConnection,
) -> None:
    """@brief 以 fencing token 标记成功 / Mark success with a fencing token.

    @param request 工具请求 / Tool request.
    @param token fencing token / Fencing token.
    @param result 规范结果 / Canonical result.
    @param now 完成时间 / Completion time.
    @param connection 活动事务 / Active transaction.
    @return None / None.
    """

    rowcount = await db.execute(
        "UPDATE assistant.tool_effect_receipts SET status = 'succeeded', "
        "result = CAST(%s AS JSONB), claim_token = NULL, lease_expires_at = NULL, "
        "completed_at = %s, updated_at = %s, last_error = NULL "
        "WHERE turn_id = CAST(%s AS UUID) AND invocation_id = %s AND effect_kind = %s "
        "AND status = 'processing' AND claim_token = CAST(%s AS UUID)",
        (
            json.dumps(result, ensure_ascii=False, separators=(",", ":")),
            now,
            now,
            str(request.context.turn_id),
            request.invocation_id,
            request.effect_kind,
            str(token),
        ),
        connection=connection,
    )
    if rowcount != 1:
        raise ToolEffectBusyError(
            f"Stale tool receipt claim for {request.invocation_id}"
        )


async def _assert_current_generation(
    fence: InferenceGenerationFence,
    *,
    connection: AsyncConnection | None = None,
) -> None:
    """@brief 以 activity claim token 与 revision fencing 副作用 / Fence effects by activity claim token and revision.

    @param fence 当前 generation identity / Current generation identity.
    @param connection 可选活动事务 / Optional active transaction.
    @return None / None.
    @raise StaleClaimError generation 不再 processing 时抛出 /
        Raised when the generation is no longer processing.
    """

    lock_clause = " FOR SHARE" if connection is not None else ""
    row = await db.fetch_one(
        "SELECT 1 FROM conversation.inference_activities "
        "WHERE activity_id = CAST(%s AS UUID) "
        "AND turn_id = CAST(%s AS UUID) AND status = 'processing' "
        "AND claim_token = CAST(%s AS UUID) AND input_revision = %s"
        + lock_clause,
        (
            str(fence.activity_id),
            str(fence.turn_id),
            str(fence.claim_token),
            int(fence.input_revision),
        ),
        connection=connection,
    )
    if row is None:
        raise StaleClaimError(
            f"Inference generation {fence.activity_id} is no longer current"
        )


def _safe_failure_detail(error: Exception) -> str:
    """@brief 为 receipt 选择不含请求载荷的失败摘要 / Select a receipt failure summary without request payloads.

    @param error operation 抛出的失败 / Failure raised by the operation.
    @return 有界安全摘要 / Bounded safe summary.
    @note 任意异常文本默认不可信；只有 Workspace error 显式声明的结构化诊断可进入
        receipt。/ Arbitrary exception text is untrusted by default; only structured diagnostics
        explicitly declared by a Workspace error may enter the receipt.
    """

    if isinstance(error, WorkspaceRuntimeUnavailableError):
        summary = error.diagnostic_summary()
        if summary is not None:
            return summary
    return "operation failed"


def _checkpoint(
    turn_id: TurnId,
    generation: int,
    step_no: int,
    row: Sequence[object],
) -> AgentStepCheckpoint:
    """@brief 映射数据库 checkpoint 行 / Map a database checkpoint row.

    @param turn_id Turn ID / Turn identifier.
    @param generation input-revision effect generation / Input-revision effect generation.
    @param step_no step 序号 / Step number.
    @param row 数据库行 / Database row.
    @return checkpoint / Checkpoint.
    """

    values = tuple(row)
    if len(values) != 3:
        raise RuntimeError("Invalid tool checkpoint row")
    return AgentStepCheckpoint(
        turn_id=turn_id,
        step_no=step_no,
        request_hash=str(values[0]),
        route_key=str(values[1]),
        completion=_decode_completion(values[2]),
        generation=generation,
    )


def _encode_completion(completion: AssistantCompletion) -> JsonObject:
    """@brief 序列化规范 V2 完成 / Serialize a canonical V2 completion.

    @param completion 完成 / Completion.
    @return 不重复工具调用的 V2 JSON 对象 / V2 JSON object without duplicate tool calls.
    """

    return {
        "schema_version": 2,
        "message": completion.message.to_json(),
    }


def _decode_completion(raw: object) -> AssistantCompletion:
    """@brief 严格解码规范 V2 完成 / Strictly decode a canonical V2 completion.

    @param raw JSONB 值 / JSONB value.
    @return 从 message parts 派生工具调用的完成 / Completion with tool calls derived from message parts.
    @raise RuntimeError checkpoint 不是规范 V2 载荷时抛出 / Raised when the checkpoint is not canonical V2.
    """

    value = _json_value(raw)
    if not isinstance(value, dict):
        raise RuntimeError("Tool checkpoint response must be an object")
    expected = {"schema_version", "message"}
    if set(value) != expected or value.get("schema_version") != 2:
        raise RuntimeError("Tool checkpoint response must be canonical V2")
    raw_message = value.get("message")
    if not isinstance(raw_message, Mapping):
        raise RuntimeError("Tool checkpoint response message must be an object")
    try:
        message = CanonicalMessage.from_json(
            cast(Mapping[str, object], raw_message)
        )
    except CanonicalMessageError as error:
        raise RuntimeError("Tool checkpoint response message is invalid") from error
    try:
        return AssistantCompletion(message=message)
    except ValueError as error:
        raise RuntimeError("Tool checkpoint completion is invalid") from error


def _json_value(raw: object) -> JsonValue:
    """@brief 将 driver JSONB 值转换为纯 JSON / Convert a driver JSONB value into plain JSON.

    @param raw driver 值 / Driver value.
    @return JSON 值 / JSON value.
    """

    if isinstance(raw, str):
        decoded: object = json.loads(raw)
    else:
        decoded = raw
    encoded = json.dumps(
        decoded, ensure_ascii=False, separators=(",", ":"), default=str
    )
    return cast(JsonValue, json.loads(encoded))


def _aware(value: datetime) -> datetime:
    """@brief 规范 aware UTC / Normalize aware UTC.

    @param value 时间 / Instant.
    @return aware UTC / Aware UTC.
    """

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = [
    "AssistantToolOperations",
    "PostgresAssistantToolStore",
    "ToolTransactionMode",
]
