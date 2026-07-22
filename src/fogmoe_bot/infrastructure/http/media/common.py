"""媒体 HTTP adapters 共享的请求头与标量解析 / Shared headers and scalar parsing for media HTTP adapters."""

from collections.abc import Mapping

HEADERS: Mapping[str, str] = {
    "User-Agent": "FogMoeBot/1.0 (+https://github.com/TelechaBot/FOGMOE-telegram-bot)",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}


def optional_str(value: object) -> str | None:
    """将非空标量转为字符串 / Convert a non-empty scalar to string."""

    if value is None or isinstance(value, bool | list | dict):
        return None
    text = str(value).strip()
    return text or None
