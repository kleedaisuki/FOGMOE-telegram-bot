"""@brief 耐久随机活动事务端口 / Durable transaction port for chance activities."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from .models import PreparedChanceRound, PrivateCommittedChanceRound
from .workflow_models import (
    BindAndSettleChanceRound,
    ChanceWorkflowResult,
    CommitDurableChanceRound,
    LookupChanceRound,
)

type ChanceRoundPreparer = Callable[[PrivateCommittedChanceRound], PreparedChanceRound]
"""@brief 在存储事务内由私有承诺态生成准备态的回调 / Callback producing prepared state from private committed state inside storage transaction."""


class ChanceRoundOperations(Protocol):
    """@brief 随机活动状态、免费钱包与回执的原子端口 / Atomic port for chance state, free wallet, and receipts.

    实现必须使用短事务和稳定锁序。不得把 ``PrivateCommittedChanceRound.server_seed``
    返回给工作流、日志或传输层。
    Implementations must use short transactions and a stable lock order. They must never return
    ``PrivateCommittedChanceRound.server_seed`` to the workflow, logs, or transport layer.
    """

    async def commit_chance_round(
        self,
        command: CommitDurableChanceRound,
        private_round: PrivateCommittedChanceRound,
    ) -> ChanceWorkflowResult:
        """@brief 原子持久化承诺和创建回执 / Atomically persist commitment and create receipt.

        @param command 耐久承诺命令 / Durable commitment command.
        @param private_round 含私有服务器种子的承诺态 / Committed state containing private server seed.
        @return 耐久操作结果 / Durable operation result.
        @note 同一事务必须先检查同载荷幂等回执，随后保存受保护的私有种子、公开承诺、
            规则集指纹和回执。此阶段不得预留或移动任何钱包余额。/
            The same transaction must first check same-payload idempotency receipts, then save the
            protected private seed, public commitment, ruleset fingerprint, and receipt. It must
            not reserve or move any wallet balance at this stage.
        """

        ...

    async def bind_and_settle_chance_round(
        self,
        command: BindAndSettleChanceRound,
        prepare: ChanceRoundPreparer,
    ) -> ChanceWorkflowResult:
        """@brief 原子绑定玩家种子、预留免费金币并结算 / Atomically bind player seed, reserve free tokens, and settle.

        @param command 绑定和结算命令 / Bind-and-settle command.
        @param prepare 在事务内接收私有承诺态的准备回调 /
            Preparation callback receiving private committed state inside the transaction.
        @return 耐久操作结果 / Durable operation result.
        @note 一次事务必须执行以下完整序列：先检查幂等回执并锁定轮次，再同时验证持久化
            owner 与完整 personal/group scope；从受保护存储读取私有承诺态并调用 ``prepare``；
            仅从 free wallet 预留 ``FreeTokenStake``；以银行双重记账写入 ``ACTIVITY_STAKE``
            和（若获胜）``ACTIVITY_PAYOUT``；持久化 Prepared/settlement 证据、揭示证明和回执。
            任一步失败均必须回滚，且重复调用不能重复扣款或派彩。/
            One transaction must perform the complete sequence: check the idempotency receipt and
            lock the round; verify persistent owner and full personal/group scope together; load
            private committed state from protected storage and invoke ``prepare``; reserve only the
            ``FreeTokenStake`` from the free wallet; write bank double-entry ``ACTIVITY_STAKE`` and,
            on a win, ``ACTIVITY_PAYOUT``; persist Prepared/settlement evidence, reveal proof, and
            receipt. Any failure must roll back, and replay must never charge or pay twice.
        """

        ...

    async def lookup_chance_round(
        self,
        command: LookupChanceRound,
    ) -> ChanceWorkflowResult:
        """@brief 按 owner 和范围读取安全轮次视图 / Read a safe round view by owner and scope.

        @param command 查询命令 / Lookup command.
        @return 安全轮次视图或标准拒绝结果 / Safe round view or standard rejection result.
        @note 未结算轮次只能返回公开承诺，绝不可读取或暴露私有服务器种子。/
            An unsettled round may return only its public commitment; it must never read or expose
            the private server seed.
        """

        ...
