"""@brief Telegram durable-ingress mapper 测试 / Tests for the Telegram durable-ingress mapper."""

import json
from datetime import datetime, timezone

from telegram import Update

from fogmoe_bot.domain.conversation.inbox import InboundStatus
from fogmoe_bot.presentation.telegram.update_mapper import TelegramUpdateMapper


def _update(payload: str) -> Update:
    """@brief 从 JSON 构造真实 PTB Update / Construct a real PTB Update from JSON.

    @param payload Telegram Bot API JSON / Telegram Bot API JSON.
    @return PTB Update / PTB Update.
    """

    return Update.de_json(json.loads(payload), bot=None)


def test_mapper_separates_private_users_and_shares_group_topics() -> None:
    """@brief 私聊按用户、群聊按 Topic 聚合 / Private chats aggregate by user and groups by topic."""

    mapper = TelegramUpdateMapper()
    first = _update(
        '{"update_id": 101, "message": {"message_id": 1, "date": 1, '
        '"chat": {"id": 10, "type": "private"}, '
        '"from": {"id": 7, "is_bot": false, "first_name": "Klee"}, "text": "a"}}'
    )
    second = _update(
        '{"update_id": 102, "message": {"message_id": 2, "date": 2, '
        '"chat": {"id": -20, "type": "group", "title": "Lab"}, '
        '"from": {"id": 7, "is_bot": false, "first_name": "Klee"}, "text": "b"}}'
    )
    other_member = _update(
        '{"update_id": 103, "message": {"message_id": 3, "date": 3, '
        '"message_thread_id": 9, '
        '"chat": {"id": -20, "type": "supergroup", "title": "Lab"}, '
        '"from": {"id": 8, "is_bot": false, "first_name": "Alice"}, "text": "c"}}'
    )
    same_topic = _update(
        '{"update_id": 104, "message": {"message_id": 4, "date": 4, '
        '"message_thread_id": 9, '
        '"chat": {"id": -20, "type": "supergroup", "title": "Lab"}, '
        '"from": {"id": 7, "is_bot": false, "first_name": "Klee"}, "text": "d"}}'
    )

    assert mapper.identity_for(first).conversation_id.value == "assistant-user:7"
    assert (
        mapper.identity_for(second).conversation_id.value
        == "assistant-group:-20:thread:0"
    )
    assert (
        mapper.identity_for(other_member).conversation_id
        == mapper.identity_for(same_topic).conversation_id
    )
    assert (
        mapper.identity_for(other_member).conversation_id.value
        == "assistant-group:-20:thread:9"
    )


def test_mapper_creates_json_safe_pending_inbound() -> None:
    """@brief mapper 产生可领取且时间规范化的 inbox 实体 / Mapper produces a claimable, time-normalized inbox entity."""

    mapper = TelegramUpdateMapper()
    received_at = datetime(2026, 7, 11, 12, 30, tzinfo=timezone.utc)
    update = _update(
        '{"update_id": 501, "message": {"message_id": 9, "date": 1783773000, '
        '"chat": {"id": 77, "type": "private"}, '
        '"from": {"id": 77, "is_bot": false, "first_name": "Klee"}, '
        '"text": "hello"}}'
    )

    inbound = mapper.map(update, received_at=received_at)

    assert inbound.update_id.value == 501
    assert inbound.conversation_id.value == "assistant-user:77"
    assert inbound.status is InboundStatus.PENDING
    assert inbound.next_attempt_at == datetime(2026, 7, 11, 12, 30, tzinfo=timezone.utc)
    assert inbound.payload["message"]["text"] == "hello"  # type: ignore[index]
    assert isinstance(inbound.payload["message"]["date"], int)  # type: ignore[index]


def test_mapper_has_stable_fallbacks_for_non_user_updates() -> None:
    """@brief 无用户 Update 依次退化到 chat 与 update 身份 / Userless Updates fall back to chat and then update identities."""

    mapper = TelegramUpdateMapper()
    channel_post = _update(
        '{"update_id": 701, "channel_post": {"message_id": 3, "date": 3, '
        '"chat": {"id": -1008, "type": "channel", "title": "News"}, "text": "x"}}'
    )
    poll = _update(
        '{"update_id": 702, "poll": {"id": "p", "question": "q", '
        '"options": [], "total_voter_count": 0, "is_closed": false, '
        '"is_anonymous": true, "type": "regular", "allows_multiple_answers": false, '
        '"allows_revoting": false, "members_only": false}}'
    )

    assert (
        mapper.identity_for(channel_post).conversation_id.value == "telegram-chat:-1008"
    )
    assert mapper.identity_for(poll).conversation_id.value == "telegram-update:702"
