"""@brief 群组治理配置用例 / Group-moderation configuration use cases."""

from __future__ import annotations

from collections.abc import Callable

from fogmoe_bot.domain.moderation.aggregate import (
    GroupModeration,
    StaleModerationVersion,
)
from fogmoe_bot.domain.moderation.models import (
    ChatId,
    ModerationToggleResult,
    UserId,
)

from .configuration import GroupModerationConfiguration
from .ports import GroupModerationRepository


type GroupMutation = Callable[[GroupModeration], GroupModeration]
"""@brief 纯群组聚合变换 / Pure group-aggregate transformation."""


class GroupModerationCommandService:
    """@brief 以短事务和 OCC 执行群组治理命令 / Execute group-moderation commands with short transactions and OCC.

    @param repository 聚合仓储 / Aggregate repository.
    @param configuration 共享读取缓存 / Shared read cache.
    @param max_conflict_retries OCC 冲突重算次数 / Number of OCC conflict recomputations.
    """

    def __init__(
        self,
        repository: GroupModerationRepository,
        configuration: GroupModerationConfiguration,
        *,
        max_conflict_retries: int = 3,
    ) -> None:
        """@brief 注入仓储与缓存 / Inject repository and cache.

        @param repository 聚合仓储 / Aggregate repository.
        @param configuration 共享读取缓存 / Shared read cache.
        @param max_conflict_retries 最大冲突重试次数 / Maximum conflict retry count.
        @return None / None.
        @raises ValueError 重试次数无效 / If the retry count is invalid.
        """

        if isinstance(max_conflict_retries, bool) or max_conflict_retries < 1:
            raise ValueError("max_conflict_retries must be positive")
        self._repository = repository
        self._configuration = configuration
        self._max_conflict_retries = max_conflict_retries

    async def toggle(
        self,
        chat_id: ChatId,
        actor_id: UserId,
        *,
        idempotency_key: str,
    ) -> ModerationToggleResult:
        """@brief 切换垃圾过滤 / Toggle spam control.

        @param chat_id 群组 ID / Group identifier.
        @param actor_id 管理员 ID / Administrator identifier.
        @param idempotency_key source Update 稳定键 / Stable source-Update key.
        @return 首次提交或回放结果 / First committed or replayed result.
        """

        for attempt in range(self._max_conflict_retries):
            self._configuration.invalidate(chat_id)
            try:
                result = await self._repository.toggle_group(
                    chat_id,
                    actor_id=int(actor_id),
                    idempotency_key=idempotency_key,
                )
            except StaleModerationVersion:
                if attempt + 1 >= self._max_conflict_retries:
                    raise
                continue
            self._configuration.invalidate(chat_id)
            return result
        raise StaleModerationVersion("Moderation command conflict budget exhausted")

    async def set_link_blocking(
        self,
        chat_id: ChatId,
        actor_id: UserId,
        *,
        enabled: bool,
    ) -> GroupModeration:
        """@brief 设置链接过滤 / Set link filtering.

        @param chat_id 群组 ID / Group identifier.
        @param actor_id 管理员 ID / Administrator identifier.
        @param enabled 是否启用 / Whether enabled.
        @return 已提交聚合 / Committed aggregate.
        """

        return await self._mutate(
            chat_id,
            actor_id,
            lambda group: group.set_link_blocking(enabled, actor_id),
        )

    async def set_mention_blocking(
        self,
        chat_id: ChatId,
        actor_id: UserId,
        *,
        enabled: bool,
    ) -> GroupModeration:
        """@brief 设置提及过滤 / Set mention filtering.

        @param chat_id 群组 ID / Group identifier.
        @param actor_id 管理员 ID / Administrator identifier.
        @param enabled 是否启用 / Whether enabled.
        @return 已提交聚合 / Committed aggregate.
        """

        return await self._mutate(
            chat_id,
            actor_id,
            lambda group: group.set_mention_blocking(enabled, actor_id),
        )

    async def put_spam_rule(
        self,
        chat_id: ChatId,
        actor_id: UserId,
        pattern: str,
        *,
        regex: bool,
    ) -> GroupModeration:
        """@brief 新增或更新垃圾规则 / Add or update a spam rule.

        @param chat_id 群组 ID / Group identifier.
        @param actor_id 管理员 ID / Administrator identifier.
        @param pattern 规则模式 / Rule pattern.
        @param regex 是否正则 / Whether regex.
        @return 已提交聚合 / Committed aggregate.
        """

        return await self._mutate(
            chat_id,
            actor_id,
            lambda group: group.put_spam_rule(
                pattern,
                regex=regex,
                actor_id=actor_id,
            ),
        )

    async def remove_spam_rule(
        self,
        chat_id: ChatId,
        actor_id: UserId,
        pattern: str,
    ) -> bool:
        """@brief 删除垃圾规则 / Remove a spam rule.

        @param chat_id 群组 ID / Group identifier.
        @param actor_id 管理员 ID / Administrator identifier.
        @param pattern 规则模式 / Rule pattern.
        @return 是否存在并删除 / Whether found and removed.
        """

        removed = False

        def mutation(group: GroupModeration) -> GroupModeration:
            """@brief 捕获删除结果的纯变换 / Pure transform capturing removal.

            @param group 当前聚合 / Current aggregate.
            @return 更新聚合 / Updated aggregate.
            """

            nonlocal removed
            updated, removed = group.remove_spam_rule(pattern, actor_id)
            return updated

        await self._mutate(chat_id, actor_id, mutation, allow_noop=True)
        return removed

    async def put_keyword_reply(
        self,
        chat_id: ChatId,
        actor_id: UserId,
        keyword: str,
        response: str,
    ) -> GroupModeration:
        """@brief 新增或更新关键词回复 / Add or update a keyword reply.

        @param chat_id 群组 ID / Group identifier.
        @param actor_id 管理员 ID / Administrator identifier.
        @param keyword 触发关键词 / Trigger keyword.
        @param response 回复内容 / Response content.
        @return 已提交聚合 / Committed aggregate.
        """

        return await self._mutate(
            chat_id,
            actor_id,
            lambda group: group.put_keyword_reply(keyword, response, actor_id),
        )

    async def remove_keyword_reply(
        self,
        chat_id: ChatId,
        actor_id: UserId,
        keyword: str,
    ) -> bool:
        """@brief 删除关键词回复 / Remove a keyword reply.

        @param chat_id 群组 ID / Group identifier.
        @param actor_id 管理员 ID / Administrator identifier.
        @param keyword 触发关键词 / Trigger keyword.
        @return 是否存在并删除 / Whether found and removed.
        """

        removed = False

        def mutation(group: GroupModeration) -> GroupModeration:
            """@brief 捕获删除结果的纯变换 / Pure transform capturing removal.

            @param group 当前聚合 / Current aggregate.
            @return 更新聚合 / Updated aggregate.
            """

            nonlocal removed
            updated, removed = group.remove_keyword_reply(keyword, actor_id)
            return updated

        await self._mutate(chat_id, actor_id, mutation, allow_noop=True)
        return removed

    async def read(self, chat_id: ChatId) -> GroupModeration:
        """@brief 读取群组配置 / Read group configuration.

        @param chat_id 群组 ID / Group identifier.
        @return 聚合快照 / Aggregate snapshot.
        """

        return await self._configuration.get_group(chat_id)

    async def _mutate(
        self,
        chat_id: ChatId,
        actor_id: UserId,
        mutation: GroupMutation,
        *,
        allow_noop: bool = False,
    ) -> GroupModeration:
        """@brief 冲突时重读并重算命令 / Reload and recompute a command after conflicts.

        @param chat_id 群组 ID / Group identifier.
        @param actor_id 管理员 ID / Administrator identifier.
        @param mutation 纯聚合变换 / Pure aggregate transformation.
        @param allow_noop 是否允许未改变聚合 / Whether an unchanged aggregate is allowed.
        @return 已提交或未改变聚合 / Committed or unchanged aggregate.
        @raises StaleModerationVersion 冲突次数耗尽 / When the conflict retry budget is exhausted.
        """

        for attempt in range(self._max_conflict_retries):
            self._configuration.invalidate(chat_id)
            current = await self._repository.load_group(chat_id)
            updated = mutation(current)
            if updated is current or updated == current:
                if allow_noop:
                    self._configuration.put(current)
                    return current
                raise ValueError("Moderation command did not change aggregate state")
            try:
                await self._repository.save_group(
                    updated,
                    expected_version=current.version,
                    actor_id=int(actor_id),
                )
            except StaleModerationVersion:
                if attempt + 1 >= self._max_conflict_retries:
                    raise
                continue
            self._configuration.put(updated)
            return updated
        raise StaleModerationVersion("Moderation command conflict budget exhausted")


__all__ = ["GroupModerationCommandService", "GroupMutation"]
