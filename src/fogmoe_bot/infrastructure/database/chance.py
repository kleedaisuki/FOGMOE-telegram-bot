"""@brief PostgreSQL 可验证随机活动适配器 / PostgreSQL verifiable-chance adapter.

本模块实现 ``ChanceRoundOperations``，把承诺揭示（commit-reveal）、免费金币账本和
幂等回执置于同一个短 PostgreSQL 事务中。读取接口从不选择未结算的 ``server_seed``；
结算成功后会将该列清空，公开种子只存在于公平性证明（fairness proof）中。
This module implements ``ChanceRoundOperations`` and puts commit-reveal, the free-token ledger,
and idempotency receipts in one short PostgreSQL transaction. Read paths never select an
unsettled ``server_seed``; after a successful settlement the column is cleared, and the disclosed
seed exists only in the fairness proof.

所需数据库契约（由迁移维护，本模块**不**创建或修改 schema）：

``chance.rounds`` 必须包含：

* ``round_id UUID PRIMARY KEY``、``owner_id BIGINT REFERENCES identity.users``；
* ``scope_kind TEXT``（``personal`` 或 ``group``）、``scope_id BIGINT``、
  ``topic_id BIGINT NULL``；
* ``ruleset JSONB``、``ruleset_fingerprint TEXT``、``rule_code TEXT``、``stake BIGINT``、
  ``nonce BIGINT``、``commitment TEXT``；
* ``server_seed BYTEA NULL``，其值仅在 ``committed`` 状态存在且受数据库访问控制保护；
* ``client_seed TEXT NULL``、``status TEXT``（``committed`` 或 ``settled``）、
  ``outcome_code TEXT NULL``、``payout BIGINT NULL``、``proof JSONB NULL``；以及
  ``committed_at TIMESTAMPTZ``、``settled_at TIMESTAMPTZ NULL``。

推荐用状态形状约束（state-shape constraint）保证 committed 行有私有 seed，settled 行
没有私有 seed、但有 ``client_seed``、``outcome_code``、``proof`` 和 ``settled_at``。
``payout`` 对输局允许为 NULL。

``chance.operation_receipts`` 必须包含 ``idempotency_key VARCHAR(200) PRIMARY KEY``、
``operation_kind VARCHAR(80)``、``actor_id BIGINT REFERENCES identity.users``、
``request_fingerprint CHAR(64)``、``result JSONB`` 和 ``created_at TIMESTAMPTZ``。回执和
状态变更一起提交，因此成功重试不会二次扣款或派彩。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from fractions import Fraction
from hashlib import sha256
from typing import Any, cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.chance.models import (
    CommitChanceRound,
    PreparedChanceRound,
    PrivateCommittedChanceRound,
)
from fogmoe_bot.application.chance.ports import (
    ChanceRoundOperations,
    ChanceRoundPreparer,
)
from fogmoe_bot.application.chance.workflow_models import (
    BindAndSettleChanceRound,
    ChanceRoundStatus,
    ChanceRoundView,
    ChanceWorkflowCode,
    ChanceWorkflowResult,
    CommitDurableChanceRound,
    LookupChanceRound,
)
from fogmoe_bot.domain.banking.ledger import LedgerAccount, LedgerReason
from fogmoe_bot.domain.banking.money import SystemAccountKind, TokenAmount, TokenBucket
from fogmoe_bot.domain.chance.fairness import (
    ClientSeed,
    FairnessProof,
    FairnessSample,
    ServerSeed,
    ServerSeedCommitment,
)
from fogmoe_bot.domain.chance.money import FreeTokenPayout, FreeTokenStake
from fogmoe_bot.domain.chance.rounds import (
    ChanceSettlement,
    CommittedChanceRound,
)
from fogmoe_bot.domain.chance.rules import ChanceOutcome, ChanceRule, ChanceRuleset
from fogmoe_bot.domain.chance.scope import (
    ChanceScopeKind,
    GroupRoundScope,
    PersonalRoundScope,
    RoundScope,
)
from fogmoe_bot.infrastructure.database import db
from fogmoe_bot.infrastructure.database.banking import (
    lock_bank_account_balances,
    post_bank_transfer,
)

_COMMIT_OPERATION: str = "chance.commit"
"""@brief 建立承诺的稳定回执操作名 / Stable receipt operation for commitment creation."""

_BIND_OPERATION: str = "chance.bind_and_settle"
"""@brief 绑定并结算的稳定回执操作名 / Stable receipt operation for bind-and-settle."""


class _ReceiptConflictError(ValueError):
    """@brief 同一幂等键被复用于不同语义 / One idempotency key was reused with different semantics."""


class PostgresChanceRoundOperations(ChanceRoundOperations):
    """@brief 以 PostgreSQL、银行账本和回执实现耐久随机轮次 / Implement durable chance rounds with PostgreSQL, bank ledger, and receipts.

    @note 账本账户一次按 ``user free -> activity pot`` 的稳定集合锁定。随后
        每个分录复用银行适配器的稳定排序锁，以避免与其他经济用例发生锁序反转。
        / Ledger accounts are first locked as the stable set ``user free -> activity pot``.
        Each posting then reuses the banking adapter's sorted locking, avoiding lock
        order inversions with other economic use cases.
    """

    async def commit_chance_round(
        self,
        command: CommitDurableChanceRound,
        private_round: PrivateCommittedChanceRound,
    ) -> ChanceWorkflowResult:
        """@brief 原子保存公开承诺和私有服务器种子 / Atomically save a public commitment and private server seed.

        @param command 耐久承诺命令 / Durable commitment command.
        @param private_round 含尚未揭示服务器种子的私有承诺态 /
            Private committed state holding the unrevealed server seed.
        @return 成功视图、重放视图或标准冲突结果 / Success view, replayed view, or standard conflict result.
        """

        fingerprint = _commit_request_fingerprint(command)
        async with db.transaction() as connection:
            await _lock_receipt_key(command.idempotency_key, connection)
            try:
                replay = await _load_receipt(
                    command.idempotency_key,
                    _COMMIT_OPERATION,
                    command.actor_id,
                    fingerprint,
                    connection,
                )
            except _ReceiptConflictError:
                return ChanceWorkflowResult(ChanceWorkflowCode.CONFLICT)
            if replay is not None:
                return _result_from_mapping(replay, replayed=True)

            if not await _identity_exists(command.actor_id, connection):
                return ChanceWorkflowResult(ChanceWorkflowCode.NOT_FOUND)
            if command.actor_id != private_round.committed_round.player_id:
                return ChanceWorkflowResult(ChanceWorkflowCode.FORBIDDEN)
            if not _private_round_matches_command(private_round, command.round):
                return ChanceWorkflowResult(ChanceWorkflowCode.CONFLICT)

            await _lock_round_id(command.round.round_id, connection)
            existing = await _load_round_row(
                command.round.round_id,
                connection,
                for_update=True,
                include_server_seed=False,
            )
            if existing is None:
                await _insert_committed_round(private_round, connection)
                view = ChanceRoundView(
                    private_round.committed_round,
                    ChanceRoundStatus.COMMITTED,
                )
            else:
                view = _view_from_row(existing)
                if view.committed_round != private_round.committed_round:
                    return ChanceWorkflowResult(ChanceWorkflowCode.CONFLICT)

            result = ChanceWorkflowResult(ChanceWorkflowCode.SUCCESS, view)
            await _save_receipt(
                command.idempotency_key,
                _COMMIT_OPERATION,
                command.actor_id,
                fingerprint,
                _result_mapping(result),
                connection,
            )
            return result

    async def bind_and_settle_chance_round(
        self,
        command: BindAndSettleChanceRound,
        prepare: ChanceRoundPreparer,
    ) -> ChanceWorkflowResult:
        """@brief 在一个事务内绑定种子、扣免费金币并结算 / Bind seed, charge free tokens, and settle in one transaction.

        @param command 绑定和结算命令 / Bind-and-settle command.
        @param prepare 在已锁私有承诺态上运行的准备回调 /
            Preparation callback running on the locked private committed state.
        @return 成功、可靠重放或标准状态结果 / Success, reliable replay, or standard state result.
        """

        fingerprint = _bind_request_fingerprint(command)
        async with db.transaction() as connection:
            await _lock_receipt_key(command.idempotency_key, connection)
            try:
                replay = await _load_receipt(
                    command.idempotency_key,
                    _BIND_OPERATION,
                    command.actor_id,
                    fingerprint,
                    connection,
                )
            except _ReceiptConflictError:
                return ChanceWorkflowResult(ChanceWorkflowCode.CONFLICT)
            if replay is not None:
                return _result_from_mapping(replay, replayed=True)

            row = await _load_round_row(
                command.round_id,
                connection,
                for_update=True,
                include_server_seed=True,
            )
            if row is None:
                return ChanceWorkflowResult(ChanceWorkflowCode.NOT_FOUND)

            committed = _committed_round_from_row(row)
            if committed.player_id != command.actor_id:
                return ChanceWorkflowResult(ChanceWorkflowCode.FORBIDDEN)
            if committed.scope != command.scope:
                return ChanceWorkflowResult(ChanceWorkflowCode.SCOPE_MISMATCH)
            status = ChanceRoundStatus(str(row["status"]))
            if status is ChanceRoundStatus.SETTLED:
                return ChanceWorkflowResult(ChanceWorkflowCode.ALREADY_SETTLED)

            private_round = _private_round_from_row(row)
            prepared = prepare(private_round)
            if not _prepared_round_matches(
                prepared,
                private_round,
                command.client_seed,
            ):
                return ChanceWorkflowResult(ChanceWorkflowCode.CONFLICT)
            settlement = prepared.settlement()

            player_wallet = LedgerAccount.user(committed.player_id, TokenBucket.FREE)
            activity_pot = LedgerAccount.system(SystemAccountKind.ACTIVITY_POT)
            balances = await lock_bank_account_balances(
                (player_wallet, activity_pot),
                connection,
            )
            if balances[player_wallet] < committed.stake.value:
                return ChanceWorkflowResult(ChanceWorkflowCode.INSUFFICIENT_FREE_TOKENS)
            if not _activity_pot_can_cover_payout(
                balances[activity_pot],
                committed.stake.value,
                settlement.credited,
            ):
                # Do not take the player's stake, reveal the server seed, or store an
                # idempotency receipt.  The same client seed can safely retry after a
                # bank administrator funds the explicitly auditable activity pot.
                return ChanceWorkflowResult(
                    ChanceWorkflowCode.INSUFFICIENT_ACTIVITY_POT
                )

            settled_at = datetime.now(UTC)
            metadata = _ledger_metadata(committed)
            await post_bank_transfer(
                namespace="chance-stake",
                source_idempotency_key=command.idempotency_key,
                reason=LedgerReason.ACTIVITY_STAKE,
                source=player_wallet,
                destination=activity_pot,
                amount=TokenAmount(committed.stake.value),
                created_at=settled_at,
                actor_id=command.actor_id,
                connection=connection,
                metadata=metadata,
            )

            if settlement.credited > 0:
                await post_bank_transfer(
                    namespace="chance-payout",
                    source_idempotency_key=command.idempotency_key,
                    reason=LedgerReason.ACTIVITY_PAYOUT,
                    source=activity_pot,
                    destination=player_wallet,
                    amount=TokenAmount(settlement.credited),
                    created_at=settled_at,
                    actor_id=command.actor_id,
                    connection=connection,
                    metadata={**metadata, "won": True},
                )

            await _persist_settlement(
                committed,
                settlement,
                command.client_seed,
                settled_at,
                connection,
            )
            view = ChanceRoundView(
                committed,
                ChanceRoundStatus.SETTLED,
                settlement,
            )
            result = ChanceWorkflowResult(ChanceWorkflowCode.SUCCESS, view)
            await _save_receipt(
                command.idempotency_key,
                _BIND_OPERATION,
                command.actor_id,
                fingerprint,
                _result_mapping(result),
                connection,
            )
            return result

    async def lookup_chance_round(
        self,
        command: LookupChanceRound,
    ) -> ChanceWorkflowResult:
        """@brief 读取不含私有种子的安全轮次视图 / Read a safe round view without private seed.

        @param command 按 owner 和明确范围过滤的查询命令 /
            Lookup command filtered by owner and explicit scope.
        @return 安全公开视图或标准拒绝结果 / Safe public view or standard rejection result.
        """

        async with db.transaction() as connection:
            row = await _load_round_row(
                command.round_id,
                connection,
                for_update=False,
                include_server_seed=False,
            )
            if row is None:
                return ChanceWorkflowResult(ChanceWorkflowCode.NOT_FOUND)
            view = _view_from_row(row)
            if view.owner_id != command.actor_id:
                return ChanceWorkflowResult(ChanceWorkflowCode.FORBIDDEN)
            if view.scope != command.scope:
                return ChanceWorkflowResult(ChanceWorkflowCode.SCOPE_MISMATCH)
            return ChanceWorkflowResult(ChanceWorkflowCode.SUCCESS, view)


async def _identity_exists(user_id: int, connection: AsyncConnection) -> bool:
    """@brief 检查操作者身份仍存在 / Check that the actor identity still exists.

    @param user_id Telegram 用户稳定标识 / Stable Telegram user identity.
    @param connection 当前事务连接 / Current transaction connection.
    @return identity 行存在时为 True / True when the identity row exists.
    """

    row = await db.fetch_one(
        "SELECT 1 FROM identity.users WHERE id = %s",
        (user_id,),
        connection=connection,
    )
    return row is not None


async def _lock_receipt_key(
    idempotency_key: str,
    connection: AsyncConnection,
) -> None:
    """@brief 为随机活动回执获取事务 advisory lock / Acquire a transaction advisory lock for a chance receipt.

    @param idempotency_key 来源事件幂等键 / Source-event idempotency key.
    @param connection 当前事务连接 / Current transaction connection.
    @return None / None.
    """

    await db.fetch_one(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"chance:receipt:{idempotency_key}",),
        connection=connection,
    )


async def _lock_round_id(round_id: UUID, connection: AsyncConnection) -> None:
    """@brief 串行化同一 UUID 的首次承诺创建 / Serialize first commitment creation for one UUID.

    @param round_id 待创建的稳定轮次 UUID / Stable round UUID being created.
    @param connection 当前事务连接 / Current transaction connection.
    @return None / None.
    @note 在行尚不存在时 ``FOR UPDATE`` 无法保护创建竞争；该 lock 填补这一空隙。
        / ``FOR UPDATE`` cannot protect a creation race before a row exists; this lock fills it.
    """

    await db.fetch_one(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"chance:round:{round_id}",),
        connection=connection,
    )


async def _load_receipt(
    idempotency_key: str,
    operation_kind: str,
    actor_id: int,
    fingerprint: str,
    connection: AsyncConnection,
) -> Mapping[str, Any] | None:
    """@brief 读取并严格验证随机活动幂等回执 / Load and strictly validate a chance idempotency receipt.

    @param idempotency_key 来源事件幂等键 / Source-event idempotency key.
    @param operation_kind 稳定操作类别 / Stable operation kind.
    @param actor_id 预期操作者 / Expected actor.
    @param fingerprint 规范化请求 SHA-256 指纹 / Canonical request SHA-256 fingerprint.
    @param connection 当前事务连接 / Current transaction connection.
    @return 完整 JSON 结果；首次调用时为 None / Complete JSON result, or None on first call.
    @raise _ReceiptConflictError 同一键重用为不同请求时抛出 /
        Raised when one key is reused for a different request.
    """

    row = await db.fetch_one(
        "SELECT operation_kind, actor_id, request_fingerprint, result "
        "FROM chance.operation_receipts WHERE idempotency_key = %s",
        (idempotency_key,),
        mapping=True,
        connection=connection,
    )
    if row is None:
        return None
    value = cast(Mapping[str, Any], row)
    if (
        str(value["operation_kind"]) != operation_kind
        or int(value["actor_id"]) != actor_id
        or str(value["request_fingerprint"]) != fingerprint
    ):
        raise _ReceiptConflictError("Chance idempotency key changed request semantics")
    return _json_mapping(value["result"], label="chance receipt result")


async def _save_receipt(
    idempotency_key: str,
    operation_kind: str,
    actor_id: int,
    fingerprint: str,
    result: Mapping[str, object],
    connection: AsyncConnection,
) -> None:
    """@brief 与随机活动状态变更同事务保存成功回执 / Save a successful receipt in the chance-state transaction.

    @param idempotency_key 来源事件幂等键 / Source-event idempotency key.
    @param operation_kind 稳定操作类别 / Stable operation kind.
    @param actor_id 操作者标识 / Actor identity.
    @param fingerprint 规范化请求 SHA-256 指纹 / Canonical request SHA-256 fingerprint.
    @param result 完整 JSON 兼容成功结果 / Complete JSON-compatible successful result.
    @param connection 当前事务连接 / Current transaction connection.
    @return None / None.
    """

    await db.execute(
        "INSERT INTO chance.operation_receipts ("
        "idempotency_key, operation_kind, actor_id, request_fingerprint, result"
        ") VALUES (%s, %s, %s, %s, CAST(%s AS JSONB))",
        (
            idempotency_key,
            operation_kind,
            actor_id,
            fingerprint,
            json.dumps(
                dict(result), ensure_ascii=True, sort_keys=True, separators=(",", ":")
            ),
        ),
        connection=connection,
    )


async def _load_round_row(
    round_id: UUID,
    connection: AsyncConnection,
    *,
    for_update: bool,
    include_server_seed: bool,
) -> Mapping[str, Any] | None:
    """@brief 读取随机轮次；按需排除私有服务器种子 / Load a chance round and exclude private seed when requested.

    @param round_id 轮次 UUID / Round UUID.
    @param connection 当前事务连接 / Current transaction connection.
    @param for_update 是否锁定该轮次行 / Whether to lock the round row.
    @param include_server_seed 是否允许读取仅结算路径需要的私有种子 /
        Whether to read the private seed needed only by the settlement path.
    @return 原始命名行或 None / Raw named row or None.
    """

    columns = (
        "round_id, owner_id, scope_kind, scope_id, topic_id, ruleset, "
        "ruleset_fingerprint, rule_code, stake, nonce, commitment, "
        + ("server_seed, " if include_server_seed else "")
        + "client_seed, status, outcome_code, payout, proof"
    )
    lock_clause = " FOR UPDATE" if for_update else ""
    row = await db.fetch_one(
        "SELECT " + columns + " FROM chance.rounds WHERE round_id = %s" + lock_clause,
        (round_id,),
        mapping=True,
        connection=connection,
    )
    return cast(Mapping[str, Any], row) if row is not None else None


async def _insert_committed_round(
    private_round: PrivateCommittedChanceRound,
    connection: AsyncConnection,
) -> None:
    """@brief 插入一份仅含承诺和私有种子的 committed 轮次 / Insert a committed round containing only commitment and private seed.

    @param private_round 待保存的私有承诺态 / Private committed state to persist.
    @param connection 当前事务连接 / Current transaction connection.
    @return None / None.
    """

    committed = private_round.committed_round
    scope_kind, scope_id, topic_id = _scope_columns(committed.scope)
    await db.execute(
        "INSERT INTO chance.rounds ("
        "round_id, owner_id, scope_kind, scope_id, topic_id, ruleset, "
        "ruleset_fingerprint, rule_code, stake, nonce, commitment, server_seed, "
        "client_seed, status, outcome_code, payout, proof, committed_at, settled_at"
        ") VALUES ("
        "%s, %s, %s, %s, %s, CAST(%s AS JSONB), %s, %s, %s, %s, %s, %s, "
        "NULL, %s, NULL, NULL, NULL, CURRENT_TIMESTAMP, NULL"
        ")",
        (
            committed.round_id,
            committed.player_id,
            scope_kind.value,
            scope_id,
            topic_id,
            json.dumps(
                _ruleset_mapping(committed.ruleset), ensure_ascii=True, sort_keys=True
            ),
            committed.ruleset_fingerprint,
            committed.rule_code,
            committed.stake.value,
            committed.nonce,
            committed.commitment.hex_digest,
            private_round.server_seed.value,
            ChanceRoundStatus.COMMITTED.value,
        ),
        connection=connection,
    )


async def _persist_settlement(
    committed: CommittedChanceRound,
    settlement: ChanceSettlement,
    client_seed: ClientSeed,
    settled_at: datetime,
    connection: AsyncConnection,
) -> None:
    """@brief 保存结算证据并清理已揭示的私有种子 / Persist settlement evidence and clear the now-revealed private seed.

    @param committed 已锁定的公开承诺轮次 / Locked public committed round.
    @param settlement 经账本覆盖后待持久化的结算 / Settlement to persist after ledger coverage.
    @param client_seed 本次绑定的玩家种子 / Player seed bound by this request.
    @param settled_at 统一的结算业务时刻 / Shared settlement business time.
    @param connection 当前事务连接 / Current transaction connection.
    @return None / None.
    @raise RuntimeError 行状态在锁内异常变化时抛出 /
        Raised when row state unexpectedly changes while locked.
    """

    changed = await db.execute(
        "UPDATE chance.rounds SET client_seed = %s, status = %s, outcome_code = %s, "
        "payout = %s, proof = CAST(%s AS JSONB), server_seed = NULL, settled_at = %s "
        "WHERE round_id = %s AND status = %s",
        (
            client_seed.value,
            ChanceRoundStatus.SETTLED.value,
            settlement.outcome.code,
            settlement.credited if settlement.credited > 0 else None,
            json.dumps(
                _proof_mapping(settlement.proof), ensure_ascii=True, sort_keys=True
            ),
            settled_at,
            committed.round_id,
            ChanceRoundStatus.COMMITTED.value,
        ),
        connection=connection,
    )
    if changed != 1:
        raise RuntimeError("Chance round changed while it was locked")


def _view_from_row(row: Mapping[str, Any]) -> ChanceRoundView:
    """@brief 从不含未揭示 seed 的数据库行恢复安全视图 / Restore a safe view from a database row without unrevealed seed.

    @param row 公开列组成的命名数据库行 / Named database row composed of public columns.
    @return 受状态保护的公开轮次视图 / Public round view protected by status.
    """

    committed = _committed_round_from_row(row)
    status = ChanceRoundStatus(str(row["status"]))
    if status is ChanceRoundStatus.COMMITTED:
        return ChanceRoundView(committed, status)
    if status is ChanceRoundStatus.SETTLED:
        return ChanceRoundView(
            committed,
            status,
            _settlement_from_mapping(
                committed,
                {
                    "client_seed": row["client_seed"],
                    "outcome_code": row["outcome_code"],
                    "payout": row["payout"],
                    "proof": row["proof"],
                },
            ),
        )
    raise ValueError("Unknown chance round status")


def _private_round_from_row(row: Mapping[str, Any]) -> PrivateCommittedChanceRound:
    """@brief 从已锁 committed 行恢复私有承诺态 / Restore private committed state from a locked committed row.

    @param row 含 ``server_seed`` 的已锁数据库行 / Locked database row containing ``server_seed``.
    @return 仅供事务内准备回调使用的私有承诺态 /
        Private committed state usable only by the in-transaction preparation callback.
    @raise ValueError 行缺失尚未揭示种子时抛出 / Raised when row lacks its unrevealed seed.
    """

    raw_seed = row.get("server_seed")
    if raw_seed is None:
        raise ValueError("Committed chance round is missing its private server seed")
    return PrivateCommittedChanceRound(
        _committed_round_from_row(row),
        ServerSeed(_binary_value(raw_seed, label="chance server seed")),
    )


def _committed_round_from_row(row: Mapping[str, Any]) -> CommittedChanceRound:
    """@brief 从 PostgreSQL 公共列恢复承诺轮次 / Restore a committed round from PostgreSQL public columns.

    @param row 含公开轮次列的命名数据库行 / Named database row with public round columns.
    @return 经规则集指纹校验的承诺轮次 / Committed round validated against ruleset fingerprint.
    """

    ruleset = _ruleset_from_mapping(
        _json_mapping(row["ruleset"], label="chance ruleset")
    )
    persisted_fingerprint = str(row["ruleset_fingerprint"])
    if ruleset.fingerprint != persisted_fingerprint:
        raise ValueError("Persisted chance ruleset fingerprint does not match payload")
    return CommittedChanceRound(
        round_id=_uuid_value(row["round_id"], label="chance round id"),
        scope=_scope_from_columns(
            str(row["scope_kind"]),
            _integer_value(row["scope_id"], label="chance scope id"),
            _optional_integer_value(row.get("topic_id"), label="chance topic id"),
        ),
        player_id=_integer_value(row["owner_id"], label="chance owner id"),
        ruleset=ruleset,
        rule_code=str(row["rule_code"]),
        stake=FreeTokenStake(_integer_value(row["stake"], label="chance stake")),
        commitment=ServerSeedCommitment(str(row["commitment"])),
        nonce=_integer_value(row["nonce"], label="chance nonce"),
    )


def _result_mapping(result: ChanceWorkflowResult) -> dict[str, object]:
    """@brief 序列化完整随机活动结果以供可靠回放 / Serialize a complete chance result for reliable replay.

    @param result 待写入幂等回执的成功结果 / Successful result to write into an idempotency receipt.
    @return JSON 兼容且足以离线还原的结果对象 /
        JSON-compatible result object sufficient for offline restoration.
    @raise ValueError 非成功结果被错误地尝试存为回执时抛出 /
        Raised when a non-success result is incorrectly stored as a receipt.
    """

    if result.code is not ChanceWorkflowCode.SUCCESS or result.view is None:
        raise ValueError(
            "Only successful chance results may be stored as replay receipts"
        )
    return {
        "schema_version": 1,
        "code": result.code.value,
        "view": _view_mapping(result.view),
    }


def _result_from_mapping(
    value: Mapping[str, Any],
    *,
    replayed: bool,
) -> ChanceWorkflowResult:
    """@brief 从 JSON 回执恢复完整随机活动结果 / Restore a complete chance result from a JSON receipt.

    @param value JSON 回执对象 / JSON receipt object.
    @param replayed 是否应标记为幂等重放 / Whether to mark the result as an idempotency replay.
    @return 经过所有领域不变量重新校验的结果 / Result revalidated through all domain invariants.
    """

    if (
        _integer_value(
            value.get("schema_version"), label="chance receipt schema version"
        )
        != 1
    ):
        raise ValueError("Unsupported chance receipt schema version")
    code = ChanceWorkflowCode(str(value["code"]))
    raw_view = value.get("view")
    if not isinstance(raw_view, Mapping):
        raise ValueError("Chance receipt must contain a complete round view")
    return ChanceWorkflowResult(
        code,
        _view_from_mapping(cast(Mapping[str, Any], raw_view)),
        replayed=replayed,
    )


def _view_mapping(view: ChanceRoundView) -> dict[str, object]:
    """@brief 序列化公开轮次视图 / Serialize a public round view.

    @param view 不含未揭示私有 seed 的公开视图 / Public view without unrevealed private seed.
    @return JSON 兼容视图对象 / JSON-compatible view object.
    """

    return {
        "round": _committed_round_mapping(view.committed_round),
        "status": view.status.value,
        "settlement": (
            _settlement_mapping(view.settlement)
            if view.settlement is not None
            else None
        ),
    }


def _view_from_mapping(value: Mapping[str, Any]) -> ChanceRoundView:
    """@brief 从 JSON 对象恢复并验证公开轮次视图 / Restore and validate a public round view from JSON.

    @param value JSON 兼容视图对象 / JSON-compatible view object.
    @return 重新验证状态和结算一致性的公开视图 /
        Public view whose status and settlement consistency was revalidated.
    """

    raw_round = value.get("round")
    if not isinstance(raw_round, Mapping):
        raise ValueError("Chance view must contain a committed round")
    committed = _committed_round_from_mapping(cast(Mapping[str, Any], raw_round))
    status = ChanceRoundStatus(str(value["status"]))
    raw_settlement = value.get("settlement")
    settlement = (
        _settlement_from_mapping(committed, cast(Mapping[str, Any], raw_settlement))
        if isinstance(raw_settlement, Mapping)
        else None
    )
    return ChanceRoundView(committed, status, settlement)


def _committed_round_mapping(round_snapshot: CommittedChanceRound) -> dict[str, object]:
    """@brief 序列化一份公开承诺轮次 / Serialize one public committed round.

    @param round_snapshot 已公开承诺的轮次 / Publicly committed round.
    @return JSON 兼容承诺轮次对象；不包含服务器 seed /
        JSON-compatible committed-round object without server seed.
    """

    return {
        "round_id": str(round_snapshot.round_id),
        "scope": _scope_mapping(round_snapshot.scope),
        "player_id": round_snapshot.player_id,
        "ruleset": _ruleset_mapping(round_snapshot.ruleset),
        "ruleset_fingerprint": round_snapshot.ruleset_fingerprint,
        "rule_code": round_snapshot.rule_code,
        "stake": round_snapshot.stake.value,
        "commitment": round_snapshot.commitment.hex_digest,
        "nonce": round_snapshot.nonce,
    }


def _committed_round_from_mapping(value: Mapping[str, Any]) -> CommittedChanceRound:
    """@brief 从 JSON 对象恢复并验证公开承诺轮次 / Restore and validate a public committed round from JSON.

    @param value JSON 兼容承诺轮次对象 / JSON-compatible committed-round object.
    @return 规则集指纹一致的承诺轮次 / Committed round with matching ruleset fingerprint.
    """

    raw_scope = value.get("scope")
    raw_ruleset = value.get("ruleset")
    if not isinstance(raw_scope, Mapping) or not isinstance(raw_ruleset, Mapping):
        raise ValueError("Chance committed round requires scope and ruleset mappings")
    ruleset = _ruleset_from_mapping(cast(Mapping[str, Any], raw_ruleset))
    if ruleset.fingerprint != str(value["ruleset_fingerprint"]):
        raise ValueError("Chance receipt ruleset fingerprint does not match payload")
    return CommittedChanceRound(
        round_id=_uuid_value(value["round_id"], label="chance round id"),
        scope=_scope_from_mapping(cast(Mapping[str, Any], raw_scope)),
        player_id=_integer_value(value["player_id"], label="chance player id"),
        ruleset=ruleset,
        rule_code=str(value["rule_code"]),
        stake=FreeTokenStake(_integer_value(value["stake"], label="chance stake")),
        commitment=ServerSeedCommitment(str(value["commitment"])),
        nonce=_integer_value(value["nonce"], label="chance nonce"),
    )


def _settlement_mapping(settlement: ChanceSettlement) -> dict[str, object]:
    """@brief 序列化已揭示的结算证据 / Serialize revealed settlement evidence.

    @param settlement 已完成且可公开复验的结算 / Completed and publicly verifiable settlement.
    @return JSON 兼容结算对象 / JSON-compatible settlement object.
    """

    return {
        "client_seed": settlement.round.client_seed.value,
        "outcome_code": settlement.outcome.code,
        "payout": settlement.credited if settlement.credited > 0 else None,
        "proof": _proof_mapping(settlement.proof),
    }


def _settlement_from_mapping(
    committed: CommittedChanceRound,
    value: Mapping[str, Any],
) -> ChanceSettlement:
    """@brief 从 JSON 结算证据恢复可复验结算 / Restore a verifiable settlement from JSON evidence.

    @param committed 对应的公开承诺轮次 / Corresponding public committed round.
    @param value JSON 兼容结算对象 / JSON-compatible settlement object.
    @return 完整且经证明校验的结算 / Complete settlement validated by its proof.
    """

    raw_proof = value.get("proof")
    if not isinstance(raw_proof, Mapping):
        raise ValueError("Chance settlement requires a fairness proof")
    client_seed = ClientSeed(str(value["client_seed"]))
    round_snapshot = committed.bind_client_seed(client_seed)
    proof = _proof_from_mapping(cast(Mapping[str, Any], raw_proof))
    if (
        proof.round_id != round_snapshot.round_id
        or proof.commitment != round_snapshot.commitment
        or proof.client_seed != round_snapshot.client_seed
        or proof.nonce != round_snapshot.nonce
    ):
        raise ValueError("Chance settlement proof does not bind the persisted round")
    outcome = round_snapshot.ruleset.outcome_for_ticket(proof.sample.ticket)
    if outcome.code != str(value["outcome_code"]):
        raise ValueError("Chance settlement outcome does not match fairness proof")
    raw_payout = value.get("payout")
    payout = (
        FreeTokenPayout(_integer_value(raw_payout, label="chance payout"))
        if raw_payout is not None
        else None
    )
    return ChanceSettlement(round_snapshot, outcome, proof, payout)


def _proof_mapping(proof: FairnessProof) -> dict[str, object]:
    """@brief 序列化已揭示的公平性证明 / Serialize a revealed fairness proof.

    @param proof 已可公开的公平性证明 / Fairness proof that is now public.
    @return JSON 兼容证明对象 / JSON-compatible proof object.
    """

    return {
        "round_id": str(proof.round_id),
        "commitment": proof.commitment.hex_digest,
        "revealed_server_seed": proof.revealed_server_seed.reveal_hex(),
        "client_seed": proof.client_seed.value,
        "nonce": proof.nonce,
        "upper_bound": proof.upper_bound,
        "sample": {
            "ticket": proof.sample.ticket,
            "attempt": proof.sample.attempt,
            "digest_hex": proof.sample.digest_hex,
        },
    }


def _proof_from_mapping(value: Mapping[str, Any]) -> FairnessProof:
    """@brief 从 JSON 对象恢复公平性证明 / Restore a fairness proof from a JSON object.

    @param value JSON 兼容证明对象 / JSON-compatible proof object.
    @return 经公平性协议构造器校验的证明 / Proof validated by fairness-protocol constructors.
    """

    raw_sample = value.get("sample")
    if not isinstance(raw_sample, Mapping):
        raise ValueError("Chance fairness proof requires a sample mapping")
    sample = FairnessSample(
        ticket=_integer_value(raw_sample["ticket"], label="chance fairness ticket"),
        attempt=_integer_value(raw_sample["attempt"], label="chance fairness attempt"),
        digest_hex=str(raw_sample["digest_hex"]),
    )
    return FairnessProof(
        round_id=_uuid_value(value["round_id"], label="chance proof round id"),
        commitment=ServerSeedCommitment(str(value["commitment"])),
        revealed_server_seed=ServerSeed(
            bytes.fromhex(str(value["revealed_server_seed"]))
        ),
        client_seed=ClientSeed(str(value["client_seed"])),
        nonce=_integer_value(value["nonce"], label="chance proof nonce"),
        upper_bound=_integer_value(
            value["upper_bound"], label="chance proof upper bound"
        ),
        sample=sample,
    )


def _ruleset_mapping(ruleset: ChanceRuleset) -> dict[str, object]:
    """@brief 序列化冻结规则集而不依赖运行时目录 / Serialize a frozen ruleset without a runtime catalog.

    @param ruleset 不可变规则集 / Immutable ruleset.
    @return 可在历史回执中完整重建的 JSON 规则集 / JSON ruleset that can fully rebuild historical receipts.
    """

    return {
        "code": ruleset.code,
        "revision": ruleset.revision,
        "outcomes": [
            {"code": outcome.code, "weight": outcome.weight}
            for outcome in ruleset.outcomes
        ],
        "rules": [
            {
                "code": rule.code,
                "winning_outcome_codes": sorted(rule.winning_outcome_codes),
                "house_edge": [rule.house_edge.numerator, rule.house_edge.denominator],
            }
            for rule in ruleset.rules
        ],
    }


def _ruleset_from_mapping(value: Mapping[str, Any]) -> ChanceRuleset:
    """@brief 从冻结 JSON 配置恢复规则集 / Restore a ruleset from frozen JSON configuration.

    @param value JSON 兼容规则集对象 / JSON-compatible ruleset object.
    @return 经领域不变量校验的规则集 / Ruleset validated by domain invariants.
    """

    raw_outcomes = value.get("outcomes")
    raw_rules = value.get("rules")
    if not isinstance(raw_outcomes, list) or not isinstance(raw_rules, list):
        raise ValueError("Chance ruleset requires outcome and rule lists")
    outcomes = tuple(
        ChanceOutcome(
            str(_mapping_item(item, label="chance outcome")["code"]),
            _integer_value(
                _mapping_item(item, label="chance outcome")["weight"],
                label="chance outcome weight",
            ),
        )
        for item in raw_outcomes
    )
    rules = tuple(
        _rule_from_mapping(_mapping_item(item, label="chance rule"))
        for item in raw_rules
    )
    return ChanceRuleset(
        code=str(value["code"]),
        revision=_integer_value(value["revision"], label="chance ruleset revision"),
        outcomes=outcomes,
        rules=rules,
    )


def _rule_from_mapping(value: Mapping[str, Any]) -> ChanceRule:
    """@brief 从 JSON 对象恢复单条下注规则 / Restore one wager rule from a JSON object.

    @param value JSON 兼容规则对象 / JSON-compatible rule object.
    @return 经庄家优势与中奖集校验的规则 / Rule validated for edge and winning set.
    """

    raw_winners = value.get("winning_outcome_codes")
    raw_edge = value.get("house_edge")
    if not isinstance(raw_winners, list):
        raise ValueError("Chance rule requires a winning-outcome list")
    if not isinstance(raw_edge, list) or len(raw_edge) != 2:
        raise ValueError("Chance rule requires a numerator-denominator house edge")
    return ChanceRule(
        code=str(value["code"]),
        winning_outcome_codes=frozenset(str(item) for item in raw_winners),
        house_edge=Fraction(
            _integer_value(raw_edge[0], label="chance house-edge numerator"),
            _integer_value(raw_edge[1], label="chance house-edge denominator"),
        ),
    )


def _scope_mapping(scope: RoundScope) -> dict[str, object]:
    """@brief 序列化明确个人或群组范围 / Serialize an explicit personal or group scope.

    @param scope 轮次所属范围 / Owning round scope.
    @return JSON 兼容范围对象 / JSON-compatible scope object.
    """

    if isinstance(scope, PersonalRoundScope):
        return {"kind": scope.kind.value, "user_id": scope.user_id}
    return {
        "kind": scope.kind.value,
        "group_id": scope.group_id,
        "topic_id": scope.topic_id,
    }


def _scope_from_mapping(value: Mapping[str, Any]) -> RoundScope:
    """@brief 从 JSON 对象恢复明确轮次范围 / Restore an explicit round scope from a JSON object.

    @param value JSON 兼容范围对象 / JSON-compatible scope object.
    @return 个人或群组范围 / Personal or group scope.
    """

    kind = ChanceScopeKind(str(value["kind"]))
    if kind is ChanceScopeKind.PERSONAL:
        return PersonalRoundScope(
            _integer_value(value["user_id"], label="chance user id")
        )
    return GroupRoundScope(
        _integer_value(value["group_id"], label="chance group id"),
        _optional_integer_value(value.get("topic_id"), label="chance topic id"),
    )


def _scope_columns(scope: RoundScope) -> tuple[ChanceScopeKind, int, int | None]:
    """@brief 将明确范围编码为轮次表列 / Encode an explicit scope into round-table columns.

    @param scope 待写入的轮次范围 / Round scope to persist.
    @return 范围类别、稳定范围标识和可选话题 / Kind, stable scope identity, and optional topic.
    """

    if isinstance(scope, PersonalRoundScope):
        return scope.kind, scope.user_id, None
    return scope.kind, scope.group_id, scope.topic_id


def _scope_from_columns(
    raw_kind: str,
    scope_id: int,
    topic_id: int | None,
) -> RoundScope:
    """@brief 从轮次表列恢复明确范围 / Restore an explicit scope from round-table columns.

    @param raw_kind 持久化范围类别 / Persisted scope kind.
    @param scope_id 个人或群组稳定标识 / Personal or group stable identity.
    @param topic_id 可选群组话题 / Optional group topic.
    @return 经范围不变量校验的个人或群组范围 /
        Personal or group scope validated by scope invariants.
    """

    kind = ChanceScopeKind(raw_kind)
    if kind is ChanceScopeKind.PERSONAL:
        if topic_id is not None:
            raise ValueError("Personal chance scope cannot contain a topic")
        return PersonalRoundScope(scope_id)
    return GroupRoundScope(scope_id, topic_id)


def _commit_request_fingerprint(command: CommitDurableChanceRound) -> str:
    """@brief 计算开轮请求的规范语义指纹 / Compute canonical semantics fingerprint for a commit request.

    @param command 耐久开轮命令 / Durable commit command.
    @return SHA-256 十六进制请求指纹 / SHA-256 hexadecimal request fingerprint.
    @note 私有服务器 seed 和随机 commitment 故意不参与指纹；同一来源事件重试时工作流可能
        已消耗一份新熵，但必须重放最初的承诺。/ The private server seed and random commitment
        are deliberately excluded: a retried source event may have consumed new entropy but must
        replay the original commitment.
    """

    round_command = command.round
    return _request_fingerprint(
        {
            "round_id": str(round_command.round_id),
            "actor_id": command.actor_id,
            "scope": _scope_mapping(round_command.scope),
            "player_id": round_command.player_id,
            "ruleset_fingerprint": round_command.ruleset.fingerprint,
            "rule_code": round_command.rule_code,
            "stake": round_command.stake.value,
            "nonce": round_command.nonce,
        }
    )


def _bind_request_fingerprint(command: BindAndSettleChanceRound) -> str:
    """@brief 计算绑定结算请求的规范语义指纹 / Compute canonical semantics fingerprint for a bind-and-settle request.

    @param command 绑定和结算命令 / Bind-and-settle command.
    @return SHA-256 十六进制请求指纹 / SHA-256 hexadecimal request fingerprint.
    """

    return _request_fingerprint(
        {
            "round_id": str(command.round_id),
            "actor_id": command.actor_id,
            "scope": _scope_mapping(command.scope),
            "client_seed": command.client_seed.value,
        }
    )


def _request_fingerprint(value: Mapping[str, object]) -> str:
    """@brief 对规范 JSON 请求语义生成 SHA-256 / Generate SHA-256 over canonical JSON request semantics.

    @param value 完全由公开、确定性字段组成的请求语义 / Request semantics made solely of public deterministic fields.
    @return 小写 SHA-256 十六进制摘要 / Lowercase SHA-256 hexadecimal digest.
    """

    encoded = json.dumps(
        dict(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _private_round_matches_command(
    private_round: PrivateCommittedChanceRound,
    command: CommitChanceRound,
) -> bool:
    """@brief 验证私有承诺态确实由给定开轮命令产生 / Verify private committed state was produced by the given commit command.

    @param private_round 待持久化私有承诺态 / Private committed state to persist.
    @param command 原始纯数学开轮命令 / Original pure mathematical commit command.
    @return 所有非随机业务字段相同时为 True / True when all non-random business fields match.
    """

    committed = private_round.committed_round
    return (
        committed.round_id == command.round_id
        and committed.scope == command.scope
        and committed.player_id == command.player_id
        and committed.ruleset == command.ruleset
        and committed.rule_code == command.rule_code
        and committed.stake == command.stake
        and committed.nonce == command.nonce
    )


def _prepared_round_matches(
    prepared: object,
    private_round: PrivateCommittedChanceRound,
    client_seed: ClientSeed,
) -> bool:
    """@brief 验证准备回调未替换轮次、seed 或免费押注 / Verify preparer did not replace round, seed, or free stake.

    @param prepared 准备回调返回的私有准备态 / Private prepared state returned by callback.
    @param private_round 从受保护行读取的私有承诺态 /
        Private committed state read from protected row.
    @param client_seed 请求绑定的玩家种子 / Player seed requested for binding.
    @return 完整轮次和服务器 seed 都严格派生自锁定状态时为 True /
        True when full round and server seed strictly derive from locked state.
    """

    if not isinstance(prepared, PreparedChanceRound):
        return False
    round_snapshot = prepared.round
    committed = private_round.committed_round
    return (
        prepared.server_seed == private_round.server_seed
        and round_snapshot.round_id == committed.round_id
        and round_snapshot.scope == committed.scope
        and round_snapshot.player_id == committed.player_id
        and round_snapshot.ruleset == committed.ruleset
        and round_snapshot.rule_code == committed.rule_code
        and round_snapshot.stake == committed.stake
        and round_snapshot.commitment == committed.commitment
        and round_snapshot.client_seed == client_seed
        and round_snapshot.nonce == committed.nonce
    )


def _activity_pot_can_cover_payout(
    activity_pot_balance: int,
    incoming_stake: int,
    payout: int,
) -> bool:
    """@brief 判断现有奖池连同本轮押注能否覆盖派彩 / Check whether the pot plus this stake can cover a payout.

    @param activity_pot_balance 本轮扣款前锁定的非负奖池余额 /
        Locked non-negative activity-pot balance before this round's debit.
    @param incoming_stake 即将从玩家免费钱包转入的严格正押注 /
        Strictly positive stake about to move from the player's free wallet.
    @param payout 获胜时应贷记的非负总派彩；输局为零 /
        Non-negative gross payout to credit on a win, or zero on a loss.
    @return 奖池可以在不隐式发行的情况下覆盖派彩时为 True /
        True when the pot can cover the payout without implicit issuance.
    @raise ValueError 任一金额不满足其整数/符号约束时抛出 /
        Raised when an amount violates its integer or sign constraint.
    """

    if (
        isinstance(activity_pot_balance, bool)
        or not isinstance(activity_pot_balance, int)
        or activity_pot_balance < 0
    ):
        raise ValueError("Chance activity-pot balance must be a non-negative integer")
    if (
        isinstance(incoming_stake, bool)
        or not isinstance(incoming_stake, int)
        or incoming_stake <= 0
    ):
        raise ValueError("Chance incoming stake must be a positive integer")
    if isinstance(payout, bool) or not isinstance(payout, int) or payout < 0:
        raise ValueError("Chance payout must be a non-negative integer")
    return activity_pot_balance + incoming_stake >= payout


def _ledger_metadata(committed: CommittedChanceRound) -> dict[str, str | int | bool]:
    """@brief 构造三条机会活动分录共用的审计元数据 / Build shared audit metadata for chance ledger entries.

    @param committed 已固定的公开承诺轮次 / Frozen public committed round.
    @return 符合银行元数据值域的审计映射 / Audit mapping within the bank metadata value domain.
    """

    return {
        "chance_round_id": str(committed.round_id),
        "chance_ruleset_fingerprint": committed.ruleset_fingerprint,
        "chance_rule_code": committed.rule_code,
        "chance_scope_kind": committed.scope.kind.value,
        "chance_stake": committed.stake.value,
    }


def _json_mapping(value: object, *, label: str) -> Mapping[str, Any]:
    """@brief 将 PostgreSQL JSONB 值规范为命名映射 / Normalize a PostgreSQL JSONB value to a named mapping.

    @param value JSONB driver 值或 JSON 文本 / JSONB driver value or JSON text.
    @param label 用于错误说明的字段名称 / Field name used in error messages.
    @return 只读语义的映射视图 / Mapping view with read-only semantics.
    @raise ValueError 值不是 JSON object 时抛出 / Raised when value is not a JSON object.
    """

    decoded: object = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    return cast(Mapping[str, Any], decoded)


def _mapping_item(value: object, *, label: str) -> Mapping[str, Any]:
    """@brief 验证嵌套 JSON 项为 object / Validate a nested JSON item is an object.

    @param value 待验证嵌套值 / Nested value to validate.
    @param label 用于错误说明的字段名称 / Field name used in error messages.
    @return 已验证映射 / Validated mapping.
    """

    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    return cast(Mapping[str, Any], value)


def _integer_value(value: object, *, label: str) -> int:
    """@brief 读取严格整数 JSON/数据库值 / Read a strict integer JSON/database value.

    @param value 待读取值 / Value to read.
    @param label 用于错误说明的字段名称 / Field name used in error messages.
    @return 严格整数 / Strict integer.
    @raise ValueError 值不是整数或为 bool 时抛出 / Raised when value is not an integer or is bool.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _optional_integer_value(value: object, *, label: str) -> int | None:
    """@brief 读取可空严格整数 JSON/数据库值 / Read an optional strict integer JSON/database value.

    @param value 待读取可空值 / Optional value to read.
    @param label 用于错误说明的字段名称 / Field name used in error messages.
    @return None 或严格整数 / None or strict integer.
    """

    return None if value is None else _integer_value(value, label=label)


def _uuid_value(value: object, *, label: str) -> UUID:
    """@brief 读取 UUID 或其规范文本 / Read a UUID or its canonical text.

    @param value UUID 对象或文本 / UUID object or text.
    @param label 用于错误说明的字段名称 / Field name used in error messages.
    @return UUID 对象 / UUID object.
    @raise ValueError 值无法表示 UUID 时抛出 / Raised when value cannot represent a UUID.
    """

    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be a UUID") from error


def _binary_value(value: object, *, label: str) -> bytes:
    """@brief 读取 PostgreSQL BYTEA 为不可变 bytes / Read PostgreSQL BYTEA as immutable bytes.

    @param value bytes、bytearray 或 memoryview / Bytes, bytearray, or memoryview.
    @param label 用于错误说明的字段名称 / Field name used in error messages.
    @return 不可变 bytes / Immutable bytes.
    @raise ValueError 值不是二进制值时抛出 / Raised when value is not binary.
    """

    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise ValueError(f"{label} must be binary")
    return bytes(value)


__all__ = ["PostgresChanceRoundOperations"]
