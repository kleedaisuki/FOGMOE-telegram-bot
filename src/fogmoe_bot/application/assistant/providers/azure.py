import logging

from fogmoe_bot.infrastructure import config

from ..agent_loop import run_agent_loop
from ..types import AIResponse, VisibleContentHandler
from typing import Optional


def get_ai_response(
    messages,
    user_id: int,
    visible_content_handler: Optional[VisibleContentHandler] = None,
) -> AIResponse:
    """同步版本的Azure OpenAI响应函数（支持工具调用）"""
    azure_model = config.AZURE_OPENAI_CHAT_MODEL
    if not azure_model:
        raise RuntimeError("Missing AZURE_OPENAI_CHAT_MODEL configuration.")

    try:
        return run_agent_loop(
            "azure",
            azure_model,
            messages,
            provider_name="Azure",
            visible_content_handler=visible_content_handler,
        )
    except Exception as exc:
        logging.error("Azure OpenAI 请求失败: %s", exc)
        raise
