from fogmoe_bot.infrastructure import config
from typing import Optional

from ..agent_loop import run_agent_loop
from ..agent_response import AgentResponse
from ..delivery.contracts import VisibleContentSink


def get_ai_response(
    messages,
    user_id: int,
    visible_content_handler: Optional[VisibleContentSink] = None,
) -> AgentResponse:
    """同步版本的 Z.ai（原智谱）响应函数（支持工具调用）"""
    return run_agent_loop(
        "zhipu",
        config.ZHIPU_CHAT_MODEL,
        messages,
        provider_name="Z.ai",
        skip_tools=("web_search", "web_browser"),
        visible_content_handler=visible_content_handler,
    )
