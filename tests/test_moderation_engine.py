from fogmoe_bot.domain.moderation import (
    ActorRole,
    ChatId,
    ContentKind,
    GroupModerationPolicy,
    MessageId,
    ModerationEngine,
    ModerationRequest,
    ModerationRule,
    RuleKind,
    RuleMergeMode,
    RuleScope,
    UserId,
    Verdict,
)


def _request(content: str, *, role: ActorRole = ActorRole.MEMBER) -> ModerationRequest:
    return ModerationRequest(
        chat_id=ChatId(-1001),
        user_id=UserId(42),
        message_id=MessageId(7),
        content=content,
        content_kind=ContentKind.TEXT,
        actor_role=role,
    )


def _policy(**overrides) -> GroupModerationPolicy:
    values = {
        "chat_id": ChatId(-1001),
        "enabled": True,
    }
    values.update(overrides)
    return GroupModerationPolicy(**values)


def _literal(pattern: str, scope: RuleScope) -> ModerationRule:
    return ModerationRule(
        pattern=pattern,
        kind=RuleKind.LITERAL,
        scope=scope,
    )


def test_disabled_policy_allows_matching_content():
    decision = ModerationEngine().evaluate(
        _request("博彩"),
        _policy(enabled=False),
        global_rules=(_literal("博彩", RuleScope.GLOBAL),),
    )

    assert decision.verdict is Verdict.ALLOW
    assert decision.stop_downstream is False


def test_admin_exemption_is_explicit_policy():
    decision = ModerationEngine().evaluate(
        _request("博彩", role=ActorRole.ADMINISTRATOR),
        _policy(exempt_administrators=True),
        global_rules=(_literal("博彩", RuleScope.GLOBAL),),
    )

    assert decision.verdict is Verdict.ALLOW


def test_legacy_override_uses_group_rules_instead_of_global_rules():
    engine = ModerationEngine()
    policy = _policy(rule_merge_mode=RuleMergeMode.OVERRIDE_GLOBAL)
    global_rules = (_literal("博彩", RuleScope.GLOBAL),)
    group_rules = (_literal("广告", RuleScope.GROUP),)

    assert engine.evaluate(
        _request("博彩"),
        policy,
        global_rules=global_rules,
        group_rules=group_rules,
    ).verdict is Verdict.ALLOW
    assert engine.evaluate(
        _request("广告"),
        policy,
        global_rules=global_rules,
        group_rules=group_rules,
    ).verdict is Verdict.BLOCK


def test_extend_mode_combines_group_and_global_rules():
    decision = ModerationEngine().evaluate(
        _request("博彩"),
        _policy(rule_merge_mode=RuleMergeMode.EXTEND_GLOBAL),
        global_rules=(_literal("博彩", RuleScope.GLOBAL),),
        group_rules=(_literal("广告", RuleScope.GROUP),),
    )

    assert decision.verdict is Verdict.BLOCK
    assert decision.primary_match is not None
    assert decision.primary_match.matched_text == "博彩"


def test_link_and_mention_policy_precede_word_rules():
    engine = ModerationEngine()
    policy = _policy(block_links=True, block_mentions=True)

    link_decision = engine.evaluate(_request("https://example.com @someone"), policy)
    mention_decision = engine.evaluate(_request("hello @someone"), policy)

    assert link_decision.primary_match is not None
    assert link_decision.primary_match.rule.kind is RuleKind.LINK
    assert mention_decision.primary_match is not None
    assert mention_decision.primary_match.rule.kind is RuleKind.MENTION


def test_invalid_regex_is_ignored_without_crashing():
    decision = ModerationEngine().evaluate(
        _request("hello"),
        _policy(),
        global_rules=(
            ModerationRule(
                pattern="(",
                kind=RuleKind.REGEX,
                scope=RuleScope.GLOBAL,
            ),
        ),
    )

    assert decision.verdict is Verdict.ALLOW
