"""@brief Telegram 治理 bounded context 组合根 / Telegram moderation bounded-context composition root."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from telegram import Bot

from fogmoe_bot.application.moderation.commands import (
    GroupModerationCommandService,
)
from fogmoe_bot.application.moderation.configuration import (
    BoundedTtlCache,
    GroupModerationConfiguration,
)
from fogmoe_bot.application.moderation.effect_service import ModerationEffectService
from fogmoe_bot.application.moderation.ingress import (
    KeywordAutomationService,
    KeywordIngressObserver,
    ModerationIngressGuard,
)
from fogmoe_bot.application.moderation.rate_windows import (
    CooldownGate,
    FixedWindowGate,
)
from fogmoe_bot.application.moderation.reporting_service import ReportingService
from fogmoe_bot.application.moderation.service import ModerationService
from fogmoe_bot.application.runtime import SystemUtcClock
from fogmoe_bot.domain.moderation.aggregate import GroupModeration
from fogmoe_bot.domain.moderation.models import ChatId
from fogmoe_bot.infrastructure.database.moderation.effects import (
    PostgresModerationEffectRepository,
)
from fogmoe_bot.infrastructure.database.moderation.group import (
    PostgresModerationGroupRepository,
)
from fogmoe_bot.infrastructure.database.moderation.reports import (
    PostgresModerationReportRepository,
)
from fogmoe_bot.infrastructure.database.group_message_projection import (
    PostgresGroupMessageProjection,
)
from fogmoe_bot.infrastructure.moderation.wordlist import FileModerationRuleProvider

from .moderation_adapter import (
    TelegramModerationEffectSink,
    TelegramModerationMapper,
    TelegramReportDelivery,
)
from .group_message_observer import (
    GroupMessageIngressObserver,
    TelegramObserverPipeline,
)


MODERATION_CAPABILITY_DATA_KEY = "fogmoe.moderation_capability"
"""@brief bot_data 中治理 capability 的稳定键 / Stable bot_data key for the moderation capability."""


@dataclass(frozen=True, slots=True)
class TelegramModerationCapability:
    """@brief 治理入口、命令与 P1 状态的单一运行时所有者 / Sole runtime owner of moderation ingress, commands, and P1 state.

    @param guard durable ingress Guard / Durable-ingress Guard.
    @param observer durable ingress Observer / Durable-ingress Observer.
    @param commands 群组配置命令服务 / Group-configuration command service.
    @param reports 举报服务 / Reporting service.
    @param callback_cooldown callback 级 3 秒有界冷却 / Bounded three-second callback cooldown.
    """

    guard: ModerationIngressGuard
    observer: TelegramObserverPipeline
    commands: GroupModerationCommandService
    reports: ReportingService
    callback_cooldown: CooldownGate[tuple[str, int, int]]


def create_moderation_ingress_capability(
    bot: Bot,
    *,
    wordlist_path: Path,
) -> TelegramModerationCapability:
    """@brief 构造治理 bounded context / Compose the moderation bounded context.

    @param bot 共享 Telegram Bot / Shared Telegram Bot.
    @param wordlist_path 受版本控制的治理词表路径 /
        Version-controlled moderation-word-list path.
    @return 完整治理 capability / Complete moderation capability.
    @note 调用方应将 ``guard`` 与 ``observer`` 直接加入 ``IngressRouter``；不应再声明
    legacy PTB message guard/observer。/ Callers should add ``guard`` and ``observer``
    directly to ``IngressRouter`` and must not declare legacy PTB message guards/observers.
    """

    groups = PostgresModerationGroupRepository()
    effect_repository = PostgresModerationEffectRepository()
    reports = PostgresModerationReportRepository()
    cache = BoundedTtlCache[ChatId, GroupModeration](
        ttl_seconds=300.0,
        max_entries=4096,
    )
    configuration = GroupModerationConfiguration(groups, cache)
    mapper = TelegramModerationMapper(bot)
    clock = SystemUtcClock()
    effects = ModerationEffectService(
        effect_repository,
        TelegramModerationEffectSink(bot),
        clock,
    )
    moderation = ModerationService(
        configuration,
        configuration,
        FileModerationRuleProvider(wordlist_path),
    )
    automation = KeywordAutomationService(
        configuration=configuration,
        effect_repository=effect_repository,
        effects=effects,
        rate_limit=FixedWindowGate[int](
            window_seconds=60.0,
            max_admissions=5,
            max_entries=4096,
        ),
    )
    return TelegramModerationCapability(
        guard=ModerationIngressGuard(
            mapper=mapper,
            moderation=moderation,
            configuration=configuration,
            effects=effects,
        ),
        observer=TelegramObserverPipeline(
            (
                GroupMessageIngressObserver(PostgresGroupMessageProjection()),
                KeywordIngressObserver(
                    mapper=mapper,
                    automation=automation,
                ),
            )
        ),
        commands=GroupModerationCommandService(groups, configuration),
        reports=ReportingService(
            reports,
            TelegramReportDelivery(bot),
            clock,
        ),
        callback_cooldown=CooldownGate[tuple[str, int, int]](
            cooldown_seconds=3.0,
            max_entries=4096,
        ),
    )


__all__ = [
    "MODERATION_CAPABILITY_DATA_KEY",
    "TelegramModerationCapability",
    "create_moderation_ingress_capability",
]
