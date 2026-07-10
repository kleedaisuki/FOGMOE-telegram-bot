import logging
from typing import Optional

from fogmoe_bot.infrastructure import config

from ..agent_response import AgentResponse
from ..delivery.contracts import VisibleContentSink
from ..errors import PartialAgentResponseError, SafetyBlockError
from ..agent_loop import run_agent_loop


def get_ai_response(
    messages,
    user_id: int,
    visible_content_handler: Optional[VisibleContentSink] = None,
) -> AgentResponse:
    """同步版本的 Google Gemini 响应函数（LiteLLM）。"""
    primary_model = config.GEMINI_CHAT_MODEL
    fallback_model = config.GEMINI_CHAT_FALLBACK_MODEL

    def _run(model_name: str) -> AgentResponse:
        return run_agent_loop(
            "gemini",
            model_name,
            messages,
            provider_name="Gemini",
            completion_kwargs=(
                {"reasoning_effort": "high"}
                if not config.GEMINI_OPENAI_COMPATIBLE
                else None
            ),
            visible_content_handler=visible_content_handler,
        )

    try:
        return _run(primary_model)
    except PartialAgentResponseError:
        raise
    except Exception as exc:
        error_str = str(exc)
        if fallback_model and fallback_model != primary_model:
            logging.warning(
                "Gemini 主模型失败，尝试回退模型 %s: %s",
                fallback_model,
                error_str,
            )
            return _run(fallback_model)
        if "SAFETY" in error_str and "blocked" in error_str:
            logging.warning("Gemini safety block triggered: %s", error_str)
            raise SafetyBlockError(error_str) from exc

        logging.error("Google Gemini 请求失败: %s", error_str)
        raise
