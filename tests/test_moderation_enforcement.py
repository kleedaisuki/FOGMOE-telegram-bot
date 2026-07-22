import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from telegram.error import BadRequest

from fogmoe_bot.application.moderation.effect_service import (
    ModerationEffectDeliveryError,
    ModerationEffectService,
)
from fogmoe_bot.domain.moderation.aggregate import StaleModerationVersion
from fogmoe_bot.domain.moderation.effects import (
    ModerationEffect,
    ModerationEffectId,
    ModerationEffectKind,
    ModerationEffectStatus,
    SpamEnforcementPlan,
)
from fogmoe_bot.domain.moderation.models import (
    ChatId,
    EnforcementFailureMode,
    MessageId,
    RuleKind,
    UserId,
)
from fogmoe_bot.presentation.telegram.moderation_adapter import (
    TelegramModerationEffectSink,
)

NOW = datetime(2026, 7, 11, tzinfo=UTC)


class _Clock:
    def now(self) -> datetime:
        return NOW


class _Repository:
    def __init__(self) -> None:
        self.effect: ModerationEffect | None = None
        self.reservations = 0

    async def load_effect(
        self,
        effect_id: ModerationEffectId,
    ) -> ModerationEffect | None:
        assert self.effect is None or self.effect.effect_id == effect_id
        return self.effect

    async def reserve_effect(
        self,
        plan: SpamEnforcementPlan,
        *,
        now: datetime,
        warning_window: timedelta,
    ) -> ModerationEffect:
        assert warning_window == timedelta(hours=1)
        if self.effect is None:
            self.reservations += 1
            self.effect = ModerationEffect(
                plan=plan,
                status=ModerationEffectStatus.PENDING,
                version=0,
                warning_count=1,
                last_error=None,
                updated_at=now,
            )
        return self.effect

    async def save_effect(
        self,
        effect: ModerationEffect,
        *,
        expected_version: int,
    ) -> None:
        if self.effect is None or self.effect.version != expected_version:
            raise StaleModerationVersion
        self.effect = effect


class _Sink:
    def __init__(self) -> None:
        self.deletions = 0
        self.warnings = 0
        self.deletion_error: Exception | None = None
        self.warning_error: Exception | None = None

    async def delete_spam(self, plan: SpamEnforcementPlan) -> None:
        del plan
        self.deletions += 1
        if self.deletion_error is not None:
            raise self.deletion_error

    async def send_spam_warning(
        self,
        plan: SpamEnforcementPlan,
        *,
        warning_count: int,
    ) -> None:
        del plan
        assert warning_count == 1
        self.warnings += 1
        if self.warning_error is not None:
            raise self.warning_error

    async def send_keyword_reply(self, plan: object) -> None:
        del plan


class _Bot:
    def __init__(self) -> None:
        self.sent_text = ""
        self.deletion_error: Exception | None = None

    async def delete_message(self, **kwargs: object) -> None:
        del kwargs
        if self.deletion_error is not None:
            raise self.deletion_error

    async def send_message(self, **kwargs: object) -> None:
        self.sent_text = str(kwargs["text"])


def _plan() -> SpamEnforcementPlan:
    return SpamEnforcementPlan(
        effect_id=ModerationEffectId.for_update(
            99,
            ModerationEffectKind.SPAM_ENFORCEMENT,
        ),
        update_id=99,
        chat_id=ChatId(-1001),
        user_id=UserId(42),
        message_id=MessageId(7),
        matched_text="<bad&>",
        rule_kind=RuleKind.LITERAL,
        failure_mode=EnforcementFailureMode.FAIL_CLOSED,
    )


def test_effect_replay_reuses_intent_warning_count_and_terminal_result() -> None:
    repository = _Repository()
    sink = _Sink()
    service = ModerationEffectService(repository, sink, _Clock())

    first = asyncio.run(service.enforce_spam(_plan()))
    second = asyncio.run(service.enforce_spam(_plan()))

    assert first.message_deleted and first.warning_sent
    assert second.message_deleted and second.warning_sent
    assert repository.reservations == 1
    assert sink.deletions == 1
    assert sink.warnings == 1
    assert repository.effect is not None
    assert repository.effect.status is ModerationEffectStatus.DELIVERED


def test_deletion_failure_is_persisted_and_returned_to_failure_policy() -> None:
    repository = _Repository()
    sink = _Sink()
    sink.deletion_error = RuntimeError("no permission")
    service = ModerationEffectService(repository, sink, _Clock())

    outcome = asyncio.run(service.enforce_spam(_plan()))

    assert outcome.message_deleted is False
    assert outcome.error == "no permission"
    assert repository.effect is not None
    assert repository.effect.status is ModerationEffectStatus.FAILED


def test_warning_failure_requests_inbox_replay() -> None:
    repository = _Repository()
    sink = _Sink()
    sink.warning_error = RuntimeError("network")
    service = ModerationEffectService(repository, sink, _Clock())

    with pytest.raises(ModerationEffectDeliveryError, match="network"):
        asyncio.run(service.enforce_spam(_plan()))

    assert repository.effect is not None
    assert repository.effect.status is ModerationEffectStatus.MESSAGE_DELETED

    sink.warning_error = None
    outcome = asyncio.run(service.enforce_spam(_plan()))

    assert outcome.warning_sent is True
    assert sink.deletions == 1
    assert sink.warnings == 2


def test_telegram_warning_escapes_user_controlled_match_text() -> None:
    bot = _Bot()

    asyncio.run(
        TelegramModerationEffectSink(bot).send_spam_warning(_plan(), warning_count=1)
    )

    assert "&lt;bad&amp;&gt;" in bot.sent_text
    assert "<bad&>" not in bot.sent_text


def test_telegram_deletion_treats_replay_not_found_as_idempotent_success() -> None:
    bot = _Bot()
    bot.deletion_error = BadRequest("Message to delete not found")

    asyncio.run(TelegramModerationEffectSink(bot).delete_spam(_plan()))
