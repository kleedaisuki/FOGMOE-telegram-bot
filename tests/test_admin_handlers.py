"""@brief Admin durable Telegram handler 和 outbound factory 测试 / Tests for the durable Admin Telegram handler and outbound factory."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid4

from fogmoe_bot.application.admin.models import (
    AdminStats,
    AnnouncementAcceptance,
    GroupFeatureStats,
    LogTail,
    RequestAnnouncement,
)
from fogmoe_bot.application.admin.service import AdminService
from fogmoe_bot.application.banking.models import (
    BankCode,
    ListPendingTokenRequests,
    PendingTokenRequestsResult,
    RequestTokens,
)
from fogmoe_bot.application.banking.ports import BankOperations
from fogmoe_bot.application.banking.service import BankService
from fogmoe_bot.application.conversation.standalone_outbound import (
    StandaloneOutboundCommand,
)
from fogmoe_bot.domain.admin import (
    AnnouncementId,
    AnnouncementRecipientClaim,
    AnnouncementRecipientKind,
)
from fogmoe_bot.domain.banking.money import TokenAmount
from fogmoe_bot.domain.banking.requests import TokenRequest
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    UpdateId,
)
from fogmoe_bot.domain.conversation.inbox import InboundUpdate
from fogmoe_bot.presentation.telegram.admin_handlers import (
    AdminTelegramCommandHandler,
    TelegramAnnouncementOutboundFactory,
)
from fogmoe_bot.presentation.telegram.command_cooldown_guard import (
    ParsedTelegramCommand,
)


NOW = datetime(2030, 1, 1, tzinfo=UTC)
"""@brief 固定 Update 时间 / Fixed Update instant."""


class StaticStats:
    """@brief 固定统计投影 / Static statistics projection."""

    async def fetch(self, *, group_limit: int) -> AdminStats:
        """@brief 返回固定快照 / Return a fixed snapshot.

        @param group_limit 样本上限 / Sample limit.
        @return 统计快照 / Statistics snapshot.
        """

        del group_limit
        empty = GroupFeatureStats(0, ())
        return AdminStats(1, empty, empty, empty, empty, ())


class StaticLogs:
    """@brief 固定日志源 / Static log source."""

    async def tail(self, *, lines: int) -> LogTail | None:
        """@brief 返回包含敏感异常文本的测试日志 / Return test logs containing sensitive exception text.

        @param lines 行数 / Line count.
        @return 日志快照 / Log snapshot.
        """

        del lines
        return LogTail(("internal stack line\n",), False)


class RecordingAnnouncements:
    """@brief 记录公告命令并模拟重放 / Record announcement commands and simulate replay."""

    def __init__(self) -> None:
        """@brief 初始化记录 / Initialize recordings."""

        self.commands: list[RequestAnnouncement] = []
        """@brief 公告命令 / Announcement commands."""

    async def accept(self, command: RequestAnnouncement) -> AnnouncementAcceptance:
        """@brief 记录并以次数模拟 inserted/replayed / Record and simulate inserted/replayed from call count.

        @param command 公告命令 / Announcement command.
        @return 接收回执 / Acceptance receipt.
        """

        self.commands.append(command)
        return AnnouncementAcceptance(
            AnnouncementId.for_idempotency_key(command.idempotency_key),
            4,
            len(self.commands) == 1,
        )


class RecordingOutbound:
    """@brief 记录 standalone outbox 命令 / Record standalone-outbox commands."""

    def __init__(self) -> None:
        """@brief 初始化记录 / Initialize recordings."""

        self.commands: list[StandaloneOutboundCommand] = []
        """@brief 出站命令 / Outbound commands."""

    async def enqueue(self, command: StandaloneOutboundCommand) -> None:
        """@brief 记录命令 / Record a command.

        @param command 出站命令 / Outbound command.
        @return None / None.
        """

        self.commands.append(command)


class StaticBankOperations:
    """@brief 仅记录管理员待审读取的银行端口替身 / Bank-port double recording only administrator pending reads."""

    def __init__(self, requests: tuple[TokenRequest, ...] = ()) -> None:
        """@brief 注入固定待审请求 / Inject fixed pending requests.

        @param requests 固定待审申请 / Fixed pending token requests.
        """

        self.requests = requests
        """@brief 将被返回的待审申请 / Pending requests to return."""
        self.pending_commands: list[ListPendingTokenRequests] = []
        """@brief 已调用的待审读取 / Recorded pending-read commands."""

    async def list_pending_token_requests(
        self,
        command: ListPendingTokenRequests,
    ) -> PendingTokenRequestsResult:
        """@brief 记录并返回固定队列 / Record and return the fixed queue.

        @param command 管理员队列读取 / Administrator queue read.
        @return 成功待审请求结果 / Successful pending-request result.
        """

        self.pending_commands.append(command)
        return PendingTokenRequestsResult(BankCode.SUCCESS, self.requests)


def _service(announcements: RecordingAnnouncements) -> AdminService:
    """@brief 构造 AdminService / Build AdminService.

    @param announcements 公告端口 / Announcement port.
    @return AdminService / AdminService.
    """

    return AdminService(
        administrator_id=42,
        stats=StaticStats(),
        logs=StaticLogs(),
        announcements=announcements,  # type: ignore[arg-type]
    )


def _bank(operations: StaticBankOperations | None = None) -> BankService:
    """@brief 构造仅供控制台测试的银行服务 / Build a bank service for dashboard tests.

    @param operations 可选的待审读取替身 / Optional pending-read double.
    @return 使用管理员 42 的银行服务 / Bank service using administrator 42.
    """

    return BankService(
        cast(BankOperations, operations or StaticBankOperations()),
        administrator_id=42,
    )


def _update(update_id: int) -> InboundUpdate:
    """@brief 构造 durable Update / Build a durable Update.

    @param update_id Update ID / Update ID.
    @return pending Update / Pending Update.
    """

    return InboundUpdate.pending(
        update_id=UpdateId(update_id),
        conversation_id=ConversationId("assistant-user:42"),
        payload={"update_id": update_id},
        received_at=NOW,
    )


def _command(
    name: str,
    *,
    user_id: int = 42,
    argument_text: str = "",
    chat_type: str = "private",
) -> ParsedTelegramCommand:
    """@brief 构造 parsed command / Build a parsed command.

    @param name 命令名 / Command name.
    @param user_id 操作者 ID / Actor ID.
    @param argument_text 参数文本 / Argument text.
    @return 命令 envelope / Command envelope.
    """

    return ParsedTelegramCommand(
        command=name,
        target=None,
        user_id=user_id,
        chat_id=42,
        message_id=9,
        message_thread_id=7,
        username="klee",
        argument_text=argument_text,
        arguments=tuple(argument_text.split()),
        chat_type=chat_type,
    )


def test_announcement_update_replay_reuses_intent_and_response_identity() -> None:
    """@brief 同一 Update 重放得到同一意图和 response identity / Replaying one Update yields the same intent and response identity."""

    announcements = RecordingAnnouncements()
    outbound = RecordingOutbound()
    handler = AdminTelegramCommandHandler(
        service=_service(announcements),
        bank=_bank(),
        outbound=outbound,
    )
    update = _update(17)
    command = _command("admin_announce", argument_text="hello everyone")

    asyncio.run(handler.handle(update, command))
    asyncio.run(handler.handle(update, command))

    assert len(announcements.commands) == 2
    assert announcements.commands[0] == announcements.commands[1]
    assert announcements.commands[0].idempotency_key == (
        "telegram:admin-announcement:17"
    )
    assert len(outbound.commands) == 2
    assert outbound.commands[0].idempotency_key == outbound.commands[1].idempotency_key
    assert "replay" in str(outbound.commands[1].payload["text"])


def test_permission_denial_is_rendered_without_calling_projection() -> None:
    """@brief 权限拒绝由 service 决定并写入 durable 固定文案 / Permission denial is service-decided and written as fixed durable copy."""

    outbound = RecordingOutbound()
    handler = AdminTelegramCommandHandler(
        service=_service(RecordingAnnouncements()),
        bank=_bank(),
        outbound=outbound,
    )

    asyncio.run(handler.handle(_update(18), _command("stats", user_id=7)))

    assert len(outbound.commands) == 1
    text = str(outbound.commands[0].payload["text"])
    assert "没有权限" in text
    assert "internal" not in text.casefold()


def test_private_admin_dashboard_combines_pending_queue_and_next_steps() -> None:
    """@brief `/admin` 并行汇总银行队列并给出可操作的下一步 / `/admin` combines the bank queue and actionable next steps."""

    async def scenario() -> None:
        """@brief 执行管理员工作台场景 / Exercise the administrator-dashboard scenario.

        @return None / None.
        """

        request = RequestTokens(
            user_id=7,
            amount=TokenAmount(12),
            purpose="修复群组灯塔",
            requested_at=NOW,
            idempotency_key="test:admin-dashboard:request",
            request_id=uuid4(),
        ).aggregate()
        operations = StaticBankOperations((request,))
        outbound = RecordingOutbound()
        handler = AdminTelegramCommandHandler(
            service=_service(RecordingAnnouncements()),
            bank=_bank(operations),
            outbound=outbound,
        )

        await handler.handle(_update(23), _command("admin"))

        assert operations.pending_commands == [ListPendingTokenRequests(42, limit=5)]
        text = str(outbound.commands[0].payload["text"])
        assert "管理控制台" in text
        assert str(request.request_id) in text
        assert "用户 7｜12 枚" in text
        assert "/bank_review" in text
        assert "/admin_announce" in text

    asyncio.run(scenario())


def test_admin_dashboard_is_private_and_does_not_read_in_group() -> None:
    """@brief 群聊 `/admin` 被拒绝前不读取管理员或银行数据 / Group `/admin` is rejected before any administrative or bank read."""

    async def scenario() -> None:
        """@brief 执行群聊拒绝场景 / Exercise the group-chat rejection scenario.

        @return None / None.
        """

        operations = StaticBankOperations()
        outbound = RecordingOutbound()
        handler = AdminTelegramCommandHandler(
            service=_service(RecordingAnnouncements()),
            bank=_bank(operations),
            outbound=outbound,
        )

        await handler.handle(
            _update(24),
            _command("admin", chat_type="supergroup"),
        )

        assert operations.pending_commands == []
        assert "仅限私聊" in str(outbound.commands[0].payload["text"])

    asyncio.run(scenario())


def test_completion_factory_reports_terminal_delivery_counts() -> None:
    """@brief 完成回执只在终态计数上渲染 / Completion outbound renders only terminal delivery counts."""

    claim = AnnouncementRecipientClaim(
        announcement_id=AnnouncementId.for_idempotency_key("announcement:done"),
        recipient_kind=AnnouncementRecipientKind.COMPLETION,
        chat_id=42,
        message_thread_id=7,
        reply_to_message_id=9,
        body="secret body",
        recipient_count=4,
        delivered_count=3,
        failed_count=1,
        claim_token=uuid4(),
        attempt_count=1,
        announcement_created_at=NOW,
        claimed_at=NOW,
        lease_expires_at=NOW + timedelta(minutes=1),
    )

    outbound = TelegramAnnouncementOutboundFactory().build(claim)

    assert outbound.idempotency_key == "recipient:completion:42"
    assert outbound.delivery_stream_id.value == "telegram:primary:chat:42:thread:7"
    assert outbound.payload["reply_to_message_id"] == 9
    text = str(outbound.payload["text"])
    assert "Delivered: 3" in text and "Failed: 1" in text
    assert "secret body" not in text
