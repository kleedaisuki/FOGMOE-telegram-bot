import logging
from typing import Optional

from fogmoe_bot.infrastructure import config

from ..agent_loop import run_agent_loop
from ..agent_response import AgentResponse
from ..delivery.contracts import VisibleContentSink


def get_ai_response(
    messages,
    user_id: int,
    visible_content_handler: Optional[VisibleContentSink] = None,
) -> AgentResponse:
    """@brief 同步 OpenRouter 响应函数 / Synchronous OpenRouter response."""
    openrouter_model = config.OPENROUTER_CHAT_MODEL
    if not openrouter_model:
        raise RuntimeError("Missing OPENROUTER_CHAT_MODEL configuration.")

    try:
        return run_agent_loop(
            "openrouter",
            openrouter_model,
            messages,
            provider_name="OpenRouter",
            visible_content_handler=visible_content_handler,
        )
    except Exception as exc:
        logging.error("OpenRouter 请求失败: %s", exc)
        raise
