"""@brief 主对话上下文组装 / Main conversation-context composition."""

from fogmoe_bot.domain.context import ContextBuilder
from fogmoe_bot.infrastructure import config


CHAT_CONTEXT_BUILDER = ContextBuilder(config.SYSTEM_PROMPT)
"""@brief 主对话上下文构造器 / Main chat context builder."""
