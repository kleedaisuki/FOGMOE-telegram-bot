"""@brief 无副作用的内容审核规则引擎 / Side-effect-free content moderation engine."""

from __future__ import annotations

import re
from collections.abc import Iterable

from .models import (
    ActorRole,
    GroupModerationPolicy,
    ModerationDecision,
    ModerationRequest,
    ModerationRule,
    RuleKind,
    RuleMatch,
    RuleMergeMode,
    RuleScope,
    Verdict,
)
from .normalization import normalize_for_matching

URL_PATTERN = re.compile(
    r"https?://\S+|www\.\S+|t\.me/\S+|\S+\.\S*|"
    r"\S+\.(com|org|net|io|co|ru|cn|me|app|xyz|gov|edu)\b",
    re.IGNORECASE,
)
"""@brief 兼容旧实现的链接模式 / Legacy-compatible URL pattern."""

MENTION_PATTERN = re.compile(r"@[a-zA-Z0-9_]+")
"""@brief Telegram 用户名提及模式 / Telegram username mention pattern."""


class ModerationEngine:
    """@brief 按显式策略求值内容规则 / Evaluate content rules under an explicit policy."""

    def evaluate(
        self,
        request: ModerationRequest,
        policy: GroupModerationPolicy,
        *,
        global_rules: Iterable[ModerationRule] = (),
        group_rules: Iterable[ModerationRule] = (),
    ) -> ModerationDecision:
        """@brief 对单条内容执行审核 / Moderate one content item.

        @param request 审核请求 / Moderation request.
        @param policy 群组策略快照 / Group policy snapshot.
        @param global_rules 全局规则 / Global rules.
        @param group_rules 群组自定义规则 / Group-specific rules.
        @return 无副作用审核判决 / Side-effect-free moderation decision.
        """

        if not policy.enabled or not request.content:
            return self._allow(policy)
        if policy.exempt_administrators and request.actor_role in {
            ActorRole.ADMINISTRATOR,
            ActorRole.OWNER,
        }:
            return self._allow(policy)

        if policy.block_links:
            regex_match = URL_PATTERN.search(request.content)
            if regex_match:
                return self._block(
                    policy,
                    self._synthetic_match(RuleKind.LINK, regex_match),
                )

        if policy.block_mentions:
            regex_match = MENTION_PATTERN.search(request.content)
            if regex_match:
                return self._block(
                    policy,
                    self._synthetic_match(RuleKind.MENTION, regex_match),
                )

        for rule in self._select_rules(policy, global_rules, group_rules):
            rule_match = self._match_rule(request.content, rule)
            if rule_match:
                return self._block(policy, rule_match)

        return self._allow(policy)

    def _select_rules(
        self,
        policy: GroupModerationPolicy,
        global_rules: Iterable[ModerationRule],
        group_rules: Iterable[ModerationRule],
    ) -> tuple[ModerationRule, ...]:
        """@brief 按策略选择规则集合 / Select rules according to merge policy.

        @param policy 群组策略 / Group policy.
        @param global_rules 全局规则 / Global rules.
        @param group_rules 群组规则 / Group rules.
        @return 有序规则元组 / Ordered rule tuple.
        """

        globals_tuple = tuple(rule for rule in global_rules if rule.enabled)
        groups_tuple = tuple(rule for rule in group_rules if rule.enabled)
        if policy.rule_merge_mode is RuleMergeMode.GLOBAL_ONLY:
            return globals_tuple
        if policy.rule_merge_mode is RuleMergeMode.EXTEND_GLOBAL:
            return groups_tuple + globals_tuple
        return groups_tuple if groups_tuple else globals_tuple

    def _match_rule(self, content: str, rule: ModerationRule) -> RuleMatch | None:
        """@brief 匹配单条规则 / Match a single rule.

        @param content 原始文本 / Original text.
        @param rule 审核规则 / Moderation rule.
        @return 命中证据；未命中返回 None / Match evidence, or None.
        """

        if not rule.pattern:
            return None
        if rule.kind is RuleKind.LITERAL:
            normalized_content = normalize_for_matching(content)
            normalized_pattern = normalize_for_matching(rule.pattern)
            start = normalized_content.find(normalized_pattern)
            if start < 0:
                return None
            return RuleMatch(
                rule=rule,
                matched_text=content[start : start + len(rule.pattern)],
                start=start,
                end=start + len(rule.pattern),
            )
        if rule.kind is RuleKind.REGEX:
            try:
                match = re.search(rule.pattern, content, re.IGNORECASE)
            except re.error:
                return None
            if not match:
                return None
            matched_text = match.group(0) or rule.pattern
            return RuleMatch(
                rule=rule,
                matched_text=matched_text,
                start=match.start(),
                end=match.end(),
            )
        return None

    def _synthetic_match(self, kind: RuleKind, match: re.Match[str]) -> RuleMatch:
        """@brief 构造配置型规则的命中证据 / Build evidence for a policy-level matcher.

        @param kind 链接或提及规则类型 / Link or mention rule kind.
        @param match 正则命中 / Regular-expression match.
        @return 命中证据 / Match evidence.
        """

        return RuleMatch(
            rule=ModerationRule(
                pattern=match.re.pattern,
                kind=kind,
                scope=RuleScope.GROUP,
            ),
            matched_text=match.group(0),
            start=match.start(),
            end=match.end(),
        )

    def _allow(self, policy: GroupModerationPolicy) -> ModerationDecision:
        """@brief 构造允许判决 / Build an allow decision.

        @param policy 当前策略 / Current policy.
        @return 允许判决 / Allow decision.
        """

        return ModerationDecision(
            verdict=Verdict.ALLOW,
            policy_version=policy.version,
        )

    def _block(
        self,
        policy: GroupModerationPolicy,
        match: RuleMatch,
    ) -> ModerationDecision:
        """@brief 构造阻断判决 / Build a block decision.

        @param policy 当前策略 / Current policy.
        @param match 命中证据 / Match evidence.
        @return 阻断判决 / Block decision.
        """

        return ModerationDecision(
            verdict=Verdict.BLOCK,
            matches=(match,),
            stop_downstream=True,
            policy_version=policy.version,
        )
