"""@brief 御神签应用服务测试 / Tests for the Omikuji application service."""

from __future__ import annotations

import asyncio
from datetime import date
from typing import cast

from fogmoe_bot.application.games.omikuji.models import (
    DrawOmikuji,
    OmikujiCode,
    OmikujiResult,
)
from fogmoe_bot.application.games.omikuji.service import OmikujiService
from fogmoe_bot.application.games.ports.omikuji import OmikujiOperations
from fogmoe_bot.domain.games import daily_fortune


class _Operations:
    """@brief 记录服务提交的确定性签文 / Record the deterministic fortune submitted by the service."""

    def __init__(self) -> None:
        """@brief 初始化命令记录 / Initialize command recording.

        @return None / None.
        """

        self.drawn: list[DrawOmikuji] = []

    async def draw_omikuji(self, command: DrawOmikuji) -> OmikujiResult:
        """@brief 记录抽签命令 / Record a draw command.

        @param command 抽签命令 / Draw command.
        @return 成功结果 / Successful result.
        """

        self.drawn.append(command)
        return OmikujiResult(OmikujiCode.SUCCESS, command.drawn_fortune)


def test_omikuji_service_derives_the_stable_daily_fortune() -> None:
    """@brief 端口只接收已确定的稳定运势 / The port receives an already determined stable fortune.

    @return None / None.
    """

    async def scenario() -> None:
        """@brief 执行一次确定性抽签 / Execute one deterministic draw.

        @return None / None.
        """

        operations = _Operations()
        service = OmikujiService(cast(OmikujiOperations, operations))
        day = date(2030, 2, 3)
        await service.draw(user_id=42, day=day, idempotency_key="draw")
        assert operations.drawn[0].drawn_fortune is daily_fortune(42, day)

    asyncio.run(scenario())
