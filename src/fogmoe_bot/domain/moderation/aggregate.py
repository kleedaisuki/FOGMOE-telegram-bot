"""@brief 群组治理聚合 / Group-moderation aggregate.

策略、垃圾规则与关键词回复共享一个乐观并发版本（optimistic concurrency
control, OCC）。群组的三份持久化投影必须由同一事务保存，避免配置只更新一半。
/ Policy, spam rules, and keyword replies share one optimistic-concurrency-control
(OCC) version. Their three persistence projections must be saved in one transaction so
a group configuration cannot be partially updated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Self

from fogmoe_bot.domain.moderation.models import KeywordReply

from .models import (
    ChatId,
    GroupModerationPolicy,
    ModerationRule,
    RuleKind,
    RuleScope,
    UserId,
)

MAX_GROUP_RULES = 10
"""@brief 每群最大垃圾规则数 / Maximum spam rules per group."""

MAX_KEYWORD_REPLIES = 10
"""@brief 每群最大自动回复数 / Maximum keyword replies per group."""

MAX_PATTERN_LENGTH = 255
"""@brief 规则或触发词最大长度 / Maximum rule or trigger length."""

MAX_RESPONSE_LENGTH = 1000
"""@brief 关键词回复最大长度 / Maximum keyword-response length."""


class StaleModerationVersion(RuntimeError):
    """@brief 群组治理聚合版本已过期 / Group-moderation aggregate version is stale."""


class ModerationLimitExceeded(ValueError):
    """@brief 群组治理配置达到明确上限 / Group-moderation configuration reached its explicit limit."""


@dataclass(frozen=True, slots=True)
class GroupModeration:
    """@brief 群组治理的一致性边界 / Consistency boundary for group moderation.

    @param policy 内容审核策略 / Content-moderation policy.
    @param spam_rules 群组垃圾规则 / Group-specific spam rules.
    @param keyword_replies 群组关键词回复 / Group keyword replies.
    @param version 聚合 OCC 版本 / Aggregate OCC version.
    """

    policy: GroupModerationPolicy
    spam_rules: tuple[ModerationRule, ...] = ()
    keyword_replies: tuple[KeywordReply, ...] = ()
    version: int = 0

    def __post_init__(self) -> None:
        """@brief 验证聚合不变量 / Validate aggregate invariants.

        @return None / None.
        @raises ValueError 版本或作用域不合法 / For an invalid version or scope.
        @note 历史数据库可能因旧并发竞态包含超过十条或 Unicode 大小写等价的规则；
        聚合必须可加载这些数据，十条上限只约束新增命令。/ Historical databases may
        contain more than ten entries or Unicode-case-equivalent keys due to legacy races;
        the aggregate must remain loadable, and the ten-entry limit constrains only new commands.
        """

        if self.version < 0:
            raise ValueError("Moderation version cannot be negative")
        if self.policy.version != self.version:
            raise ValueError("Policy and aggregate versions must match")
        if any(rule.scope is not RuleScope.GROUP for rule in self.spam_rules):
            raise ValueError("Aggregate spam rules must have group scope")

    @classmethod
    def empty(cls, chat_id: ChatId) -> Self:
        """@brief 创建未配置群组 / Create an unconfigured group.

        @param chat_id 群组 ID / Group identifier.
        @return 禁用且版本为零的聚合 / Disabled version-zero aggregate.
        """

        return cls(policy=GroupModerationPolicy(chat_id=chat_id))

    @property
    def chat_id(self) -> ChatId:
        """@brief 返回聚合键 / Return the aggregate key.

        @return 群组 ID / Group identifier.
        """

        return self.policy.chat_id

    def toggle(self, actor_id: UserId) -> Self:
        """@brief 切换垃圾过滤总开关 / Toggle the spam-filter master switch.

        @param actor_id 执行管理员 ID / Acting administrator identifier.
        @return 新版本聚合 / New-version aggregate.
        @note actor_id 由审计投影保存；领域状态只验证其有效性 / actor_id is stored by the audit projection; domain state only validates it.
        """

        _validate_actor(actor_id)
        return self._advance(
            policy=replace(self.policy, enabled=not self.policy.enabled)
        )

    def set_link_blocking(self, enabled: bool, actor_id: UserId) -> Self:
        """@brief 设置链接拦截 / Set link blocking.

        @param enabled 是否启用 / Whether enabled.
        @param actor_id 执行管理员 ID / Acting administrator identifier.
        @return 新版本聚合 / New-version aggregate.
        """

        self._require_enabled()
        _validate_actor(actor_id)
        return self._advance(policy=replace(self.policy, block_links=enabled))

    def set_mention_blocking(self, enabled: bool, actor_id: UserId) -> Self:
        """@brief 设置提及拦截 / Set mention blocking.

        @param enabled 是否启用 / Whether enabled.
        @param actor_id 执行管理员 ID / Acting administrator identifier.
        @return 新版本聚合 / New-version aggregate.
        """

        self._require_enabled()
        _validate_actor(actor_id)
        return self._advance(policy=replace(self.policy, block_mentions=enabled))

    def put_spam_rule(
        self,
        pattern: str,
        *,
        regex: bool,
        actor_id: UserId,
    ) -> Self:
        """@brief 新增或更新群组垃圾规则 / Add or update a group spam rule.

        @param pattern 字面词或正则表达式 / Literal term or regular expression.
        @param regex 是否按正则解释 / Whether to interpret as a regular expression.
        @param actor_id 执行管理员 ID / Acting administrator identifier.
        @return 新版本聚合 / New-version aggregate.
        @raises ModerationLimitExceeded 新规则超过十条 / If a new rule exceeds the ten-rule limit.
        @raises ValueError 规则为空、过长或正则无效 / If blank, too long, or invalid regex.
        """

        _validate_actor(actor_id)
        normalized = _validate_pattern(pattern)
        if regex:
            try:
                re.compile(normalized)
            except re.error as error:
                raise ValueError(f"Invalid regular expression: {error}") from error
        replacement = ModerationRule(
            pattern=normalized,
            kind=RuleKind.REGEX if regex else RuleKind.LITERAL,
            scope=RuleScope.GROUP,
        )
        rules = list(self.spam_rules)
        position = _casefold_position(tuple(rule.pattern for rule in rules), normalized)
        if position is None:
            if len(rules) >= MAX_GROUP_RULES:
                raise ModerationLimitExceeded(
                    "A group can contain at most 10 spam rules"
                )
            rules.append(replacement)
        else:
            rules[position] = replacement
        return self._advance(spam_rules=tuple(rules))

    def remove_spam_rule(self, pattern: str, actor_id: UserId) -> tuple[Self, bool]:
        """@brief 删除群组垃圾规则 / Remove a group spam rule.

        @param pattern 待删除规则 / Rule to remove.
        @param actor_id 执行管理员 ID / Acting administrator identifier.
        @return ``(新聚合, 是否删除)`` / ``(new aggregate, whether removed)``.
        """

        _validate_actor(actor_id)
        normalized = _validate_pattern(pattern)
        kept = tuple(
            rule
            for rule in self.spam_rules
            if rule.pattern.casefold() != normalized.casefold()
        )
        if len(kept) == len(self.spam_rules):
            return self, False
        return self._advance(spam_rules=kept), True

    def put_keyword_reply(
        self,
        keyword: str,
        response: str,
        actor_id: UserId,
    ) -> Self:
        """@brief 新增或更新关键词回复 / Add or update a keyword reply.

        @param keyword 触发关键词 / Trigger keyword.
        @param response 回复内容 / Response content.
        @param actor_id 执行管理员 ID / Acting administrator identifier.
        @return 新版本聚合 / New-version aggregate.
        @raises ModerationLimitExceeded 新触发器超过十条 / If a new trigger exceeds the ten-reply limit.
        @raises ValueError 关键词或回复无效 / If the keyword or response is invalid.
        """

        _validate_actor(actor_id)
        normalized_keyword = _validate_pattern(keyword)
        normalized_response = response.strip()
        if not normalized_response:
            raise ValueError("Keyword response cannot be blank")
        if len(normalized_response) > MAX_RESPONSE_LENGTH:
            raise ValueError("Keyword response cannot exceed 1000 characters")
        replacement = KeywordReply(normalized_keyword, normalized_response)
        replies = list(self.keyword_replies)
        position = _casefold_position(
            tuple(item.keyword for item in replies), normalized_keyword
        )
        if position is None:
            if len(replies) >= MAX_KEYWORD_REPLIES:
                raise ModerationLimitExceeded(
                    "A group can contain at most 10 keyword replies"
                )
            replies.append(replacement)
        else:
            replies[position] = replacement
        return self._advance(keyword_replies=tuple(replies))

    def remove_keyword_reply(
        self,
        keyword: str,
        actor_id: UserId,
    ) -> tuple[Self, bool]:
        """@brief 删除关键词回复 / Remove a keyword reply.

        @param keyword 触发关键词 / Trigger keyword.
        @param actor_id 执行管理员 ID / Acting administrator identifier.
        @return ``(新聚合, 是否删除)`` / ``(new aggregate, whether removed)``.
        """

        _validate_actor(actor_id)
        normalized = _validate_pattern(keyword)
        kept = tuple(
            item
            for item in self.keyword_replies
            if item.keyword.casefold() != normalized.casefold()
        )
        if len(kept) == len(self.keyword_replies):
            return self, False
        return self._advance(keyword_replies=kept), True

    def _require_enabled(self) -> None:
        """@brief 要求总开关已启用 / Require the master switch to be enabled.

        @return None / None.
        @raises ValueError 总开关未启用 / If the master switch is disabled.
        """

        if not self.policy.enabled:
            raise ValueError("Spam control must be enabled first")

    def _advance(
        self,
        *,
        policy: GroupModerationPolicy | None = None,
        spam_rules: tuple[ModerationRule, ...] | None = None,
        keyword_replies: tuple[KeywordReply, ...] | None = None,
    ) -> Self:
        """@brief 构造下一版本 / Build the next version.

        @param policy 可选替换策略 / Optional replacement policy.
        @param spam_rules 可选替换垃圾规则 / Optional replacement spam rules.
        @param keyword_replies 可选替换关键词回复 / Optional replacement keyword replies.
        @return 版本加一的新聚合 / New aggregate with incremented version.
        """

        next_version = self.version + 1
        next_policy = replace(
            policy or self.policy,
            version=next_version,
        )
        return replace(
            self,
            policy=next_policy,
            spam_rules=self.spam_rules if spam_rules is None else spam_rules,
            keyword_replies=(
                self.keyword_replies if keyword_replies is None else keyword_replies
            ),
            version=next_version,
        )


def _validate_actor(actor_id: UserId) -> None:
    """@brief 验证管理员 ID / Validate an administrator identifier.

    @param actor_id 管理员 ID / Administrator identifier.
    @return None / None.
    @raises ValueError ID 非正数 / If the identifier is not positive.
    """

    if int(actor_id) <= 0:
        raise ValueError("Actor ID must be positive")


def _validate_pattern(pattern: str) -> str:
    """@brief 规范并验证触发模式 / Normalize and validate a trigger pattern.

    @param pattern 原始模式 / Raw pattern.
    @return 去除两端空白的模式 / Trimmed pattern.
    @raises ValueError 模式为空或过长 / If blank or too long.
    """

    normalized = pattern.strip()
    if not normalized:
        raise ValueError("Pattern cannot be blank")
    if len(normalized) > MAX_PATTERN_LENGTH:
        raise ValueError("Pattern cannot exceed 255 characters")
    return normalized


def _casefold_position(values: tuple[str, ...], target: str) -> int | None:
    """@brief 查找 Unicode 大小写无关位置 / Find a Unicode case-insensitive position.

    @param values 候选文本 / Candidate values.
    @param target 目标文本 / Target text.
    @return 首个位置；不存在为 None / First position, or None.
    """

    folded = target.casefold()
    return next(
        (index for index, value in enumerate(values) if value.casefold() == folded),
        None,
    )


__all__ = [
    "GroupModeration",
    "MAX_GROUP_RULES",
    "MAX_KEYWORD_REPLIES",
    "MAX_PATTERN_LENGTH",
    "MAX_RESPONSE_LENGTH",
    "ModerationLimitExceeded",
    "StaleModerationVersion",
]
