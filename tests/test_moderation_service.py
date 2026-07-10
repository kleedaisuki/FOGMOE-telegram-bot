import asyncio

from fogmoe_bot.application.moderation.service import ModerationService
from fogmoe_bot.domain.moderation import (
    ActorRole,
    ChatId,
    ContentKind,
    GroupModerationPolicy,
    MessageId,
    ModerationRequest,
    ModerationRule,
    RuleKind,
    RuleScope,
    UserId,
    Verdict,
)


class _PolicyProvider:
    def __init__(self, policy: GroupModerationPolicy) -> None:
        self.policy = policy

    async def get_policy(self, chat_id: ChatId) -> GroupModerationPolicy:
        assert chat_id == self.policy.chat_id
        return self.policy


class _GroupRuleProvider:
    def __init__(self, rules: tuple[ModerationRule, ...]) -> None:
        self.rules = rules
        self.calls = 0

    async def get_group_rules(self, chat_id: ChatId) -> tuple[ModerationRule, ...]:
        self.calls += 1
        return self.rules


class _GlobalRuleProvider:
    def __init__(self, rules: tuple[ModerationRule, ...]) -> None:
        self.rules = rules

    def get_global_rules(self) -> tuple[ModerationRule, ...]:
        return self.rules


def _request() -> ModerationRequest:
    return ModerationRequest(
        chat_id=ChatId(-1001),
        user_id=UserId(42),
        message_id=MessageId(7),
        content="博彩",
        content_kind=ContentKind.TEXT,
        actor_role=ActorRole.MEMBER,
    )


def test_service_skips_rule_io_when_policy_is_disabled():
    group_rules = _GroupRuleProvider(())
    service = ModerationService(
        _PolicyProvider(GroupModerationPolicy(chat_id=ChatId(-1001))),
        group_rules,
        _GlobalRuleProvider(()),
    )

    decision = asyncio.run(service.moderate(_request()))

    assert decision.verdict is Verdict.ALLOW
    assert group_rules.calls == 0


def test_service_composes_typed_providers_and_engine():
    service = ModerationService(
        _PolicyProvider(
            GroupModerationPolicy(chat_id=ChatId(-1001), enabled=True)
        ),
        _GroupRuleProvider(()),
        _GlobalRuleProvider(
            (
                ModerationRule(
                    pattern="博彩",
                    kind=RuleKind.LITERAL,
                    scope=RuleScope.GLOBAL,
                ),
            )
        ),
    )

    decision = asyncio.run(service.moderate(_request()))

    assert decision.verdict is Verdict.BLOCK
    assert decision.stop_downstream is True
