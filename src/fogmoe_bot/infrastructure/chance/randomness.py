"""@brief 随机活动服务器熵适配器 / Chance-activity server-entropy adapter."""

from __future__ import annotations

import secrets

from fogmoe_bot.domain.chance.fairness import ServerSeed


class SystemServerSeedSource:
    """@brief 以操作系统密码学熵实现服务器种子端口 /
    Implement the server-seed port with operating-system cryptographic entropy.
    """

    def next_server_seed(self) -> ServerSeed:
        """@brief 生成 256 bit 服务器种子 / Generate a 256-bit server seed.

        @return 新的未揭示服务器种子 / New unrevealed server seed.
        """

        return ServerSeed(secrets.token_bytes(32))
