import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from fogmoe_bot.application.conversation.router import Allow, Reject
from fogmoe_bot.application.moderation.configuration import (
    BoundedTtlCache,
    GroupModerationConfiguration,
)
from fogmoe_bot.application.moderation.effect_service import ModerationEffectService
from fogmoe_bot.application.moderation.ingress import (
    KeywordAutomationService,
    KeywordIngressObserver,
    ModerationIngressGuard,
)
from fogmoe_bot.application.moderation.rate_windows import FixedWindowGate
from fogmoe_bot.application.moderation.service import ModerationService
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    UpdateId,
)
from fogmoe_bot.domain.conversation.inbox import InboundUpdate
from fogmoe_bot.domain.moderation.aggregate import (
    GroupModeration,
    StaleModerationVersion,
)
from fogmoe_bot.domain.moderation.effects import (
    KeywordReplyPlan,
    ModerationEffect,
    ModerationEffectId,
    ModerationEffectStatus,
    SpamEnforcementPlan,
)
from fogmoe_bot.domain.moderation.models import (
    ActorRole,
    ChatId,
    ContentKind,
    EnforcementFailureMode,
    MessageId,
    ModerationRequest,
    ModerationRule,
    UserId,
)


NOW = datetime(2026, 7, 11, tzinfo=UTC)


class _Clock:
    def now(self) -> datetime:
        return NOW


class _GroupRepository:
    def __init__(self, group: GroupModeration) -> None:
        self.group = group

    async def load_group(self, chat_id: ChatId) -> GroupModeration:
        assert chat_id == self.group.chat_id
        return self.group

    async def save_group(
        self,
        aggregate: GroupModeration,
        *,
        expected_version: int,
        actor_id: int,
    ) -> None:
        del actor_id
        if self.group.version != expected_version:
            raise StaleModerationVersion
        self.group = aggregate


class _Effects:
    def __init__(self) -> None:
        self.values: dict[ModerationEffectId, ModerationEffect] = {}

    async def load_effect(
        self,
        effect_id: ModerationEffectId,
    ) -> ModerationEffect | None:
        return self.values.get(effect_id)

    async def reserve_effect(
        self,
        plan: SpamEnforcementPlan | KeywordReplyPlan,
        *,
        now: datetime,
        warning_window: timedelta,
    ) -> ModerationEffect:
        del warning_window
        current = self.values.get(plan.effect_id)
        if current is not None:
            return current
        effect = ModerationEffect(
            plan=plan,
            status=ModerationEffectStatus.PENDING,
            version=0,
            warning_count=1 if isinstance(plan, SpamEnforcementPlan) else None,
            last_error=None,
            updated_at=now,
        )
        self.values[plan.effect_id] = effect
        return effect

    async def save_effect(
        self,
        effect: ModerationEffect,
        *,
        expected_version: int,
    ) -> None:
        current = self.values[effect.effect_id]
        if current.version != expected_version:
            raise StaleModerationVersion
        self.values[effect.effect_id] = effect


class _Sink:
    def __init__(self) -> None:
        self.delete_error: Exception | None = None
        self.replies: list[str] = []

    async def delete_spam(self, plan: SpamEnforcementPlan) -> None:
        del plan
        if self.delete_error is not None:
            raise self.delete_error

    async def send_spam_warning(
        self,
        plan: SpamEnforcementPlan,
        *,
        warning_count: int,
    ) -> None:
        del plan, warning_count

    async def send_keyword_reply(self, plan: KeywordReplyPlan) -> None:
        self.replies.append(plan.response)


class _Mapper:
    def __init__(self, request: ModerationRequest) -> None:
        self.request = request

    async def moderation_request(
        self,
        update: InboundUpdate,
    ) -> ModerationRequest | None:
        del update
        return self.request

    def keyword_request(self, update: InboundUpdate) -> ModerationRequest | None:
        del update
        return self.request


class _Globals:
    def get_global_rules(self) -> tuple[ModerationRule, ...]:
        return ()


def _update(update_id: int = 1) -> InboundUpdate:
    return InboundUpdate.pending(
        update_id=UpdateId(update_id),
        conversation_id=ConversationId("telegram-chat:-1001"),
        payload={},
        received_at=NOW,
    )


def _request(
    content: str,
    *,
    role: ActorRole = ActorRole.MEMBER,
) -> ModerationRequest:
    return ModerationRequest(
        chat_id=ChatId(-1001),
        user_id=UserId(42),
        message_id=MessageId(7),
        content=content,
        content_kind=ContentKind.TEXT,
        actor_role=role,
    )


def _configuration(group: GroupModeration) -> GroupModerationConfiguration:
    return GroupModerationConfiguration(
        _GroupRepository(group),
        BoundedTtlCache(ttl_seconds=300, max_entries=16),
    )


def test_guard_blocks_and_persists_effect_before_downstream() -> None:
    group = GroupModeration.empty(ChatId(-1001)).toggle(UserId(42))
    group = group.put_spam_rule("博彩", regex=False, actor_id=UserId(42))
    configuration = _configuration(group)
    effects = _Effects()
    sink = _Sink()
    guard = ModerationIngressGuard(
        mapper=_Mapper(_request("博彩")),
        moderation=ModerationService(configuration, configuration, _Globals()),
        configuration=configuration,
        effects=ModerationEffectService(effects, sink, _Clock()),
    )

    decision = asyncio.run(guard.evaluate(_update()))

    assert isinstance(decision, Reject)
    assert decision.reason == "moderation:literal"
    assert (
        next(iter(effects.values.values())).status is ModerationEffectStatus.DELIVERED
    )


def test_guard_preserves_admin_exemption() -> None:
    group = GroupModeration.empty(ChatId(-1001)).toggle(UserId(42))
    group = group.put_spam_rule("博彩", regex=False, actor_id=UserId(42))
    configuration = _configuration(group)
    effects = _Effects()
    guard = ModerationIngressGuard(
        mapper=_Mapper(_request("博彩", role=ActorRole.ADMINISTRATOR)),
        moderation=ModerationService(configuration, configuration, _Globals()),
        configuration=configuration,
        effects=ModerationEffectService(effects, _Sink(), _Clock()),
    )

    assert isinstance(asyncio.run(guard.evaluate(_update())), Allow)
    assert effects.values == {}


def test_fail_open_allows_downstream_when_deletion_fails() -> None:
    group = GroupModeration.empty(ChatId(-1001)).toggle(UserId(42))
    group = group.put_spam_rule("博彩", regex=False, actor_id=UserId(42))
    group = replace(
        group,
        policy=replace(
            group.policy,
            failure_mode=EnforcementFailureMode.FAIL_OPEN,
        ),
    )
    configuration = _configuration(group)
    sink = _Sink()
    sink.delete_error = RuntimeError("forbidden")
    guard = ModerationIngressGuard(
        mapper=_Mapper(_request("博彩")),
        moderation=ModerationService(configuration, configuration, _Globals()),
        configuration=configuration,
        effects=ModerationEffectService(_Effects(), sink, _Clock()),
    )

    assert isinstance(asyncio.run(guard.evaluate(_update())), Allow)


def test_keyword_observer_has_explicit_five_per_minute_policy_and_idempotent_replay() -> (
    None
):
    group = GroupModeration.empty(ChatId(-1001)).put_keyword_reply(
        "Klee",
        "可莉在这里！",
        UserId(42),
    )
    configuration = _configuration(group)
    effects = _Effects()
    sink = _Sink()
    mapper = _Mapper(_request("hello Klee"))
    automation = KeywordAutomationService(
        configuration=configuration,
        effect_repository=effects,
        effects=ModerationEffectService(effects, sink, _Clock()),
        rate_limit=FixedWindowGate(
            window_seconds=60,
            max_admissions=5,
            max_entries=16,
        ),
    )
    observer = KeywordIngressObserver(mapper=mapper, automation=automation)

    async def scenario() -> None:
        for update_id in range(1, 7):
            update = _update(update_id)
            operation = await observer.operation(update, primary_route=None)
            assert operation is not None
            await operation.call()
        replay = _update(1)
        operation = await observer.operation(replay, primary_route=None)
        assert operation is not None
        await operation.call()

    asyncio.run(scenario())

    assert sink.replies == ["可莉在这里！"] * 5
