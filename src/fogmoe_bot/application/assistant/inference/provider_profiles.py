"""@brief 应用配置到 Agent 推理 route 的映射 / Application configuration to Agent inference routes."""

from fogmoe_bot.domain.agent_routing import ProviderRoute
from fogmoe_bot.infrastructure import config

from ..provider_resolver import completion_kwargs_for_task, get_models_for_task


_DISPLAY_NAMES = {
    "openai": "OpenAI",
    "openrouter": "OpenRouter",
    "gemini": "Gemini",
    "azure": "Azure",
    "siliconflow": "SiliconFlow",
    "zhipu": "Z.ai",
    "zai": "Z.ai",
}


def build_provider_profiles() -> dict[str, ProviderRoute]:
    """@brief 构造当前配置的 provider route / Build provider routes from current configuration.

    @return 服务名到 route 的映射 / Mapping from service name to route.
    """
    profiles: dict[str, ProviderRoute] = {}
    for service_name in ("openai", "openrouter", "gemini", "azure", "siliconflow", "zhipu", "zai"):
        profiles[service_name] = ProviderRoute(
            service_name=service_name,
            provider_name="zhipu" if service_name in {"zhipu", "zai"} else service_name,
            display_name=_DISPLAY_NAMES[service_name],
            models=tuple(get_models_for_task(service_name, "chat")),
            completion_kwargs=completion_kwargs_for_task(service_name, "chat"),
            skip_tools=("web_search", "web_browser") if service_name in {"zhipu", "zai"} else (),
            safety_block_on_error=service_name == "gemini",
        )
    return profiles


def configured_service_order() -> tuple[str, ...]:
    """@brief 读取 chat 服务优先级 / Read chat service priority.

    @return 配置的服务优先级 / Configured service priority.
    """
    return tuple(config.AI_SERVICE_ORDER)
