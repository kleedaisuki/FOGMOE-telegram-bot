"""@brief 内容审核领域值对象 / Content-moderation domain value objects."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, StrEnum, auto
from typing import NewType


ChatId = NewType("ChatId", int)
"""@brief Telegram 群组 ID / Telegram chat ID."""

UserId = NewType("UserId", int)
"""@brief Telegram 用户 ID / Telegram user ID."""

MessageId = NewType("MessageId", int)
"""@brief Telegram 消息 ID / Telegram message ID."""

RuleId = NewType("RuleId", int)
"""@brief 持久化审核规则 ID / Persisted moderation-rule ID."""


class ModerationCommandReceiptConflict(RuntimeError):
    """@brief 幂等键被复用于不同治理命令 / An idempotency key was reused for a different moderation command."""


@dataclass(frozen=True, slots=True)
class ModerationToggleResult:
    """@brief 可重放的治理开关结果 / Replayable moderation-toggle result.

    @param enabled 首次命令提交后的开关值 / Switch value committed by the first command.
    @param replayed 是否来自既有回执 / Whether the result came from an existing receipt.
    """

    enabled: bool
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class KeywordReply:
    """@brief 一条关键词自动回复 / One keyword auto-reply.

    @param keyword 触发关键词 / Trigger keyword.
    @param response 回复内容 / Reply content.
    """

    keyword: str
    response: str


class ContentKind(Enum):
    """@brief 待审核内容种类 / Kind of content being moderated."""

    TEXT = auto()
    CAPTION = auto()
    COMMAND = auto()


class ActorRole(Enum):
    """@brief 消息发送者角色 / Role of the message author."""

    MEMBER = auto()
    ADMINISTRATOR = auto()
    OWNER = auto()
    BOT = auto()


class RuleKind(Enum):
    """@brief 审核规则匹配方式 / Moderation-rule matching strategy."""

    LITERAL = auto()
    REGEX = auto()
    LINK = auto()
    MENTION = auto()


class RuleScope(Enum):
    """@brief 审核规则作用域 / Moderation-rule scope."""

    GLOBAL = auto()
    GROUP = auto()


class RuleMergeMode(StrEnum):
    """@brief 群规则与全局规则的合并策略 / Group/global rule merge strategy."""

    GLOBAL_ONLY = "global_only"
    EXTEND_GLOBAL = "extend_global"
    OVERRIDE_GLOBAL = "override_global"


class EnforcementFailureMode(StrEnum):
    """@brief 处置失败时的传播策略 / Propagation policy after enforcement failure."""

    FAIL_OPEN = "fail_open"
    FAIL_CLOSED = "fail_closed"


class Verdict(Enum):
    """@brief 内容审核结论 / Content-moderation verdict."""

    ALLOW = auto()
    BLOCK = auto()


@dataclass(frozen=True, slots=True)
class GroupModerationPolicy:
    """@brief 群组审核策略快照 / Immutable group-moderation policy snapshot.

    @param chat_id Telegram 群组 ID / Telegram chat ID.
    @param enabled 是否启用内容审核 / Whether moderation is enabled.
    @param block_links 是否拦截链接 / Whether links are blocked.
    @param block_mentions 是否拦截提及 / Whether mentions are blocked.
    @param exempt_administrators 是否豁免管理员 / Whether administrators are exempt.
    @param rule_merge_mode 群规则与全局规则合并方式 / Group/global rule merge mode.
    @param failure_mode Telegram 处置失败后的传播策略 / Propagation policy after enforcement failure.
    @param version 策略版本 / Policy version.
    """

    chat_id: ChatId
    enabled: bool = False
    block_links: bool = False
    block_mentions: bool = False
    exempt_administrators: bool = True
    rule_merge_mode: RuleMergeMode = RuleMergeMode.OVERRIDE_GLOBAL
    failure_mode: EnforcementFailureMode = EnforcementFailureMode.FAIL_CLOSED
    version: int = 0


@dataclass(frozen=True, slots=True)
class ModerationRule:
    """@brief 一条可执行审核规则 / One executable moderation rule.

    @param pattern 字面词或正则表达式 / Literal term or regular expression.
    @param kind 规则匹配方式 / Matching strategy.
    @param scope 规则作用域 / Rule scope.
    @param id 可选持久化 ID / Optional persisted ID.
    @param enabled 是否启用 / Whether the rule is enabled.
    """

    pattern: str
    kind: RuleKind
    scope: RuleScope
    id: RuleId | None = None
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class ModerationRequest:
    """@brief 一次内容审核请求 / One content-moderation request.

    @param chat_id Telegram 群组 ID / Telegram chat ID.
    @param user_id Telegram 用户 ID / Telegram user ID.
    @param message_id Telegram 消息 ID / Telegram message ID.
    @param content 待审核文本 / Text to moderate.
    @param content_kind 内容种类 / Content kind.
    @param actor_role 发送者角色 / Author role.
    @param is_edited 是否为编辑消息 / Whether this is an edited message.
    """

    chat_id: ChatId
    user_id: UserId
    message_id: MessageId
    content: str
    content_kind: ContentKind
    actor_role: ActorRole
    is_edited: bool = False


@dataclass(frozen=True, slots=True)
class RuleMatch:
    """@brief 规则命中证据 / Evidence of a rule match.

    @param rule 命中的规则 / Matched rule.
    @param matched_text 实际命中文本 / Actual matched text.
    @param start 原文本起始位置 / Start offset in the original text.
    @param end 原文本结束位置 / End offset in the original text.
    """

    rule: ModerationRule
    matched_text: str
    start: int | None = None
    end: int | None = None


@dataclass(frozen=True, slots=True)
class ModerationDecision:
    """@brief 与 Telegram 副作用无关的审核判决 / Moderation decision independent of Telegram effects.

    @param verdict 允许或阻断 / Allow or block verdict.
    @param matches 命中证据 / Matching evidence.
    @param stop_downstream 是否阻止后续业务消费消息 / Whether downstream consumers must be stopped.
    @param policy_version 作出判决的策略版本 / Policy version used for the decision.
    """

    verdict: Verdict
    matches: tuple[RuleMatch, ...] = ()
    stop_downstream: bool = False
    policy_version: int = 0

    @property
    def primary_match(self) -> RuleMatch | None:
        """@brief 返回首个命中证据 / Return the primary match.

        @return 首个命中；无命中返回 None / First match, or None when allowed.
        """

        return self.matches[0] if self.matches else None
