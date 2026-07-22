"""@brief 耐久随机活动工作流边界测试 / Boundary tests for the durable chance workflow."""

from __future__ import annotations

import asyncio
from typing import cast
from uuid import UUID

from fogmoe_bot.application.chance.models import (
    CommitChanceRound,
    PreparedChanceRound,
    PrivateCommittedChanceRound,
)
from fogmoe_bot.application.chance.ports import (
    ChanceRoundOperations,
    ChanceRoundPreparer,
)
from fogmoe_bot.application.chance.service import ChanceService, ServerSeedSource
from fogmoe_bot.application.chance.workflow import ChanceWorkflow
from fogmoe_bot.application.chance.workflow_models import (
    BindAndSettleChanceRound,
    ChanceRoundStatus,
    ChanceRoundView,
    ChanceWorkflowCode,
    ChanceWorkflowResult,
    CommitDurableChanceRound,
    LookupChanceRound,
)
from fogmoe_bot.domain.chance.examples import sicbo_like_ruleset
from fogmoe_bot.domain.chance.fairness import ClientSeed, ServerSeed
from fogmoe_bot.domain.chance.money import FreeTokenStake
from fogmoe_bot.domain.chance.scope import GroupRoundScope, PersonalRoundScope

_ROUND_ID = UUID("00000000-0000-0000-0000-000000000456")
"""@brief 工作流测试的稳定轮次 UUID / Stable round UUID for workflow tests."""


class _FixedSeeds:
    """@brief 记录取种次数的固定服务器种子来源 / Fixed server-seed source recording consumption count."""

    def __init__(self) -> None:
        """@brief 初始化固定测试种子 / Initialize the fixed test seed."""

        self.calls = 0
        self._seed = ServerSeed(b"w" * 32)

    def next_server_seed(self) -> ServerSeed:
        """@brief 返回固定服务器种子 / Return the fixed server seed.

        @return 固定服务器种子 / Fixed server seed.
        """

        self.calls += 1
        return self._seed


class _Operations:
    """@brief 在事务边界内执行准备回调的内存端口 / In-memory port executing preparation callback inside transaction boundary."""

    def __init__(self) -> None:
        """@brief 初始化调用记录与私有承诺槽位 / Initialize call recordings and private-commitment slot."""

        self.commit_commands: list[CommitDurableChanceRound] = []
        self.bind_commands: list[BindAndSettleChanceRound] = []
        self.lookup_commands: list[LookupChanceRound] = []
        self.private_round: PrivateCommittedChanceRound | None = None
        self.prepared_rounds: list[PreparedChanceRound] = []
        self.preparer_calls = 0

    async def commit_chance_round(
        self,
        command: CommitDurableChanceRound,
        private_round: PrivateCommittedChanceRound,
    ) -> ChanceWorkflowResult:
        """@brief 模拟原子保存承诺和回执 / Simulate atomically storing commitment and receipt.

        @param command 耐久承诺命令 / Durable commitment command.
        @param private_round 含私有种子的承诺态 / Committed state containing private seed.
        @return 成功的公开承诺视图 / Successful public commitment view.
        """

        self.commit_commands.append(command)
        self.private_round = private_round
        return ChanceWorkflowResult(
            ChanceWorkflowCode.SUCCESS,
            ChanceRoundView(
                private_round.committed_round,
                ChanceRoundStatus.COMMITTED,
            ),
        )

    async def bind_and_settle_chance_round(
        self,
        command: BindAndSettleChanceRound,
        prepare: ChanceRoundPreparer,
    ) -> ChanceWorkflowResult:
        """@brief 模拟同一事务内锁定、准备、免费扣款和结算 / Simulate locking, preparation, free-only charge, and settlement in one transaction.

        @param command 绑定和结算命令 / Bind-and-settle command.
        @param prepare 事务内准备回调 / In-transaction preparation callback.
        @return 成功、范围不匹配或未找到结果 / Success, scope mismatch, or not-found result.
        """

        self.bind_commands.append(command)
        if self.private_round is None:
            return ChanceWorkflowResult(ChanceWorkflowCode.NOT_FOUND)
        if command.actor_id != self.private_round.committed_round.player_id:
            return ChanceWorkflowResult(ChanceWorkflowCode.FORBIDDEN)
        if command.scope != self.private_round.committed_round.scope:
            return ChanceWorkflowResult(ChanceWorkflowCode.SCOPE_MISMATCH)
        self.preparer_calls += 1
        prepared = prepare(self.private_round)
        self.prepared_rounds.append(prepared)
        assert isinstance(prepared.round.stake, FreeTokenStake)
        settlement = prepared.settlement()
        return ChanceWorkflowResult(
            ChanceWorkflowCode.SUCCESS,
            ChanceRoundView(
                self.private_round.committed_round,
                ChanceRoundStatus.SETTLED,
                settlement,
            ),
        )

    async def lookup_chance_round(
        self,
        command: LookupChanceRound,
    ) -> ChanceWorkflowResult:
        """@brief 模拟 owner/scope 过滤后的公开查询 / Simulate owner/scope-filtered public lookup.

        @param command 查询命令 / Lookup command.
        @return 匹配的承诺视图或未找到结果 / Matching commitment view or not-found result.
        """

        self.lookup_commands.append(command)
        if self.private_round is None:
            return ChanceWorkflowResult(ChanceWorkflowCode.NOT_FOUND)
        if command.actor_id != self.private_round.committed_round.player_id:
            return ChanceWorkflowResult(ChanceWorkflowCode.FORBIDDEN)
        if command.scope != self.private_round.committed_round.scope:
            return ChanceWorkflowResult(ChanceWorkflowCode.SCOPE_MISMATCH)
        return ChanceWorkflowResult(
            ChanceWorkflowCode.SUCCESS,
            ChanceRoundView(
                self.private_round.committed_round,
                ChanceRoundStatus.COMMITTED,
            ),
        )


def _round(*, scope: PersonalRoundScope | GroupRoundScope) -> CommitChanceRound:
    """@brief 构造有效纯数学承诺命令 / Build a valid pure mathematical commitment command.

    @param scope 个人或群组范围 / Personal or group scope.
    @return 纯数学承诺命令 / Pure mathematical commitment command.
    """

    return CommitChanceRound(
        round_id=_ROUND_ID,
        scope=scope,
        player_id=42,
        ruleset=sicbo_like_ruleset(),
        rule_code="big",
        stake=FreeTokenStake(10),
        nonce=3,
    )


def _workflow() -> tuple[ChanceWorkflow, _Operations, _FixedSeeds]:
    """@brief 创建隔离工作流与端口记录器 / Create isolated workflow and port recorder.

    @return 工作流、内存端口和种子来源 / Workflow, in-memory port, and seed source.
    """

    operations = _Operations()
    seeds = _FixedSeeds()
    chance = ChanceService(cast(ServerSeedSource, seeds))
    return (
        ChanceWorkflow(cast(ChanceRoundOperations, operations), chance),
        operations,
        seeds,
    )


def test_commit_rejects_delegated_wager_before_consuming_seed_or_touching_port() -> (
    None
):
    """@brief 委托下注在随机与持久化前被拒绝 / Delegated wager is rejected before randomness or persistence."""

    async def scenario() -> None:
        """@brief 执行委托下注拒绝场景 / Execute delegated-wager rejection scenario."""

        workflow, operations, seeds = _workflow()
        result = await workflow.commit(
            CommitDurableChanceRound(
                actor_id=7,
                round=_round(scope=GroupRoundScope(-100_42, topic_id=9)),
                idempotency_key="chance:commit:delegated",
            )
        )

        assert result.code is ChanceWorkflowCode.FORBIDDEN
        assert operations.commit_commands == []
        assert seeds.calls == 0

    asyncio.run(scenario())


def test_workflow_persists_public_commitment_then_binds_and_settles_inside_port() -> (
    None
):
    """@brief 承诺公开后，准备态仅在端口事务内创建 / Prepared state is created only inside port transaction after public commitment."""

    async def scenario() -> None:
        """@brief 执行承诺、绑定与结算场景 / Execute commitment, binding, and settlement scenario."""

        workflow, operations, seeds = _workflow()
        scope = PersonalRoundScope(42)
        commit_result = await workflow.commit(
            CommitDurableChanceRound(
                actor_id=42,
                round=_round(scope=scope),
                idempotency_key="chance:commit:owned",
            )
        )

        assert commit_result.code is ChanceWorkflowCode.SUCCESS
        assert commit_result.view is not None
        assert commit_result.view.status is ChanceRoundStatus.COMMITTED
        assert not hasattr(commit_result.view, "server_seed")
        assert len(operations.commit_commands) == 1
        assert operations.prepared_rounds == []
        assert seeds.calls == 1

        forbidden_bind = await workflow.bind_and_settle(
            BindAndSettleChanceRound(
                round_id=_ROUND_ID,
                actor_id=7,
                scope=scope,
                client_seed=ClientSeed("wrong-owner"),
                idempotency_key="chance:bind:forbidden",
            )
        )
        assert forbidden_bind.code is ChanceWorkflowCode.FORBIDDEN
        assert operations.bind_commands == []

        settled = await workflow.bind_and_settle(
            BindAndSettleChanceRound(
                round_id=_ROUND_ID,
                actor_id=42,
                scope=scope,
                client_seed=ClientSeed("Klee-durable-seed"),
                idempotency_key="chance:bind:owned",
            )
        )

        assert settled.code is ChanceWorkflowCode.SUCCESS
        assert settled.view is not None
        assert settled.view.status is ChanceRoundStatus.SETTLED
        assert settled.view.settlement is not None
        assert settled.view.settlement.proof.verifies()
        assert operations.preparer_calls == 1
        assert len(operations.prepared_rounds) == 1
        assert operations.prepared_rounds[0].round.client_seed == ClientSeed(
            "Klee-durable-seed"
        )
        assert operations.prepared_rounds[0].round.stake == FreeTokenStake(10)

    asyncio.run(scenario())


def test_group_scope_mismatch_is_delegated_to_locked_persistence_and_lookup_keeps_owner_boundary() -> (
    None
):
    """@brief 群组范围需端口锁定比对，个人查询先守住 owner 边界 / Group scope comparison is delegated under port lock while personal lookup guards owner first."""

    async def scenario() -> None:
        """@brief 执行群组范围与查询边界场景 / Execute group-scope and lookup-boundary scenario."""

        workflow, operations, _ = _workflow()
        committed_scope = GroupRoundScope(-100_42, topic_id=9)
        await workflow.commit(
            CommitDurableChanceRound(
                actor_id=42,
                round=_round(scope=committed_scope),
                idempotency_key="chance:commit:group",
            )
        )

        mismatch = await workflow.bind_and_settle(
            BindAndSettleChanceRound(
                round_id=_ROUND_ID,
                actor_id=42,
                scope=GroupRoundScope(-100_42, topic_id=10),
                client_seed=ClientSeed("scope-mismatch"),
                idempotency_key="chance:bind:scope-mismatch",
            )
        )
        assert mismatch.code is ChanceWorkflowCode.SCOPE_MISMATCH
        assert len(operations.bind_commands) == 1
        assert operations.preparer_calls == 0

        personal_lookup = await workflow.lookup(
            LookupChanceRound(
                round_id=_ROUND_ID,
                actor_id=7,
                scope=PersonalRoundScope(42),
            )
        )
        assert personal_lookup.code is ChanceWorkflowCode.FORBIDDEN
        assert operations.lookup_commands == []

        group_lookup = await workflow.lookup(
            LookupChanceRound(
                round_id=_ROUND_ID,
                actor_id=42,
                scope=committed_scope,
            )
        )
        assert group_lookup.code is ChanceWorkflowCode.SUCCESS
        assert len(operations.lookup_commands) == 1

    asyncio.run(scenario())
