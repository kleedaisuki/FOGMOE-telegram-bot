"""@brief 内容审核领域模型与规则引擎 / Content-moderation domain models and engine."""

from .engine import ModerationEngine
from .models import (
    ActorRole,
    ChatId,
    ContentKind,
    EnforcementFailureMode,
    EnforcementResult,
    GroupModerationPolicy,
    MessageId,
    ModerationDecision,
    ModerationRequest,
    ModerationRule,
    RuleId,
    RuleKind,
    RuleMatch,
    RuleMergeMode,
    RuleScope,
    UserId,
    Verdict,
)
from .verification import VerificationStatus, VerificationTask, hash_verification_token
from .reporting import (
    InMemoryReportDeduplicator,
    ReportDeliveryResult,
    ReportKey,
    ReportRecord,
    ReportRegistration,
)

__all__ = [
    "ActorRole",
    "ChatId",
    "ContentKind",
    "EnforcementFailureMode",
    "EnforcementResult",
    "GroupModerationPolicy",
    "MessageId",
    "ModerationDecision",
    "ModerationEngine",
    "ModerationRequest",
    "ModerationRule",
    "RuleId",
    "RuleKind",
    "RuleMatch",
    "RuleMergeMode",
    "RuleScope",
    "InMemoryReportDeduplicator",
    "ReportDeliveryResult",
    "ReportKey",
    "ReportRecord",
    "ReportRegistration",
    "UserId",
    "Verdict",
    "VerificationStatus",
    "VerificationTask",
    "hash_verification_token",
]
