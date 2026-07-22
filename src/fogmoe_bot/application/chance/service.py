"""@brief 随机活动准备与结算应用服务 / Chance-activity preparation and settlement application service."""

from __future__ import annotations

from typing import Protocol

from fogmoe_bot.domain.chance.fairness import (
    ClientSeed,
    ServerSeed,
    commit_server_seed,
)
from fogmoe_bot.domain.chance.rounds import ChanceSettlement, CommittedChanceRound
from fogmoe_bot.domain.chance.rules import ChanceQuote

from .models import CommitChanceRound, PreparedChanceRound, PrivateCommittedChanceRound


class ServerSeedSource(Protocol):
    """@brief 服务器种子来源端口 / Port supplying server randomness seeds."""

    def next_server_seed(self) -> ServerSeed:
        """@brief 提供一个尚未使用的服务器种子 / Provide one unused server seed.

        @return 尚未揭示的服务器种子 / Unrevealed server seed.
        """

        ...


class SystemServerSeedSource:
    """@brief 基于操作系统熵的服务器种子来源 / Server-seed source backed by operating-system entropy."""

    def next_server_seed(self) -> ServerSeed:
        """@brief 生成 256 bit 服务器种子 / Generate a 256-bit server seed.

        @return 新的未揭示服务器种子 / New unrevealed server seed.
        """

        return ServerSeed.random()


class ChanceService:
    """@brief 在无传输与无数据库依赖下准备和结算随机活动 / Prepare and settle chance activities without transport or database dependencies.

    持久化适配器应在 ``commit`` 后原子保存和公开承诺，再收集玩家种子并调用
    ``bind_client_seed``；免费金币账本预留应与绑定操作同事务保存，随后恰好一次调用
    ``settle``。
    A persistence adapter should atomically save and publish the commitment after ``commit``, then
    collect the player seed and call ``bind_client_seed``. The free-token ledger reservation belongs
    in the same transaction as binding; ``settle`` must subsequently run exactly once.

    @param seeds 服务器种子来源 / Server-seed source.
    """

    def __init__(self, seeds: ServerSeedSource | None = None) -> None:
        """@brief 注入种子来源 / Inject the seed source.

        @param seeds 可选测试或生产种子来源 / Optional test or production seed source.
        """

        self._seeds = seeds or SystemServerSeedSource()

    def quote(self, command: CommitChanceRound) -> ChanceQuote:
        """@brief 在承诺前验证并报价 / Validate and quote before commitment.

        @param command 开轮命令 / Open-round command.
        @return 严格负期望报价 / Strictly negative-EV quote.
        """

        return command.ruleset.quote(command.rule_code, command.stake)

    def commit(self, command: CommitChanceRound) -> PrivateCommittedChanceRound:
        """@brief 在玩家种子前生成并公开种子承诺 / Generate and publish a seed commitment before a player seed.

        @param command 开轮命令 / Open-round command.
        @return 等待玩家种子的私有承诺态 / Private committed state awaiting a player seed.
        """

        self.quote(command)
        server_seed = self._seeds.next_server_seed()
        if not isinstance(server_seed, ServerSeed):
            raise TypeError("Chance server-seed source must return ServerSeed")
        committed_round = CommittedChanceRound(
            round_id=command.round_id,
            scope=command.scope,
            player_id=command.player_id,
            ruleset=command.ruleset,
            rule_code=command.rule_code,
            stake=command.stake,
            commitment=commit_server_seed(server_seed),
            nonce=command.nonce,
        )
        return PrivateCommittedChanceRound(committed_round, server_seed)

    def bind_client_seed(
        self,
        committed: PrivateCommittedChanceRound,
        client_seed: ClientSeed,
    ) -> PreparedChanceRound:
        """@brief 在已公开承诺后绑定玩家种子 / Bind a player seed after a public commitment.

        @param committed 已持久化和公开的私有承诺态 /
            Private committed state that has been persisted and published.
        @param client_seed 玩家在揭示前给出的种子 / Player seed supplied before reveal.
        @return 可结算的私有准备态 / Private prepared state ready for settlement.
        @raise TypeError 参数类型不正确时抛出 / Raised when argument types are invalid.
        """

        if not isinstance(committed, PrivateCommittedChanceRound):
            raise TypeError(
                "Chance client-seed binding requires PrivateCommittedChanceRound"
            )
        return committed.bind_client_seed(client_seed)

    def settle(self, prepared: PreparedChanceRound) -> ChanceSettlement:
        """@brief 结算一个已准备轮次 / Settle one prepared round.

        @param prepared 由 ``bind_client_seed`` 产生并受保护存放的准备态 /
            Prepared state produced by ``bind_client_seed`` and stored safely.
        @return 含公平性证明的结算 / Settlement containing a fairness proof.
        @raise TypeError 参数不是准备态时抛出 / Raised when the argument is not prepared state.
        """

        if not isinstance(prepared, PreparedChanceRound):
            raise TypeError("Chance settlement requires PreparedChanceRound")
        return prepared.settlement()
