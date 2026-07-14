"""@brief 个人 RPG 应用端口 / Personal-RPG application ports."""

from __future__ import annotations

from typing import Protocol

from fogmoe_bot.application.personal_rpg.models import (
    CraftPersonalRecipe,
    CreatePersonalCharacter,
    ExploreDaily,
    PersonalRpgResult,
)
from fogmoe_bot.domain.world.scope import PersonalScope


class PersonalRpgOperations(Protocol):
    """@brief 个人 RPG 的原子持久化操作 / Atomic persistence operations for the personal RPG.

    @note 每个写操作都必须只锁定 ``PersonalScope`` 所属的个人进度、每日唯一键和操作回执；
        不得接受群组 ID、话题 ID 或第二用户。/ Every write must lock only the personal
        progression owned by ``PersonalScope``, the daily uniqueness key, and its operation
        receipt; it must not accept a group ID, topic ID, or second user.

    @note 本核心当前不定义金币奖励或金币配方成本。未来若引入货币效果，必须先通过迁移
        增加一个专属、可审计的 ``LedgerReason``，再在同一事务中只借记/贷记用户 ``FREE``
        钱包并写入平衡账本分录和业务回执；不得触碰 ``PAID`` 钱包。/ This core currently
        defines no token reward or recipe cost. If a future monetary effect is introduced, it must
        first add a dedicated auditable ``LedgerReason`` through a migration, then in the same
        transaction debit/credit only the user's ``FREE`` wallet and write a balanced ledger entry
        plus a business receipt; it must not touch the ``PAID`` wallet.
    """

    async def create_character(
        self,
        command: CreatePersonalCharacter,
    ) -> PersonalRpgResult:
        """@brief 原子创建或幂等回放一名个人角色 / Atomically create or idempotently replay one personal character.

        @param command 创建个人角色命令 / Personal-character creation command.
        @return 创建结果与可选进度快照 / Creation result and optional progression snapshot.
        """

        ...

    async def explore_daily(self, command: ExploreDaily) -> PersonalRpgResult:
        """@brief 原子结算一次确定性每日探索 / Atomically settle one deterministic daily exploration.

        实现必须在同一事务中验证角色、锁定 ``(PersonalScope, UTC day)`` 唯一性、保存可审计
        探索、更新经验和材料，并保存幂等回执。
        Implementations must in one transaction validate the character, lock uniqueness of
        ``(PersonalScope, UTC day)``, persist the auditable exploration, update experience and
        materials, and save an idempotency receipt.

        @param command 每日探索命令 / Daily-exploration command.
        @return 探索结算结果 / Exploration settlement result.
        """

        ...

    async def craft_recipe(self, command: CraftPersonalRecipe) -> PersonalRpgResult:
        """@brief 原子消耗材料并写入图鉴 / Atomically consume materials and update compendium.

        实现必须在同一事务中锁定个人进度、校验配方与材料、扣减库存、记录图鉴条目，并保存
        幂等回执。
        Implementations must in one transaction lock personal progress, validate recipe and
        materials, decrement inventory, record the compendium entry, and save an idempotency receipt.

        @param command 制作配方命令 / Recipe-crafting command.
        @return 制作结算结果 / Crafting settlement result.
        """

        ...

    async def overview(self, scope: PersonalScope) -> PersonalRpgResult:
        """@brief 读取一个个人范围的 RPG 进度 / Read RPG progression for one personal scope.

        @param scope 仅限个人的范围 / Personal-only scope.
        @return 进度快照，角色不存在时为 ``NOT_REGISTERED`` / Progress snapshot, or ``NOT_REGISTERED`` when absent.
        """

        ...
