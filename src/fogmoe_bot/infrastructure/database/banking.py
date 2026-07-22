"""@brief PostgreSQL 银行账本适配器 / PostgreSQL banking-ledger adapter."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from hashlib import sha256
import json
from types import MappingProxyType
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.banking.models import (
    ActivityPotFundingResult,
    BankCode,
    BankOverview,
    FundActivityPot,
    IssueTokens,
    ListPendingTokenRequests,
    PendingTokenRequestsResult,
    RequestTokens,
    ReviewTokenRequest,
    TokenRequestResult,
    TokenReviewDecision,
)
from fogmoe_bot.application.banking.ports import BankOperations
from fogmoe_bot.domain.banking.ledger import (
    AccountScope,
    LedgerAccount,
    LedgerEntry,
    LedgerReason,
)
from fogmoe_bot.domain.banking.money import (
    SystemAccountKind,
    TokenAmount,
    TokenBucket,
    WalletBalance,
)
from fogmoe_bot.domain.banking.requests import TokenRequest, TokenRequestStatus
from fogmoe_bot.infrastructure.database import connection as db_connection


class PostgresBankOperations(BankOperations):
    """@brief 以不可变双重记账实现银行原子操作 / Implement atomic bank operations with an immutable double-entry ledger."""

    async def request_tokens(self, command: RequestTokens) -> TokenRequestResult:
        """@brief 创建或幂等重放免费代币申请 / Create or idempotently replay a free-token request.

        @param command 代币申请命令 / Token-request command.
        @return 创建或重放后的申请结果 / Created or replayed request result.
        """

        operation_kind = "token_request.create"
        async with db_connection.transaction() as connection:
            await _lock_idempotency_key(command.idempotency_key, connection)
            if not await _identity_exists(command.user_id, connection):
                return TokenRequestResult(BankCode.NOT_REGISTERED)
            replay = await _load_receipt(
                command.idempotency_key,
                operation_kind,
                command.user_id,
                connection,
            )
            if replay is not None:
                return _result_from_mapping(replay, replayed=True)

            request = command.aggregate()
            await db_connection.execute(
                "INSERT INTO bank.token_requests ("
                "request_id, requester_id, requested_amount, requested_bucket, purpose, "
                "status, requested_at, version, updated_at"
                ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)",
                (
                    request.request_id,
                    request.requester_id,
                    request.requested_amount.value,
                    request.requested_bucket.value,
                    request.purpose,
                    request.status.value,
                    request.requested_at,
                    request.version,
                ),
                connection=connection,
            )
            result = TokenRequestResult(BankCode.SUCCESS, request=request)
            await _save_receipt(
                command.idempotency_key,
                operation_kind,
                command.user_id,
                _result_mapping(result),
                connection,
            )
            return result

    async def review_token_request(
        self, command: ReviewTokenRequest
    ) -> TokenRequestResult:
        """@brief 审核申请并在批准时入账发行 / Review a request and post issuance on approval.

        @param command 管理员审核命令 / Administrator review command.
        @return 审核后的申请与钱包快照 / Reviewed request and wallet snapshot.
        """

        operation_kind = "token_request.review"
        async with db_connection.transaction() as connection:
            await _lock_idempotency_key(command.idempotency_key, connection)
            replay = await _load_receipt(
                command.idempotency_key,
                operation_kind,
                command.reviewer_id,
                connection,
            )
            if replay is not None:
                return _result_from_mapping(replay, replayed=True)

            request = await _lock_token_request(command.request_id, connection)
            if request is None:
                result = TokenRequestResult(BankCode.NOT_FOUND)
            elif request.status is not TokenRequestStatus.PENDING:
                result = TokenRequestResult(BankCode.NOT_PENDING, request=request)
            elif request.requester_id == command.reviewer_id:
                result = TokenRequestResult(BankCode.FORBIDDEN, request=request)
            elif command.decision is TokenReviewDecision.REJECT:
                reviewed = request.reject(
                    reviewer_id=command.reviewer_id,
                    reviewed_at=command.reviewed_at,
                    note=command.note,
                )
                await _persist_reviewed_request(reviewed, connection)
                result = TokenRequestResult(BankCode.SUCCESS, request=reviewed)
            else:
                entry = LedgerEntry.transfer(
                    entry_id=uuid4(),
                    idempotency_key=command.idempotency_key,
                    reason=LedgerReason.BANK_ISSUANCE,
                    source=LedgerAccount.system(SystemAccountKind.ISSUANCE),
                    destination=LedgerAccount.user(
                        request.requester_id, request.requested_bucket
                    ),
                    amount=request.requested_amount,
                    created_at=command.reviewed_at,
                    actor_id=command.reviewer_id,
                    metadata={"token_request_id": str(request.request_id)},
                )
                reviewed = request.approve(
                    reviewer_id=command.reviewer_id,
                    reviewed_at=command.reviewed_at,
                    ledger_entry_id=entry.entry_id,
                    note=command.note,
                )
                await append_bank_entry(entry, connection)
                await _persist_reviewed_request(reviewed, connection)
                overview = await load_bank_overview(request.requester_id, connection)
                result = TokenRequestResult(
                    BankCode.SUCCESS,
                    request=reviewed,
                    overview=overview,
                )

            await _save_receipt(
                command.idempotency_key,
                operation_kind,
                command.reviewer_id,
                _result_mapping(result),
                connection,
            )
            return result

    async def issue_tokens(self, command: IssueTokens) -> TokenRequestResult:
        """@brief 由管理员直接发行免费金币 / Directly issue free tokens as an administrator.

        @param command 管理员发行命令 / Administrator issuance command.
        @return 发行后的钱包快照 / Post-issuance wallet snapshot.
        """

        operation_kind = "bank.issue"
        async with db_connection.transaction() as connection:
            await _lock_idempotency_key(command.idempotency_key, connection)
            replay = await _load_receipt(
                command.idempotency_key,
                operation_kind,
                command.administrator_id,
                connection,
            )
            if replay is not None:
                return _result_from_mapping(replay, replayed=True)
            if not await _identity_exists(command.recipient_id, connection):
                result = TokenRequestResult(BankCode.NOT_REGISTERED)
            else:
                entry = LedgerEntry.transfer(
                    entry_id=uuid4(),
                    idempotency_key=command.idempotency_key,
                    reason=LedgerReason.BANK_ISSUANCE,
                    source=LedgerAccount.system(SystemAccountKind.ISSUANCE),
                    destination=LedgerAccount.user(
                        command.recipient_id, command.bucket
                    ),
                    amount=command.amount,
                    created_at=command.issued_at,
                    actor_id=command.administrator_id,
                    metadata={"purpose": command.purpose.strip()},
                )
                await append_bank_entry(entry, connection)
                overview = await load_bank_overview(command.recipient_id, connection)
                result = TokenRequestResult(BankCode.SUCCESS, overview=overview)
            await _save_receipt(
                command.idempotency_key,
                operation_kind,
                command.administrator_id,
                _result_mapping(result),
                connection,
            )
            return result

    async def fund_activity_pot(
        self, command: FundActivityPot
    ) -> ActivityPotFundingResult:
        """@brief 由银行管理员从发行账户显式注资活动奖池 / Explicitly fund the activity pot from issuance as bank administrator.

        @param command 管理员可审计注资命令 / Auditable administrator-funding command.
        @return 幂等的注资结果及奖池余额 / Idempotent funding result and pot balance.
        @note 此操作是 Chance 派彩的唯一发行入口；Chance 结算自身绝不自动从
            issuance 补足。/ This operation is the only issuance ingress for Chance payouts;
            Chance settlement itself never auto-tops-up from issuance.
        """

        operation_kind = "bank.activity_pot_fund"
        activity_pot = LedgerAccount.system(SystemAccountKind.ACTIVITY_POT)
        async with db_connection.transaction() as connection:
            await _lock_idempotency_key(command.idempotency_key, connection)
            replay = await _load_receipt(
                command.idempotency_key,
                operation_kind,
                command.administrator_id,
                connection,
            )
            if replay is not None:
                return _activity_pot_funding_from_mapping(
                    replay,
                    command=command,
                    replayed=True,
                )
            if not await _identity_exists(command.administrator_id, connection):
                result = ActivityPotFundingResult(BankCode.NOT_REGISTERED)
            else:
                entry = LedgerEntry.transfer(
                    entry_id=uuid4(),
                    idempotency_key=command.idempotency_key,
                    reason=LedgerReason.BANK_ISSUANCE,
                    source=LedgerAccount.system(SystemAccountKind.ISSUANCE),
                    destination=activity_pot,
                    amount=command.amount,
                    created_at=command.funded_at,
                    actor_id=command.administrator_id,
                    metadata={
                        "fund_kind": "activity_pot",
                        "purpose": command.purpose.strip(),
                    },
                )
                await append_bank_entry(entry, connection)
                balances = await lock_bank_account_balances((activity_pot,), connection)
                result = ActivityPotFundingResult(
                    BankCode.SUCCESS,
                    amount=command.amount,
                    activity_pot_balance=balances[activity_pot],
                    ledger_entry_id=entry.entry_id,
                )
            await _save_receipt(
                command.idempotency_key,
                operation_kind,
                command.administrator_id,
                _activity_pot_funding_mapping(result, command=command),
                connection,
            )
            return result

    async def list_pending_token_requests(
        self,
        command: ListPendingTokenRequests,
    ) -> PendingTokenRequestsResult:
        """@brief 读取按时间排序的待审批代币申请 / Read time-ordered token requests awaiting approval.

        @param command 管理员分页查询命令 / Administrator paginated query command.
        @return 待审批申请或管理员身份缺失结果 / Pending requests or a missing-administrator result.
        @note 授权由 BankService 负责；此处额外确认管理员 identity 存在，避免孤儿账户读取。/
            Authorization belongs to BankService; this also confirms the administrator identity exists
            to avoid an orphan-account read.
        """

        async with db_connection.transaction() as connection:
            if not await _identity_exists(command.administrator_id, connection):
                return PendingTokenRequestsResult(BankCode.NOT_REGISTERED)
            rows = await db_connection.fetch_all(
                "SELECT request_id, requester_id, requested_amount, requested_bucket, "
                "purpose, status, requested_at, reviewed_at, reviewer_id, review_note, "
                "ledger_entry_id, version FROM bank.token_requests "
                "WHERE status = 'pending' "
                "ORDER BY requested_at ASC, request_id ASC LIMIT %s",
                (command.limit,),
                connection=connection,
            )
            requests = tuple(
                request
                for row in rows
                if (request := _request_from_row(row)) is not None
            )
            return PendingTokenRequestsResult(BankCode.SUCCESS, requests=requests)

    async def overview(self, user_id: int) -> BankOverview | None:
        """@brief 查询并惰性初始化用户双钱包 / Query and lazily initialize a user's two wallets.

        @param user_id 用户标识 / User identity.
        @return 钱包概览；未注册用户为 None / Wallet overview, or None for an unregistered user.
        """

        async with db_connection.transaction() as connection:
            if not await _identity_exists(user_id, connection):
                return None
            await ensure_bank_user_wallets(user_id, connection)
            return await load_bank_overview(user_id, connection)


async def _identity_exists(user_id: int, connection: AsyncConnection) -> bool:
    """@brief 检查身份账户是否存在 / Check whether the identity account exists.

    @param user_id 用户标识 / User identity.
    @param connection 当前事务连接 / Current transactional connection.
    @return 用户存在时为 True / True when the user exists.
    """

    row = await db_connection.fetch_one(
        "SELECT 1 FROM identity.users WHERE id = %s",
        (user_id,),
        connection=connection,
    )
    return row is not None


async def _lock_idempotency_key(
    idempotency_key: str,
    connection: AsyncConnection,
) -> None:
    """@brief 对单个幂等键持有事务级 advisory lock / Hold a transaction-level advisory lock for one idempotency key.

    @param idempotency_key 幂等键 / Idempotency key.
    @param connection 当前事务连接 / Current transactional connection.
    @return None / None.
    @note 哈希碰撞至多让不相关操作串行化，不会错误合并业务结果。/
        A hash collision can only serialize unrelated operations; it cannot merge their business results.
    """

    await db_connection.fetch_one(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (idempotency_key,),
        connection=connection,
    )


async def _load_receipt(
    idempotency_key: str,
    operation_kind: str,
    actor_id: int,
    connection: AsyncConnection,
) -> Mapping[str, Any] | None:
    """@brief 读取并验证银行幂等回执 / Load and validate a banking idempotency receipt.

    @param idempotency_key 幂等键 / Idempotency key.
    @param operation_kind 操作种类 / Operation kind.
    @param actor_id 操作者标识 / Actor identity.
    @param connection 当前事务连接 / Current transactional connection.
    @return 回执对象；首次操作为 None / Receipt object, or None on first execution.
    @raise ValueError 同一键改变操作含义时抛出 / Raised when one key changes operation meaning.
    """

    row = await db_connection.fetch_one(
        "SELECT operation_kind, actor_id, result FROM bank.operation_receipts "
        "WHERE idempotency_key = %s",
        (idempotency_key,),
        connection=connection,
    )
    if row is None:
        return None
    if cast(str, row[0]) != operation_kind or cast(int, row[1]) != actor_id:
        raise ValueError("Bank idempotency key changed operation meaning")
    raw_result: object = row[2]
    decoded: object = (
        json.loads(raw_result) if isinstance(raw_result, str) else raw_result
    )
    if not isinstance(decoded, Mapping):
        raise ValueError("Invalid bank operation receipt")
    return cast(Mapping[str, Any], decoded)


async def _save_receipt(
    idempotency_key: str,
    operation_kind: str,
    actor_id: int,
    result: Mapping[str, object],
    connection: AsyncConnection,
) -> None:
    """@brief 与业务变更同事务保存银行回执 / Save a bank receipt in the business transaction.

    @param idempotency_key 幂等键 / Idempotency key.
    @param operation_kind 操作种类 / Operation kind.
    @param actor_id 操作者标识 / Actor identity.
    @param result JSON 可序列化结果 / JSON-serializable result.
    @param connection 当前事务连接 / Current transactional connection.
    @return None / None.
    """

    await db_connection.execute(
        "INSERT INTO bank.operation_receipts "
        "(idempotency_key, operation_kind, actor_id, result) "
        "VALUES (%s, %s, %s, CAST(%s AS JSONB))",
        (idempotency_key, operation_kind, actor_id, json.dumps(dict(result))),
        connection=connection,
    )


async def _lock_token_request(
    request_id: UUID,
    connection: AsyncConnection,
) -> TokenRequest | None:
    """@brief 锁定并还原一份代币申请 / Lock and restore a token request.

    @param request_id 请求标识 / Request identity.
    @param connection 当前事务连接 / Current transactional connection.
    @return 请求聚合；不存在为 None / Request aggregate, or None when absent.
    """

    row = await db_connection.fetch_one(
        "SELECT request_id, requester_id, requested_amount, requested_bucket, purpose, "
        "status, requested_at, reviewed_at, reviewer_id, review_note, ledger_entry_id, "
        "version FROM bank.token_requests WHERE request_id = %s FOR UPDATE",
        (request_id,),
        connection=connection,
    )
    return _request_from_row(row)


async def _persist_reviewed_request(
    request: TokenRequest,
    connection: AsyncConnection,
) -> None:
    """@brief 保存已决申请的状态机变迁 / Persist a resolved request state-machine transition.

    @param request 已决请求聚合 / Resolved request aggregate.
    @param connection 当前事务连接 / Current transactional connection.
    @return None / None.
    @raise RuntimeError 乐观版本未匹配时抛出 / Raised when the optimistic version does not match.
    """

    changed = await db_connection.execute(
        "UPDATE bank.token_requests SET status = %s, reviewed_at = %s, reviewer_id = %s, "
        "review_note = %s, ledger_entry_id = %s, version = %s, "
        "updated_at = CURRENT_TIMESTAMP WHERE request_id = %s AND version = %s",
        (
            request.status.value,
            request.reviewed_at,
            request.reviewer_id,
            request.review_note,
            request.ledger_entry_id,
            request.version,
            request.request_id,
            request.version - 1,
        ),
        connection=connection,
    )
    if changed != 1:
        raise RuntimeError("Token request changed while it was locked")


async def append_bank_entry(entry: LedgerEntry, connection: AsyncConnection) -> None:
    """@brief 原子追加分录与所有过账行 / Atomically append an entry and all posting lines.

    @param entry 已验证平衡分录 / Validated balanced entry.
    @param connection 当前事务连接 / Current transactional connection.
    @return None / None.
    @note 账户按稳定键排序写入，以减少多账户转移的死锁机会。/
        Accounts are written in stable-key order to reduce multi-account transfer deadlocks.
    """

    sorted_postings = tuple(
        sorted(entry.postings, key=lambda posting: _account_key(posting.account))
    )
    await lock_bank_account_balances(
        (posting.account for posting in sorted_postings),
        connection,
    )
    await db_connection.execute(
        "INSERT INTO bank.ledger_entries "
        "(entry_id, idempotency_key, reason, actor_id, metadata, created_at) "
        "VALUES (%s, %s, %s, %s, CAST(%s AS JSONB), %s)",
        (
            entry.entry_id,
            entry.idempotency_key,
            entry.reason.value,
            entry.actor_id,
            json.dumps(dict(entry.metadata)),
            entry.created_at,
        ),
        connection=connection,
    )
    for line_no, posting in enumerate(sorted_postings, start=1):
        await db_connection.execute(
            "INSERT INTO bank.ledger_postings "
            "(entry_id, line_no, account_key, delta) VALUES (%s, %s, %s, %s)",
            (entry.entry_id, line_no, _account_key(posting.account), posting.delta),
            connection=connection,
        )


async def lock_bank_account_balances(
    accounts: Iterable[LedgerAccount],
    connection: AsyncConnection,
) -> Mapping[LedgerAccount, int]:
    """@brief 按稳定顺序创建并锁定银行余额投影 / Create and lock bank balance projections in a stable order.

    @param accounts 待锁定的逻辑账户；重复项会合并 / Logical accounts to lock; duplicates are coalesced.
    @param connection 当前事务连接 / Current transactional connection.
    @return 账户到已锁定余额的不可变快照 / Account-to-locked-balance immutable snapshot.
    @raise ValueError 未提供任何账户时抛出 / Raised when no accounts are provided.
    @raise RuntimeError 账户投影在加锁期间消失时抛出 /
        Raised when an account projection disappears while being locked.
    @note 跨领域用例必须在检查可用余额和追加分录前调用此函数，避免先锁用户账户、
        后锁群组账户导致的锁顺序反转。/ Cross-domain use cases must call this before
        checking available funds and appending a ledger entry, preventing a user-then-group
        lock-order inversion.
    """

    accounts_by_key: dict[str, LedgerAccount] = {}
    """@brief 由稳定数据库键索引的去重账户 / Deduplicated accounts indexed by stable database key."""
    for account in accounts:
        accounts_by_key[_account_key(account)] = account
    if not accounts_by_key:
        raise ValueError("At least one bank account must be locked")

    ordered_keys = tuple(sorted(accounts_by_key))
    """@brief 全局一致的加锁顺序 / Globally consistent locking order."""
    for account_key in ordered_keys:
        await _ensure_account(accounts_by_key[account_key], connection)
    rows = await db_connection.fetch_all(
        "SELECT account_key, balance FROM bank.account_balances "
        "WHERE account_key = ANY(%s) ORDER BY account_key FOR UPDATE",
        (list(ordered_keys),),
        connection=connection,
    )
    if len(rows) != len(ordered_keys):
        raise RuntimeError("Bank account projection disappeared while locking")
    balances: dict[LedgerAccount, int] = {}
    """@brief 领域账户到锁定余额的临时映射 / Temporary domain-account-to-locked-balance mapping."""
    for row in rows:
        account_key = cast(str, row[0])
        balances[accounts_by_key[account_key]] = cast(int, row[1])
    return MappingProxyType(balances)


def derive_bank_entry_key(namespace: str, source_idempotency_key: str) -> str:
    """@brief 派生受长度约束的账本幂等键 / Derive a length-bounded ledger idempotency key.

    @param namespace 稳定业务命名空间 / Stable business namespace.
    @param source_idempotency_key 调用方原始幂等键 / Original caller idempotency key.
    @return 可安全写入银行账本的派生键 / Derived key safe for the bank ledger.
    @raise ValueError 命名空间或来源键为空时抛出 / Raised when the namespace or source key is blank.
    @note 原始键仍应存入分录元数据，派生键只解决不同子动作和长度上限问题。/
        The original key should remain in entry metadata; this derived key only separates
        sub-actions and handles the ledger's length bound.
    """

    normalized_namespace = namespace.strip().lower()
    normalized_source = source_idempotency_key.strip()
    if not normalized_namespace or not normalized_source:
        raise ValueError("Bank entry namespace and source key must be non-blank")
    if len(normalized_namespace) > 120:
        raise ValueError("Bank entry namespace must contain at most 120 characters")
    digest = sha256(
        f"{normalized_namespace}\x00{normalized_source}".encode("utf-8")
    ).hexdigest()
    return f"{normalized_namespace}:{digest}"


async def post_bank_transfer(
    *,
    namespace: str,
    source_idempotency_key: str,
    reason: LedgerReason,
    source: LedgerAccount,
    destination: LedgerAccount,
    amount: TokenAmount,
    created_at: datetime,
    actor_id: int | None,
    connection: AsyncConnection,
    metadata: Mapping[str, str | int | bool] | None = None,
) -> LedgerEntry:
    """@brief 追加一个带来源追踪的银行转账 / Append one bank transfer with source traceability.

    @param namespace 子动作稳定命名空间 / Stable sub-action namespace.
    @param source_idempotency_key 上层业务幂等键 / Parent business idempotency key.
    @param reason 审计原因 / Audit reason.
    @param source 扣款账户 / Debited account.
    @param destination 收款账户 / Credited account.
    @param amount 严格正金币数量 / Strictly positive token amount.
    @param created_at 业务发生时刻 / Business occurrence time.
    @param actor_id 可选发起人 / Optional initiating actor.
    @param connection 当前事务连接 / Current transactional connection.
    @param metadata 可选业务审计元数据 / Optional business audit metadata.
    @return 已追加的不可变账本分录 / Appended immutable ledger entry.
    """

    entry_metadata = dict(metadata or {})
    entry_metadata["source_idempotency_key"] = source_idempotency_key
    entry = LedgerEntry.transfer(
        entry_id=uuid4(),
        idempotency_key=derive_bank_entry_key(namespace, source_idempotency_key),
        reason=reason,
        source=source,
        destination=destination,
        amount=amount,
        created_at=created_at,
        actor_id=actor_id,
        metadata=entry_metadata,
    )
    await append_bank_entry(entry, connection)
    return entry


async def _ensure_account(
    account: LedgerAccount,
    connection: AsyncConnection,
) -> None:
    """@brief 确保逻辑账户和余额投影存在 / Ensure a logical account and its balance projection exist.

    @param account 领域账户标识 / Domain account identity.
    @param connection 当前事务连接 / Current transactional connection.
    @return None / None.
    @raise RuntimeError 系统账户未由迁移安装时抛出 / Raised when the migration-installed system account is missing.
    """

    if account.scope is AccountScope.USER:
        await ensure_bank_user_wallets(cast(int, account.owner_id), connection)
        return
    if account.scope is AccountScope.GROUP:
        key = _account_key(account)
        await db_connection.execute(
            "INSERT INTO bank.accounts ("
            "account_key, account_scope, owner_id, token_bucket, system_kind, allow_negative"
            ") VALUES (%s, 'group', %s, NULL, 'group_treasury', FALSE) "
            "ON CONFLICT (account_key) DO NOTHING",
            (key, account.owner_id),
            connection=connection,
        )
        await db_connection.execute(
            "INSERT INTO bank.account_balances (account_key, balance, version, updated_at) "
            "VALUES (%s, 0, 0, CURRENT_TIMESTAMP) ON CONFLICT (account_key) DO NOTHING",
            (key,),
            connection=connection,
        )
        return
    row = await db_connection.fetch_one(
        "SELECT 1 FROM bank.accounts WHERE account_key = %s",
        (_account_key(account),),
        connection=connection,
    )
    if row is None:
        raise RuntimeError("Bank system accounts are not installed")


async def ensure_bank_user_wallets(
    user_id: int,
    connection: AsyncConnection,
) -> None:
    """@brief 以零余额创建用户 free/paid 两个隔离钱包 / Create a user's free/paid isolated wallets at zero balance.

    @param user_id 用户标识 / User identity.
    @param connection 当前事务连接 / Current transactional connection.
    @return None / None.
    @raise RuntimeError 用户身份不存在时抛出 / Raised when the user identity does not exist.
    @note 历史余额导入只能由显式数据库迁移完成；运行时绝不读取 identity 金币投影。/
        Historical balance import is migration-only; runtime never reads the identity token projection.
    """

    if not await _identity_exists(user_id, connection):
        raise RuntimeError("Bank wallet owner does not exist")
    for bucket in TokenBucket:
        account = LedgerAccount.user(user_id, bucket)
        key = _account_key(account)
        await db_connection.execute(
            "INSERT INTO bank.accounts ("
            "account_key, account_scope, owner_id, token_bucket, system_kind, allow_negative"
            ") VALUES (%s, 'user', %s, %s, NULL, FALSE) "
            "ON CONFLICT (account_key) DO NOTHING",
            (key, user_id, bucket.value),
            connection=connection,
        )
        await db_connection.execute(
            "INSERT INTO bank.account_balances (account_key, balance, version, updated_at) "
            "VALUES (%s, 0, 0, CURRENT_TIMESTAMP) ON CONFLICT (account_key) DO NOTHING",
            (key,),
            connection=connection,
        )


async def load_bank_overview(
    user_id: int,
    connection: AsyncConnection,
) -> BankOverview:
    """@brief 读取已初始化用户的钱包余额 / Read balances for an initialized user wallet.

    @param user_id 用户标识 / User identity.
    @param connection 当前事务连接 / Current transactional connection.
    @return 双钱包概览 / Two-pocket wallet overview.
    @raise RuntimeError 钱包投影缺失时抛出 / Raised when a wallet projection is missing.
    """

    row = await db_connection.fetch_one(
        "SELECT "
        "(SELECT balance FROM bank.account_balances WHERE account_key = %s), "
        "(SELECT balance FROM bank.account_balances WHERE account_key = %s)",
        (
            _user_wallet_key(user_id, TokenBucket.FREE),
            _user_wallet_key(user_id, TokenBucket.PAID),
        ),
        connection=connection,
    )
    if row is None or row[0] is None or row[1] is None:
        raise RuntimeError("Bank user wallet projection is missing")
    return BankOverview(
        user_id=user_id,
        free=WalletBalance(TokenBucket.FREE, cast(int, row[0])),
        paid=WalletBalance(TokenBucket.PAID, cast(int, row[1])),
    )


def _account_key(account: LedgerAccount) -> str:
    """@brief 将领域账户编码为稳定数据库键 / Encode a domain account as a stable database key.

    @param account 领域账户 / Domain account.
    @return 受数据库唯一约束保护的账户键 / Database-constrained account key.
    """

    if account.scope is AccountScope.USER:
        return _user_wallet_key(
            cast(int, account.owner_id), cast(TokenBucket, account.bucket)
        )
    if account.scope is AccountScope.GROUP:
        return f"group:{cast(int, account.owner_id)}:treasury"
    return f"system:{cast(SystemAccountKind, account.system_kind).value}"


def _user_wallet_key(user_id: int, bucket: TokenBucket) -> str:
    """@brief 构造用户钱包数据库键 / Construct a user-wallet database key.

    @param user_id 用户标识 / User identity.
    @param bucket 钱包类别 / Wallet bucket.
    @return 稳定钱包键 / Stable wallet key.
    """

    return f"user:{user_id}:{bucket.value}"


def _request_from_row(row: object | None) -> TokenRequest | None:
    """@brief 从 PostgreSQL 行还原代币申请 / Restore a token request from a PostgreSQL row.

    @param row 数据库行或 None / Database row or None.
    @return 领域请求聚合或 None / Domain request aggregate or None.
    """

    if row is None:
        return None
    values = cast(tuple[object, ...], row)
    return TokenRequest(
        request_id=cast(UUID, values[0]),
        requester_id=cast(int, values[1]),
        requested_amount=TokenAmount(cast(int, values[2])),
        requested_bucket=TokenBucket(cast(str, values[3])),
        purpose=cast(str, values[4]),
        status=TokenRequestStatus(cast(str, values[5])),
        requested_at=_as_utc(cast(datetime, values[6])),
        reviewed_at=(
            _as_utc(cast(datetime, values[7])) if values[7] is not None else None
        ),
        reviewer_id=cast(int, values[8]) if values[8] is not None else None,
        review_note=cast(str, values[9]) if values[9] is not None else None,
        ledger_entry_id=cast(UUID, values[10]) if values[10] is not None else None,
        version=cast(int, values[11]),
    )


def _result_mapping(result: TokenRequestResult) -> dict[str, object]:
    """@brief 序列化银行结果以供幂等回放 / Serialize a bank result for idempotent replay.

    @param result 银行结果 / Bank result.
    @return JSON 兼容对象 / JSON-compatible object.
    """

    return {
        "code": result.code.value,
        "request": _request_mapping(result.request)
        if result.request is not None
        else None,
        "overview": _overview_mapping(result.overview)
        if result.overview is not None
        else None,
    }


def _result_from_mapping(
    value: Mapping[str, Any],
    *,
    replayed: bool,
) -> TokenRequestResult:
    """@brief 从幂等回执恢复银行结果 / Restore a bank result from an idempotency receipt.

    @param value 回执对象 / Receipt object.
    @param replayed 是否标记为重放 / Whether to mark as replayed.
    @return 恢复后的银行结果 / Restored bank result.
    """

    raw_request = value.get("request")
    raw_overview = value.get("overview")
    return TokenRequestResult(
        code=BankCode(str(value["code"])),
        request=(
            _request_from_mapping(cast(Mapping[str, Any], raw_request))
            if isinstance(raw_request, Mapping)
            else None
        ),
        overview=(
            _overview_from_mapping(cast(Mapping[str, Any], raw_overview))
            if isinstance(raw_overview, Mapping)
            else None
        ),
        replayed=replayed,
    )


def _activity_pot_funding_mapping(
    result: ActivityPotFundingResult,
    *,
    command: FundActivityPot,
) -> dict[str, object]:
    """@brief 序列化奖池注资结果及原始请求语义 / Serialize activity-pot funding result and request semantics.

    @param result 已完成的注资结果 / Completed funding result.
    @param command 原始管理员注资命令 / Original administrator-funding command.
    @return 可安全重放的 JSON 对象 / JSON object safe for replay.
    """

    return {
        "code": result.code.value,
        "amount": result.amount.value if result.amount is not None else None,
        "activity_pot_balance": result.activity_pot_balance,
        "ledger_entry_id": (
            str(result.ledger_entry_id) if result.ledger_entry_id is not None else None
        ),
        "requested_amount": command.amount.value,
        "requested_purpose": command.purpose.strip(),
    }


def _activity_pot_funding_from_mapping(
    value: Mapping[str, Any],
    *,
    command: FundActivityPot,
    replayed: bool,
) -> ActivityPotFundingResult:
    """@brief 从回执恢复并验证活动奖池注资结果 / Restore and validate activity-pot funding from a receipt.

    @param value 回执 JSON 对象 / Receipt JSON object.
    @param command 当前管理员注资命令 / Current administrator-funding command.
    @param replayed 是否标记为幂等回放 / Whether to mark as an idempotent replay.
    @return 验证后的注资结果 / Validated funding result.
    @raise ValueError 同一幂等键改变金额或用途时抛出 /
        Raised when one idempotency key changes amount or purpose.
    """

    if (
        int(value.get("requested_amount", -1)) != command.amount.value
        or str(value.get("requested_purpose", "")) != command.purpose.strip()
    ):
        raise ValueError("Activity-pot funding idempotency key changed semantics")
    raw_amount = value.get("amount")
    raw_balance = value.get("activity_pot_balance")
    raw_entry_id = value.get("ledger_entry_id")
    return ActivityPotFundingResult(
        code=BankCode(str(value["code"])),
        amount=TokenAmount(int(raw_amount)) if raw_amount is not None else None,
        activity_pot_balance=(int(raw_balance) if raw_balance is not None else None),
        ledger_entry_id=UUID(str(raw_entry_id)) if raw_entry_id is not None else None,
        replayed=replayed,
    )


def _request_mapping(request: TokenRequest) -> dict[str, object]:
    """@brief 序列化代币请求 / Serialize a token request.

    @param request 请求聚合 / Request aggregate.
    @return JSON 兼容请求对象 / JSON-compatible request object.
    """

    return {
        "request_id": str(request.request_id),
        "requester_id": request.requester_id,
        "requested_amount": request.requested_amount.value,
        "requested_bucket": request.requested_bucket.value,
        "purpose": request.purpose,
        "status": request.status.value,
        "requested_at": request.requested_at.isoformat(),
        "reviewed_at": (
            request.reviewed_at.isoformat() if request.reviewed_at is not None else None
        ),
        "reviewer_id": request.reviewer_id,
        "review_note": request.review_note,
        "ledger_entry_id": (
            str(request.ledger_entry_id)
            if request.ledger_entry_id is not None
            else None
        ),
        "version": request.version,
    }


def _request_from_mapping(value: Mapping[str, Any]) -> TokenRequest:
    """@brief 从回执对象恢复代币请求 / Restore a token request from a receipt object.

    @param value JSON 兼容请求对象 / JSON-compatible request object.
    @return 领域请求聚合 / Domain request aggregate.
    """

    raw_reviewed_at = value.get("reviewed_at")
    raw_entry_id = value.get("ledger_entry_id")
    raw_reviewer_id = value.get("reviewer_id")
    raw_note = value.get("review_note")
    return TokenRequest(
        request_id=UUID(str(value["request_id"])),
        requester_id=int(value["requester_id"]),
        requested_amount=TokenAmount(int(value["requested_amount"])),
        requested_bucket=TokenBucket(str(value["requested_bucket"])),
        purpose=str(value["purpose"]),
        status=TokenRequestStatus(str(value["status"])),
        requested_at=datetime.fromisoformat(str(value["requested_at"])),
        reviewed_at=(
            datetime.fromisoformat(str(raw_reviewed_at))
            if raw_reviewed_at is not None
            else None
        ),
        reviewer_id=int(raw_reviewer_id) if raw_reviewer_id is not None else None,
        review_note=str(raw_note) if raw_note is not None else None,
        ledger_entry_id=UUID(str(raw_entry_id)) if raw_entry_id is not None else None,
        version=int(value["version"]),
    )


def _overview_mapping(overview: BankOverview) -> dict[str, object]:
    """@brief 序列化钱包概览 / Serialize a wallet overview.

    @param overview 钱包概览 / Wallet overview.
    @return JSON 兼容钱包对象 / JSON-compatible wallet object.
    """

    return {
        "user_id": overview.user_id,
        "free": overview.free.value,
        "paid": overview.paid.value,
    }


def _overview_from_mapping(value: Mapping[str, Any]) -> BankOverview:
    """@brief 从回执对象恢复钱包概览 / Restore a wallet overview from a receipt object.

    @param value JSON 兼容钱包对象 / JSON-compatible wallet object.
    @return 钱包概览 / Wallet overview.
    """

    return BankOverview(
        user_id=int(value["user_id"]),
        free=WalletBalance(TokenBucket.FREE, int(value["free"])),
        paid=WalletBalance(TokenBucket.PAID, int(value["paid"])),
    )


def _as_utc(value: datetime) -> datetime:
    """@brief 将数据库时间规范为 UTC aware datetime / Normalize a database time to a UTC-aware datetime.

    @param value 数据库时间 / Database timestamp.
    @return UTC aware 时间 / UTC-aware time.
    """

    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


__all__ = [
    "PostgresBankOperations",
    "append_bank_entry",
    "derive_bank_entry_key",
    "ensure_bank_user_wallets",
    "lock_bank_account_balances",
    "load_bank_overview",
    "post_bank_transfer",
]
