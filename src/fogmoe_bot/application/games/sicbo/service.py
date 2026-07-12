"""@brief 骰宝应用服务 / Sic Bo application service."""

from __future__ import annotations

from datetime import datetime
import random
from typing import Final

from fogmoe_bot.application.games.ports.sicbo import DiceSource, SicBoOperations
from fogmoe_bot.application.games.sicbo.models import (
    CancelSicBo,
    OpenSicBo,
    PlaySicBo,
    SelectSicBoBet,
    SicBoResult,
)
from fogmoe_bot.domain.games import DiceRoll, GameSessionId, SicBoSession

SICBO_SERVICE_DATA_KEY = "games.sicbo.service"
"""@brief runtime capability 中骰宝服务的键 / Sic Bo service capability key."""

SICBO_AMOUNTS: Final = frozenset({1, 5, 10, 20, 50, 100})
"""@brief 骰宝允许押注额 / Allowed Sic Bo wagers."""


class SystemDiceSource(DiceSource):
    """@brief 使用操作系统熵的骰子源 / Dice source backed by operating-system entropy."""

    def __init__(self) -> None:
        """@brief 初始化隔离的系统随机生成器 / Initialize an isolated system RNG."""

        self._random = random.SystemRandom()

    def roll_three(self) -> DiceRoll:
        """@brief 掷三枚六面骰 / Roll three six-sided dice."""

        return DiceRoll(
            (
                self._random.randint(1, 6),
                self._random.randint(1, 6),
                self._random.randint(1, 6),
            )
        )


class SicBoService:
    """@brief 校验骰宝命令并在事务外生成随机数 / Validate Sic Bo commands and generate randomness outside transactions."""

    def __init__(
        self,
        operations: SicBoOperations,
        *,
        dice: DiceSource | None = None,
    ) -> None:
        self._operations = operations
        self._dice = dice or SystemDiceSource()

    async def open(self, command: OpenSicBo) -> SicBoResult:
        """@brief 开启骰宝选择流程 / Open a Sic Bo selection flow."""

        if command.expires_at <= command.now:
            raise ValueError("Sic Bo expiration must follow its opening time")
        return await self._operations.open_sicbo(command)

    async def active(self, user_id: int, now: datetime) -> SicBoSession | None:
        """@brief 读取活动骰宝会话 / Read an active Sic Bo session."""

        return await self._operations.active_sicbo(user_id, now)

    async def select_bet(self, command: SelectSicBoBet) -> SicBoResult:
        """@brief 选择骰宝下注类型 / Select a Sic Bo wager."""

        return await self._operations.select_sicbo_bet(command)

    async def cancel(self, command: CancelSicBo) -> SicBoResult:
        """@brief 取消骰宝 / Cancel Sic Bo."""

        return await self._operations.cancel_sicbo(command)

    async def play(self, command: PlaySicBo) -> SicBoResult:
        """@brief 使用命令中的骰子结算 / Settle with the command's pre-generated roll."""

        if command.amount not in SICBO_AMOUNTS:
            raise ValueError("Unsupported Sic Bo wager")
        return await self._operations.play_sicbo(command)

    async def roll_and_play(
        self,
        *,
        session_id: GameSessionId,
        user_id: int,
        amount: int,
        expected_version: int | None,
        now: datetime,
        idempotency_key: str,
    ) -> SicBoResult:
        """@brief 事务外掷骰后原子结算 / Roll outside persistence and then settle atomically."""

        return await self.play(
            PlaySicBo(
                session_id=session_id,
                user_id=user_id,
                amount=amount,
                roll=self._dice.roll_three(),
                expected_version=expected_version,
                now=now,
                idempotency_key=idempotency_key,
            )
        )
