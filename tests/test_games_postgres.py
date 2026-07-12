"""@brief Games bounded context 的真实 PostgreSQL 契约 / Real-PostgreSQL contracts for the Games bounded context."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import os
from uuid import uuid4

import pytest

from fogmoe_bot.application.games.gamble.models import (
    GambleCode,
    OpenGamble,
    PlaceGambleBet,
    SettleGamble,
)
from fogmoe_bot.application.games.gamble.service import GambleService
from fogmoe_bot.application.games.omikuji.models import OmikujiCode
from fogmoe_bot.application.games.omikuji.service import OmikujiService
from fogmoe_bot.application.games.rpg.character_models import FightPlayer
from fogmoe_bot.application.games.rpg.character_service import RpgCharacterService
from fogmoe_bot.application.games.rpg.common import RpgCode
from fogmoe_bot.application.games.rpg.equipment_models import EquipItem
from fogmoe_bot.application.games.rpg.inventory_models import (
    AddInventoryItem,
    RemoveInventoryItem,
    UseItem,
)
from fogmoe_bot.application.games.rpg.inventory_service import RpgInventoryService
from fogmoe_bot.application.games.sicbo.models import (
    OpenSicBo,
    PlaySicBo,
    SelectSicBoBet,
    SicBoCode,
)
from fogmoe_bot.application.games.sicbo.service import SicBoService
from fogmoe_bot.domain.games import DiceRoll, SicBoBet
from fogmoe_bot.infrastructure.database import db
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database.game_operations.gamble import (
    PostgresGambleOperations,
)
from fogmoe_bot.infrastructure.database.game_operations.omikuji import (
    PostgresOmikujiOperations,
)
from fogmoe_bot.infrastructure.database.game_operations.rpg.character import (
    PostgresRpgCharacterOperations,
)
from fogmoe_bot.infrastructure.database.game_operations.rpg.equipment import (
    PostgresRpgEquipmentOperations,
)
from fogmoe_bot.infrastructure.database.game_operations.rpg.inventory import (
    PostgresRpgInventoryOperations,
)
from fogmoe_bot.infrastructure.database.game_operations.sicbo import (
    PostgresSicBoOperations,
)


def _user_id(offset: int) -> int:
    """@brief 生成不与 Telegram 用户冲突的 BIGINT / Generate a BIGINT disjoint from Telegram users.

    @param offset 同一测试内偏移 / Offset within one test.
    @return 正用户 ID / Positive user ID.
    """

    return 8_200_000_000_000_000_000 + int(uuid4().hex[:10], 16) * 10 + offset


def test_games_workflows_are_atomic_replayable_and_recoverable() -> None:
    """@brief 御神签、骰宝、奖池、PVP 与库存均原子可回放 / Omikuji, Sic Bo, pool, PVP, and inventory are atomic and replayable."""

    if os.environ.get("FOGMOE_TEST_POSTGRES") != "1":
        pytest.skip("set FOGMOE_TEST_POSTGRES=1 to run the real PostgreSQL contract")

    async def scenario() -> None:
        """@brief 驱动跨进程级真实数据库场景 / Drive a cross-process-grade real-database scenario."""

        users = tuple(_user_id(index) for index in range(4))
        opener, sicbo_user, attacker, defender = users
        suffix = uuid4().hex
        now = datetime.now(UTC)
        gamble_operations = PostgresGambleOperations(admin_user_id=1)
        gamble_service = GambleService(gamble_operations)
        sicbo_service = SicBoService(PostgresSicBoOperations(admin_user_id=1))
        omikuji_service = OmikujiService(PostgresOmikujiOperations(admin_user_id=1))
        character_service = RpgCharacterService(
            PostgresRpgCharacterOperations(admin_user_id=1)
        )
        equipment_operations = PostgresRpgEquipmentOperations()
        inventory_service = RpgInventoryService(PostgresRpgInventoryOperations())
        equipment_id: int | None = None
        item_id: int | None = None
        gamble_conversation: str | None = None
        try:
            await db_connection.execute(
                "INSERT INTO identity.users "
                "(id, tg_uid, provider, name, coins, coins_paid, user_plan, permission) "
                "VALUES (%s, %s, 'telegram', %s, 20, 0, 'free', 1), "
                "(%s, %s, 'telegram', %s, 100, 0, 'free', 0), "
                "(%s, %s, 'telegram', %s, 100, 0, 'free', 0), "
                "(%s, %s, 'telegram', %s, 100, 0, 'free', 0)",
                (
                    opener,
                    opener,
                    f"opener-{suffix}",
                    sicbo_user,
                    sicbo_user,
                    f"sicbo-{suffix}",
                    attacker,
                    attacker,
                    f"attacker-{suffix}",
                    defender,
                    defender,
                    f"defender-{suffix}",
                ),
            )

            first_draw, second_draw = await asyncio.gather(
                omikuji_service.draw(
                    user_id=opener,
                    day=now.date(),
                    idempotency_key=f"games-pg:omikuji:a:{suffix}",
                ),
                omikuji_service.draw(
                    user_id=opener,
                    day=now.date(),
                    idempotency_key=f"games-pg:omikuji:b:{suffix}",
                ),
            )
            assert {first_draw.code, second_draw.code} == {
                OmikujiCode.SUCCESS,
                OmikujiCode.ALREADY_DRAWN,
            }
            winning_draw = (
                first_draw if first_draw.code is OmikujiCode.SUCCESS else second_draw
            )
            replay = await omikuji_service.draw(
                user_id=opener,
                day=now.date(),
                idempotency_key=(
                    f"games-pg:omikuji:a:{suffix}"
                    if first_draw.code is OmikujiCode.SUCCESS
                    else f"games-pg:omikuji:b:{suffix}"
                ),
            )
            assert replay.replayed and replay.fortune is winning_draw.fortune

            sicbo = await sicbo_service.open(
                OpenSicBo(
                    sicbo_user,
                    sicbo_user,
                    101,
                    now,
                    now + timedelta(minutes=10),
                    f"games-pg:sicbo:open:{suffix}",
                )
            )
            assert sicbo.code is SicBoCode.SUCCESS and sicbo.session is not None
            selected = await sicbo_service.select_bet(
                SelectSicBoBet(
                    sicbo.session.session_id,
                    sicbo_user,
                    SicBoBet.BIG,
                    sicbo.session.version,
                    now,
                    f"games-pg:sicbo:select:{suffix}",
                )
            )
            assert selected.session is not None
            play_command = PlaySicBo(
                selected.session.session_id,
                sicbo_user,
                5,
                DiceRoll((6, 4, 4)),
                selected.session.version,
                now,
                f"games-pg:sicbo:play:{suffix}",
            )
            played = await sicbo_service.play(play_command)
            assert played.code is SicBoCode.SUCCESS and played.balance == 105
            played_replay = await sicbo_service.play(play_command)
            assert played_replay.replayed and played_replay.balance == 105

            opened = await gamble_service.open(
                OpenGamble(
                    opener,
                    -100,
                    202,
                    now,
                    now + timedelta(minutes=5),
                    f"games-pg:gamble:open:{suffix}",
                )
            )
            assert opened.session is not None
            bets = await asyncio.gather(
                gamble_service.place_bet(
                    PlaceGambleBet(
                        opened.session.session_id,
                        attacker,
                        "attacker",
                        5,
                        None,
                        now,
                        f"games-pg:gamble:bet:a:{suffix}",
                    )
                ),
                gamble_service.place_bet(
                    PlaceGambleBet(
                        opened.session.session_id,
                        defender,
                        "defender",
                        10,
                        None,
                        now,
                        f"games-pg:gamble:bet:b:{suffix}",
                    )
                ),
            )
            assert all(result.code is GambleCode.SUCCESS for result in bets)
            settlement = await gamble_operations.settle_gamble(
                SettleGamble(
                    opened.session.session_id,
                    0,
                    now + timedelta(minutes=5),
                )
            )
            assert settlement is not None
            assert settlement.prize == 15
            assert settlement.session.bets
            assert settlement.winner_id == settlement.session.bets[0].user_id
            gamble_conversation = f"game:gamble:{settlement.session.session_id}"
            await gamble_operations.enqueue_gamble_notification(
                settlement,
                text="settled",
                enqueued_at=now + timedelta(minutes=5),
            )
            await gamble_operations.enqueue_gamble_notification(
                settlement,
                text="settled",
                enqueued_at=now + timedelta(minutes=5),
            )
            outbox = await db_connection.fetch_one(
                "SELECT COUNT(*) FROM conversation.outbound_messages "
                "WHERE conversation_id = %s",
                (gamble_conversation,),
            )
            assert outbox is not None and int(outbox[0]) == 1

            await db_connection.execute(
                "INSERT INTO game.rpg_characters "
                "(user_id, level, hp, max_hp, atk, matk, def, experience, "
                "allow_battle, version) VALUES "
                "(%s, 1, 10, 10, 9, 0, 1, 0, TRUE, 0), "
                "(%s, 1, 10, 10, 3, 0, 1, 0, TRUE, 0)",
                (attacker, defender),
            )
            fight_command = FightPlayer(
                attacker,
                "attacker",
                f"defender-{suffix}",
                now,
                f"games-pg:pvp:{suffix}",
            )
            fight = await character_service.fight_player(fight_command)
            assert fight.code is RpgCode.SUCCESS and fight.winner_name == "attacker"
            fight_replay = await character_service.fight_player(fight_command)
            assert fight_replay.replayed and fight_replay.battle == fight.battle

            await db_connection.execute(
                "INSERT INTO game.rpg_characters "
                "(user_id, level, hp, max_hp, atk, matk, def, experience, "
                "allow_battle, version) VALUES "
                "(%s, 1, 10, 10, 1, 0, 1, 0, TRUE, 0), "
                "(%s, 1, 10, 10, 1, 0, 1, 0, TRUE, 0)",
                (opener, sicbo_user),
            )
            draw = await character_service.fight_player(
                FightPlayer(
                    opener,
                    "opener",
                    f"sicbo-{suffix}",
                    now,
                    f"games-pg:pvp-draw:{suffix}",
                )
            )
            assert draw.code is RpgCode.SUCCESS
            assert draw.battle is not None and draw.battle.is_draw
            draw_rows = await db_connection.fetch_all(
                "SELECT user_id, hp, version FROM game.rpg_characters "
                "WHERE user_id = ANY(%s) ORDER BY user_id",
                ((opener, sicbo_user),),
            )
            assert [(int(row[1]), int(row[2])) for row in draw_rows] == [
                (10, 0),
                (10, 0),
            ]

            async with db_connection.transaction() as connection:
                equipment_row = await db_connection.fetch_one(
                    "INSERT INTO game.rpg_equipment "
                    "(name, type, atk_bonus, def_bonus, hp_bonus, matk_bonus, price, rarity) "
                    "VALUES (%s, 'weapon', 2, 0, 0, 0, 10, 1) RETURNING id",
                    (f"blade-{suffix}",),
                    connection=connection,
                )
                item_row = await db_connection.fetch_one(
                    "INSERT INTO game.rpg_items "
                    "(name, type, effect, description, price, use_limit) "
                    "VALUES (%s, 'consumable', 'legacy', 'test', 1, 1) RETURNING id",
                    (f"potion-{suffix}",),
                    connection=connection,
                )
            assert equipment_row is not None and item_row is not None
            equipment_id = int(equipment_row[0])
            item_id = int(item_row[0])
            add_command = AddInventoryItem(
                attacker,
                item_id,
                2,
                f"games-pg:add-item:{suffix}",
            )
            added = await inventory_service.add(add_command)
            added_replay = await inventory_service.add(add_command)
            assert added.code is RpgCode.SUCCESS
            assert len(added.entries) == 1 and added.entries[0].quantity == 2
            assert added_replay.replayed and added_replay.entries == added.entries

            remove_command = RemoveInventoryItem(
                attacker,
                item_id,
                1,
                f"games-pg:remove-item:{suffix}",
            )
            removed = await inventory_service.remove(remove_command)
            removed_replay = await inventory_service.remove(remove_command)
            assert removed.code is RpgCode.SUCCESS
            assert len(removed.entries) == 1 and removed.entries[0].quantity == 1
            assert removed_replay.replayed and removed_replay.entries == removed.entries

            equipped = await equipment_operations.equip(
                EquipItem(
                    attacker,
                    equipment_id,
                    f"games-pg:equip:{suffix}",
                )
            )
            used = await inventory_service.use(
                UseItem(attacker, item_id, f"games-pg:use:{suffix}")
            )
            assert equipped.code is RpgCode.SUCCESS
            assert used.code is RpgCode.SUCCESS and used.entries == ()
        finally:
            if gamble_conversation is not None:
                await db_connection.execute(
                    "DELETE FROM conversation.outbound_messages WHERE conversation_id = %s",
                    (gamble_conversation,),
                )
            await db_connection.execute(
                "DELETE FROM game.game_receipts WHERE user_id = ANY(%s)", (users,)
            )
            await db_connection.execute(
                "DELETE FROM game.game_sessions WHERE owner_id = ANY(%s)", (users,)
            )
            await db_connection.execute(
                "DELETE FROM game.user_omikuji WHERE user_id = ANY(%s)", (users,)
            )
            await db_connection.execute(
                "DELETE FROM identity.users WHERE id = ANY(%s)", (users,)
            )
            if equipment_id is not None:
                await db_connection.execute(
                    "DELETE FROM game.rpg_equipment WHERE id = %s", (equipment_id,)
                )
            if item_id is not None:
                await db_connection.execute(
                    "DELETE FROM game.rpg_items WHERE id = %s", (item_id,)
                )
            await db.dispose_current_engine()

    asyncio.run(scenario())
