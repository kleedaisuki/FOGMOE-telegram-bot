"""@brief LiteLLM 模型名规范化 / LiteLLM model-name normalization."""

from __future__ import annotations

from typing import cast

from fogmoe_bot.config import AiProvidersSettings, ProviderName


#: @brief 已经带 LiteLLM provider 前缀的模型名 / Model-name prefixes already understood by LiteLLM.
LITELLM_PREFIXES = ("openai/", "openrouter/", "azure/", "gemini/", "zai/")
#: @brief 当前配置 schema 允许的 provider 集合 / Providers permitted by the current configuration schema.
_KNOWN_PROVIDERS = frozenset(
    {"openai", "openrouter", "azure", "gemini", "siliconflow", "zai"}
)


def normalize_provider(provider: str) -> ProviderName:
    """@brief 规范化并验证 provider 名称 / Normalize and validate a provider name.

    @param provider 用户配置或 route 中的 provider 名称 / Provider name from configuration or a route.
    @return schema 允许的规范 provider 名称 / Schema-permitted normalized provider name.
    @raise RuntimeError provider 未受当前 schema 支持时抛出 /
        Raised when the provider is unsupported by the current schema.
    """

    normalized = provider.strip().casefold()
    if normalized not in _KNOWN_PROVIDERS:
        raise RuntimeError(f"Unsupported AI provider: {provider}")
    return cast(ProviderName, normalized)


def litellm_model_name(
    provider: ProviderName,
    model: str,
    *,
    providers: AiProvidersSettings,
) -> str:
    """@brief 生成 LiteLLM 可识别的模型名 / Build a LiteLLM-recognized model name.

    @param provider 已验证的配置 provider / Validated configured provider.
    @param model 未加前缀的模型或 deployment 名称 / Unprefixed model or deployment name.
    @param providers 已注入的 AI provider 设置 / Injected AI provider settings.
    @return 带正确 LiteLLM provider 前缀的模型名 / Model name with the appropriate LiteLLM prefix.
    @raise RuntimeError 模型名称为空时抛出 / Raised when the model name is blank.
    """

    if provider == "gemini" and providers.gemini.openai_compatible:
        return _prefixed_model("openai", model)
    if provider == "siliconflow":
        return _prefixed_model("openai", model)
    return _prefixed_model(provider, model)


def _prefixed_model(provider: str, model: str) -> str:
    """@brief 为模型补充 LiteLLM provider 前缀 / Add a LiteLLM provider prefix to a model.

    @param provider LiteLLM provider 前缀 / LiteLLM provider prefix.
    @param model 用户配置的模型名称 / User-configured model name.
    @return 可传入 LiteLLM 的模型字符串 / Model string ready for LiteLLM.
    @raise RuntimeError 模型名称为空时抛出 / Raised when the model name is blank.
    """

    normalized = model.strip()
    if not normalized:
        raise RuntimeError(f"Missing model configuration for provider: {provider}")
    if normalized.startswith(LITELLM_PREFIXES):
        return normalized
    return f"{provider}/{normalized}"


__all__ = ["LITELLM_PREFIXES", "litellm_model_name", "normalize_provider"]
