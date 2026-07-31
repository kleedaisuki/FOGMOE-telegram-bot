"""@brief Admin bounded context 的类型化端口 / Typed ports for the Admin bounded context."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Protocol

from fogmoe_bot.application.conversation.standalone_outbound import (
    StandaloneOutboundCommand,
)
from fogmoe_bot.domain.admin.recipient import (
    AnnouncementRecipientClaim,
    AnnouncementRecipientDeadLettered,
    AnnouncementRecipientExpanded,
    AnnouncementRecipientRetryScheduled,
)
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

    async def persist_expanded(
        self,
        decision: AnnouncementRecipientExpanded,
    ) -> bool:
        """@brief 持久化 token-fenced expanded 决策 / Persist a token-fenced expanded decision.

        @param decision 领域 expanded 决策 / Domain expanded decision.
        @return token 仍有效且终结成功时为 True / True when the token was current and finalization succeeded.
        """

        ...

    async def persist_retry(
        self,
        decision: AnnouncementRecipientRetryScheduled,
    ) -> bool:
        """@brief 持久化 token-fenced retry-wait 决策 / Persist a token-fenced retry-wait decision.

        @param decision 领域 retry-wait 决策 / Domain retry-wait decision.
        @return token 仍有效时为 True / True when the token was current.
        """

        ...

    async def persist_dead_letter(
        self,
        decision: AnnouncementRecipientDeadLettered,
    ) -> bool:
        """@brief 持久化 token-fenced failed-final 决策 / Persist a token-fenced failed-final decision.

        @param decision 领域 failed-final 决策 / Domain failed-final decision.
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
