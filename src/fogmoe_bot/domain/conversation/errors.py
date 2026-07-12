"""@brief 会话工作流持久化错误 / Conversation-workflow persistence errors."""

from __future__ import annotations


class WorkflowPersistenceError(RuntimeError):
    """@brief 会话工作流持久化错误基类 / Base conversation-workflow persistence error."""


class TurnNotFoundError(WorkflowPersistenceError):
    """@brief 指定回合不存在 / Requested turn does not exist."""


class ConcurrentTurnUpdateError(WorkflowPersistenceError):
    """@brief 乐观并发版本冲突 / Optimistic-concurrency version conflict."""


class StaleClaimError(WorkflowPersistenceError):
    """@brief worker 使用了已失效的领取凭证 / Worker used an expired or superseded claim."""


class IdempotencyConflictError(WorkflowPersistenceError):
    """@brief 同一幂等键被用于不同语义载荷 / An idempotency key was reused for different semantics."""
