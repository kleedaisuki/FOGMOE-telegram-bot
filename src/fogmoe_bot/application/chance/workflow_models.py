"""@brief 耐久随机活动命令、视图与结果 / Durable chance-activity commands, views, and results."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from fogmoe_bot.domain.chance.fairness import ClientSeed
from fogmoe_bot.domain.chance.rounds import ChanceSettlement, CommittedChanceRound
from fogmoe_bot.domain.chance.scope import (
    GroupRoundScope,
    PersonalRoundScope,
    RoundScope,
)

from .models import CommitChanceRound


class ChanceRoundStatus(StrEnum):
    """@brief 随机活动轮次的耐久状态 / Durable status of a chance-activity round."""

    COMMITTED = "committed"
    """@brief 承诺已公开，仍等待玩家种子 / Commitment is public and awaits a player seed."""

    SETTLED = "settled"
    """@brief 已揭示种子、完成账本结算 / Seed is revealed and ledger settlement is complete."""


class ChanceWorkflowCode(StrEnum):
    """@brief 耐久随机活动工作流结果代码 / Durable chance-workflow result code."""

    SUCCESS = "success"
    """@brief 请求已原子应用或可靠重放 / Request was atomically applied or safely replayed."""

    NOT_FOUND = "not_found"
    """@brief 轮次不存在或对调用者不可见 / Round does not exist or is invisible to caller."""

    FORBIDDEN = "forbidden"
    """@brief 调用者不是允许的轮次拥有者 / Caller is not an allowed round owner."""

    SCOPE_MISMATCH = "scope_mismatch"
    """@brief 请求上下文不等于已保存个人或群组范围 / Request context differs from persisted personal or group scope."""

    ALREADY_SETTLED = "already_settled"
    """@brief 轮次已完成，不能再次扣款或揭示 / Round is complete and cannot be charged or revealed again."""

    INSUFFICIENT_FREE_TOKENS = "insufficient_free_tokens"
    """@brief 免费钱包余额不足 / Free-wallet balance is insufficient."""

    INSUFFICIENT_ACTIVITY_POT = "insufficient_activity_pot"
    """@brief 活动奖池储备不足，尚未发生扣款或揭示 / Activity-pot reserve is insufficient; no debit or reveal occurred."""

    CONFLICT = "conflict"
    """@brief 并发状态或幂等键载荷冲突 / Concurrent state or idempotency-payload conflict."""


@dataclass(frozen=True, slots=True)
class CommitDurableChanceRound:
    """@brief 建立并耐久化随机活动承诺的命令 / Command to create and durably persist a chance commitment.

    ``actor_id`` 与 ``round.player_id`` 分开建模，用于让工作流显式拒绝委托下注
    （delegated wagering）。
    ``actor_id`` and ``round.player_id`` are modeled separately so the workflow can explicitly
    reject delegated wagering.

    @param actor_id 发起请求的用户标识 / User identity initiating the request.
    @param round 纯数学承诺命令 / Pure mathematical commitment command.
    @param idempotency_key 来源事件的稳定幂等键 / Stable idempotency key of the source event.
    """

    actor_id: int
    """@brief 发起用户标识 / Initiating-user identity."""

    round: CommitChanceRound
    """@brief 纯数学承诺命令 / Pure mathematical commitment command."""

    idempotency_key: str
    """@brief 稳定幂等键 / Stable idempotency key."""

    def __post_init__(self) -> None:
        """@brief 校验耐久承诺命令形状 / Validate durable commitment-command shape.

        @return None / None.
        @raise TypeError 身份或数学命令类型不匹配时抛出 /
            Raised when identity or mathematical-command type does not match.
        @raise ValueError 身份或幂等键非法时抛出 / Raised when identity or idempotency key is invalid.
        """

        _validate_actor_id(self.actor_id)
        if not isinstance(self.round, CommitChanceRound):
            raise TypeError("Durable chance commitment requires CommitChanceRound")
        object.__setattr__(
            self, "idempotency_key", _normalize_idempotency_key(self.idempotency_key)
        )


@dataclass(frozen=True, slots=True)
class BindAndSettleChanceRound:
    """@brief 绑定玩家种子并原子结算的命令 / Command to bind a player seed and settle atomically.

    该命令不携带服务器种子；端口必须在事务内从私有承诺态读取它。
    This command does not carry a server seed; the port must load it from private committed state
    inside its transaction.

    @param round_id 已公开承诺的轮次 UUID / UUID of the publicly committed round.
    @param actor_id 请求绑定和结算的用户 / User requesting bind and settlement.
    @param scope 请求所在的明确个人或群组范围 / Explicit personal or group scope of the request.
    @param client_seed 玩家在揭示前提交的种子 / Player seed supplied before reveal.
    @param idempotency_key 来源事件的稳定幂等键 / Stable idempotency key of the source event.
    """

    round_id: UUID
    """@brief 已承诺轮次 UUID / Committed round UUID."""

    actor_id: int
    """@brief 发起用户标识 / Initiating-user identity."""

    scope: RoundScope
    """@brief 请求所在范围 / Request scope."""

    client_seed: ClientSeed
    """@brief 玩家客户端种子 / Player client seed."""

    idempotency_key: str
    """@brief 稳定幂等键 / Stable idempotency key."""

    def __post_init__(self) -> None:
        """@brief 校验绑定和结算命令 / Validate bind-and-settle command.

        @return None / None.
        @raise TypeError 轮次、范围或种子类型不匹配时抛出 /
            Raised when round, scope, or seed type does not match.
        @raise ValueError 身份或幂等键非法时抛出 / Raised when identity or idempotency key is invalid.
        """

        if not isinstance(self.round_id, UUID):
            raise TypeError("Chance bind-and-settle requires a UUID round identifier")
        _validate_actor_id(self.actor_id)
        _validate_scope(self.scope)
        if not isinstance(self.client_seed, ClientSeed):
            raise TypeError("Chance bind-and-settle requires ClientSeed")
        object.__setattr__(
            self, "idempotency_key", _normalize_idempotency_key(self.idempotency_key)
        )


@dataclass(frozen=True, slots=True)
class LookupChanceRound:
    """@brief 查询一个耐久随机活动轮次的命令 / Command to look up one durable chance round.

    @param round_id 待查询轮次 UUID / UUID of the round to look up.
    @param actor_id 请求查看的用户 / User requesting visibility.
    @param scope 请求所在的明确个人或群组范围 / Explicit personal or group scope of the request.
    """

    round_id: UUID
    """@brief 轮次 UUID / Round UUID."""

    actor_id: int
    """@brief 请求用户标识 / Requesting-user identity."""

    scope: RoundScope
    """@brief 请求所在范围 / Request scope."""

    def __post_init__(self) -> None:
        """@brief 校验查询命令 / Validate lookup command.

        @return None / None.
        @raise TypeError 轮次或范围类型不匹配时抛出 / Raised when round or scope type does not match.
        @raise ValueError 身份非法时抛出 / Raised when identity is invalid.
        """

        if not isinstance(self.round_id, UUID):
            raise TypeError("Chance lookup requires a UUID round identifier")
        _validate_actor_id(self.actor_id)
        _validate_scope(self.scope)


@dataclass(frozen=True, slots=True)
class ChanceRoundView:
    """@brief 可安全返回给调用方的耐久轮次视图 / Durable round view safe to return to callers.

    结算前不包含私有服务器种子；结算后只通过 ``settlement.proof`` 显示协议要求
    揭示的种子。
    Before settlement this view never contains the private server seed. After settlement it
    exposes only the seed deliberately revealed through ``settlement.proof``.

    @param committed_round 已公开承诺的轮次快照 / Publicly committed round snapshot.
    @param status 耐久状态 / Durable status.
    @param settlement 已结算时的公开结算；未结算为 None / Public settlement when settled; None otherwise.
    """

    committed_round: CommittedChanceRound
    """@brief 已公开承诺的轮次 / Publicly committed round."""

    status: ChanceRoundStatus
    """@brief 耐久轮次状态 / Durable round status."""

    settlement: ChanceSettlement | None = None
    """@brief 可公开结算或空值 / Public settlement or null."""

    def __post_init__(self) -> None:
        """@brief 校验可见状态与结算的一致性 / Validate consistency of visible status and settlement.

        @return None / None.
        @raise TypeError 承诺轮次、状态或结算类型不匹配时抛出 /
            Raised when committed-round, status, or settlement type does not match.
        @raise ValueError 状态与结算不一致时抛出 / Raised when status and settlement are inconsistent.
        """

        if not isinstance(self.committed_round, CommittedChanceRound):
            raise TypeError("Chance round view requires CommittedChanceRound")
        if not isinstance(self.status, ChanceRoundStatus):
            raise TypeError("Chance round view requires ChanceRoundStatus")
        if self.settlement is not None and not isinstance(
            self.settlement, ChanceSettlement
        ):
            raise TypeError("Chance round view settlement must be ChanceSettlement")
        if self.status is ChanceRoundStatus.COMMITTED and self.settlement is not None:
            raise ValueError("Committed chance view cannot expose a settlement")
        if self.status is ChanceRoundStatus.SETTLED and self.settlement is None:
            raise ValueError("Settled chance view requires a settlement")
        if self.settlement is not None:
            round_snapshot = self.settlement.round
            if round_snapshot.round_id != self.committed_round.round_id:
                raise ValueError("Chance view settlement belongs to another round")
            if round_snapshot.player_id != self.committed_round.player_id:
                raise ValueError("Chance view settlement belongs to another owner")
            if round_snapshot.scope != self.committed_round.scope:
                raise ValueError("Chance view settlement belongs to another scope")
            if round_snapshot.commitment != self.committed_round.commitment:
                raise ValueError("Chance view settlement has another commitment")
            if round_snapshot.ruleset != self.committed_round.ruleset:
                raise ValueError("Chance view settlement has another ruleset")
            if round_snapshot.rule_code != self.committed_round.rule_code:
                raise ValueError("Chance view settlement has another selected rule")
            if round_snapshot.stake != self.committed_round.stake:
                raise ValueError("Chance view settlement has another free-token stake")
            if round_snapshot.nonce != self.committed_round.nonce:
                raise ValueError("Chance view settlement has another nonce")

    @property
    def round_id(self) -> UUID:
        """@brief 返回耐久轮次 UUID / Return durable round UUID.

        @return 轮次 UUID / Round UUID.
        """

        return self.committed_round.round_id

    @property
    def owner_id(self) -> int:
        """@brief 返回拥有免费押注的玩家 / Return player owning the free-token stake.

        @return 玩家标识 / Player identity.
        """

        return self.committed_round.player_id

    @property
    def scope(self) -> RoundScope:
        """@brief 返回明确的个人或群组范围 / Return explicit personal or group scope.

        @return 轮次范围 / Round scope.
        """

        return self.committed_round.scope


@dataclass(frozen=True, slots=True)
class ChanceWorkflowResult:
    """@brief 耐久随机活动操作结果 / Result of a durable chance-activity operation.

    @param code 稳定工作流结果代码 / Stable workflow result code.
    @param view 可选的安全轮次视图 / Optional safe round view.
    @param replayed 是否由同载荷幂等回执重放 / Whether replayed from a same-payload idempotency receipt.
    """

    code: ChanceWorkflowCode
    """@brief 工作流结果代码 / Workflow result code."""

    view: ChanceRoundView | None = None
    """@brief 可选安全轮次视图 / Optional safe round view."""

    replayed: bool = False
    """@brief 幂等重放标志 / Idempotency-replay flag."""

    def __post_init__(self) -> None:
        """@brief 校验结果语义 / Validate result semantics.

        @return None / None.
        @raise TypeError 代码、视图或重放标志类型不匹配时抛出 /
            Raised when code, view, or replay flag type does not match.
        @raise ValueError 成功或重放结果缺少视图时抛出 /
            Raised when a success or replay result lacks a view.
        """

        if not isinstance(self.code, ChanceWorkflowCode):
            raise TypeError("Chance workflow result requires ChanceWorkflowCode")
        if self.view is not None and not isinstance(self.view, ChanceRoundView):
            raise TypeError("Chance workflow result view must be ChanceRoundView")
        if not isinstance(self.replayed, bool):
            raise TypeError("Chance workflow replay flag must be bool")
        if self.code is ChanceWorkflowCode.SUCCESS and self.view is None:
            raise ValueError("Successful chance workflow result requires a view")
        if self.replayed and self.code is not ChanceWorkflowCode.SUCCESS:
            raise ValueError("Replayed chance workflow result must be successful")


def _validate_actor_id(actor_id: int) -> None:
    """@brief 校验工作流调用者身份 / Validate workflow actor identity.

    @param actor_id 待校验调用者标识 / Actor identity to validate.
    @return None / None.
    @raise ValueError 调用者不是正整数时抛出 / Raised when the actor is not a positive integer.
    """

    if isinstance(actor_id, bool) or not isinstance(actor_id, int) or actor_id <= 0:
        raise ValueError("Chance workflow actor must be a positive integer")


def _validate_scope(scope: RoundScope) -> None:
    """@brief 校验显式轮次范围 / Validate explicit round scope.

    @param scope 个人或群组范围 / Personal or group scope.
    @return None / None.
    @raise TypeError 范围不是受支持的显式类型时抛出 /
        Raised when scope is not a supported explicit type.
    """

    if not isinstance(scope, (PersonalRoundScope, GroupRoundScope)):
        raise TypeError("Chance workflow scope must be personal or group")


def _normalize_idempotency_key(value: str) -> str:
    """@brief 规范化来源事件幂等键 / Normalize a source-event idempotency key.

    @param value 原始幂等键 / Raw idempotency key.
    @return 去除首尾空白后的稳定键 / Stable key with surrounding whitespace removed.
    @raise ValueError 键为空或过长时抛出 / Raised when the key is empty or oversized.
    """

    if not isinstance(value, str):
        raise ValueError("Chance idempotency key must be text")
    normalized = value.strip()
    if not 1 <= len(normalized) <= 200:
        raise ValueError("Chance idempotency key must contain 1-200 characters")
    return normalized
