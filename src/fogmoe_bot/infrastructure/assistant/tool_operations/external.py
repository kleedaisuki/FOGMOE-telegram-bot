"""Assistant 外部读取与生成 adapter ports / Assistant external-read and generation adapter ports."""

from typing import Protocol

from fogmoe_bot.application.assistant.tool_runtime import ToolEffectRequest
from fogmoe_bot.domain.conversation.payloads import JsonValue


class ExternalReadTools(Protocol):
    """HTTP/code 等无业务 mutation 的异步 adapter。"""

    async def execute(self, request: ToolEffectRequest) -> JsonValue:
        """执行一个外部读取或计算 / Execute an external read or computation."""

        ...


class GeneratedMediaTools(Protocol):
    """用稳定 effect identity 生成 durable artifact 的 adapter。"""

    async def generate(self, request: ToolEffectRequest) -> JsonValue:
        """生成并持久化 artifact，但不投递 Telegram。"""

        ...


class StickerCatalogReader(Protocol):
    """无进程缓存的贴纸目录 reader。"""

    async def list_packs(self, pack_name: str | None) -> JsonValue:
        """读取配置与上游贴纸 metadata / Read sticker metadata."""

        ...
