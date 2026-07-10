import asyncio
from types import SimpleNamespace

from fogmoe_bot.application.moderation.spam_control import enforce_moderation_decision
from fogmoe_bot.domain.moderation import (
    ChatId,
    EnforcementFailureMode,
    GroupModerationPolicy,
    ModerationDecision,
    ModerationRule,
    RuleKind,
    RuleMatch,
    RuleScope,
    Verdict,
)


class _User:
    id = 42

    def mention_html(self) -> str:
        return '<a href="tg://user?id=42">user</a>'


class _Bot:
    def __init__(self, *, deletion_error: Exception | None = None) -> None:
        self.deletion_error = deletion_error
        self.sent_text: str | None = None

    async def delete_message(self, **kwargs) -> None:
        if self.deletion_error:
            raise self.deletion_error

    async def send_message(self, **kwargs) -> None:
        self.sent_text = kwargs["text"]


def _decision() -> ModerationDecision:
    rule = ModerationRule(
        pattern="bad",
        kind=RuleKind.LITERAL,
        scope=RuleScope.GLOBAL,
    )
    return ModerationDecision(
        verdict=Verdict.BLOCK,
        matches=(RuleMatch(rule=rule, matched_text="<bad&>"),),
        stop_downstream=True,
    )


def _update():
    user = _User()
    return SimpleNamespace(
        message=SimpleNamespace(message_id=7, from_user=user),
        edited_message=None,
        effective_chat=SimpleNamespace(id=-1001),
        effective_user=user,
    )


def test_enforcement_escapes_user_controlled_match_text():
    bot = _Bot()
    result = asyncio.run(
        enforce_moderation_decision(
            _update(),
            SimpleNamespace(bot=bot),
            _decision(),
            GroupModerationPolicy(chat_id=ChatId(-1001), enabled=True),
        )
    )

    assert result.message_deleted is True
    assert result.warning_sent is True
    assert result.downstream_stopped is True
    assert "&lt;bad&amp;&gt;" in bot.sent_text


def test_fail_closed_stops_downstream_when_telegram_deletion_fails():
    bot = _Bot(deletion_error=RuntimeError("no permission"))
    result = asyncio.run(
        enforce_moderation_decision(
            _update(),
            SimpleNamespace(bot=bot),
            _decision(),
            GroupModerationPolicy(
                chat_id=ChatId(-1001),
                enabled=True,
                failure_mode=EnforcementFailureMode.FAIL_CLOSED,
            ),
        )
    )

    assert result.message_deleted is False
    assert result.warning_sent is False
    assert result.downstream_stopped is True
    assert result.error == "no permission"
