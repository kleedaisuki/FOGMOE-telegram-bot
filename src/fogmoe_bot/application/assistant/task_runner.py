import logging
from typing import Any, Dict, List

from fogmoe_bot.infrastructure.llm.litellm_client import create_chat_completion
from .provider_resolver import (
    completion_kwargs_for_task,
    get_models_for_task,
    get_provider_order_for_task,
)


def run_ai_task(
    task: str,
    messages: List[Dict[str, Any]],
    **kwargs: Any,
) -> Any:
    last_error: Exception | None = None
    for provider in get_provider_order_for_task(task):
        models = get_models_for_task(provider, task)
        if not models:
            logging.warning("AI task %s skipped provider %s: no model configured", task, provider)
            continue

        for model in models:
            try:
                request_kwargs = {
                    **completion_kwargs_for_task(provider, task),
                    **kwargs,
                }
                return create_chat_completion(provider, model, messages, **request_kwargs)
            except Exception as exc:
                logging.warning(
                    "AI task %s failed via provider=%s model=%s: %s",
                    task,
                    provider,
                    model,
                    exc,
                )
                last_error = exc

    raise RuntimeError(f"All providers failed for AI task: {task}") from last_error
