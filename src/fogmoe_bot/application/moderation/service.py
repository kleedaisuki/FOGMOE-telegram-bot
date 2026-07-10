"""@brief 内容审核应用服务与端口 / Content-moderation application service and ports."""

from typing import Protocol

from fogmoe_bot.domain.moderation import (
    ChatId,
    GroupModerationPolicy,
    ModerationDecision,
    ModerationEngine,
    ModerationRequest,
    ModerationRule,
)


class ModerationPolicyProvider(Protocol):
    """@brief 群组审核策略读取端口 / Group moderation-policy provider port."""

    async def get_policy(self, chat_id: ChatId) -> GroupModerationPolicy:
        """@brief 读取群组策略 / Read a group policy.

        @param chat_id Telegram 群组 ID / Telegram chat ID.
        @return 群组策略快照 / Group policy snapshot.
        """

        ...


class GroupRuleProvider(Protocol):
    """@brief 群组审核规则读取端口 / Group moderation-rule provider port."""

    async def get_group_rules(self, chat_id: ChatId) -> tuple[ModerationRule, ...]:
        """@brief 读取群组规则 / Read group-specific rules.

        @param chat_id Telegram 群组 ID / Telegram chat ID.
        @return 群组规则 / Group-specific rules.
        """

        ...


class GlobalRuleProvider(Protocol):
    """@brief 全局审核规则读取端口 / Global moderation-rule provider port."""

    def get_global_rules(self) -> tuple[ModerationRule, ...]:
        """@brief 读取全局规则 / Read global rules.

        @return 全局规则 / Global rules.
        """

        ...


class ModerationService:
    """@brief 编排策略、规则和纯审核引擎 / Orchestrate policy, rules, and the pure engine.

    @param policy_provider 群组策略端口 / Group policy provider.
    @param group_rule_provider 群组规则端口 / Group rule provider.
    @param global_rule_provider 全局规则端口 / Global rule provider.
    @param engine 可选规则引擎 / Optional moderation engine.
    """

    def __init__(
        self,
        policy_provider: ModerationPolicyProvider,
        group_rule_provider: GroupRuleProvider,
        global_rule_provider: GlobalRuleProvider,
        engine: ModerationEngine | None = None,
    ) -> None:
        self._policy_provider = policy_provider
        self._group_rule_provider = group_rule_provider
        self._global_rule_provider = global_rule_provider
        self._engine = engine or ModerationEngine()

    async def moderate(self, request: ModerationRequest) -> ModerationDecision:
        """@brief 审核一条消息 / Moderate one message.

        @param request 类型化审核请求 / Typed moderation request.
        @return 审核判决 / Moderation decision.
        """

        policy = await self._policy_provider.get_policy(request.chat_id)
        if not policy.enabled:
            return self._engine.evaluate(request, policy)
        group_rules = await self._group_rule_provider.get_group_rules(request.chat_id)
        return self._engine.evaluate(
            request,
            policy,
            global_rules=self._global_rule_provider.get_global_rules(),
            group_rules=group_rules,
        )
