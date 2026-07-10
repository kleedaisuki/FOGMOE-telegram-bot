import logging

from fogmoe_bot.infrastructure import config

from ..agent_loop import run_agent_loop
from ..agent_response import AgentResponse
from ..delivery.contracts import VisibleContentSink


def get_ai_response(
    messages,
    user_id: int,
    visible_content_handler: Optional[VisibleContentSink] = None,
) -> AgentResponse:
    """同步版本的 OpenAI 响应函数（支持工具调用）"""
    openai_model = config.OPENAI_CHAT_MODEL
    if not openai_model:
        raise RuntimeError("Missing OPENAI_CHAT_MODEL configuration.")

    try:
        return run_agent_loop(
            "openai",
            openai_model,
            messages,
            provider_name="OpenAI",
            visible_content_handler=visible_content_handler,
        )
    except Exception as exc:
        logging.error("OpenAI 请求失败: %s", exc)
        raise
