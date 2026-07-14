"""@brief PostgreSQL 个人 RPG 原子适配器 / PostgreSQL atomic adapter for the personal RPG.

本模块只实现适配器，不拥有迁移。迁移 ``0052_personal_rpg`` 提供下列 PostgreSQL 表和约束：

- ``personal_rpg.characters(user_id PK/FK identity.users, name, experience,
  last_exploration_day NULL, character_version, profile_version, created_at, updated_at)``；
- ``personal_rpg.materials(user_id, material_kind, quantity, PRIMARY KEY(user_id, material_kind))``；
- ``personal_rpg.explorations(exploration_id PK, user_id, exploration_day, route,
  explored_at, experience_reward, material_rewards JSONB, audit_digest,
  UNIQUE(user_id, exploration_day))``；
- ``personal_rpg.collections(user_id, collectible_kind, recipe_code, craft_id UNIQUE,
  crafted_at, PRIMARY KEY(user_id, collectible_kind))``；
- ``personal_rpg.operation_receipts(idempotency_key PK, operation_kind, actor_id,
  request_fingerprint JSONB, result JSONB, created_at)``。

``characters`` 行是同一 ``PersonalScope`` 所有既有进度写入的聚合锁；新角色尚未有行时以范围
advisory lock 补足创建窗口。材料、探索、图鉴和回执始终在该锁持有的同一短事务内写入。当前领域规则不包含金币奖励或成本，故本模块刻意不导入或调用
``bank``；未来若规则加入货币效果，必须先通过迁移定义专属 ``LedgerReason``，并在同一
事务中遵守 ``FREE`` 钱包与平衡账本约束，而不能绕过银行账本。
The module implements only the adapter, not migrations. Migration ``0052_personal_rpg`` owns the
listed tables and constraints. The ``characters`` row is the aggregate lock for existing progress
of one ``PersonalScope``; a scope advisory lock closes the no-row creation window.
Materials, explorations, collections, and receipts are written under those locks in one short
transaction. Current domain rules have no token rewards or costs, so this
module deliberately neither imports nor calls ``bank``. A future monetary rule must first add a
dedicated ``LedgerReason`` through a migration, then use the ``FREE`` wallet and a balanced ledger
entry in the same transaction rather than bypassing the bank ledger.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
import json
from typing import Any, cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.personal_rpg.models import (
    CraftPersonalRecipe,
    CreatePersonalCharacter,
    ExploreDaily,
    PersonalRpgCode,
    PersonalRpgResult,
)
from fogmoe_bot.application.personal_rpg.ports import PersonalRpgOperations
from fogmoe_bot.domain.personal_rpg.catalog import (
    CollectibleKind,
    CollectionCompendium,
    CraftingRecipe,
    MaterialBundle,
    MaterialInventory,
    MaterialKind,
    RecipeCode,
    recipe_for,
)
from fogmoe_bot.domain.personal_rpg.character import PersonalCharacter
from fogmoe_bot.domain.personal_rpg.exploration import (
    DailyExploration,
    ExplorationReward,
    ExplorationRoute,
)
from fogmoe_bot.domain.personal_rpg.profile import PersonalRpgProfile
from fogmoe_bot.domain.world.scope import PersonalScope
from fogmoe_bot.infrastructure.database import connection as db_connection


class PostgresPersonalRpgOperations(PersonalRpgOperations):
    """@brief 将私聊角色进度、日唯一探索和图鉴置于同一事务 / Put private character progress, daily uniqueness, and compendium in one transaction.

    @note 此类从不接受裸用户 ID、群组 ID 或第二玩家；所有写路径的身份只能来自 typed
        command 中的 ``PersonalScope``。/ This class accepts neither a bare user ID, a group ID,
        nor a second player; identities on all write paths come only from ``PersonalScope`` in a
        typed command.
    """

    async def create_character(
        self,
        command: CreatePersonalCharacter,
    ) -> PersonalRpgResult:
        """@brief 原子创建个人角色或重放历史结果 / Atomically create a personal character or replay historical result.

        @param command 创建角色命令 / Character-creation command.
        @return 创建、已存在、未注册或幂等回放结果 / Creation, already-exists, not-registered, or replay result.
        """

        operation_kind = "personal_rpg.create_character"
        fingerprint = _create_fingerprint(command)
        actor_id = command.scope.user_id
        async with db_connection.transaction() as connection:
            await _lock_receipt_key(command.idempotency_key, connection)
            await _lock_scope(command.scope, connection)
            replay = await _load_receipt(
                command.idempotency_key,
                operation_kind,
                actor_id=actor_id,
                fingerprint=fingerprint,
                connection=connection,
            )
            if replay is not None:
                return _result_from_mapping(replay, replayed=True)

            if not await _identity_exists(command.scope, connection):
                result = PersonalRpgResult(PersonalRpgCode.NOT_REGISTERED)
            else:
                existing = await _load_profile(
                    command.scope,
                    connection,
                    for_update=True,
                )
                if existing is not None:
                    result = PersonalRpgResult(
                        PersonalRpgCode.ALREADY_EXISTS,
                        profile=existing,
                    )
                else:
                    profile = command.profile()
                    await _insert_character(profile, command.created_at, connection)
                    result = PersonalRpgResult(
                        PersonalRpgCode.SUCCESS,
                        profile=profile,
                    )
            await _save_receipt(
                command.idempotency_key,
                operation_kind,
                actor_id=actor_id,
                fingerprint=fingerprint,
                result=_result_mapping(result),
                connection=connection,
            )
            return result

    async def explore_daily(self, command: ExploreDaily) -> PersonalRpgResult:
        """@brief 原子结算确定性每日探索 / Atomically settle deterministic daily exploration.

        @param command 每日探索命令 / Daily-exploration command.
        @return 探索、已探索、未注册或幂等回放结果 / Exploration, already-explored, not-registered, or replay result.
        """

        operation_kind = "personal_rpg.explore_daily"
        fingerprint = _exploration_fingerprint(command)
        actor_id = command.scope.user_id
        async with db_connection.transaction() as connection:
            await _lock_receipt_key(command.idempotency_key, connection)
            await _lock_scope(command.scope, connection)
            replay = await _load_receipt(
                command.idempotency_key,
                operation_kind,
                actor_id=actor_id,
                fingerprint=fingerprint,
                connection=connection,
            )
            if replay is not None:
                return _result_from_mapping(replay, replayed=True)

            if not await _identity_exists(command.scope, connection):
                result = PersonalRpgResult(PersonalRpgCode.NOT_REGISTERED)
            else:
                profile = await _load_profile(
                    command.scope,
                    connection,
                    for_update=True,
                )
                if profile is None:
                    result = PersonalRpgResult(PersonalRpgCode.NOT_REGISTERED)
                elif profile.last_exploration_day is not None and (
                    command.day <= profile.last_exploration_day
                ):
                    result = PersonalRpgResult(
                        PersonalRpgCode.ALREADY_EXPLORED,
                        profile=profile,
                    )
                elif await _exploration_day_exists(
                    command.scope, command.day, connection
                ):
                    result = PersonalRpgResult(
                        PersonalRpgCode.ALREADY_EXPLORED,
                        profile=profile,
                    )
                elif await _exploration_id_exists(command.exploration_id, connection):
                    result = PersonalRpgResult(
                        PersonalRpgCode.CONFLICT,
                        profile=profile,
                    )
                else:
                    exploration = command.exploration()
                    updated = profile.apply_exploration(exploration)
                    await _persist_profile(
                        previous=profile,
                        current=updated,
                        connection=connection,
                    )
                    await _replace_materials(updated, connection)
                    await _insert_exploration(exploration, connection)
                    result = PersonalRpgResult(
                        PersonalRpgCode.SUCCESS,
                        profile=updated,
                        exploration=exploration,
                    )
            await _save_receipt(
                command.idempotency_key,
                operation_kind,
                actor_id=actor_id,
                fingerprint=fingerprint,
                result=_result_mapping(result),
                connection=connection,
            )
            return result

    async def craft_recipe(self, command: CraftPersonalRecipe) -> PersonalRpgResult:
        """@brief 原子消耗材料并收录配方产物 / Atomically consume materials and record recipe output.

        @param command 制作配方命令 / Recipe-crafting command.
        @return 制作、材料不足、已收录、未注册或幂等回放结果 /
            Crafting, insufficient-material, already-collected, not-registered, or replay result.
        """

        operation_kind = "personal_rpg.craft_recipe"
        fingerprint = _craft_fingerprint(command)
        actor_id = command.scope.user_id
        async with db_connection.transaction() as connection:
            await _lock_receipt_key(command.idempotency_key, connection)
            await _lock_scope(command.scope, connection)
            replay = await _load_receipt(
                command.idempotency_key,
                operation_kind,
                actor_id=actor_id,
                fingerprint=fingerprint,
                connection=connection,
            )
            if replay is not None:
                return _result_from_mapping(replay, replayed=True)

            if not await _identity_exists(command.scope, connection):
                result = PersonalRpgResult(PersonalRpgCode.NOT_REGISTERED)
            else:
                profile = await _load_profile(
                    command.scope,
                    connection,
                    for_update=True,
                )
                if profile is None:
                    result = PersonalRpgResult(PersonalRpgCode.NOT_REGISTERED)
                else:
                    recipe = command.recipe()
                    if profile.compendium.contains(recipe.output):
                        result = PersonalRpgResult(
                            PersonalRpgCode.ALREADY_COLLECTED,
                            profile=profile,
                            recipe=recipe,
                        )
                    elif not profile.materials.covers(recipe.ingredients):
                        result = PersonalRpgResult(
                            PersonalRpgCode.MATERIALS_INSUFFICIENT,
                            profile=profile,
                            recipe=recipe,
                        )
                    elif await _craft_id_exists(command.craft_id, connection):
                        result = PersonalRpgResult(
                            PersonalRpgCode.CONFLICT,
                            profile=profile,
                            recipe=recipe,
                        )
                    else:
                        updated = profile.craft(recipe)
                        await _persist_profile(
                            previous=profile,
                            current=updated,
                            connection=connection,
                        )
                        await _replace_materials(updated, connection)
                        await _insert_collection(command, recipe, connection)
                        result = PersonalRpgResult(
                            PersonalRpgCode.SUCCESS,
                            profile=updated,
                            recipe=recipe,
                        )
            await _save_receipt(
                command.idempotency_key,
                operation_kind,
                actor_id=actor_id,
                fingerprint=fingerprint,
                result=_result_mapping(result),
                connection=connection,
            )
            return result

    async def overview(self, scope: PersonalScope) -> PersonalRpgResult:
        """@brief 读取一个个人范围的完整 RPG 进度 / Read full RPG progression for one personal scope.

        @param scope 仅限个人的范围 / Personal-only scope.
        @return 进度快照；角色不存在时为 ``NOT_REGISTERED`` / Progress snapshot, or ``NOT_REGISTERED`` when absent.
        """

        if not isinstance(scope, PersonalScope):
            raise TypeError("Personal RPG overview must use PersonalScope")
        async with db_connection.transaction() as connection:
            profile = await _load_profile(scope, connection, for_update=False)
            return (
                PersonalRpgResult(PersonalRpgCode.SUCCESS, profile=profile)
                if profile is not None
                else PersonalRpgResult(PersonalRpgCode.NOT_REGISTERED)
            )


async def _identity_exists(
    scope: PersonalScope,
    connection: AsyncConnection,
) -> bool:
    """@brief 检查个人范围是否具有账户身份 / Check whether a personal scope has an account identity.

    @param scope 个人范围 / Personal scope.
    @param connection 当前事务连接 / Current transactional connection.
    @return 身份存在时为 True / True when identity exists.
    """

    row = await db_connection.fetch_one(
        "SELECT 1 FROM identity.users WHERE id = %s",
        (scope.user_id,),
        connection=connection,
    )
    return row is not None


async def _lock_receipt_key(
    idempotency_key: str,
    connection: AsyncConnection,
) -> None:
    """@brief 用事务级 advisory lock 串行同一幂等键 / Serialize one idempotency key with a transaction advisory lock.

    @param idempotency_key 业务幂等键 / Business idempotency key.
    @param connection 当前事务连接 / Current transactional connection.
    @return None / None.
    @note 哈希碰撞只会让不相关操作串行化，不会合并业务结果。/
        A hash collision can only serialize unrelated work and cannot merge business results.
    """

    await db_connection.fetch_one(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (idempotency_key,),
        connection=connection,
    )


async def _lock_scope(
    scope: PersonalScope,
    connection: AsyncConnection,
) -> None:
    """@brief 串行同一个人范围的所有 RPG 写入 / Serialize all RPG writes for one personal scope.

    @param scope 个人范围 / Personal scope.
    @param connection 当前事务连接 / Current transactional connection.
    @return None / None.
    @note 新角色尚不存在 ``characters`` 行时，行锁无法阻止双创建；该锁补足这一空行窗口。
        / When a new character has no ``characters`` row yet, a row lock cannot prevent two
        creations; this lock closes that empty-row window.
    """

    await db_connection.fetch_one(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"personal-rpg:scope:{scope.user_id}",),
        connection=connection,
    )


async def _load_receipt(
    idempotency_key: str,
    operation_kind: str,
    *,
    actor_id: int,
    fingerprint: Mapping[str, object],
    connection: AsyncConnection,
) -> Mapping[str, Any] | None:
    """@brief 读取并验证个人 RPG 幂等回执 / Load and validate a personal-RPG idempotency receipt.

    @param idempotency_key 业务幂等键 / Business idempotency key.
    @param operation_kind 稳定操作类别 / Stable operation kind.
    @param actor_id 个人操作主体 / Personal operation actor.
    @param fingerprint 规范化命令语义指纹 / Canonical command-semantics fingerprint.
    @param connection 当前事务连接 / Current transactional connection.
    @return JSON 回执结果；首次调用为 None / JSON receipt result, or None on first call.
    @raise ValueError 同一键改变操作、主体或语义时抛出 /
        Raised when one key changes operation, actor, or semantics.
    """

    row = await db_connection.fetch_one(
        "SELECT operation_kind, actor_id, request_fingerprint, result "
        "FROM personal_rpg.operation_receipts WHERE idempotency_key = %s",
        (idempotency_key,),
        connection=connection,
    )
    if row is None:
        return None
    values = tuple(row)
    if (
        _as_text(values[0], field="Receipt operation kind") != operation_kind
        or _as_int(
            values[1],
            field="Receipt actor ID",
        )
        != actor_id
    ):
        raise ValueError("Personal RPG idempotency key changed ownership")
    persisted_fingerprint = _json_mapping(values[2], field="Receipt fingerprint")
    if dict(persisted_fingerprint) != dict(fingerprint):
        raise ValueError("Personal RPG idempotency key changed command semantics")
    return _json_mapping(values[3], field="Receipt result")


async def _save_receipt(
    idempotency_key: str,
    operation_kind: str,
    *,
    actor_id: int,
    fingerprint: Mapping[str, object],
    result: Mapping[str, object],
    connection: AsyncConnection,
) -> None:
    """@brief 与个人 RPG 状态变更同事务保存不可变回执 / Save immutable receipt in the personal-RPG state transaction.

    @param idempotency_key 业务幂等键 / Business idempotency key.
    @param operation_kind 稳定操作类别 / Stable operation kind.
    @param actor_id 个人操作主体 / Personal operation actor.
    @param fingerprint 规范化命令语义指纹 / Canonical command-semantics fingerprint.
    @param result JSON 兼容结果 / JSON-compatible result.
    @param connection 当前事务连接 / Current transactional connection.
    @return None / None.
    """

    await db_connection.execute(
        "INSERT INTO personal_rpg.operation_receipts ("
        "idempotency_key, operation_kind, actor_id, request_fingerprint, result"
        ") VALUES (%s, %s, %s, CAST(%s AS JSONB), CAST(%s AS JSONB))",
        (
            idempotency_key,
            operation_kind,
            actor_id,
            json.dumps(dict(fingerprint), sort_keys=True),
            json.dumps(dict(result), sort_keys=True),
        ),
        connection=connection,
    )


async def _load_profile(
    scope: PersonalScope,
    connection: AsyncConnection,
    *,
    for_update: bool,
) -> PersonalRpgProfile | None:
    """@brief 加载个人角色、材料和图鉴快照 / Load personal character, materials, and compendium snapshot.

    @param scope 个人范围 / Personal scope.
    @param connection 当前事务连接 / Current transactional connection.
    @param for_update 是否锁定聚合和从属行 / Whether to lock aggregate and dependent rows.
    @return 完整个人 RPG 进度；角色不存在时为 None / Complete personal-RPG profile, or None when character is absent.
    """

    lock_clause = " FOR UPDATE" if for_update else ""
    character_row = await db_connection.fetch_one(
        "SELECT user_id, name, experience, last_exploration_day, character_version, "
        "profile_version FROM personal_rpg.characters WHERE user_id = %s" + lock_clause,
        (scope.user_id,),
        connection=connection,
    )
    if character_row is None:
        return None
    material_rows = await db_connection.fetch_all(
        "SELECT material_kind, quantity FROM personal_rpg.materials WHERE user_id = %s "
        "ORDER BY material_kind" + lock_clause,
        (scope.user_id,),
        connection=connection,
    )
    collection_rows = await db_connection.fetch_all(
        "SELECT collectible_kind FROM personal_rpg.collections WHERE user_id = %s "
        "ORDER BY collectible_kind" + lock_clause,
        (scope.user_id,),
        connection=connection,
    )
    values = tuple(character_row)
    persisted_user_id = _as_int(values[0], field="Character user ID")
    if persisted_user_id != scope.user_id:
        raise RuntimeError("Personal RPG character scope changed while loading")
    material_quantities: dict[MaterialKind, int] = {}
    """@brief 由数据库材料行构造的临时库存 / Temporary inventory constructed from database material rows."""
    for row in material_rows:
        material_values = tuple(row)
        kind = MaterialKind(_as_text(material_values[0], field="Material kind"))
        if kind in material_quantities:
            raise ValueError("Personal RPG material rows contain a duplicate kind")
        material_quantities[kind] = _as_int(
            material_values[1],
            field="Material quantity",
        )
    discovered: set[CollectibleKind] = set()
    """@brief 由数据库图鉴行构造的临时发现集合 / Temporary discovery set constructed from collection rows."""
    for row in collection_rows:
        collection_values = tuple(row)
        collectible = CollectibleKind(
            _as_text(collection_values[0], field="Collectible kind")
        )
        if collectible in discovered:
            raise ValueError("Personal RPG collection rows contain a duplicate kind")
        discovered.add(collectible)
    raw_last_day = values[3]
    last_day = (
        _as_day(raw_last_day, field="Last exploration day")
        if raw_last_day is not None
        else None
    )
    return PersonalRpgProfile(
        character=PersonalCharacter(
            scope=scope,
            name=_as_text(values[1], field="Character name"),
            experience=_as_int(values[2], field="Character experience"),
            version=_as_int(values[4], field="Character version"),
        ),
        materials=MaterialInventory(material_quantities),
        compendium=CollectionCompendium(frozenset(discovered)),
        last_exploration_day=last_day,
        version=_as_int(values[5], field="Profile version"),
    )


async def _insert_character(
    profile: PersonalRpgProfile,
    created_at: datetime,
    connection: AsyncConnection,
) -> None:
    """@brief 插入一份空白个人 RPG 角色聚合 / Insert an empty personal-RPG character aggregate.

    @param profile 待插入空白进度 / Blank profile to insert.
    @param created_at 角色创建时刻 / Character creation instant.
    @param connection 当前事务连接 / Current transactional connection.
    @return None / None.
    @raise ValueError 进度不是空白角色时抛出 / Raised when profile is not a blank character.
    """

    if (
        profile.materials.quantities
        or profile.compendium.discovered
        or profile.last_exploration_day is not None
        or profile.character.experience != 0
    ):
        raise ValueError("A new personal RPG character must have blank progression")
    await db_connection.execute(
        "INSERT INTO personal_rpg.characters ("
        "user_id, name, experience, last_exploration_day, character_version, profile_version, "
        "created_at, updated_at"
        ") VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)",
        (
            profile.scope.user_id,
            profile.character.name,
            profile.character.experience,
            profile.last_exploration_day,
            profile.character.version,
            profile.version,
            created_at,
        ),
        connection=connection,
    )


async def _persist_profile(
    *,
    previous: PersonalRpgProfile,
    current: PersonalRpgProfile,
    connection: AsyncConnection,
) -> None:
    """@brief 以乐观版本保存个人 RPG 聚合头 / Persist personal-RPG aggregate header with optimistic version.

    @param previous 更新前进度 / Profile before mutation.
    @param current 更新后进度 / Profile after mutation.
    @param connection 当前事务连接 / Current transactional connection.
    @return None / None.
    @raise ValueError 更新前后个人范围不一致时抛出 / Raised when scopes differ across mutation.
    @raise RuntimeError 锁持有期间版本意外改变时抛出 / Raised when version changes unexpectedly while locked.
    """

    if previous.scope != current.scope:
        raise ValueError("Personal RPG profile mutation cannot change scope")
    changed = await db_connection.execute(
        "UPDATE personal_rpg.characters SET name = %s, experience = %s, "
        "last_exploration_day = %s, character_version = %s, profile_version = %s, "
        "updated_at = CURRENT_TIMESTAMP WHERE user_id = %s AND profile_version = %s",
        (
            current.character.name,
            current.character.experience,
            current.last_exploration_day,
            current.character.version,
            current.version,
            current.scope.user_id,
            previous.version,
        ),
        connection=connection,
    )
    if changed != 1:
        raise RuntimeError("Personal RPG profile changed while it was locked")


async def _replace_materials(
    profile: PersonalRpgProfile,
    connection: AsyncConnection,
) -> None:
    """@brief 在已锁定聚合中替换材料投影 / Replace material projection under the already locked aggregate.

    @param profile 材料已更新的个人进度 / Personal profile with updated materials.
    @param connection 当前事务连接 / Current transactional connection.
    @return None / None.
    @note 材料类别最多为固定小集合；删除后按稳定编码插入可消除残留零数量行。/
        Material kinds form a fixed small set; delete-then-insert by stable code removes stale zero rows.
    """

    await db_connection.execute(
        "DELETE FROM personal_rpg.materials WHERE user_id = %s",
        (profile.scope.user_id,),
        connection=connection,
    )
    for kind, quantity in sorted(
        profile.materials.quantities.items(),
        key=lambda item: item[0].value,
    ):
        await db_connection.execute(
            "INSERT INTO personal_rpg.materials (user_id, material_kind, quantity) "
            "VALUES (%s, %s, %s)",
            (profile.scope.user_id, kind.value, quantity),
            connection=connection,
        )


async def _exploration_day_exists(
    scope: PersonalScope,
    exploration_day: date,
    connection: AsyncConnection,
) -> bool:
    """@brief 锁定检查某人某日是否已有探索 / Lock-check whether one person already explored on a day.

    @param scope 个人范围 / Personal scope.
    @param exploration_day UTC 业务日 / UTC business day.
    @param connection 当前事务连接 / Current transactional connection.
    @return 已有探索时为 True / True when an exploration already exists.
    """

    row = await db_connection.fetch_one(
        "SELECT exploration_id FROM personal_rpg.explorations "
        "WHERE user_id = %s AND exploration_day = %s FOR UPDATE",
        (scope.user_id, exploration_day),
        connection=connection,
    )
    return row is not None


async def _exploration_id_exists(
    exploration_id: UUID,
    connection: AsyncConnection,
) -> bool:
    """@brief 锁定检查探索 ID 是否已被使用 / Lock-check whether an exploration ID is already used.

    @param exploration_id 稳定探索标识 / Stable exploration identity.
    @param connection 当前事务连接 / Current transactional connection.
    @return 标识已被使用时为 True / True when identity is already used.
    """

    row = await db_connection.fetch_one(
        "SELECT exploration_id FROM personal_rpg.explorations "
        "WHERE exploration_id = %s FOR UPDATE",
        (exploration_id,),
        connection=connection,
    )
    return row is not None


async def _insert_exploration(
    exploration: DailyExploration,
    connection: AsyncConnection,
) -> None:
    """@brief 保存已验证的每日探索审计快照 / Persist a validated daily-exploration audit snapshot.

    @param exploration 已验证探索快照 / Validated exploration snapshot.
    @param connection 当前事务连接 / Current transactional connection.
    @return None / None.
    """

    await db_connection.execute(
        "INSERT INTO personal_rpg.explorations ("
        "exploration_id, user_id, exploration_day, route, explored_at, experience_reward, "
        "material_rewards, audit_digest"
        ") VALUES (%s, %s, %s, %s, %s, %s, CAST(%s AS JSONB), %s)",
        (
            exploration.exploration_id,
            exploration.scope.user_id,
            exploration.day,
            exploration.route.value,
            exploration.explored_at,
            exploration.reward.experience,
            json.dumps(_material_mapping(exploration.reward.materials)),
            exploration.audit_digest,
        ),
        connection=connection,
    )


async def _craft_id_exists(
    craft_id: UUID,
    connection: AsyncConnection,
) -> bool:
    """@brief 锁定检查制作 ID 是否已被使用 / Lock-check whether a crafting ID is already used.

    @param craft_id 稳定制作标识 / Stable crafting identity.
    @param connection 当前事务连接 / Current transactional connection.
    @return 标识已被使用时为 True / True when identity is already used.
    """

    row = await db_connection.fetch_one(
        "SELECT craft_id FROM personal_rpg.collections WHERE craft_id = %s FOR UPDATE",
        (craft_id,),
        connection=connection,
    )
    return row is not None


async def _insert_collection(
    command: CraftPersonalRecipe,
    recipe: CraftingRecipe,
    connection: AsyncConnection,
) -> None:
    """@brief 保存一条已制作的收藏图鉴记录 / Persist one crafted compendium record.

    @param command 已验证制作命令 / Validated crafting command.
    @param recipe 对应固定配方 / Corresponding fixed recipe.
    @param connection 当前事务连接 / Current transactional connection.
    @return None / None.
    """

    await db_connection.execute(
        "INSERT INTO personal_rpg.collections ("
        "user_id, collectible_kind, recipe_code, craft_id, crafted_at"
        ") VALUES (%s, %s, %s, %s, %s)",
        (
            command.scope.user_id,
            recipe.output.value,
            recipe.code.value,
            command.craft_id,
            command.crafted_at,
        ),
        connection=connection,
    )


def _create_fingerprint(command: CreatePersonalCharacter) -> dict[str, object]:
    """@brief 构造创建角色命令的规范语义指纹 / Construct canonical semantics fingerprint for character creation.

    @param command 创建角色命令 / Character-creation command.
    @return JSON 兼容语义指纹 / JSON-compatible semantics fingerprint.
    """

    return {
        "name": command.name,
        "created_at": command.created_at.isoformat(),
    }


def _exploration_fingerprint(command: ExploreDaily) -> dict[str, object]:
    """@brief 构造每日探索命令的规范语义指纹 / Construct canonical semantics fingerprint for daily exploration.

    @param command 每日探索命令 / Daily-exploration command.
    @return JSON 兼容语义指纹 / JSON-compatible semantics fingerprint.
    """

    return {
        "exploration_id": str(command.exploration_id),
        "day": command.day.isoformat(),
        "route": command.route.value,
        "explored_at": command.explored_at.isoformat(),
    }


def _craft_fingerprint(command: CraftPersonalRecipe) -> dict[str, object]:
    """@brief 构造制作配方命令的规范语义指纹 / Construct canonical semantics fingerprint for recipe crafting.

    @param command 制作配方命令 / Recipe-crafting command.
    @return JSON 兼容语义指纹 / JSON-compatible semantics fingerprint.
    """

    return {
        "craft_id": str(command.craft_id),
        "recipe_code": command.recipe_code.value,
        "crafted_at": command.crafted_at.isoformat(),
    }


def _result_mapping(result: PersonalRpgResult) -> dict[str, object]:
    """@brief 序列化完整个人 RPG 结果供幂等回放 / Serialize complete personal-RPG result for idempotent replay.

    @param result 个人 RPG 应用结果 / Personal-RPG application result.
    @return 完整 JSON 兼容结果 / Complete JSON-compatible result.
    """

    return {
        "code": result.code.value,
        "profile": _profile_mapping(result.profile)
        if result.profile is not None
        else None,
        "exploration": (
            _exploration_mapping(result.exploration)
            if result.exploration is not None
            else None
        ),
        "recipe": _recipe_mapping(result.recipe) if result.recipe is not None else None,
    }


def _result_from_mapping(
    value: Mapping[str, Any],
    *,
    replayed: bool,
) -> PersonalRpgResult:
    """@brief 从完整 JSON 回执还原个人 RPG 结果 / Restore personal-RPG result from complete JSON receipt.

    @param value 完整 JSON 回执对象 / Complete JSON receipt object.
    @param replayed 是否标记为幂等回放 / Whether to mark as idempotent replay.
    @return 还原的个人 RPG 应用结果 / Restored personal-RPG application result.
    @raise ValueError 回执字段缺失或非法时抛出 / Raised when receipt fields are missing or invalid.
    """

    raw_profile = value.get("profile")
    raw_exploration = value.get("exploration")
    raw_recipe = value.get("recipe")
    return PersonalRpgResult(
        code=PersonalRpgCode(_as_text(value.get("code"), field="Result code")),
        profile=(
            _profile_from_mapping(_mapping(raw_profile, field="Result profile"))
            if raw_profile is not None
            else None
        ),
        exploration=(
            _exploration_from_mapping(
                _mapping(raw_exploration, field="Result exploration")
            )
            if raw_exploration is not None
            else None
        ),
        recipe=(
            _recipe_from_mapping(_mapping(raw_recipe, field="Result recipe"))
            if raw_recipe is not None
            else None
        ),
        replayed=replayed,
    )


def _profile_mapping(profile: PersonalRpgProfile) -> dict[str, object]:
    """@brief 序列化个人 RPG 进度 / Serialize personal-RPG progression.

    @param profile 个人 RPG 进度 / Personal-RPG progression.
    @return JSON 兼容进度对象 / JSON-compatible progression object.
    """

    return {
        "scope": profile.scope.user_id,
        "character": {
            "name": profile.character.name,
            "experience": profile.character.experience,
            "version": profile.character.version,
        },
        "materials": _material_mapping(profile.materials),
        "discovered": sorted(kind.value for kind in profile.compendium.discovered),
        "last_exploration_day": (
            profile.last_exploration_day.isoformat()
            if profile.last_exploration_day is not None
            else None
        ),
        "version": profile.version,
    }


def _profile_from_mapping(value: Mapping[str, Any]) -> PersonalRpgProfile:
    """@brief 从 JSON 还原个人 RPG 进度 / Restore personal-RPG progression from JSON.

    @param value JSON 进度对象 / JSON progression object.
    @return 还原的个人 RPG 进度 / Restored personal-RPG progression.
    @raise ValueError JSON 字段非法时抛出 / Raised when a JSON field is invalid.
    """

    raw_character = _mapping(value.get("character"), field="Profile character")
    raw_materials = _mapping(value.get("materials"), field="Profile materials")
    raw_discovered = value.get("discovered")
    if not isinstance(raw_discovered, list):
        raise ValueError("Profile discoveries must be a JSON list")
    material_quantities: dict[MaterialKind, int] = {}
    """@brief 从 JSON 材料对象重建的临时库存 / Temporary inventory rebuilt from JSON materials."""
    for raw_kind, raw_quantity in raw_materials.items():
        kind = MaterialKind(_as_text(raw_kind, field="Profile material kind"))
        if kind in material_quantities:
            raise ValueError("Profile materials repeat one kind")
        material_quantities[kind] = _as_int(
            raw_quantity,
            field="Profile material quantity",
        )
    discovered: set[CollectibleKind] = set()
    """@brief 从 JSON 图鉴数组重建的临时发现集合 / Temporary discovery set rebuilt from JSON compendium."""
    for raw_collectible in raw_discovered:
        collectible = CollectibleKind(
            _as_text(raw_collectible, field="Profile collectible kind")
        )
        if collectible in discovered:
            raise ValueError("Profile discoveries repeat one collectible")
        discovered.add(collectible)
    raw_last_day = value.get("last_exploration_day")
    return PersonalRpgProfile(
        character=PersonalCharacter(
            scope=PersonalScope(_as_int(value.get("scope"), field="Profile scope")),
            name=_as_text(raw_character.get("name"), field="Profile character name"),
            experience=_as_int(
                raw_character.get("experience"),
                field="Profile character experience",
            ),
            version=_as_int(
                raw_character.get("version"),
                field="Profile character version",
            ),
        ),
        materials=MaterialInventory(material_quantities),
        compendium=CollectionCompendium(frozenset(discovered)),
        last_exploration_day=(
            _date_from_iso(raw_last_day, field="Profile last exploration day")
            if raw_last_day is not None
            else None
        ),
        version=_as_int(value.get("version"), field="Profile version"),
    )


def _material_mapping(
    inventory: MaterialInventory | MaterialBundle,
) -> dict[str, int]:
    """@brief 序列化材料库存或材料束 / Serialize a material inventory or bundle.

    @param inventory 个人库存或固定材料束 / Personal inventory or fixed material bundle.
    @return 按稳定材料编码排序的 JSON 对象 / JSON object ordered by stable material code.
    """

    return {
        kind.value: quantity
        for kind, quantity in sorted(
            inventory.quantities.items(),
            key=lambda item: item[0].value,
        )
    }


def _exploration_mapping(exploration: DailyExploration) -> dict[str, object]:
    """@brief 序列化每日探索审计快照 / Serialize a daily-exploration audit snapshot.

    @param exploration 每日探索审计快照 / Daily-exploration audit snapshot.
    @return JSON 兼容探索对象 / JSON-compatible exploration object.
    """

    return {
        "exploration_id": str(exploration.exploration_id),
        "scope": exploration.scope.user_id,
        "day": exploration.day.isoformat(),
        "route": exploration.route.value,
        "explored_at": exploration.explored_at.isoformat(),
        "reward": {
            "experience": exploration.reward.experience,
            "materials": _material_mapping(exploration.reward.materials),
        },
        "audit_digest": exploration.audit_digest,
    }


def _exploration_from_mapping(value: Mapping[str, Any]) -> DailyExploration:
    """@brief 从 JSON 还原每日探索审计快照 / Restore daily-exploration audit snapshot from JSON.

    @param value JSON 探索对象 / JSON exploration object.
    @return 还原且经领域校验的探索 / Restored and domain-validated exploration.
    @raise ValueError JSON 字段非法或审计摘要不一致时抛出 /
        Raised when JSON fields are invalid or audit digest is inconsistent.
    """

    raw_reward = _mapping(value.get("reward"), field="Exploration reward")
    raw_materials = _mapping(raw_reward.get("materials"), field="Exploration materials")
    materials: dict[MaterialKind, int] = {}
    """@brief 从 JSON 奖励重建的临时材料束 / Temporary material bundle rebuilt from JSON reward."""
    for raw_kind, raw_quantity in raw_materials.items():
        kind = MaterialKind(_as_text(raw_kind, field="Exploration material kind"))
        if kind in materials:
            raise ValueError("Exploration materials repeat one kind")
        materials[kind] = _as_int(
            raw_quantity,
            field="Exploration material quantity",
        )
    return DailyExploration(
        exploration_id=UUID(
            _as_text(value.get("exploration_id"), field="Exploration ID")
        ),
        scope=PersonalScope(_as_int(value.get("scope"), field="Exploration scope")),
        day=_date_from_iso(value.get("day"), field="Exploration day"),
        route=ExplorationRoute(_as_text(value.get("route"), field="Exploration route")),
        explored_at=_datetime_from_iso(
            value.get("explored_at"),
            field="Exploration time",
        ),
        reward=ExplorationReward(
            experience=_as_int(
                raw_reward.get("experience"),
                field="Exploration experience",
            ),
            materials=MaterialBundle(materials),
        ),
        audit_digest=_as_text(
            value.get("audit_digest"), field="Exploration audit digest"
        ),
    )


def _recipe_mapping(recipe: CraftingRecipe) -> dict[str, object]:
    """@brief 序列化固定配方标识 / Serialize fixed recipe identity.

    @param recipe 固定制作配方 / Fixed crafting recipe.
    @return JSON 兼容配方对象 / JSON-compatible recipe object.
    """

    return {"code": recipe.code.value}


def _recipe_from_mapping(value: Mapping[str, Any]) -> CraftingRecipe:
    """@brief 从 JSON 还原固定配方 / Restore fixed recipe from JSON.

    @param value JSON 配方对象 / JSON recipe object.
    @return 当前规则集中的固定配方 / Fixed recipe in the current ruleset.
    """

    return recipe_for(RecipeCode(_as_text(value.get("code"), field="Recipe code")))


def _json_mapping(value: object, *, field: str) -> Mapping[str, Any]:
    """@brief 将驱动返回的 JSONB 规范为对象映射 / Normalize driver-returned JSONB into an object mapping.

    @param value PostgreSQL JSONB 值 / PostgreSQL JSONB value.
    @param field 错误信息中的字段名称 / Field name used in error messages.
    @return JSON 对象映射 / JSON object mapping.
    @raise ValueError JSON 值不是对象时抛出 / Raised when JSON value is not an object.
    """

    decoded: object = json.loads(value) if isinstance(value, str) else value
    return _mapping(decoded, field=field)


def _mapping(value: object, *, field: str) -> Mapping[str, Any]:
    """@brief 校验一个对象为字符串键映射 / Validate an object as a string-key mapping.

    @param value 待校验对象 / Object to validate.
    @param field 错误信息中的字段名称 / Field name used in error messages.
    @return 已校验对象映射 / Validated object mapping.
    @raise ValueError 不是对象或含非字符串键时抛出 /
        Raised when value is not an object or contains non-string keys.
    """

    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{field} must be a JSON object")
    return cast(Mapping[str, Any], value)


def _as_text(value: object, *, field: str) -> str:
    """@brief 读取非空字符串 / Read a non-empty string.

    @param value 原始值 / Raw value.
    @param field 错误信息中的字段名称 / Field name used in error messages.
    @return 非空字符串 / Non-empty string.
    @raise ValueError 值不是非空字符串时抛出 / Raised when value is not a non-empty string.
    """

    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _as_int(value: object, *, field: str) -> int:
    """@brief 读取严格整数 / Read a strict integer.

    @param value 原始值 / Raw value.
    @param field 错误信息中的字段名称 / Field name used in error messages.
    @return 严格整数 / Strict integer.
    @raise ValueError 值不是整数或为布尔值时抛出 / Raised when value is not an integer or is Boolean.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    return value


def _as_day(value: object, *, field: str) -> date:
    """@brief 读取数据库 date 值 / Read a database date value.

    @param value 原始数据库值 / Raw database value.
    @param field 错误信息中的字段名称 / Field name used in error messages.
    @return 纯日期 / Plain date.
    @raise ValueError 值不是纯日期时抛出 / Raised when value is not a plain date.
    """

    if isinstance(value, datetime) or not isinstance(value, date):
        raise ValueError(f"{field} must be a date")
    return value


def _date_from_iso(value: object, *, field: str) -> date:
    """@brief 从 ISO 文本读取纯日期 / Read a plain date from ISO text.

    @param value ISO 日期文本 / ISO date text.
    @param field 错误信息中的字段名称 / Field name used in error messages.
    @return 纯日期 / Plain date.
    @raise ValueError 文本不是 ISO 日期时抛出 / Raised when text is not an ISO date.
    """

    try:
        return date.fromisoformat(_as_text(value, field=field))
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO date") from error


def _datetime_from_iso(value: object, *, field: str) -> datetime:
    """@brief 从 ISO 文本读取并规范化 UTC 时刻 / Read and normalize UTC instant from ISO text.

    @param value ISO 时刻文本 / ISO instant text.
    @param field 错误信息中的字段名称 / Field name used in error messages.
    @return UTC aware 时刻 / UTC-aware instant.
    @raise ValueError 文本不是带时区 ISO 时刻时抛出 / Raised when text is not a timezone-aware ISO instant.
    """

    try:
        parsed = datetime.fromisoformat(_as_text(value, field=field))
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO datetime") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(UTC)


__all__ = ["PostgresPersonalRpgOperations"]
"""@brief 对外导出的 PostgreSQL 个人 RPG 端口实现 / Exported PostgreSQL personal-RPG port implementation."""
