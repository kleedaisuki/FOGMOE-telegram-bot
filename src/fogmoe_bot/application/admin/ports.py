"""@brief Admin bounded context 的类型化端口 / Typed ports for the Admin bounded context."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Protocol

from fogmoe_bot.application.conversation.standalone_outbound import (
    StandaloneOutboundCommand,
)
from fogmoe_bot.domain.admin import AnnouncementRecipientClaim
from fogmoe_bot.domain.conversation.identity import OutboundMessageId

from .models import AdminStats, AnnouncementAcceptance, LogTail, RequestAnnouncement


class AdminStatsProjection(Protocol):
    """@brief 管理统计的强类型读投影 / Strongly typed read projection for administrative statistics."""

    async def fetch(self, *, group_limit: int) -> AdminStats:
        """@brief 读取一个一致统计快照 / Read one consistent statistics snapshot.

        @param group_limit 每类群组样本上限 / Per-feature group sample limit.
        @return 强类型统计 / Strongly typed statistics.
        """

        ...


class AdminLogSource(Protocol):
    """@brief 异步有界日志读取端口 / Asynchronous bounded log-reading port."""

    async def tail(self, *, lines: int) -> LogTail | None:
        """@brief 读取最后若干行 / Read the last requested lines.

        @param lines 行数上限 / Maximum line count.
        @return 日志快照；源不存在时为 None / Log snapshot, or None when the source is absent.
        """

        ...


class AdminAnnouncementOperations(Protocol):
    """@brief 公告意图、受众快照与租约回执端口 / Port for announcement intents, audience snapshots, and leased receipts."""

    async def accept(self, command: RequestAnnouncement) -> AnnouncementAcceptance:
        """@brief 原子创建意图和受众快照 / Atomically create an intent and audience snapshot.

        @param command 公告命令 / Announcement command.
        @return 规范持久化回执 / Canonical persistence receipt.
        """

        ...

    async def promote_delivery_completions(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> int:
        """@brief 将所有受众 outbox 终态的公告推进到完成回执 / Promote announcements whose audience outboxes are all terminal to completion reporting.

        @param now 当前 UTC 时间 / Current UTC instant.
        @param limit 最大推进数 / Maximum promotions.
        @return 推进的公告数 / Number of promoted announcements.
        """

        ...

    async def claim_ready(
        self,
        *,
        now: datetime,
        lease_for: timedelta,
        limit: int,
    ) -> Sequence[AnnouncementRecipientClaim]:
        """@brief 用 SKIP LOCKED 领取有界出站回执 / Claim a bounded outbound-receipt batch with SKIP LOCKED.

        @param now 当前 UTC 时间 / Current UTC instant.
        @param lease_for 租约时长 / Lease duration.
        @param limit 最大领取数 / Maximum claim count.
        @return 带 fencing token 的领取 / Claims carrying fencing tokens.
        """

        ...

    async def mark_expanded(
        self,
        claim: AnnouncementRecipientClaim,
        *,
        outbound_message_id: OutboundMessageId,
        completed_at: datetime,
    ) -> bool:
        """@brief 用 fencing token 终结出站回执 / Finalize an outbound receipt with its fencing token.

        @param claim 领取凭证 / Claim receipt.
        @param outbound_message_id 确定性 outbox 消息 ID / Deterministic outbox message ID.
        @param completed_at 终结时间 / Completion instant.
        @return token 仍有效且终结成功时为 True / True when the token was current and finalization succeeded.
        """

        ...

    async def schedule_retry(
        self,
        claim: AnnouncementRecipientClaim,
        *,
        retry_at: datetime,
        error_category: str,
    ) -> bool:
        """@brief 用 fencing token 安排重试 / Schedule a retry with the fencing token.

        @param claim 领取凭证 / Claim receipt.
        @param retry_at 下次尝试时间 / Next-attempt instant.
        @param error_category 有界错误分类 / Bounded error category.
        @return token 仍有效时为 True / True when the token was current.
        """

        ...

    async def mark_failed_final(
        self,
        claim: AnnouncementRecipientClaim,
        *,
        failed_at: datetime,
        error_category: str,
    ) -> bool:
        """@brief 用 fencing token 记录最终失败 / Record a final failure with the fencing token.

        @param claim 领取凭证 / Claim receipt.
        @param failed_at 失败时间 / Failure instant.
        @param error_category 有界错误分类 / Bounded error category.
        @return token 仍有效时为 True / True when the token was current.
        """

        ...

    async def recover_expired(self, *, now: datetime, limit: int) -> int:
        """@brief 回收崩溃 worker 留下的过期租约 / Recover expired leases left by crashed workers.

        @param now 当前 UTC 时间 / Current UTC instant.
        @param limit 最大回收数 / Maximum recovery count.
        @return 回收数 / Recovery count.
        """

        ...


class AnnouncementOutboundFactory(Protocol):
    """@brief 将 provider-neutral 公告领取映射到出站意图 / Map provider-neutral announcement claims to outbound intents."""

    def build(self, claim: AnnouncementRecipientClaim) -> StandaloneOutboundCommand:
        """@brief 构造确定性 standalone outbox 命令 / Build a deterministic standalone-outbox command.

        @param claim 已领取回执 / Claimed receipt.
        @return connector 出站命令 / Connector outbound command.
        """

        ...


__all__ = [
    "AdminAnnouncementOperations",
    "AdminLogSource",
    "AdminStatsProjection",
    "AnnouncementOutboundFactory",
]
