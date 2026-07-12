"""@brief durable inbox 的治理 Guard 与 Observer / Moderation Guard and Observer for the durable inbox."""

from __future__ import annotations

import re

from fogmoe_bot.application.conversation.router import (
    Allow,
    Reject,
    RoutedOperation,
)
from fogmoe_bot.application.runtime import AggregateKey, WorkPriority
from fogmoe_bot.domain.conversation.inbox import InboundUpdate
from fogmoe_bot.domain.moderation.effects import (
    KeywordReplyPlan,
    ModerationEffectId,
    ModerationEffectKind,
    SpamEnforcementPlan,
)
from fogmoe_bot.domain.moderation.models import (
    EnforcementFailureMode,
    ModerationRequest,
    Verdict,
)

from .configuration import GroupModerationConfiguration
from .effect_service import ModerationEffectService
from .ports import ModerationEffectRepository, ModerationIngressMapper
from .rate_windows import FixedWindowGate
from .service import ModerationService


class ModerationIngressGuard:
    """@brief 在所有 primary route 前执行内容治理 / Enforce content moderation before every primary route.

    @param mapper 传输适配器 / Transport mapper.
    @param moderation 纯规则编排服务 / Pure-rule orchestration service.
    @param configuration 群组配置读取 capability / Group-configuration reader.
    @param effects 可持久化副作用执行器 / Persisted-effect executor.
    """

    def __init__(
        self,
        *,
        mapper: ModerationIngressMapper,
        moderation: ModerationService,
        configuration: GroupModerationConfiguration,
        effects: ModerationEffectService,
    ) -> None:
        """@brief 注入 Guard 依赖 / Inject Guard dependencies.

        @param mapper 传输适配器 / Transport mapper.
        @param moderation 审核服务 / Moderation service.
        @param configuration 群组配置读取 capability / Group-configuration reader.
        @param effects 副作用执行器 / Effect executor.
        @return None / None.
        """

        self._mapper = mapper
        self._moderation = moderation
        self._configuration = configuration
        self._effects = effects

    @property
    def name(self) -> str:
        """@brief 返回稳定 Guard 名 / Return the stable Guard name.

        @return ``moderation-content-policy`` / ``moderation-content-policy``.
        """

        return "moderation-content-policy"

    async def evaluate(self, update: InboundUpdate) -> Allow | Reject:
        """@brief 审核、处置并决定传播 / Moderate, enforce, and decide propagation.

        @param update 已领取 durable Update / Claimed durable Update.
        @return 允许或拒绝 / Allow or reject.
        """

        request = await self._mapper.moderation_request(update)
        if request is None:
            return Allow()
        policy = await self._configuration.get_policy(request.chat_id)
        if not policy.enabled:
            return Allow()
        decision = await self._moderation.moderate(request)
        match = decision.primary_match
        if decision.verdict is Verdict.ALLOW or match is None:
            return Allow()
        outcome = await self._effects.enforce_spam(
            SpamEnforcementPlan(
                effect_id=ModerationEffectId.for_update(
                    update.update_id.value,
                    ModerationEffectKind.SPAM_ENFORCEMENT,
                ),
                update_id=update.update_id.value,
                chat_id=request.chat_id,
                user_id=request.user_id,
                message_id=request.message_id,
                matched_text=match.matched_text,
                rule_kind=match.rule.kind,
                failure_mode=policy.failure_mode,
            )
        )
        if outcome.message_deleted:
            return Reject(reason=f"moderation:{match.rule.kind.name.lower()}")
        if policy.failure_mode is EnforcementFailureMode.FAIL_CLOSED:
            return Reject(reason="moderation:enforcement-failed-closed")
        return Allow()


class KeywordAutomationService:
    """@brief 匹配、限流并投递关键词回复 / Match, rate-limit, and deliver keyword replies.

    @param configuration 群组配置读取 capability / Group-configuration reader.
    @param effect_repository 效果读取仓储 / Effect-reading repository.
    @param effects 副作用执行器 / Effect executor.
    @param rate_limit 运行时拥有的每群固定窗口 / Runtime-owned per-group fixed window.
    """

    def __init__(
        self,
        *,
        configuration: GroupModerationConfiguration,
        effect_repository: ModerationEffectRepository,
        effects: ModerationEffectService,
        rate_limit: FixedWindowGate[int],
    ) -> None:
        """@brief 注入自动回复依赖 / Inject auto-reply dependencies.

        @param configuration 群组配置读取 capability / Group-configuration reader.
        @param effect_repository 效果读取仓储 / Effect-reading repository.
        @param effects 副作用执行器 / Effect executor.
        @param rate_limit 每群固定窗口准入器 / Per-group fixed-window gate.
        @return None / None.
        """

        self._configuration = configuration
        self._effect_repository = effect_repository
        self._effects = effects
        self._rate_limit = rate_limit

    async def respond(
        self,
        update_id: int,
        request: ModerationRequest,
    ) -> None:
        """@brief 为一条非命令群文本尝试回复 / Attempt a reply for one non-command group text.

        @param update_id 来源 Update ID / Source Update identifier.
        @param request 已映射关键词输入 / Mapped keyword input.
        @return None / None.
        """

        group = await self._configuration.get_group(request.chat_id)
        matched = next(
            (
                item
                for item in group.keyword_replies
                if _keyword_matches(item.keyword, request.content)
            ),
            None,
        )
        if matched is None:
            return
        effect_id = ModerationEffectId.for_update(
            update_id,
            ModerationEffectKind.KEYWORD_REPLY,
        )
        existing = await self._effect_repository.load_effect(effect_id)
        if existing is None and not self._rate_limit.try_acquire(int(request.chat_id)):
            return
        await self._effects.deliver_keyword(
            KeywordReplyPlan(
                effect_id=effect_id,
                update_id=update_id,
                chat_id=request.chat_id,
                user_id=request.user_id,
                message_id=request.message_id,
                keyword=matched.keyword,
                response=matched.response,
            )
        )


class KeywordIngressObserver:
    """@brief primary 之后计划关键词自动回复 / Schedule keyword auto-replies after the primary route.

    @param mapper Update 映射端口 / Update-mapping port.
    @param automation 自动回复服务 / Auto-reply service.
    """

    def __init__(
        self,
        *,
        mapper: ModerationIngressMapper,
        automation: KeywordAutomationService,
    ) -> None:
        """@brief 注入 observer 依赖 / Inject observer dependencies.

        @param mapper Update 映射端口 / Update-mapping port.
        @param automation 自动回复服务 / Auto-reply service.
        @return None / None.
        """

        self._mapper = mapper
        self._automation = automation

    @property
    def name(self) -> str:
        """@brief 返回稳定 observer 名 / Return the stable observer name.

        @return ``moderation-keyword-reply`` / ``moderation-keyword-reply``.
        """

        return "moderation-keyword-reply"

    async def operation(
        self,
        update: InboundUpdate,
        *,
        primary_route: str | None,
    ) -> RoutedOperation | None:
        """@brief 为可观察群文本构造延迟操作 / Build a lazy operation for observable group text.

        @param update durable Update / Durable Update.
        @param primary_route 已执行 primary 名称 / Executed primary name.
        @return 操作或 None / Operation or None.
        """

        del primary_route
        request = self._mapper.keyword_request(update)
        if request is None:
            return None

        async def call() -> None:
            """@brief 在群组 mailbox 内执行自动回复 / Execute auto-reply in the group mailbox.

            @return None / None.
            """

            await self._automation.respond(update.update_id.value, request)

        return RoutedOperation(
            name="moderation.keyword-reply",
            key=AggregateKey.of("moderation-group", int(request.chat_id)),
            call=call,
            priority=WorkPriority.LOW,
        )


def _keyword_matches(keyword: str, message: str) -> bool:
    """@brief 保持拉丁词边界与 CJK 子串语义 / Preserve Latin word-boundary and CJK substring semantics.

    @param keyword 触发词 / Trigger keyword.
    @param message 消息文本 / Message text.
    @return 命中为 True / True on match.
    """

    if re.fullmatch(r"[\x00-\x7f]+", keyword):
        return (
            re.search(rf"\b{re.escape(keyword)}\b", message, re.IGNORECASE) is not None
        )
    return keyword.casefold() in message.casefold()


__all__ = [
    "KeywordAutomationService",
    "KeywordIngressObserver",
    "ModerationIngressGuard",
]
