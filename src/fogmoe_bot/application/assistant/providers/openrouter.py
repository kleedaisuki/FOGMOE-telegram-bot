import logging
from typing import Dict, Optional

from fogmoe_bot.infrastructure import config

from ..tool_runner import run_tool_loop
from ..types import AIResponse, VisibleContentHandler


def get_ai_response(
    messages,
    user_id: int,
    tool_context: Optional[Dict[str, object]] = None,
    visible_content_handler: Optional[VisibleContentHandler] = None,
) -> AIResponse:
    """@brief 同步 OpenRouter 响应函数 / Synchronous OpenRouter response."""
    openrouter_model = config.OPENROUTER_CHAT_MODEL
    if not openrouter_model:
        raise RuntimeError("Missing OPENROUTER_CHAT_MODEL configuration.")

    try:
        return run_tool_loop(
            "openrouter",
            openrouter_model,
            messages,
            tool_context,
            provider_name="OpenRouter",
            visible_content_handler=visible_content_handler,
        )
    except Exception as exc:
        logging.error("OpenRouter 请求失败: %s", exc)
        raise
