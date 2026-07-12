"""@brief RPG 角色与战斗 PostgreSQL adapter / PostgreSQL adapter for RPG characters and battles."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
import math
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.games.rpg.character_models import (
    FightMonster,
    FightPlayer,
    HealCharacter,
    MonsterBattleResult,
    PlayerBattleResult,
    RpgMutationResult,
    RpgProfile,
    SetBattleAllowance,
)
from fogmoe_bot.application.games.rpg.common import RpgCode
from fogmoe_bot.application.games.ports.rpg.character import RpgCharacterOperations
from fogmoe_bot.domain.games import (
    BattleResult,
    BattleTurn,
    Character,
    Combatant,
    LevelUp,
    MONSTERS,
    MonsterBattle,
    PlayerBattle,
    experience_gain,
    fight_monster,
    fight_players,
)
from fogmoe_bot.infrastructure.database import connection as db_connection

from ..common import (
    _Account,
    _AccountOperations,
    _credit_free,
    _json_object,
    _load_receipt,
    _lock_account,
    _lock_accounts,
    _lock_receipt_key,
    _read_account,
    _save_receipt,
)
from .common import (
    _character_from_json,
    _character_to_json,
    _level_up_from_json,
    _level_up_to_json,
    _load_character,
    _lock_character,
    _lock_characters,
    _save_character,
)

_PLAYER_BATTLE_COOLDOWN = timedelta(hours=1)
"""@brief 玩家战斗旧冷却 / Legacy player-battle cooldown."""

_MONSTER_BATTLE_COOLDOWN = timedelta(minutes=5)
"""@brief 怪物战斗旧冷却 / Legacy monster-battle cooldown."""


class PostgresRpgCharacterOperations(_AccountOperations, RpgCharacterOperations):
    """@brief RPG 角色、账户与冷却稳定锁序 adapter / RPG character adapter with stable account and cooldown lock order."""

    def __init__(self, *, admin_user_id: int) -> None:
        """@brief 注入管理员身份 / Inject the administrator identity.

        @param admin_user_id 管理员用户 ID / Administrator user ID.
        """

        super().__init__(admin_user_id=admin_user_id)

    async def ensure_rpg_profile(self, user_id: int) -> RpgProfile:
        """@brief 为已注册用户读取或创建角色 / Read or create a character for a registered user.

        @param user_id 玩家 ID / Player ID.
        @return RPG 状态 / RPG profile.
        """

        async with db_connection.transaction() as connection:
            account = await _read_account(user_id, connection)
            if account is None:
                return RpgProfile(RpgCode.NOT_REGISTERED)
            inserted = await db_connection.fetch_one(
                "INSERT INTO game.rpg_characters "
                "(user_id, level, hp, max_hp, atk, matk, def, experience, "
                "allow_battle, version) VALUES (%s, 1, 10, 10, 2, 0, 1, 0, TRUE, 0) "
                "ON CONFLICT (user_id) DO NOTHING RETURNING user_id",
                (user_id,),
                connection=connection,
            )
            character = await _load_character(user_id, connection)
            if character is None:
                raise RuntimeError("RPG character insert/load invariant failed")
            return RpgProfile(
                RpgCode.SUCCESS,
                character,
                account.total,
                inserted is not None,
            )

    async def rpg_profile(self, user_id: int) -> RpgProfile:
        """@brief 只读角色与账户余额 / Read a character and account balance without creating it.

        @param user_id 玩家 ID / Player ID.
        @return RPG 状态 / RPG profile.
        """

        account = await _read_account(user_id, None)
        if account is None:
            return RpgProfile(RpgCode.NOT_REGISTERED)
        character = await _load_character(user_id, None)
        if character is None:
            return RpgProfile(RpgCode.NO_CHARACTER, balance=account.total)
        return RpgProfile(RpgCode.SUCCESS, character, account.total)

    async def set_battle_allowance(
        self, command: SetBattleAllowance
    ) -> RpgMutationResult:
        """@brief 幂等设置被挑战开关 / Idempotently set challenge allowance.

        @param command 设置命令 / Setting command.
        @return 变更结果 / Mutation result.
        """

        async with db_connection.transaction() as connection:
            await _lock_receipt_key(command.idempotency_key, connection)
            replay = await _load_receipt(
                command.idempotency_key, "rpg.allow", command.user_id, connection
            )
            if replay is not None:
                return _rpg_mutation_from_json(replay, replayed=True)
            character = await _lock_character(command.user_id, connection)
            if character is None:
                result = RpgMutationResult(RpgCode.NO_CHARACTER)
            else:
                updated = character.set_battle_allowance(command.allow)
                await _save_character(updated, character.version, connection)
                result = RpgMutationResult(RpgCode.SUCCESS, updated)
            await _save_receipt(
                command.idempotency_key,
                "rpg.allow",
                command.user_id,
                _rpg_mutation_to_json(result),
                connection,
            )
            return result

    async def heal_character(self, command: HealCharacter) -> RpgMutationResult:
        """@brief 以 account→character 锁序原子治疗 / Atomically heal under account-to-character lock order.

        @param command 治疗命令 / Heal command.
        @return 治疗结果 / Heal result.
        """

        async with db_connection.transaction() as connection:
            await _lock_receipt_key(command.idempotency_key, connection)
            replay = await _load_receipt(
                command.idempotency_key, "rpg.heal", command.user_id, connection
            )
            if replay is not None:
                return _rpg_mutation_from_json(replay, replayed=True)
            account = await _lock_account(command.user_id, connection)
            if account is None:
                result = RpgMutationResult(RpgCode.NOT_REGISTERED)
            else:
                character = await _lock_character(command.user_id, connection)
                if character is None:
                    result = RpgMutationResult(
                        RpgCode.NO_CHARACTER, balance=account.total
                    )
                elif character.hp >= character.max_hp:
                    result = RpgMutationResult(
                        RpgCode.ALREADY_FULL_HP, character, account.total
                    )
                elif not await self._spend_account(account, command.cost, connection):
                    result = RpgMutationResult(
                        RpgCode.INSUFFICIENT_COINS, character, account.total
                    )
                else:
                    updated = character.heal()
                    await _save_character(updated, character.version, connection)
                    result = RpgMutationResult(
                        RpgCode.SUCCESS,
                        updated,
                        account.total - command.cost,
                    )
            await _save_receipt(
                command.idempotency_key,
                "rpg.heal",
                command.user_id,
                _rpg_mutation_to_json(result),
                connection,
            )
            return result

    async def fight_monster(self, command: FightMonster) -> MonsterBattleResult:
        """@brief 在 durable cooldown 下原子执行 PVE / Atomically execute PVE under a durable cooldown.

        @param command PVE 命令 / PVE command.
        @return 已提交战斗结果 / Committed battle result.
        """

        monster = MONSTERS.get(command.monster_id)
        if monster is None:
            return MonsterBattleResult(RpgCode.NOT_FOUND)
        async with db_connection.transaction() as connection:
            await _lock_receipt_key(command.idempotency_key, connection)
            replay = await _load_receipt(
                command.idempotency_key,
                "rpg.monster_battle",
                command.user_id,
                connection,
            )
            if replay is not None:
                return _monster_battle_from_json(replay, replayed=True)
            last_battle = await _lock_cooldown(command.user_id, "monster", connection)
            if last_battle is not None:
                remaining = last_battle + _MONSTER_BATTLE_COOLDOWN - command.now
                if remaining > timedelta():
                    result = MonsterBattleResult(
                        RpgCode.COOLDOWN,
                        monster=monster,
                        cooldown_remaining=remaining,
                    )
                    await _save_receipt(
                        command.idempotency_key,
                        "rpg.monster_battle",
                        command.user_id,
                        _monster_battle_to_json(result),
                        connection,
                    )
                    return result
            account = await _lock_account(command.user_id, connection)
            if account is None:
                result = MonsterBattleResult(RpgCode.NOT_REGISTERED, monster)
            else:
                character = await _lock_character(command.user_id, connection)
                if character is None:
                    result = MonsterBattleResult(RpgCode.NO_CHARACTER, monster)
                elif character.hp <= 0:
                    result = MonsterBattleResult(
                        RpgCode.DEAD,
                        monster,
                        character=character,
                        balance=account.total,
                    )
                else:
                    battle = fight_monster(
                        Combatant(command.display_name, character), monster
                    )
                    updated = character.with_hp(battle.player_hp)
                    level_up: LevelUp | None = None
                    experience_reward = 0
                    coin_reward = 0
                    if battle.result is BattleResult.WIN:
                        experience_reward = monster.experience_reward
                        coin_reward = monster.coin_reward
                        updated, level_up = updated.gain_experience(experience_reward)
                        await _credit_free(command.user_id, coin_reward, connection)
                    await _save_character(updated, character.version, connection)
                    await _set_cooldown(
                        command.user_id, "monster", command.now, connection
                    )
                    result = MonsterBattleResult(
                        RpgCode.SUCCESS,
                        monster,
                        battle,
                        updated,
                        account.total + coin_reward,
                        experience_reward,
                        coin_reward,
                        level_up,
                    )
            await _save_receipt(
                command.idempotency_key,
                "rpg.monster_battle",
                command.user_id,
                _monster_battle_to_json(result),
                connection,
            )
            return result

    async def fight_player(self, command: FightPlayer) -> PlayerBattleResult:
        """@brief 按稳定双用户锁序原子执行 PVP / Atomically execute PVP with stable two-user lock ordering.

        @param command PVP 命令 / PVP command.
        @return 已提交战斗结果 / Committed battle result.
        """

        target_name = command.target_username.strip().lstrip("@")
        if not target_name:
            return PlayerBattleResult(RpgCode.TARGET_NOT_FOUND)
        async with db_connection.transaction() as connection:
            await _lock_receipt_key(command.idempotency_key, connection)
            replay = await _load_receipt(
                command.idempotency_key,
                "rpg.player_battle",
                command.attacker_id,
                connection,
            )
            if replay is not None:
                return _player_battle_from_json(replay, replayed=True)
            target_row = await db_connection.fetch_one(
                "SELECT id, name FROM identity.users WHERE name = %s",
                (target_name,),
                connection=connection,
            )
            if target_row is None:
                result = PlayerBattleResult(RpgCode.TARGET_NOT_FOUND)
            else:
                defender_id = int(target_row[0])
                defender_name = str(target_row[1])
                if defender_id == command.attacker_id:
                    result = PlayerBattleResult(RpgCode.SELF_TARGET)
                else:
                    last_battle = await _lock_cooldown(
                        command.attacker_id, "player", connection
                    )
                    if (
                        last_battle is not None
                        and (
                            remaining := (
                                last_battle + _PLAYER_BATTLE_COOLDOWN - command.now
                            )
                        )
                        > timedelta()
                    ):
                        result = PlayerBattleResult(
                            RpgCode.COOLDOWN,
                            command.attacker_name,
                            defender_name,
                            cooldown_remaining=remaining,
                        )
                    else:
                        accounts = await _lock_accounts(
                            (command.attacker_id, defender_id), connection
                        )
                        attacker_account = accounts.get(command.attacker_id)
                        defender_account = accounts.get(defender_id)
                        if attacker_account is None:
                            result = PlayerBattleResult(RpgCode.NOT_REGISTERED)
                        elif defender_account is None:
                            result = PlayerBattleResult(RpgCode.TARGET_NOT_FOUND)
                        else:
                            characters = await _lock_characters(
                                (command.attacker_id, defender_id), connection
                            )
                            attacker = characters.get(command.attacker_id)
                            defender = characters.get(defender_id)
                            if attacker is None:
                                result = PlayerBattleResult(RpgCode.NO_CHARACTER)
                            elif attacker.hp <= 0:
                                result = PlayerBattleResult(RpgCode.DEAD)
                            elif defender is None:
                                result = PlayerBattleResult(
                                    RpgCode.TARGET_NO_CHARACTER,
                                    command.attacker_name,
                                    defender_name,
                                )
                            elif not defender.allow_battle:
                                result = PlayerBattleResult(
                                    RpgCode.TARGET_DISALLOWS,
                                    command.attacker_name,
                                    defender_name,
                                )
                            elif defender.hp <= 0:
                                result = PlayerBattleResult(
                                    RpgCode.TARGET_DEAD,
                                    command.attacker_name,
                                    defender_name,
                                )
                            else:
                                result = await self._commit_player_battle(
                                    command,
                                    defender_name,
                                    attacker,
                                    defender,
                                    attacker_account,
                                    defender_account,
                                    connection,
                                )
            await _save_receipt(
                command.idempotency_key,
                "rpg.player_battle",
                command.attacker_id,
                _player_battle_to_json(result),
                connection,
            )
            return result

    async def _commit_player_battle(
        self,
        command: FightPlayer,
        defender_name: str,
        attacker: Character,
        defender: Character,
        attacker_account: _Account,
        defender_account: _Account,
        connection: AsyncConnection,
    ) -> PlayerBattleResult:
        """@brief 在已锁资源上提交 PVP / Commit PVP over already locked resources.

        @param command PVP 命令 / PVP command.
        @param defender_name 被挑战者名称 / Defender display name.
        @param attacker 已锁挑战者角色 / Locked attacker character.
        @param defender 已锁被挑战者角色 / Locked defender character.
        @param attacker_account 已锁挑战者账户 / Locked attacker account.
        @param defender_account 已锁被挑战者账户 / Locked defender account.
        @param connection 活动事务 / Active transaction.
        @return 已提交结果 / Committed result.
        """

        battle = fight_players(
            Combatant(command.attacker_name, attacker),
            Combatant(defender_name, defender),
        )
        updated: dict[int, Character] = {}
        winner_name: str | None = None
        loser_name: str | None = None
        coins_lost = 0
        coins_awarded = 0
        experience_awarded = 0
        level_up: LevelUp | None = None
        # 保留旧契约：无胜者的回合上限平局仅消耗冷却，不持久化模拟伤害。
        # Preserve the legacy contract: a max-turn draw without a winner only
        # consumes the cooldown and does not persist simulated HP damage.
        if battle.winner_id is not None and battle.loser_id is not None:
            updated = {
                attacker.user_id: attacker.with_hp(battle.attacker_hp),
                defender.user_id: defender.with_hp(battle.defender_hp),
            }
            winner_name = (
                command.attacker_name
                if battle.winner_id == attacker.user_id
                else defender_name
            )
            loser_name = (
                command.attacker_name
                if battle.loser_id == attacker.user_id
                else defender_name
            )
            winner_account = (
                attacker_account
                if battle.winner_id == attacker.user_id
                else defender_account
            )
            loser_account = (
                attacker_account
                if battle.loser_id == attacker.user_id
                else defender_account
            )
            coins_lost = math.floor(loser_account.total * 0.10)
            coins_awarded = math.floor(coins_lost * 0.8)
            if coins_lost:
                spent = await self._spend_account(loser_account, coins_lost, connection)
                if not spent:
                    raise RuntimeError("Locked loser balance changed unexpectedly")
            if coins_awarded:
                await _credit_free(winner_account.user_id, coins_awarded, connection)
            winner_character = updated[battle.winner_id]
            loser_character = updated[battle.loser_id]
            experience_awarded = experience_gain(
                winner_character.computed_level, loser_character.computed_level
            )
            updated[battle.winner_id], level_up = winner_character.gain_experience(
                experience_awarded
            )
        originals = {attacker.user_id: attacker, defender.user_id: defender}
        for user_id in sorted(updated):
            await _save_character(
                updated[user_id], originals[user_id].version, connection
            )
        await _set_cooldown(command.attacker_id, "player", command.now, connection)
        return PlayerBattleResult(
            RpgCode.SUCCESS,
            command.attacker_name,
            defender_name,
            battle,
            winner_name,
            loser_name,
            coins_lost,
            coins_awarded,
            experience_awarded,
            level_up,
        )


async def _lock_cooldown(
    user_id: int, battle_kind: str, connection: AsyncConnection
) -> datetime | None:
    """@brief 创建并锁定 durable 战斗冷却 gate / Create and lock a durable battle-cooldown gate.

    @param user_id 玩家 ID / Player ID.
    @param battle_kind ``player`` 或 ``monster`` / ``player`` or ``monster``.
    @param connection 活动事务 / Active transaction.
    @return 上次战斗时间 / Last battle time.
    """

    if battle_kind not in {"player", "monster"}:
        raise ValueError("Invalid RPG battle kind")
    await db_connection.execute(
        "INSERT INTO game.rpg_battle_cooldowns (user_id, battle_kind, version) "
        "VALUES (%s, %s, 0) ON CONFLICT (user_id, battle_kind) DO NOTHING",
        (user_id, battle_kind),
        connection=connection,
    )
    row = await db_connection.fetch_one(
        "SELECT last_battle_at FROM game.rpg_battle_cooldowns "
        "WHERE user_id = %s AND battle_kind = %s FOR UPDATE",
        (user_id, battle_kind),
        connection=connection,
    )
    if row is None:
        raise RuntimeError("Ensured battle cooldown row was not loadable")
    return cast(datetime | None, row[0])


async def _set_cooldown(
    user_id: int,
    battle_kind: str,
    at: datetime,
    connection: AsyncConnection,
) -> None:
    """@brief 推进已锁冷却时间 / Advance an already locked cooldown.

    @param user_id 玩家 ID / Player ID.
    @param battle_kind 战斗种类 / Battle kind.
    @param at 新时间 / New time.
    @param connection 活动事务 / Active transaction.
    @return None / None.
    """

    affected = await db_connection.execute(
        "UPDATE game.rpg_battle_cooldowns SET last_battle_at = %s, "
        "version = version + 1 WHERE user_id = %s AND battle_kind = %s",
        (at, user_id, battle_kind),
        connection=connection,
    )
    if affected != 1:
        raise RuntimeError("Battle cooldown update lost its locked row")


def _rpg_mutation_to_json(result: RpgMutationResult) -> dict[str, object]:
    """@brief 序列化 RPG 简单变更回执 / Serialize an RPG mutation receipt.

    @param result 变更结果 / Mutation result.
    @return 版本化 JSON / Versioned JSON.
    """

    return {
        "schema": 1,
        "code": result.code.value,
        "character": _character_to_json(result.character),
        "balance": result.balance,
    }


def _rpg_mutation_from_json(
    value: Mapping[str, Any], *, replayed: bool
) -> RpgMutationResult:
    """@brief 解析 RPG 简单变更回执 / Parse an RPG mutation receipt.

    @param value 回执 JSON / Receipt JSON.
    @param replayed 是否回放 / Whether replayed.
    @return 变更结果 / Mutation result.
    """

    return RpgMutationResult(
        RpgCode(str(value["code"])),
        _character_from_json(value.get("character")),
        int(value["balance"]) if value.get("balance") is not None else None,
        replayed,
    )


def _turns_to_json(turns: Sequence[BattleTurn]) -> list[dict[str, object]]:
    """@brief 序列化战斗动作 / Serialize battle actions.

    @param turns 动作序列 / Action sequence.
    @return JSON 数组 / JSON array.
    """

    return [
        {
            "number": turn.number,
            "attacker": turn.attacker,
            "defender": turn.defender,
            "damage": turn.damage,
            "remaining_hp": turn.remaining_hp,
        }
        for turn in turns
    ]


def _turns_from_json(value: object) -> tuple[BattleTurn, ...]:
    """@brief 解析战斗动作 / Parse battle actions.

    @param value JSON 数组 / JSON array.
    @return 动作元组 / Action tuple.
    """

    if not isinstance(value, list):
        raise ValueError("Receipt battle turns must be an array")
    turns: list[BattleTurn] = []
    for raw in value:
        data = _json_object(raw)
        turns.append(
            BattleTurn(
                int(data["number"]),
                str(data["attacker"]),
                str(data["defender"]),
                int(data["damage"]),
                int(data["remaining_hp"]),
            )
        )
    return tuple(turns)


def _monster_battle_value_to_json(battle: MonsterBattle | None) -> object:
    """@brief 序列化可选 PVE 过程 / Serialize an optional PVE process.

    @param battle PVE 结果 / PVE result.
    @return JSON 值 / JSON value.
    """

    if battle is None:
        return None
    return {
        "player_hp": battle.player_hp,
        "monster_hp": battle.monster_hp,
        "result": battle.result.value,
        "turns": _turns_to_json(battle.turns),
    }


def _monster_battle_value_from_json(value: object) -> MonsterBattle | None:
    """@brief 解析可选 PVE 过程 / Parse an optional PVE process.

    @param value JSON 值 / JSON value.
    @return PVE 结果或 None / PVE result or None.
    """

    if value is None:
        return None
    data = _json_object(value)
    return MonsterBattle(
        int(data["player_hp"]),
        int(data["monster_hp"]),
        BattleResult(str(data["result"])),
        _turns_from_json(data["turns"]),
    )


def _monster_battle_to_json(result: MonsterBattleResult) -> dict[str, object]:
    """@brief 序列化 PVE 回执 / Serialize a PVE receipt.

    @param result PVE 结果 / PVE result.
    @return 版本化 JSON / Versioned JSON.
    """

    return {
        "schema": 1,
        "code": result.code.value,
        "monster_id": result.monster.monster_id if result.monster else None,
        "battle": _monster_battle_value_to_json(result.battle),
        "character": _character_to_json(result.character),
        "balance": result.balance,
        "experience_reward": result.experience_reward,
        "coin_reward": result.coin_reward,
        "level_up": _level_up_to_json(result.level_up),
        "cooldown_seconds": (
            result.cooldown_remaining.total_seconds()
            if result.cooldown_remaining is not None
            else None
        ),
    }


def _monster_battle_from_json(
    value: Mapping[str, Any], *, replayed: bool
) -> MonsterBattleResult:
    """@brief 解析 PVE 回执 / Parse a PVE receipt.

    @param value 回执 JSON / Receipt JSON.
    @param replayed 是否回放 / Whether replayed.
    @return PVE 结果 / PVE result.
    """

    monster_id = value.get("monster_id")
    cooldown = value.get("cooldown_seconds")
    return MonsterBattleResult(
        RpgCode(str(value["code"])),
        MONSTERS.get(str(monster_id)) if monster_id is not None else None,
        _monster_battle_value_from_json(value.get("battle")),
        _character_from_json(value.get("character")),
        int(value["balance"]) if value.get("balance") is not None else None,
        int(value.get("experience_reward", 0)),
        int(value.get("coin_reward", 0)),
        _level_up_from_json(value.get("level_up")),
        timedelta(seconds=float(cooldown)) if cooldown is not None else None,
        replayed,
    )


def _player_battle_value_to_json(battle: PlayerBattle | None) -> object:
    """@brief 序列化可选 PVP 过程 / Serialize an optional PVP process.

    @param battle PVP 结果 / PVP result.
    @return JSON 值 / JSON value.
    """

    if battle is None:
        return None
    return {
        "attacker_id": battle.attacker_id,
        "defender_id": battle.defender_id,
        "attacker_hp": battle.attacker_hp,
        "defender_hp": battle.defender_hp,
        "winner_id": battle.winner_id,
        "loser_id": battle.loser_id,
        "turns": _turns_to_json(battle.turns),
    }


def _player_battle_value_from_json(value: object) -> PlayerBattle | None:
    """@brief 解析可选 PVP 过程 / Parse an optional PVP process.

    @param value JSON 值 / JSON value.
    @return PVP 结果或 None / PVP result or None.
    """

    if value is None:
        return None
    data = _json_object(value)
    return PlayerBattle(
        int(data["attacker_id"]),
        int(data["defender_id"]),
        int(data["attacker_hp"]),
        int(data["defender_hp"]),
        int(data["winner_id"]) if data.get("winner_id") is not None else None,
        int(data["loser_id"]) if data.get("loser_id") is not None else None,
        _turns_from_json(data["turns"]),
    )


def _player_battle_to_json(result: PlayerBattleResult) -> dict[str, object]:
    """@brief 序列化 PVP 回执 / Serialize a PVP receipt.

    @param result PVP 结果 / PVP result.
    @return 版本化 JSON / Versioned JSON.
    """

    return {
        "schema": 1,
        "code": result.code.value,
        "attacker_name": result.attacker_name,
        "defender_name": result.defender_name,
        "battle": _player_battle_value_to_json(result.battle),
        "winner_name": result.winner_name,
        "loser_name": result.loser_name,
        "coins_lost": result.coins_lost,
        "coins_awarded": result.coins_awarded,
        "experience_awarded": result.experience_awarded,
        "level_up": _level_up_to_json(result.level_up),
        "cooldown_seconds": (
            result.cooldown_remaining.total_seconds()
            if result.cooldown_remaining is not None
            else None
        ),
    }


def _player_battle_from_json(
    value: Mapping[str, Any], *, replayed: bool
) -> PlayerBattleResult:
    """@brief 解析 PVP 回执 / Parse a PVP receipt.

    @param value 回执 JSON / Receipt JSON.
    @param replayed 是否回放 / Whether replayed.
    @return PVP 结果 / PVP result.
    """

    cooldown = value.get("cooldown_seconds")
    return PlayerBattleResult(
        RpgCode(str(value["code"])),
        str(value["attacker_name"]) if value.get("attacker_name") is not None else None,
        str(value["defender_name"]) if value.get("defender_name") is not None else None,
        _player_battle_value_from_json(value.get("battle")),
        str(value["winner_name"]) if value.get("winner_name") is not None else None,
        str(value["loser_name"]) if value.get("loser_name") is not None else None,
        int(value.get("coins_lost", 0)),
        int(value.get("coins_awarded", 0)),
        int(value.get("experience_awarded", 0)),
        _level_up_from_json(value.get("level_up")),
        timedelta(seconds=float(cooldown)) if cooldown is not None else None,
        replayed,
    )


__all__ = ["PostgresRpgCharacterOperations"]
