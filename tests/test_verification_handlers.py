"""@brief 成员验证 Telegram 薄适配器测试 / Tests for the thin member-verification Telegram adapter."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from telegram import ChatPermissions, User
from telegram.error import BadRequest

from fogmoe_bot.application.moderation.verification_service import (
    VerificationRejected,
    VerificationRejectionCode,
)
from fogmoe_bot.domain.moderation.models import ChatId, MessageId, UserId
from fogmoe_bot.domain.moderation.verification import (
    VerificationEvent,
    VerificationKey,
    VerificationTask,
    VerificationVersion,
    hash_verification_token,
)
from fogmoe_bot.presentation.telegram import verification_handlers
from fogmoe_bot.presentation.telegram.verification_handlers import (
    TelegramVerificationDelivery,
    VerificationCallback,
    verify_callback,
)

NOW = datetime(2030, 1, 1, tzinfo=UTC)
"""@brief 固定测试时间 / Fixed test time."""

PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""@brief 项目根目录 / Project root."""


def _pending() -> VerificationTask:
    """@brief 创建 PENDING 聚合 / Build a PENDING aggregate.

    @return PENDING 聚合 / PENDING aggregate.
    """

    creating = VerificationTask(
        key=VerificationKey(ChatId(-1001), UserId(42)),
        version=VerificationVersion(0),
        token_hash=hash_verification_token("secret"),
        member_name="Alice Example",
        expires_at=NOW + timedelta(minutes=5),
    )
    return creating.evolve(
        VerificationEvent.ACTIVATE,
        expected_version=creating.version,
        now=NOW,
        message_id=MessageId(7),
    )


class FakeBot:
    """@brief 记录 Telegram 权限与消息副作用 / Record Telegram permission and message effects."""

    def __init__(self) -> None:
        """@brief 初始化调用记录 / Initialize call recording.

        @return None / None.
        """

        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        """@brief 调用记录 / Call records."""

    async def get_chat(self, chat_id: int) -> Any:
        """@brief 返回带默认权限的群组 / Return a chat with default permissions.

        @param chat_id 群组 ID / Chat ID.
        @return 群组 DTO / Chat DTO.
        """

        self.calls.append(("get_chat", (chat_id,), {}))
        return SimpleNamespace(permissions=ChatPermissions(can_send_messages=True))

    async def restrict_chat_member(self, *args: Any, **kwargs: Any) -> None:
        """@brief 记录权限恢复 / Record permission restoration.

        @param args 位置参数 / Positional arguments.
        @param kwargs 关键字参数 / Keyword arguments.
        @return None / None.
        """

        self.calls.append(("restrict", args, kwargs))

    async def ban_chat_member(self, *args: Any, **kwargs: Any) -> None:
        """@brief 记录封禁 / Record ban.

        @param args 位置参数 / Positional arguments.
        @param kwargs 关键字参数 / Keyword arguments.
        @return None / None.
        """

        self.calls.append(("ban", args, kwargs))

    async def unban_chat_member(self, *args: Any, **kwargs: Any) -> None:
        """@brief 记录解封 / Record unban.

        @param args 位置参数 / Positional arguments.
        @param kwargs 关键字参数 / Keyword arguments.
        @return None / None.
        """

        self.calls.append(("unban", args, kwargs))

    async def edit_message_text(self, *args: Any, **kwargs: Any) -> None:
        """@brief 记录消息编辑 / Record message edit.

        @param args 位置参数 / Positional arguments.
        @param kwargs 关键字参数 / Keyword arguments.
        @return None / None.
        """

        self.calls.append(("edit", args, kwargs))


class FakeQuery:
    """@brief 记录 callback answer / Record callback answers."""

    def __init__(self, data: str, user_id: int) -> None:
        """@brief 创建查询 / Create query.

        @param data callback_data / callback_data.
        @param user_id 点击用户 / Clicking user.
        @return None / None.
        """

        self.data = data
        """@brief callback_data / callback_data."""
        self.from_user = User(id=user_id, first_name="Tester", is_bot=False)
        """@brief 点击用户 / Clicking user."""
        self.answers: list[tuple[str | None, bool]] = []
        """@brief answers / Answers."""

    async def answer(
        self, text: str | None = None, *, show_alert: bool = False
    ) -> None:
        """@brief 记录 answer / Record answer.

        @param text 文本 / Text.
        @param show_alert 是否弹窗 / Whether alert.
        @return None / None.
        """

        self.answers.append((text, show_alert))


class MissingMessageBot(FakeBot):
    """@brief 模拟欢迎消息已被删除 / Simulate an already-deleted welcome message."""

    async def edit_message_text(self, *args: Any, **kwargs: Any) -> None:
        """@brief 以永久 BadRequest 拒绝编辑 / Reject editing with a permanent BadRequest.

        @param args 位置参数 / Positional arguments.
        @param kwargs 关键字参数 / Keyword arguments.
        @return 不返回 / Does not return.
        @raises BadRequest 消息不存在 / The message no longer exists.
        """

        self.calls.append(("edit", args, kwargs))
        raise BadRequest("Message to edit not found")


def test_callback_codec_binds_member_version_and_token_within_64_bytes() -> None:
    """@brief callback 绑定成员、版本和 token 且符合 64 字节限制 / Callback binds member, version, and token within 64 bytes."""

    callback = VerificationCallback(
        UserId(123456), VerificationVersion(7), "abcdef0123456789"
    )
    encoded = callback.encode()

    assert encoded == "verify:123456:7:abcdef0123456789"
    assert len(encoded.encode("utf-8")) <= 64
    assert VerificationCallback.decode(encoded) == callback


def test_wrong_user_keeps_the_original_product_rejection_text() -> None:
    """@brief 非目标用户点击时保留旧产品拒绝文案 / Wrong-user click preserves the original rejection copy."""

    async def scenario() -> None:
        """@brief 驱动非目标 callback / Drive a wrong-user callback.

        @return None / None.
        """

        query = FakeQuery(
            VerificationCallback(UserId(42), VerificationVersion(1), "secret").encode(),
            user_id=99,
        )
        update = SimpleNamespace(
            callback_query=query,
            effective_chat=SimpleNamespace(id=-1001),
        )
        await verify_callback(update, SimpleNamespace())  # type: ignore[arg-type]

        assert query.answers == [("这不是为您准备的验证按钮。", True)]

    asyncio.run(scenario())


def test_delivery_preserves_pass_timeout_and_member_left_messages() -> None:
    """@brief 投递保留通过、超时和离群文案及权限行为 / Delivery preserves pass, timeout, and member-left copy and permission behavior."""

    async def scenario() -> None:
        """@brief 投递三种过渡态 / Deliver all three transitional states.

        @return None / None.
        """

        pending = _pending()
        passing = pending.evolve(
            VerificationEvent.PASS_REQUESTED,
            expected_version=pending.version,
            now=NOW,
        )
        expiring = pending.evolve(
            VerificationEvent.DEADLINE_REACHED,
            expected_version=pending.version,
            now=pending.expires_at,
        )
        cancelling = pending.evolve(
            VerificationEvent.MEMBER_LEFT,
            expected_version=pending.version,
            now=NOW,
        )
        bot = FakeBot()
        delivery = TelegramVerificationDelivery(bot)  # type: ignore[arg-type]

        await delivery.deliver(passing)
        await delivery.deliver(expiring)
        await delivery.deliver(cancelling)

        edit_texts = [
            kwargs["text"] for name, _args, kwargs in bot.calls if name == "edit"
        ]
        assert edit_texts == [
            "验证通过，欢迎加入群组！",
            "验证超时，您已被移出群组。",
            "用户 Alice Example 在验证前离开了群组。",
        ]
        assert [name for name, _args, _kwargs in bot.calls].count("restrict") == 1
        assert [name for name, _args, _kwargs in bot.calls].count("ban") == 1
        assert [name for name, _args, _kwargs in bot.calls].count("unban") == 1

    asyncio.run(scenario())


def test_missing_welcome_message_does_not_poison_moderation_effect_retries() -> None:
    """@brief 永久消息编辑错误不会让权限副作用无限重放 / A permanent edit error does not poison permission-effect retries."""

    async def scenario() -> None:
        """@brief 移出超时成员后模拟消息已删除 / Simulate a deleted message after removing an expired member.

        @return None / None.
        """

        pending = _pending()
        expiring = pending.evolve(
            VerificationEvent.DEADLINE_REACHED,
            expected_version=pending.version,
            now=pending.expires_at,
        )
        bot = MissingMessageBot()

        await TelegramVerificationDelivery(bot).deliver(expiring)  # type: ignore[arg-type]

        assert [name for name, _args, _kwargs in bot.calls] == ["ban", "unban", "edit"]

    asyncio.run(scenario())


def test_all_state_rejections_keep_the_original_expired_token_copy() -> None:
    """@brief 所有状态竞争拒绝保留旧失效文案 / All state-race rejections preserve the original expired-token copy."""

    for code in VerificationRejectionCode:
        rejection = VerificationRejected(code)
        assert (
            verification_handlers._rejection_text(rejection)
            == "验证已失效或 token 不正确。"
        )


def test_presentation_adapter_has_no_database_jobqueue_or_process_lock_dependencies() -> (
    None
):
    """@brief Telegram 适配器不依赖数据库、JobQueue 或进程锁 / Telegram adapter has no database, JobQueue, or process-lock dependency."""

    source = Path(verification_handlers.__file__).read_text(encoding="utf-8")
    assert "infrastructure.database" not in source
    assert "JobQueue" not in source
    assert "asyncio.Lock" not in source
    assert "create_task(" not in source
    assert "command_cooldown" not in source


def test_verification_layers_follow_inward_dependency_direction() -> None:
    """@brief 验证领域、应用、基础设施与表示层依赖只能向内 / Verification domain, application, infrastructure, and presentation dependencies point inward."""

    source_root = PROJECT_ROOT / "src/fogmoe_bot"
    domain_source = (source_root / "domain/moderation/verification.py").read_text(
        encoding="utf-8"
    )
    application_source = (
        source_root / "application/moderation/verification_service.py"
    ).read_text(encoding="utf-8")
    repository_source = (
        source_root / "infrastructure/database/moderation/verification.py"
    ).read_text(encoding="utf-8")
    presentation_source = (
        source_root / "presentation/telegram/verification_handlers.py"
    ).read_text(encoding="utf-8")

    assert "fogmoe_bot.application" not in domain_source
    assert "fogmoe_bot.infrastructure" not in domain_source
    assert "fogmoe_bot.presentation" not in domain_source
    assert "fogmoe_bot.infrastructure" not in application_source
    assert "from telegram" not in application_source
    assert "import telegram" not in application_source
    assert "fogmoe_bot.application" not in repository_source
    assert "fogmoe_bot.presentation" not in repository_source
    assert "from telegram" not in repository_source
    assert "fogmoe_bot.infrastructure" not in presentation_source
    assert not (source_root / "application/moderation/member_verify.py").exists()
