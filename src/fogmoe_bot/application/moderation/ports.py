"""@brief 治理应用层端口 / Moderation application-layer ports."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from fogmoe_bot.domain.conversation.inbox import InboundUpdate
from fogmoe_bot.domain.moderation.aggregate import GroupModeration
from fogmoe_bot.domain.moderation.effects import (
    KeywordReplyPlan,
    ModerationEffect,
    ModerationEffectId,
    ModerationEffectPlan,
    SpamEnforcementPlan,
)
from fogmoe_bot.domain.moderation.models import (
    ChatId,
    ModerationRequest,
    ModerationToggleResult,
)
from fogmoe_bot.domain.moderation.reporting import (
    ReportDeliveryResult,
    ReportKey,
    ReportRegistration,
    ReportRequest,
)


class GroupModerationRepository(Protocol):
    """@brief 群组治理聚合仓储端口 / Group-moderation aggregate repository port."""

    async def load_group(self, chat_id: ChatId) -> GroupModeration:
        """@brief 读取聚合 / Load an aggregate.

        @param chat_id 群组 ID / Group identifier.
        @return 当前聚合或空聚合 / Current or empty aggregate.
        """

        ...

    async def toggle_group(
        self,
        chat_id: ChatId,
        *,
        actor_id: int,
        idempotency_key: str,
    ) -> ModerationToggleResult:
        """@brief 原子切换 policy 并保存 source receipt / Atomically toggle policy and save the source receipt.

        @param chat_id 群组 ID / Group identifier.
        @param actor_id 管理员 ID / Administrator identifier.
        @param idempotency_key source Update 稳定键 / Stable source-Update key.
        @return 首次提交或回放结果 / First committed or replayed result.
        """

        ...

    async def save_group(
        self,
        aggregate: GroupModeration,
        *,
        expected_version: int,
        actor_id: int,
    ) -> None:
        """@brief 以 OCC 原子保存聚合 / Atomically save an aggregate with OCC.

        @param aggregate 新聚合 / New aggregate.
        @param expected_version 调用方观察版本 / Caller-observed version.
        @param actor_id 审计操作者 ID / Audited actor identifier.
        @return None / None.
        """

        ...


class ModerationEffectRepository(Protocol):
    """@brief 治理副作用与警告窗口仓储端口 / Moderation-effect and warning-window repository port."""

    async def load_effect(
        self,
        effect_id: ModerationEffectId,
    ) -> ModerationEffect | None:
        """@brief 读取副作用聚合 / Load an effect aggregate.

        @param effect_id 副作用 ID / Effect identifier.
        @return 聚合或 None / Aggregate or None.
        """

        ...

    async def reserve_effect(
        self,
        plan: ModerationEffectPlan,
        *,
        now: datetime,
        warning_window: timedelta,
    ) -> ModerationEffect:
        """@brief 幂等持久化效果，并为垃圾处置原子计数警告 / Idempotently persist an effect and atomically count spam warnings.

        @param plan 副作用意图 / Effect intent.
        @param now 当前时刻 / Current instant.
        @param warning_window 警告窗口 / Warning window.
        @return 已存在或新建的效果聚合 / Existing or newly created effect aggregate.
        """

        ...

    async def save_effect(
        self,
        effect: ModerationEffect,
        *,
        expected_version: int,
    ) -> None:
        """@brief 以 OCC 保存副作用进度 / Save effect progress with OCC.

        @param effect 新效果聚合 / New effect aggregate.
        @param expected_version 调用方观察版本 / Caller-observed version.
        @return None / None.
        """

        ...


class ReportRepository(Protocol):
    """@brief 举报幂等登记仓储 / Report idempotency repository."""

    async def register_report(
        self,
        key: ReportKey,
        *,
        now: datetime,
        deduplication_window: timedelta,
    ) -> ReportRegistration:
        """@brief 持久化举报 / Persist a report.

        @param key 举报幂等键 / Report idempotency key.
        @param now 登记时刻 / Registration instant.
        @param deduplication_window 同一用户重复举报窗口 / Same-user duplicate-report window.
        @return 接受或重复 / Accepted or duplicate.
        """

        ...


class ModerationIngressMapper(Protocol):
    """@brief 持久化 Update 到治理输入的适配端口 / Adapter port from persisted Updates to moderation inputs."""

    async def moderation_request(
        self,
        update: InboundUpdate,
    ) -> ModerationRequest | None:
        """@brief 映射垃圾审核请求 / Map a spam-moderation request.

        @param update durable Update / Durable Update.
        @return 群消息审核请求，或 None / Group-message request, or None.
        """

        ...

    def keyword_request(self, update: InboundUpdate) -> ModerationRequest | None:
        """@brief 映射关键词观察输入 / Map keyword-observer input.

        @param update durable Update / Durable Update.
        @return 非命令群文本请求，或 None / Non-command group-text request, or None.
        """

        ...


class ModerationEffectSink(Protocol):
    """@brief 外部治理副作用端口 / External moderation-effect port."""

    async def delete_spam(self, plan: SpamEnforcementPlan) -> None:
        """@brief 删除命中消息 / Delete a matched message.

        @param plan 垃圾处置意图 / Spam-enforcement intent.
        @return None / None.
        """

        ...

    async def send_spam_warning(
        self,
        plan: SpamEnforcementPlan,
        *,
        warning_count: int,
    ) -> None:
        """@brief 发送警告 / Send a warning.

        @param plan 垃圾处置意图 / Spam-enforcement intent.
        @param warning_count 当前窗口警告序号 / Warning ordinal in the current window.
        @return None / None.
        """

        ...

    async def send_keyword_reply(self, plan: KeywordReplyPlan) -> None:
        """@brief 发送关键词回复 / Send a keyword reply.

        @param plan 关键词回复意图 / Keyword-reply intent.
        @return None / None.
        """

        ...


class ReportDelivery(Protocol):
    """@brief 举报通知投递端口 / Report-notification delivery port."""

    async def deliver(self, request: ReportRequest) -> ReportDeliveryResult:
        """@brief 通知群管理员 / Notify group administrators.

        @param request 举报请求 / Report request.
        @return 投递统计 / Delivery statistics.
        """

        ...


__all__ = [
    "GroupModerationRepository",
    "ModerationEffectRepository",
    "ModerationEffectSink",
    "ModerationIngressMapper",
    "ReportDelivery",
    "ReportRepository",
]
