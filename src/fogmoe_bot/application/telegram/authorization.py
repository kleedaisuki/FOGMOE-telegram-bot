"""@brief 可重放的 Telegram 群管理员授权 / Replay-stable Telegram group-administrator authorization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from fogmoe_bot.domain.conversation.identity import UpdateId
from fogmoe_bot.domain.temporal import ensure_utc

GROUP_MEMORY_RESET_CAPABILITY = "memory.reset_group"
"""@brief 群记忆重置 capability / Group-memory reset capability."""


@dataclass(frozen=True, slots=True)
class GroupAdministratorDecision:
    """@brief 对一个 Update 冻结的群管理员判断 / Group-administrator decision frozen for one Update.

    @param update_id durable Telegram Update / Durable Telegram Update.
    @param chat_id 群 ID / Group identifier.
    @param actor_user_id 命令发送者 / Command actor.
    @param allowed 是否为 owner/administrator / Whether the actor is an owner or administrator.
    @param observed_at 外部权限观测时刻 / Time of the external authorization observation.
    """

    update_id: UpdateId
    chat_id: int
    actor_user_id: int
    allowed: bool
    observed_at: datetime

    def __post_init__(self) -> None:
        """@brief 校验授权事实 / Validate the authorization fact.

        @return None / None.
        @raise ValueError ID 非法 / Invalid identifiers.
        """

        if isinstance(self.chat_id, bool) or self.chat_id == 0:
            raise ValueError("Authorization chat_id must be non-zero")
        if isinstance(self.actor_user_id, bool) or self.actor_user_id <= 0:
            raise ValueError("Authorization actor_user_id must be positive")
        object.__setattr__(self, "observed_at", ensure_utc(self.observed_at))


class GroupAdministratorSource(Protocol):
    """@brief Telegram 当前群成员权限来源 / Source of current Telegram group membership privileges."""

    async def is_administrator(self, *, chat_id: int, user_id: int) -> bool:
        """@brief 查询 owner/administrator 状态 / Query owner-or-administrator status.

        @param chat_id 群 ID / Group identifier.
        @param user_id 用户 ID / User identifier.
        @return 管理员或 owner 为 True / True for an administrator or owner.
        """

        ...


class GroupAdministratorDecisionStore(Protocol):
    """@brief 授权决定的 first-writer-wins 存储 / First-writer-wins store for authorization decisions."""

    async def read(
        self,
        update_id: UpdateId,
    ) -> GroupAdministratorDecision | None:
        """@brief 读取已冻结决定 / Read a frozen decision.

        @param update_id durable Update / Durable Update.
        @return 已有决定或 None / Existing decision or None.
        """

        ...

    async def freeze(
        self,
        decision: GroupAdministratorDecision,
    ) -> GroupAdministratorDecision:
        """@brief 冻结并返回规范决定 / Freeze and return the canonical decision.

        @param decision 外部观测结果 / Externally observed decision.
        @return 首个规范决定 / First canonical decision.
        """

        ...


class DurableGroupAdministratorAuthorization:
    """@brief 将易变 Telegram 权限观测转成可重放事实 / Turn volatile Telegram authorization into a replayable fact."""

    def __init__(
        self,
        *,
        source: GroupAdministratorSource,
        store: GroupAdministratorDecisionStore,
    ) -> None:
        """@brief 注入外部来源与 durable store / Inject the external source and durable store.

        @param source Telegram 权限来源 / Telegram authorization source.
        @param store 决定存储 / Decision store.
        """

        self._source = source
        self._store = store

    async def authorize(
        self,
        *,
        update_id: UpdateId,
        chat_id: int,
        actor_user_id: int,
        observed_at: datetime,
    ) -> bool:
        """@brief 返回对同一 Update 永远稳定的授权结果 / Return an authorization result stable for the Update forever.

        @param update_id durable Update / Durable Update.
        @param chat_id 群 ID / Group identifier.
        @param actor_user_id 命令发送者 / Command actor.
        @param observed_at 首次观测时刻 / Initial observation time.
        @return 允许时为 True / True when allowed.
        @raise RuntimeError 已有决定的 subject 漂移 / Subject drift under an existing decision.
        """

        existing = await self._store.read(update_id)
        if existing is not None:
            self._validate_subject(
                existing, chat_id=chat_id, actor_user_id=actor_user_id
            )
            return existing.allowed
        observed = GroupAdministratorDecision(
            update_id=update_id,
            chat_id=chat_id,
            actor_user_id=actor_user_id,
            allowed=await self._source.is_administrator(
                chat_id=chat_id,
                user_id=actor_user_id,
            ),
            observed_at=observed_at,
        )
        canonical = await self._store.freeze(observed)
        self._validate_subject(
            canonical,
            chat_id=chat_id,
            actor_user_id=actor_user_id,
        )
        return canonical.allowed

    @staticmethod
    def _validate_subject(
        decision: GroupAdministratorDecision,
        *,
        chat_id: int,
        actor_user_id: int,
    ) -> None:
        """@brief 拒绝同一 Update 的授权 subject 漂移 / Reject authorization-subject drift for one Update.

        @param decision 已冻结决定 / Frozen decision.
        @param chat_id 预期群 ID / Expected group identifier.
        @param actor_user_id 预期发送者 / Expected actor.
        @return None / None.
        @raise RuntimeError subject 漂移 / Subject drift.
        """

        if decision.chat_id != chat_id or decision.actor_user_id != actor_user_id:
            raise RuntimeError("Group authorization decision changed subject")


__all__ = [
    "DurableGroupAdministratorAuthorization",
    "GROUP_MEMORY_RESET_CAPABILITY",
    "GroupAdministratorDecision",
    "GroupAdministratorDecisionStore",
    "GroupAdministratorSource",
]
