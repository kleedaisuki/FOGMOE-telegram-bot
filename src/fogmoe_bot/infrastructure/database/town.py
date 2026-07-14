"""@brief PostgreSQL 群组小镇与金库适配器 / PostgreSQL group-town and treasury adapter."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
import json
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.town.models import (
    CompleteTownProject,
    ContributeToTown,
    CreateTownProject,
    EnsureTown,
    TownCode,
    TownResult,
)
from fogmoe_bot.application.town.ports import TownOperations
from fogmoe_bot.domain.banking.ledger import LedgerAccount, LedgerReason
from fogmoe_bot.domain.banking.money import SystemAccountKind, TokenAmount, TokenBucket
from fogmoe_bot.domain.town.models import (
    Town,
    TownContribution,
    TownProject,
    TownProjectKind,
    TownProjectStatus,
    TownTreasury,
)
from fogmoe_bot.domain.town.scope import TownScope
from fogmoe_bot.domain.world.scope import PersonalScope
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database.banking import (
    lock_bank_account_balances,
    post_bank_transfer,
)


class PostgresTownOperations(TownOperations):
    """@brief 将群组小镇状态和双重记账置于同一事务 / Put group-town state and double-entry bookkeeping in one transaction.

    @note 小镇表的金库字段是便于读取的受审计摘要；真正可花余额来自 ``bank``
        账本。每次贡献和项目结算都会在持有同一事务、同一组锁的情况下验证二者一致。
        / Treasury fields in town tables are audited read summaries; the spendable balance lives
        in the ``bank`` ledger. Each contribution and project settlement checks them together
        while holding one transaction and one ordered lock set.
    """

    async def ensure_town(self, command: EnsureTown) -> TownResult:
        """@brief 读取或创建一个群组唯一小镇 / Read or create the one town for a group.

        @param command 小镇读取或创建命令 / Town ensure command.
        @return 可幂等重放的小镇结果 / Idempotently replayable town result.
        """

        operation_kind = "town.ensure"
        fingerprint = _ensure_fingerprint(command)
        async with db_connection.transaction() as connection:
            await _lock_receipt_key(command.idempotency_key, connection)
            await _lock_town_scope(command.town, connection)
            replay = await _load_receipt(
                command.idempotency_key,
                operation_kind,
                actor_id=None,
                town=command.town,
                fingerprint=fingerprint,
                connection=connection,
            )
            if replay is not None:
                return _result_from_mapping(replay, replayed=True)

            town = await _load_town(command.town, connection, for_update=True)
            if town is None:
                town = Town(
                    scope=command.town,
                    title=command.title,
                    created_at=command.created_at,
                )
                await _insert_town(town, connection)
            result = TownResult(TownCode.SUCCESS, town=town)
            await _save_receipt(
                command.idempotency_key,
                operation_kind,
                actor_id=None,
                town=command.town,
                fingerprint=fingerprint,
                result=_result_mapping(result),
                connection=connection,
            )
            return result

    async def create_project(self, command: CreateTownProject) -> TownResult:
        """@brief 原子保存一项管理员提议的小镇项目 / Atomically save an administrator-proposed town project.

        @param command 项目提议命令 / Project-proposal command.
        @return 可幂等重放的小镇结果 / Idempotently replayable town result.
        """

        operation_kind = "town.create_project"
        fingerprint = _project_fingerprint(command)
        async with db_connection.transaction() as connection:
            await _lock_receipt_key(command.idempotency_key, connection)
            await _lock_town_scope(command.town, connection)
            replay = await _load_receipt(
                command.idempotency_key,
                operation_kind,
                actor_id=command.proposer.user_id,
                town=command.town,
                fingerprint=fingerprint,
                connection=connection,
            )
            if replay is not None:
                return _result_from_mapping(replay, replayed=True)

            if not await _identity_exists(command.proposer, connection):
                result = TownResult(TownCode.NOT_REGISTERED)
            else:
                town = await _load_town(command.town, connection, for_update=True)
                if town is None:
                    result = TownResult(TownCode.NOT_FOUND)
                else:
                    project = command.project()
                    try:
                        updated_town = town.create_project(project)
                    except ValueError:
                        result = TownResult(TownCode.CONFLICT, town=town)
                    else:
                        await _persist_town(
                            updated_town,
                            expected_version=town.version,
                            connection=connection,
                        )
                        await _insert_project(command.town, project, connection)
                        result = TownResult(
                            TownCode.SUCCESS,
                            town=updated_town,
                            project=project,
                        )
            await _save_receipt(
                command.idempotency_key,
                operation_kind,
                actor_id=command.proposer.user_id,
                town=command.town,
                fingerprint=fingerprint,
                result=_result_mapping(result),
                connection=connection,
            )
            return result

    async def contribute(self, command: ContributeToTown) -> TownResult:
        """@brief 从个人 Free 钱包原子贡献到群组金库 / Atomically contribute from a personal Free wallet to a group treasury.

        @param command 小镇贡献命令 / Town-contribution command.
        @return 可幂等重放的小镇结果 / Idempotently replayable town result.
        """

        operation_kind = "town.contribute"
        fingerprint = _contribution_fingerprint(command)
        async with db_connection.transaction() as connection:
            await _lock_receipt_key(command.idempotency_key, connection)
            await _lock_town_scope(command.town, connection)
            replay = await _load_receipt(
                command.idempotency_key,
                operation_kind,
                actor_id=command.contributor.user_id,
                town=command.town,
                fingerprint=fingerprint,
                connection=connection,
            )
            if replay is not None:
                return _result_from_mapping(replay, replayed=True)

            if not await _identity_exists(command.contributor, connection):
                result = TownResult(TownCode.NOT_REGISTERED)
            else:
                town = await _load_town(command.town, connection, for_update=True)
                if town is None:
                    result = TownResult(TownCode.NOT_FOUND)
                else:
                    candidate = TownContribution(
                        contribution_id=command.contribution_id,
                        town=command.town,
                        contributor=command.contributor,
                        amount=command.amount,
                        contributed_at=command.requested_at,
                        ledger_entry_id=uuid4(),
                        project_id=command.project_id,
                    )
                    try:
                        town.record_contribution(candidate)
                    except ValueError:
                        result = TownResult(TownCode.PROJECT_UNAVAILABLE, town=town)
                    else:
                        user_wallet = LedgerAccount.user(
                            command.contributor.user_id,
                            TokenBucket.FREE,
                        )
                        treasury_account = LedgerAccount.group_treasury(
                            command.town.group_id
                        )
                        balances = await lock_bank_account_balances(
                            (user_wallet, treasury_account),
                            connection,
                        )
                        if balances[user_wallet] < command.amount.value:
                            result = TownResult(
                                TownCode.INSUFFICIENT_FUNDS,
                                town=town,
                            )
                        elif balances[treasury_account] != town.treasury.balance:
                            result = TownResult(TownCode.CONFLICT, town=town)
                        else:
                            entry = await post_bank_transfer(
                                namespace="town-contribution",
                                source_idempotency_key=command.idempotency_key,
                                reason=LedgerReason.GROUP_CONTRIBUTION,
                                source=user_wallet,
                                destination=treasury_account,
                                amount=command.amount,
                                created_at=command.requested_at,
                                actor_id=command.contributor.user_id,
                                connection=connection,
                                metadata={
                                    "group_id": command.town.group_id,
                                    "contribution_id": str(command.contribution_id),
                                    "targeted": command.project_id is not None,
                                },
                            )
                            contribution = TownContribution(
                                contribution_id=command.contribution_id,
                                town=command.town,
                                contributor=command.contributor,
                                amount=command.amount,
                                contributed_at=command.requested_at,
                                ledger_entry_id=entry.entry_id,
                                project_id=command.project_id,
                            )
                            updated_town = town.record_contribution(contribution)
                            await _persist_town(
                                updated_town,
                                expected_version=town.version,
                                connection=connection,
                            )
                            if command.project_id is not None:
                                original_project = _project_by_id(town, command.project_id)
                                updated_project = _project_by_id(
                                    updated_town,
                                    command.project_id,
                                )
                                await _persist_project(
                                    updated_project,
                                    expected_version=original_project.version,
                                    connection=connection,
                                )
                            await _insert_contribution(contribution, connection)
                            result = TownResult(
                                TownCode.SUCCESS,
                                town=updated_town,
                                project=(
                                    _project_by_id(updated_town, command.project_id)
                                    if command.project_id is not None
                                    else None
                                ),
                                contribution=contribution,
                            )
            await _save_receipt(
                command.idempotency_key,
                operation_kind,
                actor_id=command.contributor.user_id,
                town=command.town,
                fingerprint=fingerprint,
                result=_result_mapping(result),
                connection=connection,
            )
            return result

    async def complete_project(self, command: CompleteTownProject) -> TownResult:
        """@brief 从群组金库结算足额项目并提升繁荣度 / Settle a funded project from the group treasury and raise prosperity.

        @param command 项目建成命令 / Project-completion command.
        @return 可幂等重放的小镇结果 / Idempotently replayable town result.
        """

        operation_kind = "town.complete_project"
        fingerprint = _completion_fingerprint(command)
        async with db_connection.transaction() as connection:
            await _lock_receipt_key(command.idempotency_key, connection)
            await _lock_town_scope(command.town, connection)
            replay = await _load_receipt(
                command.idempotency_key,
                operation_kind,
                actor_id=command.operator.user_id,
                town=command.town,
                fingerprint=fingerprint,
                connection=connection,
            )
            if replay is not None:
                return _result_from_mapping(replay, replayed=True)

            if not await _identity_exists(command.operator, connection):
                result = TownResult(TownCode.NOT_REGISTERED)
            else:
                town = await _load_town(command.town, connection, for_update=True)
                if town is None:
                    result = TownResult(TownCode.NOT_FOUND)
                else:
                    try:
                        project = _project_by_id(town, command.project_id)
                    except ValueError:
                        result = TownResult(TownCode.PROJECT_UNAVAILABLE, town=town)
                    else:
                        if project.status is not TownProjectStatus.READY:
                            result = TownResult(
                                TownCode.PROJECT_UNAVAILABLE,
                                town=town,
                                project=project,
                            )
                        else:
                            treasury_account = LedgerAccount.group_treasury(
                                command.town.group_id
                            )
                            burn_account = LedgerAccount.system(SystemAccountKind.BURN)
                            balances = await lock_bank_account_balances(
                                (treasury_account, burn_account),
                                connection,
                            )
                            if (
                                balances[treasury_account] != town.treasury.balance
                                or balances[treasury_account]
                                < project.required_amount.value
                            ):
                                result = TownResult(TownCode.CONFLICT, town=town)
                            else:
                                entry = await post_bank_transfer(
                                    namespace="town-project-settlement",
                                    source_idempotency_key=command.idempotency_key,
                                    reason=LedgerReason.BANK_BURN,
                                    source=treasury_account,
                                    destination=burn_account,
                                    amount=project.required_amount,
                                    created_at=command.completed_at,
                                    actor_id=command.operator.user_id,
                                    connection=connection,
                                    metadata={
                                        "group_id": command.town.group_id,
                                        "project_id": str(command.project_id),
                                        "burn_kind": "town_project_settlement",
                                    },
                                )
                                updated_town = town.complete_project(
                                    project_id=command.project_id,
                                    completed_at=command.completed_at,
                                    settlement_ledger_entry_id=entry.entry_id,
                                )
                                updated_project = _project_by_id(
                                    updated_town,
                                    command.project_id,
                                )
                                await _persist_town(
                                    updated_town,
                                    expected_version=town.version,
                                    connection=connection,
                                )
                                await _persist_project(
                                    updated_project,
                                    expected_version=project.version,
                                    connection=connection,
                                )
                                result = TownResult(
                                    TownCode.SUCCESS,
                                    town=updated_town,
                                    project=updated_project,
                                )
            await _save_receipt(
                command.idempotency_key,
                operation_kind,
                actor_id=command.operator.user_id,
                town=command.town,
                fingerprint=fingerprint,
                result=_result_mapping(result),
                connection=connection,
            )
            return result

    async def overview(self, town: TownScope) -> TownResult:
        """@brief 读取群组唯一小镇的完整只读快照 / Read the complete read-only snapshot of a group-unique town.

        @param town 显式群组小镇范围 / Explicit group-town scope.
        @return 小镇快照或不存在代码 / Town snapshot or not-found code.
        """

        async with db_connection.transaction() as connection:
            current = await _load_town(town, connection, for_update=False)
            return (
                TownResult(TownCode.SUCCESS, town=current)
                if current is not None
                else TownResult(TownCode.NOT_FOUND)
            )


async def _identity_exists(
    actor: PersonalScope,
    connection: AsyncConnection,
) -> bool:
    """@brief 检查个人范围是否已注册 / Check whether a personal scope is registered.

    @param actor 个人范围 / Personal scope.
    @param connection 当前事务连接 / Current transactional connection.
    @return 已注册时为 True / True when registered.
    """

    row = await db_connection.fetch_one(
        "SELECT 1 FROM identity.users WHERE id = %s",
        (actor.user_id,),
        connection=connection,
    )
    return row is not None


async def _lock_receipt_key(
    idempotency_key: str,
    connection: AsyncConnection,
) -> None:
    """@brief 用事务级 advisory lock 串行同一业务键 / Serialize one business key with a transaction-scoped advisory lock.

    @param idempotency_key 小镇业务幂等键 / Town business idempotency key.
    @param connection 当前事务连接 / Current transactional connection.
    @return None / None.
    """

    await db_connection.fetch_one(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (idempotency_key,),
        connection=connection,
    )


async def _lock_town_scope(
    town: TownScope,
    connection: AsyncConnection,
) -> None:
    """@brief 串行化同一群组小镇聚合的首次创建与写入 / Serialize first creation and writes of one group-town aggregate.

    @param town 显式群组小镇范围 / Explicit group-town scope.
    @param connection 当前事务连接 / Current transactional connection.
    @return None / None.
    @note 在小镇尚不存在时，``SELECT ... FOR UPDATE`` 没有可锁的行；这个 advisory
        lock 让两个不同幂等键不能同时创建同一座群组小镇。/ Before a town exists,
        ``SELECT ... FOR UPDATE`` has no row to lock; this advisory lock prevents two different
        idempotency keys from creating the same group town concurrently.
    """

    await db_connection.fetch_one(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"town:scope:{town.group_id}",),
        connection=connection,
    )


async def _load_receipt(
    idempotency_key: str,
    operation_kind: str,
    *,
    actor_id: int | None,
    town: TownScope,
    fingerprint: Mapping[str, object],
    connection: AsyncConnection,
) -> Mapping[str, Any] | None:
    """@brief 读取并验证小镇幂等回执 / Load and validate a town idempotency receipt.

    @param idempotency_key 业务幂等键 / Business idempotency key.
    @param operation_kind 稳定操作种类 / Stable operation kind.
    @param actor_id 可选操作者 / Optional actor.
    @param town 目标群组小镇范围 / Target group-town scope.
    @param fingerprint 规范化命令语义摘要 / Canonical command-semantics digest.
    @param connection 当前事务连接 / Current transactional connection.
    @return JSON 回执；首次执行时为 None / JSON receipt, or None on first execution.
    @raise ValueError 同一键改变操作、主体、范围或语义时抛出 /
        Raised when one key changes operation, actor, scope, or semantics.
    """

    row = await db_connection.fetch_one(
        "SELECT operation_kind, actor_id, group_id, request_fingerprint, result "
        "FROM town.operation_receipts WHERE idempotency_key = %s",
        (idempotency_key,),
        connection=connection,
    )
    if row is None:
        return None
    if (
        cast(str, row[0]) != operation_kind
        or row[1] != actor_id
        or cast(int, row[2]) != town.group_id
    ):
        raise ValueError("Town idempotency key changed ownership")
    persisted_fingerprint = _json_mapping(row[3])
    if dict(persisted_fingerprint) != dict(fingerprint):
        raise ValueError("Town idempotency key changed command semantics")
    return _json_mapping(row[4])


async def _save_receipt(
    idempotency_key: str,
    operation_kind: str,
    *,
    actor_id: int | None,
    town: TownScope,
    fingerprint: Mapping[str, object],
    result: Mapping[str, object],
    connection: AsyncConnection,
) -> None:
    """@brief 与小镇写入同事务保存不可变回执 / Save an immutable receipt in the town-write transaction.

    @param idempotency_key 业务幂等键 / Business idempotency key.
    @param operation_kind 稳定操作种类 / Stable operation kind.
    @param actor_id 可选操作者 / Optional actor.
    @param town 目标群组小镇范围 / Target group-town scope.
    @param fingerprint 规范化命令语义摘要 / Canonical command-semantics digest.
    @param result JSON 可序列化结果 / JSON-serializable result.
    @param connection 当前事务连接 / Current transactional connection.
    @return None / None.
    """

    await db_connection.execute(
        "INSERT INTO town.operation_receipts ("
        "idempotency_key, operation_kind, actor_id, group_id, request_fingerprint, result"
        ") VALUES (%s, %s, %s, %s, CAST(%s AS JSONB), CAST(%s AS JSONB))",
        (
            idempotency_key,
            operation_kind,
            actor_id,
            town.group_id,
            json.dumps(dict(fingerprint), sort_keys=True),
            json.dumps(dict(result), sort_keys=True),
        ),
        connection=connection,
    )


async def _load_town(
    scope: TownScope,
    connection: AsyncConnection,
    *,
    for_update: bool,
) -> Town | None:
    """@brief 加载一座小镇及其项目 / Load one town and its projects.

    @param scope 显式群组小镇范围 / Explicit group-town scope.
    @param connection 当前事务连接 / Current transactional connection.
    @param for_update 是否对聚合与项目行加锁 / Whether to lock aggregate and project rows.
    @return 完整小镇聚合；不存在时为 None / Complete town aggregate, or None when absent.
    """

    lock_clause = " FOR UPDATE" if for_update else ""
    row = await db_connection.fetch_one(
        "SELECT group_id, title, created_at, treasury_balance, treasury_reserved, "
        "lifetime_contributed, lifetime_settled, contribution_count, prosperity, version "
        "FROM town.towns WHERE group_id = %s" + lock_clause,
        (scope.group_id,),
        connection=connection,
    )
    if row is None:
        return None
    project_rows = await db_connection.fetch_all(
        "SELECT project_id, kind, title, required_amount, created_by, created_at, "
        "prosperity_reward, funded_amount, status, completed_at, settlement_ledger_entry_id, "
        "version FROM town.projects WHERE group_id = %s "
        "ORDER BY created_at ASC, project_id ASC" + lock_clause,
        (scope.group_id,),
        connection=connection,
    )
    values = tuple(row)
    return Town(
        scope=scope,
        title=cast(str, values[1]),
        created_at=_as_utc(cast(datetime, values[2])),
        treasury=TownTreasury(
            balance=cast(int, values[3]),
            reserved=cast(int, values[4]),
            lifetime_contributed=cast(int, values[5]),
            lifetime_settled=cast(int, values[6]),
            contribution_count=cast(int, values[7]),
        ),
        projects=tuple(_project_from_row(project_row) for project_row in project_rows),
        prosperity=cast(int, values[8]),
        version=cast(int, values[9]),
    )


async def _insert_town(town: Town, connection: AsyncConnection) -> None:
    """@brief 插入全新小镇聚合头 / Insert a brand-new town aggregate header.

    @param town 待插入小镇 / Town to insert.
    @param connection 当前事务连接 / Current transactional connection.
    @return None / None.
    """

    treasury = town.treasury
    await db_connection.execute(
        "INSERT INTO town.towns ("
        "group_id, title, created_at, treasury_balance, treasury_reserved, "
        "lifetime_contributed, lifetime_settled, contribution_count, prosperity, version"
        ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            town.scope.group_id,
            town.title,
            town.created_at,
            treasury.balance,
            treasury.reserved,
            treasury.lifetime_contributed,
            treasury.lifetime_settled,
            treasury.contribution_count,
            town.prosperity,
            town.version,
        ),
        connection=connection,
    )


async def _persist_town(
    town: Town,
    *,
    expected_version: int,
    connection: AsyncConnection,
) -> None:
    """@brief 以乐观版本更新小镇聚合头 / Update a town aggregate header with an optimistic version.

    @param town 更新后小镇 / Updated town.
    @param expected_version 更新前版本 / Version before update.
    @param connection 当前事务连接 / Current transactional connection.
    @return None / None.
    @raise RuntimeError 小镇版本意外变化时抛出 / Raised when the town version unexpectedly changed.
    """

    treasury = town.treasury
    changed = await db_connection.execute(
        "UPDATE town.towns SET title = %s, treasury_balance = %s, treasury_reserved = %s, "
        "lifetime_contributed = %s, lifetime_settled = %s, contribution_count = %s, "
        "prosperity = %s, version = %s, updated_at = CURRENT_TIMESTAMP "
        "WHERE group_id = %s AND version = %s",
        (
            town.title,
            treasury.balance,
            treasury.reserved,
            treasury.lifetime_contributed,
            treasury.lifetime_settled,
            treasury.contribution_count,
            town.prosperity,
            town.version,
            town.scope.group_id,
            expected_version,
        ),
        connection=connection,
    )
    if changed != 1:
        raise RuntimeError("Town changed while it was locked")


async def _insert_project(
    town: TownScope,
    project: TownProject,
    connection: AsyncConnection,
) -> None:
    """@brief 插入新提议的小镇项目 / Insert a newly proposed town project.

    @param town 项目所属小镇范围 / Owning town scope.
    @param project 待插入项目 / Project to insert.
    @param connection 当前事务连接 / Current transactional connection.
    @return None / None.
    """

    await db_connection.execute(
        "INSERT INTO town.projects ("
        "project_id, group_id, kind, title, required_amount, created_by, created_at, "
        "prosperity_reward, funded_amount, status, completed_at, settlement_ledger_entry_id, "
        "version"
        ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        _project_values(town, project),
        connection=connection,
    )


async def _persist_project(
    project: TownProject,
    *,
    expected_version: int,
    connection: AsyncConnection,
) -> None:
    """@brief 以乐观版本保存项目状态变迁 / Save a project state transition with an optimistic version.

    @param project 更新后项目 / Updated project.
    @param expected_version 更新前项目版本 / Project version before update.
    @param connection 当前事务连接 / Current transactional connection.
    @return None / None.
    @raise RuntimeError 项目版本意外变化时抛出 / Raised when the project version unexpectedly changed.
    """

    changed = await db_connection.execute(
        "UPDATE town.projects SET funded_amount = %s, status = %s, completed_at = %s, "
        "settlement_ledger_entry_id = %s, version = %s, updated_at = CURRENT_TIMESTAMP "
        "WHERE project_id = %s AND version = %s",
        (
            project.funded_amount,
            project.status.value,
            project.completed_at,
            project.settlement_ledger_entry_id,
            project.version,
            project.project_id,
            expected_version,
        ),
        connection=connection,
    )
    if changed != 1:
        raise RuntimeError("Town project changed while it was locked")


async def _insert_contribution(
    contribution: TownContribution,
    connection: AsyncConnection,
) -> None:
    """@brief 记录一笔已发生账本转账的小镇贡献 / Record a town contribution whose bank transfer has already posted.

    @param contribution 已确认贡献 / Confirmed contribution.
    @param connection 当前事务连接 / Current transactional connection.
    @return None / None.
    """

    await db_connection.execute(
        "INSERT INTO town.contributions ("
        "contribution_id, group_id, contributor_id, amount, contributed_at, ledger_entry_id, "
        "project_id"
        ") VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            contribution.contribution_id,
            contribution.town.group_id,
            contribution.contributor.user_id,
            contribution.amount.value,
            contribution.contributed_at,
            contribution.ledger_entry_id,
            contribution.project_id,
        ),
        connection=connection,
    )


def _project_values(town: TownScope, project: TownProject) -> tuple[object, ...]:
    """@brief 将领域项目编码为 SQL 插入参数 / Encode a domain project as SQL insert parameters.

    @param town 项目所属小镇范围 / Owning town scope.
    @param project 领域项目 / Domain project.
    @return 参数元组 / Parameter tuple.
    """

    return (
        project.project_id,
        town.group_id,
        project.kind.value,
        project.title,
        project.required_amount.value,
        project.created_by.user_id,
        project.created_at,
        project.prosperity_reward,
        project.funded_amount,
        project.status.value,
        project.completed_at,
        project.settlement_ledger_entry_id,
        project.version,
    )


def _project_from_row(row: object) -> TownProject:
    """@brief 从 PostgreSQL 行还原领域项目 / Restore a domain project from a PostgreSQL row.

    @param row 项目原始数据库行 / Raw project database row.
    @return 完整领域项目 / Complete domain project.
    """

    values = tuple(cast(tuple[object, ...], row))
    return TownProject(
        project_id=cast(UUID, values[0]),
        kind=TownProjectKind(cast(str, values[1])),
        title=cast(str, values[2]),
        required_amount=TokenAmount(cast(int, values[3])),
        created_by=PersonalScope(cast(int, values[4])),
        created_at=_as_utc(cast(datetime, values[5])),
        prosperity_reward=cast(int, values[6]),
        funded_amount=cast(int, values[7]),
        status=TownProjectStatus(cast(str, values[8])),
        completed_at=(
            _as_utc(cast(datetime, values[9])) if values[9] is not None else None
        ),
        settlement_ledger_entry_id=(
            cast(UUID, values[10]) if values[10] is not None else None
        ),
        version=cast(int, values[11]),
    )


def _project_by_id(town: Town, project_id: UUID) -> TownProject:
    """@brief 从小镇快照查找项目 / Find one project from a town snapshot.

    @param town 小镇聚合 / Town aggregate.
    @param project_id 项目稳定标识 / Stable project identity.
    @return 匹配项目 / Matching project.
    @raise ValueError 项目不存在时抛出 / Raised when the project is absent.
    """

    for project in town.projects:
        if project.project_id == project_id:
            return project
    raise ValueError("Town project does not exist")


def _ensure_fingerprint(command: EnsureTown) -> dict[str, object]:
    """@brief 构造建镇命令的规范幂等语义 / Construct canonical idempotency semantics for a town ensure command.

    @param command 建镇命令 / Town ensure command.
    @return JSON 兼容指纹 / JSON-compatible fingerprint.
    """

    return {"title": command.title, "created_at": command.created_at.isoformat()}


def _project_fingerprint(command: CreateTownProject) -> dict[str, object]:
    """@brief 构造提议项目的规范幂等语义 / Construct canonical idempotency semantics for a project proposal.

    @param command 项目提议命令 / Project-proposal command.
    @return JSON 兼容指纹 / JSON-compatible fingerprint.
    """

    return {
        "project_id": str(command.project_id),
        "kind": command.kind.value,
        "title": command.title,
        "required_amount": command.required_amount.value,
        "created_at": command.created_at.isoformat(),
        "prosperity_reward": command.prosperity_reward,
    }


def _contribution_fingerprint(command: ContributeToTown) -> dict[str, object]:
    """@brief 构造贡献命令的规范幂等语义 / Construct canonical idempotency semantics for a contribution command.

    @param command 贡献命令 / Contribution command.
    @return JSON 兼容指纹 / JSON-compatible fingerprint.
    """

    return {
        "contribution_id": str(command.contribution_id),
        "amount": command.amount.value,
        "requested_at": command.requested_at.isoformat(),
        "project_id": str(command.project_id) if command.project_id is not None else None,
    }


def _completion_fingerprint(command: CompleteTownProject) -> dict[str, object]:
    """@brief 构造项目结算的规范幂等语义 / Construct canonical idempotency semantics for project completion.

    @param command 项目结算命令 / Project-completion command.
    @return JSON 兼容指纹 / JSON-compatible fingerprint.
    """

    return {
        "project_id": str(command.project_id),
        "completed_at": command.completed_at.isoformat(),
    }


def _result_mapping(result: TownResult) -> dict[str, object]:
    """@brief 序列化小镇结果供幂等回放 / Serialize a town result for idempotent replay.

    @param result 小镇应用结果 / Town application result.
    @return JSON 兼容结果 / JSON-compatible result.
    """

    return {
        "code": result.code.value,
        "town": _town_mapping(result.town) if result.town is not None else None,
        "project": (
            _project_mapping(result.project) if result.project is not None else None
        ),
        "contribution": (
            _contribution_mapping(result.contribution)
            if result.contribution is not None
            else None
        ),
    }


def _result_from_mapping(
    value: Mapping[str, Any],
    *,
    replayed: bool,
) -> TownResult:
    """@brief 从 JSON 回执还原小镇结果 / Restore a town result from a JSON receipt.

    @param value JSON 回执对象 / JSON receipt object.
    @param replayed 是否标记为回放 / Whether to mark the result as replayed.
    @return 还原后应用结果 / Restored application result.
    """

    raw_town = value.get("town")
    raw_project = value.get("project")
    raw_contribution = value.get("contribution")
    return TownResult(
        code=TownCode(str(value["code"])),
        town=(
            _town_from_mapping(cast(Mapping[str, Any], raw_town))
            if isinstance(raw_town, Mapping)
            else None
        ),
        project=(
            _project_from_mapping(cast(Mapping[str, Any], raw_project))
            if isinstance(raw_project, Mapping)
            else None
        ),
        contribution=(
            _contribution_from_mapping(cast(Mapping[str, Any], raw_contribution))
            if isinstance(raw_contribution, Mapping)
            else None
        ),
        replayed=replayed,
    )


def _town_mapping(town: Town) -> dict[str, object]:
    """@brief 序列化小镇聚合 / Serialize a town aggregate.

    @param town 小镇聚合 / Town aggregate.
    @return JSON 兼容小镇对象 / JSON-compatible town object.
    """

    treasury = town.treasury
    return {
        "scope": town.scope.group_id,
        "title": town.title,
        "created_at": town.created_at.isoformat(),
        "treasury": {
            "balance": treasury.balance,
            "reserved": treasury.reserved,
            "lifetime_contributed": treasury.lifetime_contributed,
            "lifetime_settled": treasury.lifetime_settled,
            "contribution_count": treasury.contribution_count,
        },
        "projects": [_project_mapping(project) for project in town.projects],
        "prosperity": town.prosperity,
        "version": town.version,
    }


def _town_from_mapping(value: Mapping[str, Any]) -> Town:
    """@brief 从 JSON 还原小镇聚合 / Restore a town aggregate from JSON.

    @param value JSON 小镇对象 / JSON town object.
    @return 还原的小镇聚合 / Restored town aggregate.
    """

    raw_treasury = value["treasury"]
    if not isinstance(raw_treasury, Mapping):
        raise ValueError("Invalid town receipt treasury")
    raw_projects = value.get("projects")
    if not isinstance(raw_projects, list):
        raise ValueError("Invalid town receipt projects")
    projects: list[TownProject] = []
    """@brief 经验证的项目回放列表 / Validated project replay list."""
    for project in raw_projects:
        if not isinstance(project, Mapping):
            raise ValueError("Invalid town receipt project")
        projects.append(_project_from_mapping(cast(Mapping[str, Any], project)))
    return Town(
        scope=TownScope(int(value["scope"])),
        title=str(value["title"]),
        created_at=datetime.fromisoformat(str(value["created_at"])),
        treasury=TownTreasury(
            balance=int(raw_treasury["balance"]),
            reserved=int(raw_treasury["reserved"]),
            lifetime_contributed=int(raw_treasury["lifetime_contributed"]),
            lifetime_settled=int(raw_treasury["lifetime_settled"]),
            contribution_count=int(raw_treasury["contribution_count"]),
        ),
        projects=tuple(projects),
        prosperity=int(value["prosperity"]),
        version=int(value["version"]),
    )


def _project_mapping(project: TownProject) -> dict[str, object]:
    """@brief 序列化小镇项目 / Serialize a town project.

    @param project 小镇项目 / Town project.
    @return JSON 兼容项目对象 / JSON-compatible project object.
    """

    return {
        "project_id": str(project.project_id),
        "kind": project.kind.value,
        "title": project.title,
        "required_amount": project.required_amount.value,
        "created_by": project.created_by.user_id,
        "created_at": project.created_at.isoformat(),
        "prosperity_reward": project.prosperity_reward,
        "funded_amount": project.funded_amount,
        "status": project.status.value,
        "completed_at": (
            project.completed_at.isoformat() if project.completed_at is not None else None
        ),
        "settlement_ledger_entry_id": (
            str(project.settlement_ledger_entry_id)
            if project.settlement_ledger_entry_id is not None
            else None
        ),
        "version": project.version,
    }


def _project_from_mapping(value: Mapping[str, Any]) -> TownProject:
    """@brief 从 JSON 还原小镇项目 / Restore a town project from JSON.

    @param value JSON 项目对象 / JSON project object.
    @return 还原的小镇项目 / Restored town project.
    """

    raw_completed_at = value.get("completed_at")
    raw_entry_id = value.get("settlement_ledger_entry_id")
    return TownProject(
        project_id=UUID(str(value["project_id"])),
        kind=TownProjectKind(str(value["kind"])),
        title=str(value["title"]),
        required_amount=TokenAmount(int(value["required_amount"])),
        created_by=PersonalScope(int(value["created_by"])),
        created_at=datetime.fromisoformat(str(value["created_at"])),
        prosperity_reward=int(value["prosperity_reward"]),
        funded_amount=int(value["funded_amount"]),
        status=TownProjectStatus(str(value["status"])),
        completed_at=(
            datetime.fromisoformat(str(raw_completed_at))
            if raw_completed_at is not None
            else None
        ),
        settlement_ledger_entry_id=(
            UUID(str(raw_entry_id)) if raw_entry_id is not None else None
        ),
        version=int(value["version"]),
    )


def _contribution_mapping(contribution: TownContribution) -> dict[str, object]:
    """@brief 序列化小镇贡献 / Serialize a town contribution.

    @param contribution 小镇贡献 / Town contribution.
    @return JSON 兼容贡献对象 / JSON-compatible contribution object.
    """

    return {
        "contribution_id": str(contribution.contribution_id),
        "group_id": contribution.town.group_id,
        "contributor_id": contribution.contributor.user_id,
        "amount": contribution.amount.value,
        "contributed_at": contribution.contributed_at.isoformat(),
        "ledger_entry_id": str(contribution.ledger_entry_id),
        "project_id": (
            str(contribution.project_id) if contribution.project_id is not None else None
        ),
    }


def _contribution_from_mapping(value: Mapping[str, Any]) -> TownContribution:
    """@brief 从 JSON 还原小镇贡献 / Restore a town contribution from JSON.

    @param value JSON 贡献对象 / JSON contribution object.
    @return 还原的小镇贡献 / Restored town contribution.
    """

    raw_project_id = value.get("project_id")
    return TownContribution(
        contribution_id=UUID(str(value["contribution_id"])),
        town=TownScope(int(value["group_id"])),
        contributor=PersonalScope(int(value["contributor_id"])),
        amount=TokenAmount(int(value["amount"])),
        contributed_at=datetime.fromisoformat(str(value["contributed_at"])),
        ledger_entry_id=UUID(str(value["ledger_entry_id"])),
        project_id=UUID(str(raw_project_id)) if raw_project_id is not None else None,
    )


def _json_mapping(value: object) -> Mapping[str, Any]:
    """@brief 将驱动返回的 JSON 值规范为对象映射 / Normalize a driver-returned JSON value into an object mapping.

    @param value PostgreSQL JSONB 返回值 / PostgreSQL JSONB return value.
    @return 只读 JSON 对象映射 / Read-only JSON object mapping.
    @raise ValueError JSON 不是对象时抛出 / Raised when JSON is not an object.
    """

    decoded: object = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, Mapping):
        raise ValueError("Town operation receipt JSON must be an object")
    return cast(Mapping[str, Any], decoded)


def _as_utc(value: datetime) -> datetime:
    """@brief 将数据库时间规范为 UTC aware 时间 / Normalize a database time to an aware UTC time.

    @param value 数据库时间 / Database time.
    @return UTC aware 时间 / UTC-aware time.
    """

    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


__all__ = ["PostgresTownOperations"]
