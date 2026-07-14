"""@brief Admin durable Telegram 命令与公告出站渲染 / Durable Admin Telegram commands and announcement outbound rendering."""

from __future__ import annotations

from fogmoe_bot.application.admin.models import (
    AdminCode,
    AdminStats,
    AnnouncementRequestResult,
    RequestAnnouncement,
)
from fogmoe_bot.application.admin.service import AdminService
from fogmoe_bot.application.conversation.standalone_outbound import (
    StandaloneOutboundCapability,
    StandaloneOutboundCommand,
)
from fogmoe_bot.domain.admin import (
    AnnouncementRecipientClaim,
    AnnouncementRecipientKind,
)
from fogmoe_bot.domain.conversation.identity import ConversationId
from fogmoe_bot.domain.conversation.inbox import InboundUpdate
from fogmoe_bot.domain.conversation.outbox import SEND_TELEGRAM_MESSAGE

from .command_cooldown_guard import ParsedTelegramCommand
from .delivery import delivery_stream_for_chat


_PERMISSION_DENIED_TEXT = (
    "您没有权限执行此操作。\nYou do not have permission to perform this operation."
)
"""@brief 不泄露系统状态的固定拒绝文案 / Fixed denial copy that discloses no system state."""

_INVALID_ARGUMENT_TEXT = (
    "命令参数无效，请检查数值范围。\n"
    "Invalid command arguments; please check the accepted range."
)
"""@brief 固定参数错误文案 / Fixed invalid-argument copy."""


class AdminTelegramCommandHandler:
    """@brief 将 Admin durable 命令映射到类型化服务和 outbox / Map durable Admin commands to the typed service and outbox."""

    def __init__(
        self,
        *,
        service: AdminService,
        outbound: StandaloneOutboundCapability,
    ) -> None:
        """@brief 注入 AdminService 与共享 outbox / Inject AdminService and the shared outbox.

        @param service 唯一授权与用例边界 / Sole authorization and use-case boundary.
        @param outbound standalone outbox 能力 / Standalone-outbox capability.
        """

        self._service = service
        self._outbound = outbound

    @property
    def commands(self) -> frozenset[str]:
        """@brief 返回 Admin 命令所有权 / Return Admin command ownership.

        @return admin_announce、stats 与 logs / admin_announce, stats, and logs.
        """

        return frozenset({"admin_announce", "stats", "logs"})

    async def handle(
        self,
        update: InboundUpdate,
        command: ParsedTelegramCommand,
    ) -> None:
        """@brief 执行唯一命令状态转移并幂等回复 / Execute the sole command state transition and idempotently reply.

        @param update 已持久化 Update / Persisted Update.
        @param command 规范命令 envelope / Canonical command envelope.
        @return None / None.
        """

        if command.command == "stats":
            text = await self._statistics(command)
        elif command.command == "logs":
            text = await self._logs(command)
        elif command.command == "admin_announce":
            text = await self._announcement(update, command)
        else:
            raise ValueError("Admin handler received an unowned command")
        await _reply(self._outbound, update, command, text)

    async def _statistics(self, command: ParsedTelegramCommand) -> str:
        """@brief 读取并渲染统计 / Read and render statistics.

        @param command stats 命令 / stats command.
        @return 用户文本 / User-facing text.
        """

        limit = _single_bounded_integer(command.arguments, default=20, maximum=50)
        result = await self._service.statistics(
            actor_id=command.user_id,
            group_limit=limit,
        )
        if result.code is AdminCode.PERMISSION_DENIED:
            return _PERMISSION_DENIED_TEXT
        if result.code is AdminCode.INVALID_REQUEST or result.stats is None:
            return _INVALID_ARGUMENT_TEXT
        return _stats_text(result.stats)

    async def _logs(self, command: ParsedTelegramCommand) -> str:
        """@brief 读取并渲染有界日志尾部 / Read and render a bounded log tail.

        @param command logs 命令 / logs command.
        @return 用户文本 / User-facing text.
        """

        lines = _single_bounded_integer(command.arguments, default=50, maximum=200)
        result = await self._service.log_tail(
            actor_id=command.user_id,
            lines=lines,
        )
        if result.code is AdminCode.PERMISSION_DENIED:
            return _PERMISSION_DENIED_TEXT
        if result.code is AdminCode.INVALID_REQUEST:
            return _INVALID_ARGUMENT_TEXT
        if result.code is AdminCode.NOT_FOUND:
            return "日志文件不存在。\nThe log file does not exist."
        if result.tail is None:
            return "日志暂时不可用。\nLogs are temporarily unavailable."
        return _log_text(result.tail.lines, truncated=result.tail.truncated)

    async def _announcement(
        self,
        update: InboundUpdate,
        command: ParsedTelegramCommand,
    ) -> str:
        """@brief 持久化公告意图并立即返回受众快照数 / Persist an announcement intent and immediately return its audience-snapshot count.

        @param update 来源 durable Update / Source durable Update.
        @param command admin_announce 命令 / admin_announce command.
        @return 用户文本 / User-facing text.
        """

        result = await self._service.request_announcement(
            RequestAnnouncement(
                actor_id=command.user_id,
                source_update_id=int(update.update_id),
                idempotency_key=(
                    f"telegram:admin-announcement:{int(update.update_id)}"
                ),
                body=command.argument_text,
                reply_chat_id=command.chat_id,
                reply_message_id=command.message_id,
                reply_message_thread_id=command.message_thread_id,
                requested_at=update.received_at,
            )
        )
        return _announcement_acceptance_text(result)


class TelegramAnnouncementOutboundFactory:
    """@brief 将公告回执渲染为 Telegram standalone outbox 命令 / Render announcement receipts as Telegram standalone-outbox commands."""

    def build(self, claim: AnnouncementRecipientClaim) -> StandaloneOutboundCommand:
        """@brief 为受众或终态报告构造确定性出站 / Build deterministic outbound data for an audience member or terminal report.

        @param claim 公告回执领取 / Announcement receipt claim.
        @return Telegram standalone outbox 命令 / Telegram standalone-outbox command.
        """

        conversation_id = ConversationId(f"admin-announcement:{claim.announcement_id}")
        if claim.recipient_kind is AnnouncementRecipientKind.USER:
            text = f"📢 公告 Announcement\n\n{claim.body}"
        elif claim.recipient_kind is AnnouncementRecipientKind.GROUP:
            text = f"📢 群组公告 Group Announcement\n\n{claim.body}"
        else:
            text = (
                "📢 公告投递完成 Announcement delivery completed\n\n"
                f"受众 Audience: {claim.recipient_count}\n"
                f"成功 Delivered: {claim.delivered_count}\n"
                f"失败 Failed: {claim.failed_count}"
            )
        return StandaloneOutboundCommand(
            conversation_id=conversation_id,
            delivery_stream_id=delivery_stream_for_chat(
                claim.chat_id,
                claim.message_thread_id,
            ),
            kind=SEND_TELEGRAM_MESSAGE,
            payload={
                "chat_id": claim.chat_id,
                "text": text,
                "message_thread_id": claim.message_thread_id,
                "reply_to_message_id": claim.reply_to_message_id,
                "disable_web_page_preview": True,
            },
            idempotency_key=(f"recipient:{claim.recipient_kind.value}:{claim.chat_id}"),
            created_at=claim.announcement_created_at,
        )


def _single_bounded_integer(
    arguments: tuple[str, ...],
    *,
    default: int,
    maximum: int,
) -> int:
    """@brief 解析单个有界正整数，非法时返回零 / Parse one bounded positive integer, returning zero when invalid.

    @param arguments 命令参数 / Command arguments.
    @param default 无参数默认值 / Default without arguments.
    @param maximum 上限 / Upper bound.
    @return 有效值或零 / Valid value or zero.
    """

    if not arguments:
        return default
    if len(arguments) != 1 or not arguments[0].isdigit():
        return 0
    value = int(arguments[0])
    return value if 1 <= value <= maximum else 0


def _stats_text(stats: AdminStats) -> str:
    """@brief 渲染强类型统计 / Render strongly typed statistics.

    @param stats 统计快照 / Statistics snapshot.
    @return 最多 4096 字符的纯文本 / Plain text within 4096 characters.
    """

    recent = (
        "\n".join(
            f"- ID: {user.user_id}, Name: {user.name}" for user in stats.recent_users
        )
        or "无"
    )
    text = (
        "🤖 机器人统计 Bot statistics\n\n"
        f"👤 总用户数: {stats.user_count}\n"
        f"💬 配置关键词群组: {stats.keywords.count}\n"
        f"✅ 启用验证群组: {stats.verification.count}\n"
        f"🛡️ 启用垃圾控制群组: {stats.spam_control.count}\n"
        f"📈 配置图表群组: {stats.charts.count}\n\n"
        "最近用户 Recent users:\n"
        f"{recent}\n\n"
        f"💬 关键词群组: {_ids(stats.keywords.group_ids)}\n"
        f"✅ 验证群组: {_ids(stats.verification.group_ids)}\n"
        f"🛡️ 垃圾控制群组: {_ids(stats.spam_control.group_ids)}\n"
        f"📈 图表群组: {_ids(stats.charts.group_ids)}"
    )
    return _clean(text)[:4000]


def _ids(values: tuple[int, ...]) -> str:
    """@brief 渲染 ID 样本 / Render an ID sample.

    @param values ID 元组 / ID tuple.
    @return 逗号分隔文本或“无” / Comma-separated text or "none".
    """

    return ", ".join(str(value) for value in values) or "无"


def _log_text(lines: tuple[str, ...], *, truncated: bool) -> str:
    """@brief 渲染有界日志，保留最新字符 / Render bounded logs while preserving the newest characters.

    @param lines 日志行 / Log lines.
    @param truncated 字节边界是否截断更早内容 / Whether the byte bound dropped older content.
    @return Telegram-safe 纯文本 / Telegram-safe plain text.
    """

    content = _clean("".join(lines))
    clipped = len(content) > 3500
    if clipped:
        content = content[-3500:]
    warning = (
        "⚠️ 较早内容已截断 Older content was truncated\n" if truncated or clipped else ""
    )
    return f"📋 最近日志 Recent logs\n\n{warning}{content or '(空 empty)'}"


def _announcement_acceptance_text(result: AnnouncementRequestResult) -> str:
    """@brief 渲染公告接收结果 / Render announcement acceptance.

    @param result 类型化结果 / Typed result.
    @return 用户文本 / User-facing text.
    """

    if result.code is AdminCode.PERMISSION_DENIED:
        return _PERMISSION_DENIED_TEXT
    if result.code is AdminCode.INVALID_REQUEST:
        return (
            "请在命令后输入 1–3500 个字符的公告内容。\n"
            "Enter 1–3500 characters after /admin_announce."
        )
    replay = "（幂等重放 replay）" if result.code is AdminCode.REPLAYED else ""
    return (
        f"公告已进入持久化投递队列{replay}。\n"
        f"Announcement queued durably for {result.recipient_count} recipients.\n"
        "投递全部进入终态后会发送完成报告。"
    )


async def _reply(
    outbound: StandaloneOutboundCapability,
    update: InboundUpdate,
    command: ParsedTelegramCommand,
    text: str,
) -> None:
    """@brief 幂等写入 Admin 命令回复 / Idempotently write an Admin command response.

    @param outbound standalone outbox 能力 / Standalone-outbox capability.
    @param update 来源 durable Update / Source durable Update.
    @param command 命令 envelope / Command envelope.
    @param text 用户文本 / User-facing text.
    @return None / None.
    """

    await outbound.enqueue(
        StandaloneOutboundCommand(
            conversation_id=update.conversation_id,
            delivery_stream_id=delivery_stream_for_chat(
                command.chat_id,
                command.message_thread_id,
            ),
            kind=SEND_TELEGRAM_MESSAGE,
            payload={
                "chat_id": command.chat_id,
                "text": _clean(text)[:4000],
                "message_thread_id": command.message_thread_id,
                "reply_to_message_id": command.message_id,
                "disable_web_page_preview": True,
            },
            idempotency_key=(
                f"update:{int(update.update_id)}:command:{command.command}:response"
            ),
            created_at=update.received_at,
        )
    )


def _clean(value: str) -> str:
    """@brief 删除 Telegram 不接受的 NUL / Remove NUL characters rejected by Telegram.

    @param value 原始文本 / Raw text.
    @return 安全纯文本 / Safe plain text.
    """

    return value.replace("\x00", "�")


__all__ = [
    "AdminTelegramCommandHandler",
    "TelegramAnnouncementOutboundFactory",
]
