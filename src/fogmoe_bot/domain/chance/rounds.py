"""@brief 带范围与公平性证明的随机活动轮次 / Scoped chance rounds with fairness proofs."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from .fairness import (
    MAX_NONCE,
    ClientSeed,
    FairnessProof,
    ServerSeed,
    ServerSeedCommitment,
    reveal_fairness_proof,
)
from .money import FreeTokenPayout, FreeTokenStake
from .rules import ChanceOutcome, ChanceQuote, ChanceRule, ChanceRuleset
from .scope import GroupRoundScope, PersonalRoundScope, RoundScope


@dataclass(frozen=True, slots=True)
class CommittedChanceRound:
    """@brief 已先行承诺、尚待玩家种子的轮次 / Round committed before a player seed is supplied.

    该阶段必须先持久化和公开 ``commitment``，再允许玩家提供 ``ClientSeed``。这使
    服务器无法在看见玩家种子后挑选有利的服务器种子。
    This stage must persist and publish ``commitment`` before accepting ``ClientSeed``. It
    prevents the server from selecting a favorable server seed after seeing the player seed.

    @param round_id 轮次 UUID / Round UUID.
    @param scope 明确的个人或群组范围 / Explicit personal or group scope.
    @param player_id 下单玩家标识 / Wagering-player identity.
    @param ruleset 固化规则集 / Frozen ruleset.
    @param rule_code 玩家将要选择的规则编码 / Rule code that the player will wager on.
    @param stake 只允许免费金币的押注 / Free-token-only stake.
    @param commitment 已发布服务器种子承诺 / Published server-seed commitment.
    @param nonce 轮次内业务 nonce / Business nonce within the round.
    """

    round_id: UUID
    """@brief 轮次 UUID / Round UUID."""

    scope: RoundScope
    """@brief 个人或群组范围 / Personal or group scope."""

    player_id: int
    """@brief 下单玩家标识 / Wagering-player identity."""

    ruleset: ChanceRuleset
    """@brief 固化规则集 / Frozen ruleset."""

    rule_code: str
    """@brief 选中规则编码 / Selected rule code."""

    stake: FreeTokenStake
    """@brief 免费金币押注 / Free-token stake."""

    commitment: ServerSeedCommitment
    """@brief 已发布服务器种子承诺 / Published server-seed commitment."""

    nonce: int
    """@brief 业务 nonce / Business nonce."""

    def __post_init__(self) -> None:
        """@brief 校验承诺阶段的轮次边界 / Validate commitment-stage round boundaries.

        @return None / None.
        @raise TypeError 轮次字段类型不匹配时抛出 / Raised when round field types do not match.
        @raise ValueError 轮次身份、范围或赔率非法时抛出 /
            Raised when round identity, scope, or odds are invalid.
        """

        _validate_round_core(
            round_id=self.round_id,
            scope=self.scope,
            player_id=self.player_id,
            ruleset=self.ruleset,
            rule_code=self.rule_code,
            stake=self.stake,
            commitment=self.commitment,
            nonce=self.nonce,
        )

    @property
    def quote(self) -> ChanceQuote:
        """@brief 返回在承诺阶段已固定的赔率报价 / Return odds quote fixed at commitment stage.

        @return 严格负期望报价 / Strictly negative-EV quote.
        """

        return self.ruleset.quote(self.rule_code, self.stake)

    @property
    def ruleset_fingerprint(self) -> str:
        """@brief 返回用于公开审计的规则集指纹 / Return ruleset fingerprint for public audit.

        @return 稳定 SHA-256 规则集指纹 / Stable SHA-256 ruleset fingerprint.
        """

        return self.ruleset.fingerprint

    def bind_client_seed(self, client_seed: ClientSeed) -> ChanceRound:
        """@brief 在承诺已公开后绑定玩家种子 / Bind the player seed after commitment publication.

        @param client_seed 玩家在揭示前给出的种子 / Player seed supplied before reveal.
        @return 可结算的完整轮次 / Complete round ready for settlement.
        @raise TypeError 玩家种子类型不匹配时抛出 / Raised when the player seed type does not match.
        """

        if not isinstance(client_seed, ClientSeed):
            raise TypeError("Chance round requires ClientSeed")
        return ChanceRound(
            round_id=self.round_id,
            scope=self.scope,
            player_id=self.player_id,
            ruleset=self.ruleset,
            rule_code=self.rule_code,
            stake=self.stake,
            commitment=self.commitment,
            client_seed=client_seed,
            nonce=self.nonce,
        )


@dataclass(frozen=True, slots=True)
class ChanceRound:
    """@brief 尚未结算的免费金币随机活动轮次 / Unsettled free-token chance-activity round.

    @param round_id 轮次 UUID / Round UUID.
    @param scope 明确的个人或群组范围 / Explicit personal or group scope.
    @param player_id 下单玩家标识 / Player placing the wager.
    @param ruleset 固化在轮次中的规则集 / Ruleset frozen into the round.
    @param rule_code 玩家选中的规则编码 / Rule code selected by the player.
    @param stake 只允许免费金币的押注 / Free-token-only stake.
    @param commitment 结算前已发布的服务器种子承诺 / Server-seed commitment published before settlement.
    @param client_seed 玩家结算前给出的客户端种子 / Client seed supplied by the player before settlement.
    @param nonce 轮次内业务 nonce / Business nonce within the round.
    """

    round_id: UUID
    """@brief 轮次 UUID / Round UUID."""

    scope: RoundScope
    """@brief 个人或群组范围 / Personal or group scope."""

    player_id: int
    """@brief 下单玩家标识 / Wagering-player identity."""

    ruleset: ChanceRuleset
    """@brief 固化规则集 / Frozen ruleset."""

    rule_code: str
    """@brief 选中规则编码 / Selected rule code."""

    stake: FreeTokenStake
    """@brief 免费金币押注 / Free-token stake."""

    commitment: ServerSeedCommitment
    """@brief 服务器种子承诺 / Server-seed commitment."""

    client_seed: ClientSeed
    """@brief 玩家客户端种子 / Player client seed."""

    nonce: int
    """@brief 业务 nonce / Business nonce."""

    def __post_init__(self) -> None:
        """@brief 校验轮次边界与报价 / Validate round boundaries and quote.

        @return None / None.
        @raise TypeError 范围、规则集、押注或公平性类型不匹配时抛出 /
            Raised when scope, ruleset, stake, or fairness types do not match.
        @raise ValueError 玩家身份或个人范围不匹配时抛出 /
            Raised when player identity or personal scope does not match.
        """

        _validate_round_core(
            round_id=self.round_id,
            scope=self.scope,
            player_id=self.player_id,
            ruleset=self.ruleset,
            rule_code=self.rule_code,
            stake=self.stake,
            commitment=self.commitment,
            nonce=self.nonce,
        )
        if not isinstance(self.client_seed, ClientSeed):
            raise TypeError("Chance round requires ClientSeed")

    @property
    def rule(self) -> ChanceRule:
        """@brief 返回玩家选择的规则 / Return the player-selected rule.

        @return 选中规则 / Selected rule.
        """

        return self.ruleset.rule_for(self.rule_code)

    @property
    def quote(self) -> ChanceQuote:
        """@brief 返回轮次被冻结的赔率报价 / Return the quote frozen into the round.

        @return 严格负期望报价 / Strictly negative-EV quote.
        """

        return self.ruleset.quote(self.rule_code, self.stake)

    @property
    def ruleset_fingerprint(self) -> str:
        """@brief 返回用于公开审计的规则集指纹 / Return ruleset fingerprint for public audit.

        @return 稳定 SHA-256 规则集指纹 / Stable SHA-256 ruleset fingerprint.
        """

        return self.ruleset.fingerprint

    def settle(self, server_seed: ServerSeed) -> ChanceSettlement:
        """@brief 揭示种子、取样并得到不可变结算 / Reveal seed, sample, and produce immutable settlement.

        @param server_seed 与已发布承诺匹配的服务器种子 / Server seed matching the published commitment.
        @return 结果、派彩与公平性证明 / Outcome, payout, and fairness proof.
        @raise ValueError 服务器种子与承诺不匹配时抛出 /
            Raised when the server seed does not match the commitment.
        """

        proof = reveal_fairness_proof(
            round_id=self.round_id,
            commitment=self.commitment,
            server_seed=server_seed,
            client_seed=self.client_seed,
            nonce=self.nonce,
            upper_bound=self.ruleset.total_weight,
        )
        outcome = self.ruleset.outcome_for_ticket(proof.sample.ticket)
        won = outcome.code in self.rule.winning_outcome_codes
        return ChanceSettlement(
            round=self,
            outcome=outcome,
            proof=proof,
            payout=self.quote.gross_payout if won else None,
        )


@dataclass(frozen=True, slots=True)
class ChanceSettlement:
    """@brief 一轮免费金币随机活动的结算 / Settlement of one free-token chance activity.

    @param round 原始未结算轮次 / Original unsettled round.
    @param outcome 从无偏整数票映射的结果 / Outcome mapped from the unbiased ticket.
    @param proof 可公开复算的公平性证明 / Publicly reproducible fairness proof.
    @param payout 获胜时的总派彩；失败为 None / Gross payout on win; None on loss.
    """

    round: ChanceRound
    """@brief 原始轮次 / Original round."""

    outcome: ChanceOutcome
    """@brief 结算结果 / Settlement outcome."""

    proof: FairnessProof
    """@brief 公平性证明 / Fairness proof."""

    payout: FreeTokenPayout | None
    """@brief 获胜总派彩或失败空值 / Winning gross payout or loss null."""

    def __post_init__(self) -> None:
        """@brief 校验结算与证明严格一致 / Validate settlement and proof consistency.

        @return None / None.
        @raise ValueError 结果、胜负、派彩或证明不一致时抛出 /
            Raised when outcome, win/loss, payout, or proof is inconsistent.
        """

        if not isinstance(self.round, ChanceRound):
            raise TypeError("Chance settlement requires ChanceRound")
        if not isinstance(self.outcome, ChanceOutcome):
            raise TypeError("Chance settlement requires ChanceOutcome")
        if not isinstance(self.proof, FairnessProof):
            raise TypeError("Chance settlement requires FairnessProof")
        if self.proof.round_id != self.round.round_id:
            raise ValueError("Chance settlement proof belongs to another round")
        if self.proof.upper_bound != self.round.ruleset.total_weight:
            raise ValueError("Chance settlement proof uses another outcome-space bound")
        if not self.proof.verifies():
            raise ValueError("Chance settlement contains an invalid fairness proof")
        expected_outcome = self.round.ruleset.outcome_for_ticket(
            self.proof.sample.ticket
        )
        if self.outcome != expected_outcome:
            raise ValueError(
                "Chance settlement outcome does not match its fairness ticket"
            )
        won = self.outcome.code in self.round.rule.winning_outcome_codes
        if won:
            if self.payout != self.round.quote.gross_payout:
                raise ValueError(
                    "Chance win must use the centrally quoted gross payout"
                )
        elif self.payout is not None:
            raise ValueError("Chance loss cannot contain a payout")

    @property
    def won(self) -> bool:
        """@brief 返回该轮是否命中规则 / Return whether the selected rule won.

        @return 命中时为 True / True when the selected rule won.
        """

        return self.payout is not None

    @property
    def credited(self) -> int:
        """@brief 返回结算应贷记的免费金币数 / Return free tokens to credit at settlement.

        @return 获胜总派彩或零 / Winning gross payout or zero.
        """

        return 0 if self.payout is None else self.payout.value

    @property
    def net_change(self) -> int:
        """@brief 返回本轮实现的整数净变化 / Return realized integral net change for the round.

        @return 派彩减去押注 / Payout less stake.
        """

        return self.credited - self.round.stake.value


def _validate_round_core(
    *,
    round_id: UUID,
    scope: RoundScope,
    player_id: int,
    ruleset: ChanceRuleset,
    rule_code: str,
    stake: FreeTokenStake,
    commitment: ServerSeedCommitment,
    nonce: int,
) -> None:
    """@brief 校验承诺与完整轮次共享的不变量 / Validate invariants shared by committed and full rounds.

    @param round_id 轮次 UUID / Round UUID.
    @param scope 个人或群组范围 / Personal or group scope.
    @param player_id 下单玩家标识 / Wagering-player identity.
    @param ruleset 冻结规则集 / Frozen ruleset.
    @param rule_code 选中规则编码 / Selected rule code.
    @param stake 免费金币押注 / Free-token stake.
    @param commitment 服务器种子承诺 / Server-seed commitment.
    @param nonce 业务 nonce / Business nonce.
    @return None / None.
    @raise TypeError 轮次字段类型不匹配时抛出 / Raised when round field types do not match.
    @raise ValueError 轮次身份、范围或赔率非法时抛出 /
        Raised when round identity, scope, or odds are invalid.
    """

    if not isinstance(round_id, UUID):
        raise TypeError("Chance round requires a UUID round identifier")
    if not isinstance(scope, (PersonalRoundScope, GroupRoundScope)):
        raise TypeError("Chance round scope must be personal or group")
    if isinstance(player_id, bool) or not isinstance(player_id, int):
        raise TypeError("Chance round player must be an integer")
    if player_id <= 0:
        raise ValueError("Chance round player must be positive")
    if isinstance(scope, PersonalRoundScope) and scope.user_id != player_id:
        raise ValueError("Personal chance rounds may only be played by their owner")
    if not isinstance(ruleset, ChanceRuleset):
        raise TypeError("Chance round requires ChanceRuleset")
    if not isinstance(stake, FreeTokenStake):
        raise TypeError("Chance rounds accept only FreeTokenStake")
    if not isinstance(commitment, ServerSeedCommitment):
        raise TypeError("Chance round requires ServerSeedCommitment")
    if isinstance(nonce, bool) or not isinstance(nonce, int):
        raise TypeError("Chance round nonce must be an integer")
    if nonce < 0 or nonce > MAX_NONCE:
        raise ValueError("Chance round nonce falls outside the protocol range")
    ruleset.quote(rule_code, stake)
