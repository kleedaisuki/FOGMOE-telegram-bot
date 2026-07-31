"""@brief 随机活动用例结果 / Chance-activity use-case results."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from fogmoe_bot.domain.chance.rounds import ChanceRoundView


class ChanceWorkflowCode(StrEnum):
    """@brief 耐久随机活动工作流结果代码 / Durable chance-workflow result code."""

    SUCCESS = "success"
    """@brief 请求已原子应用或可靠重放 / Request was atomically applied or safely replayed."""

    NOT_FOUND = "not_found"
    """@brief 轮次不存在或对调用者不可见 / Round does not exist or is invisible to caller."""

    FORBIDDEN = "forbidden"
    """@brief 调用者不是允许的轮次拥有者 / Caller is not an allowed round owner."""

    SCOPE_MISMATCH = "scope_mismatch"
    """@brief 请求上下文不等于已保存范围 / Request context differs from persisted scope."""

    ALREADY_SETTLED = "already_settled"
    """@brief 轮次已完成，不能再次扣款或揭示 / Round is complete and cannot be charged or revealed again."""

    INSUFFICIENT_FREE_TOKENS = "insufficient_free_tokens"
    """@brief 免费钱包余额不足 / Free-wallet balance is insufficient."""

    INSUFFICIENT_ACTIVITY_POT = "insufficient_activity_pot"
    """@brief 活动奖池储备不足且未发生扣款 / Activity-pot reserve is insufficient and no debit occurred."""

    CONFLICT = "conflict"
    """@brief 并发状态或幂等键载荷冲突 / Concurrent state or idempotency-payload conflict."""


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
