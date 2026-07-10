from datetime import datetime, timedelta

import pytest

from fogmoe_bot.domain.moderation import ChatId, MessageId, UserId
from fogmoe_bot.domain.moderation.verification import (
    VerificationStatus,
    VerificationTask,
    hash_verification_token,
)


def _task(token: str = "secret") -> VerificationTask:
    return VerificationTask(
        chat_id=ChatId(-1001),
        user_id=UserId(42),
        message_id=MessageId(7),
        token_hash=hash_verification_token(token),
        expires_at=datetime(2030, 1, 1),
    )


def test_pending_task_accepts_only_matching_unexpired_token():
    task = _task()

    assert task.accepts("secret", datetime(2029, 1, 1)) is True
    assert task.accepts("wrong", datetime(2029, 1, 1)) is False
    assert task.accepts("secret", datetime(2031, 1, 1)) is False


def test_verification_task_allows_only_one_terminal_transition():
    passed = _task().transition(VerificationStatus.PASSED)

    assert passed.status is VerificationStatus.PASSED
    with pytest.raises(RuntimeError, match="PASSED -> CANCELLED"):
        passed.transition(VerificationStatus.CANCELLED)
