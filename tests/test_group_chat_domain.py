"""@brief 群聊领域行为与架构归属测试 / Group-chat domain behavior and ownership tests."""

from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fogmoe_bot.domain.chat.group_messages import (
    GROUP_ATTACHMENT_MARKER,
    GROUP_SERVICE_MESSAGE_MARKER,
    GroupContextQuery,
    GroupConversationScope,
    GroupMessage,
    GroupMessageIdentity,
    GroupMessageKind,
    GroupMessageObservation,
)
from fogmoe_bot.domain.conversation.telegram_identity import (
    TelegramConversationAddress,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""@brief 项目根目录 / Project root."""

SRC_ROOT = PROJECT_ROOT / "src" / "fogmoe_bot"
"""@brief Bot 源码根目录 / Bot source root."""

NOW = datetime(2026, 7, 31, tzinfo=UTC)
"""@brief 测试固定时刻 / Fixed test instant."""


def _observation(
    source_update_id: int,
    *,
    message_id: int = 7,
    message_thread_id: int | None = 23,
    kind: GroupMessageKind = GroupMessageKind.TEXT,
    content: str = "hello",
) -> GroupMessageObservation:
    """@brief 构造领域观察 / Build a domain observation.

    @param source_update_id Update 单调序号 / Monotonic update sequence.
    @param message_id 群内消息 ID / Message identifier within the group.
    @param message_thread_id 可选 Topic ID / Optional topic identifier.
    @param kind 消息种类 / Message kind.
    @param content 规范内容 / Canonical content.
    @return 已验证观察 / Validated observation.
    """

    return GroupMessageObservation(
        source_update_id=source_update_id,
        identity=GroupMessageIdentity(
            GroupConversationScope(-1001, message_thread_id),
            message_id,
        ),
        sender_user_id=42,
        sender_name="  Klee  ",
        sender_username=" klee ",
        kind=kind,
        content=content,
        created_at=NOW,
        updated_at=NOW,
        edited=source_update_id > 10,
    )


def test_message_revision_order_is_a_domain_rule() -> None:
    """@brief replay、陈旧与新编辑由领域对象判定 / The domain object classifies replay, stale, and newer edits."""

    current = _observation(10)
    replay = _observation(10)
    stale = _observation(9)
    newer = _observation(11)

    assert replay.supersedes(current) is False
    assert stale.supersedes(current) is False
    assert newer.supersedes(current) is True
    with pytest.raises(ValueError, match="different group messages"):
        _observation(12, message_id=8).supersedes(current)


def test_new_non_text_observations_cannot_leak_provider_content() -> None:
    """@brief 新附件与 service 观察只能使用惰性领域标记 / New attachment and service observations admit only inert domain markers."""

    attachment = _observation(
        10,
        kind=GroupMessageKind.PHOTO,
        content=GROUP_ATTACHMENT_MARKER,
    )
    service = _observation(
        10,
        kind=GroupMessageKind.OTHER,
        content=GROUP_SERVICE_MESSAGE_MARKER,
    )

    assert attachment.kind.is_attachment
    assert service.kind.is_attachment is False
    with pytest.raises(ValueError, match="inert attachment marker"):
        _observation(
            10,
            kind=GroupMessageKind.STICKER,
            content="provider-file-id:secret",
        )
    with pytest.raises(ValueError, match="stable service marker"):
        _observation(10, kind=GroupMessageKind.OTHER, content="raw service payload")


def test_context_query_closes_topic_and_window_invariants() -> None:
    """@brief 查询值同时封闭 Topic 隔离、排他边界与条数上限 / The query value closes topic isolation, exclusive-bound, and row-limit invariants."""

    scope = GroupConversationScope(-1001, 23)
    query = GroupContextQuery(scope, before_message_id=10, limit=5)
    included = GroupMessage(
        GroupMessageIdentity(scope, 9),
        42,
        "Klee",
        GroupMessageKind.TEXT,
        "included",
        NOW,
        False,
    )
    boundary = GroupMessage(
        GroupMessageIdentity(scope, 10),
        42,
        "Klee",
        GroupMessageKind.TEXT,
        "boundary",
        NOW,
        False,
    )
    other_topic = GroupMessage(
        GroupMessageIdentity(GroupConversationScope(-1001, 24), 9),
        42,
        "Klee",
        GroupMessageKind.TEXT,
        "other",
        NOW,
        False,
    )

    assert query.includes(included)
    assert not query.includes(boundary)
    assert not query.includes(other_topic)
    with pytest.raises(ValueError, match="between 1 and 512"):
        GroupContextQuery(scope, before_message_id=10, limit=513)
    with pytest.raises(FrozenInstanceError):
        query.limit = 6  # type: ignore[misc]


def test_telegram_group_conversation_identity_is_shared_per_topic() -> None:
    """@brief 群成员共享 Topic 会话而不同 Topic 隔离 / Group members share a topic conversation while different topics remain isolated."""

    first_member = TelegramConversationAddress("supergroup", -1001, 41, 23)
    second_member = TelegramConversationAddress(" SUPERGROUP ", -1001, 42, 23)
    other_topic = TelegramConversationAddress("supergroup", -1001, 42, 24)
    private = TelegramConversationAddress("private", 42, 42, None)

    assert first_member.is_group
    assert first_member.conversation_id == second_member.conversation_id
    assert first_member.conversation_id != other_topic.conversation_id
    assert str(private.conversation_id) == "assistant-user:42"
    with pytest.raises(ValueError, match="only to group"):
        TelegramConversationAddress("private", 42, 42, 23)


def test_group_chat_types_and_ports_have_explicit_layer_ownership() -> None:
    """@brief 领域类型与应用端口不能退回旧混合模块 / Domain types and application ports cannot regress to the former mixed module."""

    domain_messages = SRC_ROOT / "domain" / "chat" / "group_messages.py"
    domain_identity = SRC_ROOT / "domain" / "conversation" / "telegram_identity.py"
    application_ports = SRC_ROOT / "application" / "chat" / "ports.py"
    removed_modules = (
        SRC_ROOT / "application" / "chat" / "group_messages.py",
        SRC_ROOT / "application" / "conversation" / "telegram_identity.py",
    )

    assert domain_messages.is_file()
    assert domain_identity.is_file()
    assert application_ports.is_file()
    assert [path for path in removed_modules if path.exists()] == []

    port_tree = ast.parse(
        application_ports.read_text(encoding="utf-8"),
        filename=str(application_ports),
    )
    classes = {
        node.name: tuple(base.id for base in node.bases if isinstance(base, ast.Name))
        for node in port_tree.body
        if isinstance(node, ast.ClassDef)
    }
    assert classes == {
        "GroupMessageWriter": ("Protocol",),
        "GroupContextReader": ("Protocol",),
    }

    forbidden_imports = {
        "fogmoe_bot.application.chat.group_messages",
        "fogmoe_bot.application.conversation.telegram_identity",
    }
    offenders: list[str] = []
    for root in (SRC_ROOT, PROJECT_ROOT / "tests"):
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ImportFrom)
                    and node.module in forbidden_imports
                ):
                    offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}")
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name in forbidden_imports:
                            offenders.append(
                                f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}"
                            )
    assert offenders == []
