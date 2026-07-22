"""@brief 注入式 LiteLLM 同步客户端 / Injected synchronous LiteLLM client."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import litellm

from fogmoe_bot.application.assistant.tools.catalog import ToolDefinition
from fogmoe_bot.config import AiProvidersSettings, NetworkSettings
from fogmoe_bot.infrastructure.llm.protocol import (
    sanitize_messages_for_provider,
)
from fogmoe_bot.infrastructure.llm.litellm_models import (
    litellm_model_name,
    normalize_provider,
)
from fogmoe_bot.infrastructure.llm.litellm_provider_config import provider_params
from fogmoe_bot.infrastructure.llm.tool_serialization import serialize_tool_definitions
from fogmoe_bot.infrastructure.network.proxy import configure_litellm_proxy


def configure_litellm_transport(settings: NetworkSettings) -> None:
    """@brief 在组合根配置 LiteLLM 传输 / Configure the LiteLLM transport at the composition root.

    @param settings 已解析的出站网络设置 / Parsed outbound network settings.
    @return None / None.
    @note 必须在首次 completion 前调用一次；请求路径不读取或缓存网络配置 /
        Call once before the first completion; request paths do not read or cache network settings.
    """

    configure_litellm_proxy(litellm, settings)


class LiteLLMChatClient:
    """@brief 以显式 provider 设置调用 LiteLLM / Call LiteLLM with explicit provider settings.

    该对象是基础设施适配器（infrastructure adapter），而不是配置服务：它不读文件、
    不读环境变量，也不缓存跨进程配置；其唯一输入是组合根已验证的不可变投影。
    """

    def __init__(self, *, providers: AiProvidersSettings) -> None:
        """@brief 注入 provider 连接设置 / Inject provider connection settings.

        @param providers 已验证的 AI provider 设置 / Validated AI provider settings.
        @return None / None.
        """

        self._providers = providers

    def complete(
        self,
        provider: str,
        model: str,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> Any:
        """@brief 执行一次同步 LiteLLM completion / Perform one synchronous LiteLLM completion.

        @param provider route 指定的 provider 名称 / Provider name selected by a route.
        @param model route 指定的模型或 deployment / Model or deployment selected by a route.
        @param messages 已规范化的对话消息 / Canonical conversation messages.
        @param kwargs provider-neutral completion 选项 / Provider-neutral completion options.
        @return LiteLLM 原始响应 / Raw LiteLLM response.
        @raise TypeError tools 不是 ToolDefinition 序列时抛出 /
            Raised when tools is not a ToolDefinition sequence.
        """

        litellm_provider = normalize_provider(provider)
        history_provider = (
            "openai"
            if (
                litellm_provider == "gemini"
                and self._providers.gemini.openai_compatible
            )
            else litellm_provider
        )
        provider_messages = sanitize_messages_for_provider(messages, history_provider)
        request_kwargs = {
            key: value for key, value in kwargs.items() if value is not None
        }
        _serialize_request_tools(request_kwargs)
        request_kwargs.setdefault("drop_params", True)

        litellm_model = litellm_model_name(
            litellm_provider,
            model,
            providers=self._providers,
        )
        logging.debug(
            "Calling LiteLLM provider=%s model=%s", litellm_provider, litellm_model
        )
        return litellm.completion(
            model=litellm_model,
            messages=provider_messages,
            **provider_params(litellm_provider, providers=self._providers),
            **request_kwargs,
        )


def _serialize_request_tools(request_kwargs: dict[str, Any]) -> None:
    """@brief 在 provider 边界序列化工具 / Serialize tools at the provider boundary.

    @param request_kwargs 即将交给 LiteLLM 的参数 / Arguments about to be passed to LiteLLM.
    @return None / None.
    @raise TypeError tools 不是 ToolDefinition 序列时抛出 /
        Raised unless tools is a ToolDefinition sequence.
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


__all__ = ["LiteLLMChatClient", "configure_litellm_transport"]
