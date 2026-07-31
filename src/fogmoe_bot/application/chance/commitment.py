"""@brief 随机活动承诺用例编排 / Chance-activity commitment use-case orchestration."""

from __future__ import annotations

from fogmoe_bot.domain.chance.fairness import ServerSeed
from fogmoe_bot.domain.chance.rounds import PrivateCommittedChanceRound

from .commands import CommitChanceRound
from .ports import ServerSeedSource


class ChanceCommitmentService:
    """@brief 取得一次服务器熵并建立领域承诺 / Acquire server entropy once and create a domain commitment.

    本应用服务只编排不可确定的熵端口。承诺计算、轮次不变量和后续状态转换全部由领域
    对象拥有，避免应用层复制领域行为或提供无意义转发。
    This application service orchestrates only the nondeterministic entropy port. Commitment
    calculation, round invariants, and subsequent state transitions belong to domain objects,
    avoiding duplicated domain behavior and forwarding-only application methods.

    @param seeds 服务器种子来源端口 / Server-seed source port.
    """

    def __init__(self, seeds: ServerSeedSource) -> None:
        """@brief 显式注入服务器熵端口 / Explicitly inject the server-entropy port.

        @param seeds 生产或测试种子来源 / Production or test seed source.
        """

        self._seeds = seeds

    def commit(self, command: CommitChanceRound) -> PrivateCommittedChanceRound:
        """@brief 获取一次熵并创建私有承诺态 / Acquire entropy once and create private committed state.

        @param command 已完成边界校验的开轮命令 / Boundary-validated open-round command.
        @return 等待玩家种子的私有承诺态 / Private committed state awaiting a player seed.
        @raise TypeError 熵端口未返回 ServerSeed 时抛出 / Raised when the entropy port returns no ServerSeed.
        """

        server_seed = self._seeds.next_server_seed()
        if not isinstance(server_seed, ServerSeed):
            raise TypeError("Chance server-seed source must return ServerSeed")
        return PrivateCommittedChanceRound.commit(
            round_id=command.round_id,
            scope=command.scope,
            player_id=command.player_id,
            ruleset=command.ruleset,
            rule_code=command.rule_code,
            stake=command.stake,
            server_seed=server_seed,
            nonce=command.nonce,
        )
