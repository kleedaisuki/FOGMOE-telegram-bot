"""@brief 耐久随机活动编排工作流 / Durable orchestration workflow for chance activities."""

from __future__ import annotations

from fogmoe_bot.domain.chance.scope import PersonalRoundScope, RoundScope

from .models import PreparedChanceRound, PrivateCommittedChanceRound
from .ports import ChanceRoundOperations
from .service import ChanceService
from .workflow_models import (
    BindAndSettleChanceRound,
    ChanceWorkflowCode,
    ChanceWorkflowResult,
    CommitDurableChanceRound,
    LookupChanceRound,
)


CHANCE_WORKFLOW_DATA_KEY = "chance.workflow"
"""@brief runtime capability 中可验证随机活动工作流稳定键 / Stable verifiable-chance workflow key in runtime capabilities."""


class ChanceWorkflow:
    """@brief 将纯数学随机核心连接到耐久原子端口 / Connect pure chance mathematics to a durable atomic port.

    本工作流不读取私有服务器种子、不写钱包、不自行结算账本。它只做可在内存中判断的
    owner/scope 授权，并把需要原子性的状态转换交给 ``ChanceRoundOperations``。
    This workflow neither reads private server seeds, writes wallets, nor settles a ledger itself.
    It performs only owner/scope authorization decidable in memory and delegates atomic state
    transitions to ``ChanceRoundOperations``.

    @param operations 耐久原子事务端口 / Durable atomic transaction port.
    @param chance 纯数学承诺、绑定与结算服务 / Pure mathematical commitment, binding, and settlement service.
    """

    def __init__(
        self,
        operations: ChanceRoundOperations,
        chance: ChanceService,
    ) -> None:
        """@brief 注入耐久端口和纯数学服务 / Inject durable port and pure mathematical service.

        @param operations 耐久原子事务端口 / Durable atomic transaction port.
        @param chance 纯数学随机服务 / Pure mathematical chance service.
        """

        self._operations = operations
        self._chance = chance

    async def commit(
        self,
        command: CommitDurableChanceRound,
    ) -> ChanceWorkflowResult:
        """@brief 授权后建立并耐久化公开承诺 / Authorize then create and durably persist a public commitment.

        @param command 耐久承诺命令 / Durable commitment command.
        @return 成功、拒绝或存储端口结果 / Success, rejection, or storage-port result.
        """

        if command.actor_id != command.round.player_id:
            return ChanceWorkflowResult(ChanceWorkflowCode.FORBIDDEN)
        if not _actor_may_use_scope(command.actor_id, command.round.scope):
            return ChanceWorkflowResult(ChanceWorkflowCode.FORBIDDEN)
        private_round = self._chance.commit(command.round)
        return await self._operations.commit_chance_round(command, private_round)

    async def bind_and_settle(
        self,
        command: BindAndSettleChanceRound,
    ) -> ChanceWorkflowResult:
        """@brief 授权后委托事务内绑定、预留与结算 / Authorize then delegate in-transaction binding, reservation, and settlement.

        @param command 绑定和结算命令 / Bind-and-settle command.
        @return 成功、拒绝或存储端口结果 / Success, rejection, or storage-port result.
        @note 对已保存轮次的 owner 和完整 scope 比较必须在端口锁定同一行时完成；工作流
            不做先 lookup 再 write 的竞态检查。/
            Comparison against persisted owner and full scope must happen while the port locks the
            same row; this workflow never performs a racy lookup-then-write check.
        """

        if not _actor_may_use_scope(command.actor_id, command.scope):
            return ChanceWorkflowResult(ChanceWorkflowCode.FORBIDDEN)

        def prepare(private_round: PrivateCommittedChanceRound) -> PreparedChanceRound:
            """@brief 在适配器事务内绑定客户端种子 / Bind client seed inside adapter transaction.

            @param private_round 受保护读取的私有承诺态 /
                Private committed state read under protection.
            @return 仅在本事务中可用于结算的准备态 /
                Prepared state usable for settlement only in this transaction.
            """

            return self._chance.bind_client_seed(private_round, command.client_seed)

        return await self._operations.bind_and_settle_chance_round(command, prepare)

    async def lookup(self, command: LookupChanceRound) -> ChanceWorkflowResult:
        """@brief 授权后读取安全轮次视图 / Authorize then read a safe round view.

        @param command 查询命令 / Lookup command.
        @return 安全视图、拒绝或端口结果 / Safe view, rejection, or port result.
        """

        if not _actor_may_use_scope(command.actor_id, command.scope):
            return ChanceWorkflowResult(ChanceWorkflowCode.FORBIDDEN)
        return await self._operations.lookup_chance_round(command)


def _actor_may_use_scope(actor_id: int, scope: RoundScope) -> bool:
    """@brief 判断调用者是否可在请求范围内发起操作 / Decide whether actor may initiate an operation in request scope.

    个人范围由其唯一拥有者控制。群组范围本身不把成员关系编码成 ``scope``，因此群成员
    资格和已保存轮次 owner 必须由端口在受锁事务中验证。
    A personal scope is controlled by its sole owner. A group scope intentionally does not encode
    membership in ``scope``, so group membership and persisted-round owner must be verified by the
    port inside its locked transaction.

    @param actor_id 请求用户标识 / Requesting-user identity.
    @param scope 请求所在个人或群组范围 / Personal or group request scope.
    @return 本地可判定授权通过时为 True / True when locally decidable authorization passes.
    """

    return not isinstance(scope, PersonalRoundScope) or scope.user_id == actor_id
