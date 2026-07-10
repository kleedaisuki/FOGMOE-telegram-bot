import asyncio

import pytest

from fogmoe_bot.application.telegram.bot_conversation import (
    ConversationSessionState,
    ConversationTurnSession,
)


def test_empty_batch_finishes_as_ignored():
    session = ConversationTurnSession(batch_items=[])

    asyncio.run(session.run())

    assert session.state is ConversationSessionState.IGNORED


def test_session_rejects_skipped_business_milestones():
    session = ConversationTurnSession(batch_items=[])

    with pytest.raises(RuntimeError, match="BATCHED -> CHARGED"):
        session._transition(ConversationSessionState.CHARGED)

    assert session.state is ConversationSessionState.BATCHED
