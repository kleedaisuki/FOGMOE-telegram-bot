"""@brief 窄 Games 应用服务的规则边界测试 / Rule-boundary tests for narrow Games application services."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from typing import cast
from uuid import UUID

import pytest

from fogmoe_bot.application.games.gamble.models import (
    GambleCode,
    GambleResult,
    OpenGamble,
    PlaceGambleBet,
)
from fogmoe_bot.application.games.gamble.service import GambleService
from fogmoe_bot.application.games.omikuji.models import (
    DrawOmikuji,
    OmikujiCode,
    OmikujiResult,
)
from fogmoe_bot.application.games.omikuji.service import OmikujiService
from fogmoe_bot.application.games.ports.gamble import GambleOperations
from fogmoe_bot.application.games.ports.omikuji import OmikujiOperations
from fogmoe_bot.application.games.ports.rpg.character import RpgCharacterOperations
from fogmoe_bot.application.games.ports.rpg.inventory import RpgInventoryOperations
from fogmoe_bot.application.games.ports.sicbo import DiceSource, SicBoOperations
from fogmoe_bot.application.games.rpg.character_models import (
    HealCharacter,
    RpgMutationResult,
)
from fogmoe_bot.application.games.rpg.character_service import RpgCharacterService
from fogmoe_bot.application.games.rpg.common import RpgCode
from fogmoe_bot.application.games.rpg.inventory_models import (
    AddInventoryItem,
    InventoryResult,
)
from fogmoe_bot.application.games.rpg.inventory_service import RpgInventoryService
from fogmoe_bot.application.games.sicbo.models import (
    PlaySicBo,
    SicBoCode,
    SicBoResult,
)
from fogmoe_bot.application.games.sicbo.service import SicBoService
from fogmoe_bot.domain.games import DiceRoll, GameSessionId, daily_fortune

_NOW = datetime(2030, 1, 1, tzinfo=UTC)
_SESSION_ID = GameSessionId(UUID("00000000-0000-0000-0000-000000000099"))


class _GambleOperations:
    """@brief 记录传入奖池命令 / Record delegated pool commands."""

    def __init__(self) -> None:
        self.opened: list[OpenGamble] = []
        self.bets: list[PlaceGambleBet] = []

    async def open_gamble(self, command: OpenGamble) -> GambleResult:
        self.opened.append(command)
        return GambleResult(GambleCode.SUCCESS)

    async def place_gamble_bet(self, command: PlaceGambleBet) -> GambleResult:
        self.bets.append(command)
        return GambleResult(GambleCode.SUCCESS)


class _Dice:
    """@brief 返回确定骰子的测试随机源 / Deterministic test dice source."""

    def roll_three(self) -> DiceRoll:
        return DiceRoll((6, 5, 4))


class _SicBoOperations:
    """@brief 记录结算命令 / Record a Sic Bo settlement command."""

    def __init__(self) -> None:
        self.played: list[PlaySicBo] = []

    async def play_sicbo(self, command: PlaySicBo) -> SicBoResult:
        self.played.append(command)
        return SicBoResult(SicBoCode.SUCCESS)


class _OmikujiOperations:
    """@brief 记录事务外确定的每日签 / Record the fortune chosen outside persistence."""

    def __init__(self) -> None:
        self.drawn: list[DrawOmikuji] = []

    async def draw_omikuji(self, command: DrawOmikuji) -> OmikujiResult:
        self.drawn.append(command)
        return OmikujiResult(OmikujiCode.SUCCESS, command.drawn_fortune)


class _CharacterOperations:
    """@brief 记录治疗价格 / Record the requested healing price."""

    def __init__(self) -> None:
        self.heals: list[HealCharacter] = []

    async def heal_character(self, command: HealCharacter) -> RpgMutationResult:
        self.heals.append(command)
        return RpgMutationResult(RpgCode.SUCCESS)


class _InventoryOperations:
    """@brief 记录通过数量校验的库存命令 / Record inventory commands that pass quantity validation."""

    def __init__(self) -> None:
        self.added: list[AddInventoryItem] = []

    async def add_inventory_item(self, command: AddInventoryItem) -> InventoryResult:
        self.added.append(command)
        return InventoryResult(RpgCode.SUCCESS)


def test_gamble_service_owns_deadline_and_wager_rules() -> None:
    """@brief 非法奖池命令不会触达事务端口 / Invalid pool commands never reach the transactional port."""

    async def scenario() -> None:
        operations = _GambleOperations()
        service = GambleService(cast(GambleOperations, operations))
        with pytest.raises(ValueError, match="deadline"):
            await service.open(OpenGamble(1, -1, 1, _NOW, _NOW, "open"))
        with pytest.raises(ValueError, match="wager"):
            await service.place_bet(
                PlaceGambleBet(_SESSION_ID, 1, "alice", 7, None, _NOW, "bet")
            )
        assert operations.opened == []
        assert operations.bets == []

    asyncio.run(scenario())


def test_sicbo_service_generates_dice_before_transactional_play() -> None:
    """@brief 骰子由应用层先生成再随命令提交 / The application generates dice before transactional play."""

    async def scenario() -> None:
        operations = _SicBoOperations()
        service = SicBoService(
            cast(SicBoOperations, operations),
            dice=cast(DiceSource, _Dice()),
        )
        await service.roll_and_play(
            session_id=_SESSION_ID,
            user_id=1,
            amount=5,
            expected_version=3,
            now=_NOW,
            idempotency_key="play",
        )
        assert len(operations.played) == 1
        assert operations.played[0].roll == DiceRoll((6, 5, 4))
        with pytest.raises(ValueError, match="wager"):
            await service.play(
                PlaySicBo(_SESSION_ID, 1, 7, DiceRoll((1, 2, 3)), 3, _NOW, "bad")
            )

    asyncio.run(scenario())


def test_omikuji_service_derives_the_stable_daily_fortune() -> None:
    """@brief 御神签端口只接收已确定的稳定运势 / The Omikuji port receives an already determined stable fortune."""

    async def scenario() -> None:
        operations = _OmikujiOperations()
        service = OmikujiService(cast(OmikujiOperations, operations))
        day = date(2030, 2, 3)
        await service.draw(user_id=42, day=day, idempotency_key="draw")
        assert operations.drawn[0].drawn_fortune is daily_fortune(42, day)

    asyncio.run(scenario())


def test_rpg_services_keep_pricing_and_quantity_rules_outside_adapters() -> None:
    """@brief RPG 应用规则先于持久化执行 / RPG application rules execute before persistence."""

    async def scenario() -> None:
        characters = _CharacterOperations()
        character_service = RpgCharacterService(
            cast(RpgCharacterOperations, characters)
        )
        await character_service.heal(1, idempotency_key="heal")
        assert characters.heals == [HealCharacter(1, 10, "heal")]
        assert character_service.monsters

        inventory = _InventoryOperations()
        inventory_service = RpgInventoryService(cast(RpgInventoryOperations, inventory))
        with pytest.raises(ValueError, match="positive"):
            await inventory_service.add(AddInventoryItem(1, 2, 0, "bad"))
        assert inventory.added == []

    asyncio.run(scenario())
