"""Assistant Telegram 贴纸目录 adapter / Assistant Telegram sticker-catalog adapter."""

from __future__ import annotations

import json
from pathlib import Path

import requests

from fogmoe_bot.domain.conversation.payloads import JsonValue
from fogmoe_bot.infrastructure.blocking import AsyncBlockingBulkhead
from fogmoe_bot.infrastructure.network.proxy import create_requests_session


class TelegramStickerCatalogReader:
    """@brief 每次读取 resource 与 Telegram 的无缓存 sticker adapter / Cache-free sticker adapter reading resources and Telegram per call."""

    def __init__(
        self,
        *,
        config_path: Path,
        bot_token: str,
        timeout_seconds: int = 20,
        bulkhead: AsyncBlockingBulkhead,
    ) -> None:
        """@brief 注入 config path 与 token / Inject config path and token.

        @param config_path pack 配置 / Pack configuration.
        @param bot_token Telegram Bot token / Telegram Bot token.
        @param timeout_seconds HTTP timeout / HTTP timeout.
        @param bulkhead 专用目录读取隔舱 / Dedicated catalog-read bulkhead.
        """

        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._config_path = config_path
        self._bot_token = bot_token
        self._timeout_seconds = timeout_seconds
        self._bulkhead = bulkhead

    async def list_packs(self, pack_name: str | None) -> JsonValue:
        """@brief 在线程边界读取 metadata / Read metadata behind a thread boundary.

        @param pack_name 可选 pack / Optional pack.
        @return JSON metadata / JSON metadata.
        """

        return await self._bulkhead.call(lambda: self._list_sync(pack_name))

    def _list_sync(self, pack_name: str | None) -> JsonValue:
        """@brief 同步读取配置与 Telegram / Read configuration and Telegram synchronously.

        @param pack_name 可选 pack / Optional pack.
        @return JSON metadata / JSON metadata.
        """

        try:
            raw = json.loads(self._config_path.read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError:
            return {"status": "unavailable", "packs": []}
        values = raw.get("packs") if isinstance(raw, dict) else None
        if not isinstance(values, list):
            return {"status": "unavailable", "packs": []}
        configs = {
            str(value.get("name")): value
            for value in values
            if isinstance(value, dict) and str(value.get("name") or "").strip()
        }
        names = [pack_name] if pack_name else list(configs)
        packs: list[JsonValue] = []
        for name in names:
            if name not in configs:
                continue
            config = configs[name]
            emojis: list[JsonValue] = []
            title = name
            if self._bot_token:
                with create_requests_session() as session:
                    try:
                        response = session.get(
                            f"https://api.telegram.org/bot{self._bot_token}/getStickerSet",
                            params={"name": name},
                            timeout=self._timeout_seconds,
                        )
                        response.raise_for_status()
                        payload = response.json()
                        result = (
                            payload.get("result") if isinstance(payload, dict) else None
                        )
                        if isinstance(result, dict):
                            title = str(result.get("title") or name)
                            stickers = result.get("stickers")
                            if isinstance(stickers, list):
                                emojis = [
                                    emoji
                                    for emoji in sorted(
                                        {
                                            str(sticker.get("emoji"))
                                            for sticker in stickers
                                            if isinstance(sticker, dict)
                                            and sticker.get("emoji")
                                        }
                                    )
                                ]
                    except requests.RequestException, ValueError:
                        pass
            packs.append(
                {
                    "name": name,
                    "title": title,
                    "summary": str(config.get("summary") or ""),
                    "avoid": str(config.get("avoid") or ""),
                    "emojis": emojis,
                }
            )
        return {"status": "available" if packs else "unavailable", "packs": packs}
