# ruff: noqa: E402

import logging
from collections.abc import Sequence
from typing import Any

from fogmoe_bot.application.assistant.tools.catalog import ToolDefinition
from fogmoe_bot.infrastructure.observability.logging import prepare_litellm_logging

prepare_litellm_logging()

import litellm

from fogmoe_bot.infrastructure import config
from fogmoe_bot.infrastructure.llm.litellm_models import (
    litellm_model_name,
    normalize_provider,
)
from fogmoe_bot.infrastructure.network.proxy import configure_litellm_proxy
from fogmoe_bot.infrastructure.llm.litellm_message_sanitizer import (
    sanitize_messages_for_provider,
)
from fogmoe_bot.infrastructure.llm.litellm_provider_config import provider_params
from fogmoe_bot.infrastructure.llm.tool_serialization import serialize_tool_definitions


def _serialize_request_tools(request_kwargs: dict[str, Any]) -> None:
    """@brief 在 provider 边界序列化工具 / Serialize tools at the provider boundary.

    @param request_kwargs 即将交给 LiteLLM 的参数 / Arguments about to be passed to LiteLLM.
    @return None / None.
    @raise TypeError tools 不是 ToolDefinition 序列时抛出 / Raised unless tools is a ToolDefinition sequence.
    """

    raw_tools: object = request_kwargs.get("tools")
    if raw_tools is None:
        return
    if not isinstance(raw_tools, Sequence) or isinstance(raw_tools, (str, bytes)):
        raise TypeError("tools must be a sequence of ToolDefinition values")
    definitions: list[ToolDefinition] = []
    for item in raw_tools:
        if not isinstance(item, ToolDefinition):
            raise TypeError("tools must contain only ToolDefinition values")
        definitions.append(item)
    request_kwargs["tools"] = serialize_tool_definitions(definitions)


def create_chat_completion(
    provider: str,
    model: str,
    messages: list[dict[str, Any]],
    **kwargs: Any,
) -> Any:
    configure_litellm_proxy(litellm)
    litellm_provider = normalize_provider(provider)
    history_provider = (
        "openai"
        if litellm_provider == "gemini" and config.GEMINI_OPENAI_COMPATIBLE
        else litellm_provider
    )
    provider_messages = sanitize_messages_for_provider(messages, history_provider)
    request_kwargs = {key: value for key, value in kwargs.items() if value is not None}
    _serialize_request_tools(request_kwargs)
    request_kwargs.setdefault("drop_params", True)

    litellm_model = litellm_model_name(litellm_provider, model)
    logging.debug(
        "Calling LiteLLM provider=%s model=%s", litellm_provider, litellm_model
    )

    return litellm.completion(
        model=litellm_model,
        messages=provider_messages,
        **provider_params(litellm_provider),
        **request_kwargs,
    )
