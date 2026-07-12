"""@brief Telegram toggle handler 重放测试 / Telegram toggle-handler replay tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from telegram.constants import ChatMemberStatus, ChatType

from fogmoe_bot.domain.moderation.models import ModerationToggleResult
from fogmoe_bot.presentation.telegram import moderation_handlers, verification_handlers


class _Message:
    """@brief 记录文本回复 / Record text replies."""

    def __init__(self) -> None:
        """@brief 初始化回复记录 / Initialize reply records.

        @return None / None.
        """

        self.replies: list[tuple[str, dict[str, object]]] = []

    async def reply_text(self, text: str, **kwargs: object) -> None:
        """@brief 记录一次回复 / Record one reply.

        @param text 回复文本 / Reply text.
        @param kwargs Telegram 回复选项 / Telegram reply options.
        @return None / None.
        """

        self.replies.append((text, kwargs))


class _ReplayableToggle:
    """@brief 模拟首次提交与同 key 回放 / Simulate first commit and same-key replay."""

    def __init__(self) -> None:
        """@brief 初始化关闭状态 / Initialize a disabled switch.

        @return None / None.
        """

        self.enabled = False
        self.receipts: dict[str, bool] = {}
        self.keys: list[str] = []

    async def toggle(
        self,
        _chat_id: object,
        _actor_id: object,
        *,
        idempotency_key: str,
    ) -> ModerationToggleResult:
        """@brief 切换一次或回放 / Toggle once or replay.

        @param _chat_id 群组 ID / Group ID.
        @param _actor_id 管理员 ID / Administrator ID.
        @param idempotency_key source key / Source key.
        @return 开关结果 / Toggle result.
        """

        self.keys.append(idempotency_key)
        if idempotency_key in self.receipts:
            return ModerationToggleResult(
                enabled=self.receipts[idempotency_key],
                replayed=True,
            )
        self.enabled = not self.enabled
        self.receipts[idempotency_key] = self.enabled
        return ModerationToggleResult(enabled=self.enabled)


class _ReplayableVerification(_ReplayableToggle):
    """@brief 验证服务形状的可回放开关 / Replayable toggle with the verification-service shape."""

    async def group_enabled(self, _chat_id: object) -> bool:
        """@brief 返回当前 policy / Return the current policy.

        @param _chat_id 群组 ID / Group ID.
        @return 当前状态 / Current state.
        """

        return self.enabled

    async def toggle_group(
        self,
        chat_id: object,
        *,
        group_name: str,
        actor_id: object,
        idempotency_key: str,
    ) -> ModerationToggleResult:
        """@brief 忽略展示名并复用开关逻辑 / Ignore display name and reuse toggle logic.

        @param chat_id 群组 ID / Group ID.
        @param group_name 群组名 / Group name.
        @param actor_id 管理员 ID / Administrator ID.
        @param idempotency_key source key / Source key.
        @return 开关结果 / Toggle result.
        """

        del group_name
        return await self.toggle(chat_id, actor_id, idempotency_key=idempotency_key)


def test_spam_handler_replays_the_first_enabled_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 同一 /spam Update 不会第二次反转 / The same /spam Update does not reverse twice."""

    commands = _ReplayableToggle()
    message = _Message()
    update = SimpleNamespace(
        update_id=701,
        effective_message=message,
        effective_chat=SimpleNamespace(id=-1001, type=ChatType.GROUP),
        effective_user=SimpleNamespace(id=42),
    )
    context = SimpleNamespace(args=[], bot=SimpleNamespace(id=99))

    async def allowed(*_args: object) -> bool:
        """@brief 允许权限检查 / Allow a permission check.

        @param _args 忽略参数 / Ignored arguments.
        @return True / True.
        """

        return True

    monkeypatch.setattr(
        moderation_handlers,
        "_capability",
        lambda _context: SimpleNamespace(commands=commands),
    )
    monkeypatch.setattr(moderation_handlers, "_is_administrator", allowed)
    monkeypatch.setattr(moderation_handlers, "_bot_can_delete", allowed)

    async def scenario() -> None:
        """@brief 投递同一 Update 两次 / Deliver the same Update twice.

        @return None / None.
        """

        await moderation_handlers.toggle_spam_control(update, context)  # type: ignore[arg-type]
        await moderation_handlers.toggle_spam_control(update, context)  # type: ignore[arg-type]

    asyncio.run(scenario())
    assert commands.enabled is True
    assert commands.keys == [
        "telegram-update:701:moderation.spam-toggle",
        "telegram-update:701:moderation.spam-toggle",
    ]
    assert len(message.replies) == 2
    assert all("***开启***" in text for text, _kwargs in message.replies)


def test_verification_handler_replays_the_first_enabled_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 同一 /verify Update 不会第二次关闭 / The same /verify Update does not disable on replay."""

    service = _ReplayableVerification()
    message = _Message()
    update = SimpleNamespace(
        update_id=702,
        effective_message=message,
        effective_chat=SimpleNamespace(id=-1002, type="group", title="Test Group"),
        effective_user=SimpleNamespace(id=43),
    )

    class _Bot:
        """@brief 返回管理员身份 / Return administrator membership."""

        async def get_chat_member(self, _chat_id: int, _user_id: int) -> Any:
            """@brief 返回管理员 / Return an administrator.

            @param _chat_id 群组 ID / Group ID.
            @param _user_id 用户 ID / User ID.
            @return member DTO / Member DTO.
            """

            return SimpleNamespace(status=ChatMemberStatus.ADMINISTRATOR)

    context = SimpleNamespace(bot=_Bot())

    async def permitted(_bot: object, _chat_id: int) -> tuple[bool, str]:
        """@brief 允许 Bot 权限 / Allow Bot permissions.

        @param _bot Bot / Bot.
        @param _chat_id 群组 ID / Group ID.
        @return 允许结果 / Allowed result.
        """

        return True, "ok"

    monkeypatch.setattr(verification_handlers, "_service", lambda _context: service)
    monkeypatch.setattr(verification_handlers, "check_bot_permissions", permitted)

    async def scenario() -> None:
        """@brief 投递同一 Update 两次 / Deliver the same Update twice.

        @return None / None.
        """

        await verification_handlers.verify_command(update, context)  # type: ignore[arg-type]
        await verification_handlers.verify_command(update, context)  # type: ignore[arg-type]

    asyncio.run(scenario())
    assert service.enabled is True
    assert service.keys == [
        "telegram-update:702:moderation.verification-toggle",
        "telegram-update:702:moderation.verification-toggle",
    ]
    assert len(message.replies) == 2
    assert all("验证功能已开启" in text for text, _kwargs in message.replies)
