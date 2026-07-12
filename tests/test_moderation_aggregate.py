import asyncio

import pytest

from fogmoe_bot.application.moderation.commands import GroupModerationCommandService
from fogmoe_bot.application.moderation.configuration import (
    BoundedTtlCache,
    GroupModerationConfiguration,
)
from fogmoe_bot.domain.moderation.aggregate import (
    GroupModeration,
    ModerationLimitExceeded,
    StaleModerationVersion,
)
from fogmoe_bot.domain.moderation.models import (
    ChatId,
    ModerationToggleResult,
    RuleKind,
    UserId,
)


class _Repository:
    def __init__(self) -> None:
        self.value = GroupModeration.empty(ChatId(-1001))
        self.inject_conflict = False

    async def load_group(self, chat_id: ChatId) -> GroupModeration:
        assert chat_id == self.value.chat_id
        return self.value

    async def save_group(
        self,
        aggregate: GroupModeration,
        *,
        expected_version: int,
        actor_id: int,
    ) -> None:
        assert actor_id == 42
        if self.inject_conflict:
            self.inject_conflict = False
            self.value = self.value.toggle(UserId(99))
            raise StaleModerationVersion
        if self.value.version != expected_version:
            raise StaleModerationVersion
        self.value = aggregate

    async def toggle_group(
        self,
        chat_id: ChatId,
        *,
        actor_id: int,
        idempotency_key: str,
    ) -> ModerationToggleResult:
        assert chat_id == self.value.chat_id
        assert actor_id == 42
        assert idempotency_key == "telegram-update:7:moderation.spam-toggle"
        if self.inject_conflict:
            self.inject_conflict = False
            self.value = self.value.toggle(UserId(99))
            raise StaleModerationVersion
        self.value = self.value.toggle(UserId(actor_id))
        return ModerationToggleResult(self.value.policy.enabled)


def test_aggregate_eliminates_duplicate_special_cases_and_validates_regex() -> None:
    group = GroupModeration.empty(ChatId(-1001))

    group = group.put_spam_rule("博彩", regex=False, actor_id=UserId(42))
    group = group.put_spam_rule("博彩", regex=True, actor_id=UserId(42))

    assert len(group.spam_rules) == 1
    assert group.spam_rules[0].kind is RuleKind.REGEX
    assert group.version == 2
    with pytest.raises(ValueError, match="Invalid regular expression"):
        group.put_spam_rule("(", regex=True, actor_id=UserId(42))


def test_aggregate_enforces_ten_rule_limit() -> None:
    group = GroupModeration.empty(ChatId(-1001))
    for index in range(10):
        group = group.put_spam_rule(
            f"rule-{index}",
            regex=False,
            actor_id=UserId(42),
        )

    with pytest.raises(ModerationLimitExceeded):
        group.put_spam_rule("overflow", regex=False, actor_id=UserId(42))


def test_command_recomputes_against_latest_version_after_occ_conflict() -> None:
    repository = _Repository()
    repository.inject_conflict = True
    configuration = GroupModerationConfiguration(
        repository,
        BoundedTtlCache(ttl_seconds=300, max_entries=4),
    )
    service = GroupModerationCommandService(repository, configuration)

    committed = asyncio.run(
        service.toggle(
            ChatId(-1001),
            UserId(42),
            idempotency_key="telegram-update:7:moderation.spam-toggle",
        )
    )

    assert committed.enabled is False
    assert repository.value.version == 2
    assert repository.value.policy.enabled is False


def test_runtime_owned_ttl_cache_is_bounded_and_lazy_expires() -> None:
    now = 0.0

    def clock() -> float:
        return now

    cache = BoundedTtlCache[int, str](
        ttl_seconds=5,
        max_entries=2,
        clock=clock,
    )
    cache.put(1, "one")
    cache.put(2, "two")
    cache.put(3, "three")

    assert cache.entry_count == 2
    assert cache.get(1) is None
    now = 6
    assert cache.get(2) is None
    assert cache.entry_count == 0
