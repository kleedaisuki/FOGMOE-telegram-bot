"""@brief 从显式 AI 设置构造 LiteLLM 连接参数 / Build LiteLLM connection parameters from explicit AI settings."""

from __future__ import annotations

from pydantic import SecretStr

from fogmoe_bot.config import (
    AiProvidersSettings,
    AzureProviderSettings,
    GeminiProviderSettings,
    OpenAICompatibleProviderSettings,
    ProviderName,
    reveal_secret,
)


def provider_params(
    provider: ProviderName,
    *,
    providers: AiProvidersSettings,
) -> dict[str, object]:
    """@brief 构造一个 provider 的 LiteLLM 参数 / Build LiteLLM parameters for one provider.

    @param provider 已验证的 provider 名称 / Validated provider name.
    @param providers 注入的 provider 设置 / Injected provider settings.
    @return 不含模型与消息的 LiteLLM 参数 / LiteLLM parameters excluding model and messages.
    @raise RuntimeError 凭据或该 provider 的必需端点缺失时抛出 /
        Raised when credentials or a required provider endpoint is missing.
    """

    match provider:
        case "openai":
            return _openai_params(providers.openai)
        case "openrouter":
            return _openrouter_params(providers.openrouter)
        case "gemini":
            return _gemini_params(providers.gemini)
        case "zai":
            return _zai_params(providers.zai)
        case "siliconflow":
            return _siliconflow_params(providers.siliconflow)
        case "azure":
            return _azure_params(providers.azure)


def openai_compatible_api_base(value: str | None) -> str:
    """@brief 规范 OpenAI-compatible API 根路径 / Normalize an OpenAI-compatible API root.

    @param value 用户提供的 API 根或 chat-completions URL / User-provided API root or chat-completions URL.
    @return 不含尾部 chat-completions 路径的根 URL / Root URL without a trailing chat-completions path.
    """

    base_url = (value or "").rstrip("/")
    suffix = "/chat/completions"
    if base_url.casefold().endswith(suffix):
        return base_url[: -len(suffix)].rstrip("/")
    return base_url


def gemini_native_api_base(value: str | None) -> str:
    """@brief 规范 Gemini 原生 API 根路径 / Normalize a native Gemini API root.

    @param value 用户提供的 Gemini API 根或 models URL / User-provided Gemini API root or models URL.
    @return 不含尾部 models 路径的根 URL / Root URL without a trailing models path.
    """

    base_url = (value or "").rstrip("/")
    suffix = "/models"
    if base_url.casefold().endswith(suffix):
        return base_url[: -len(suffix)].rstrip("/")
    return base_url


def _openai_params(settings: OpenAICompatibleProviderSettings) -> dict[str, object]:
    """@brief 构造 OpenAI 或自托管兼容端点参数 / Build OpenAI or self-hosted compatible-endpoint parameters.

    @param settings 单个 OpenAI-compatible provider 设置 / One OpenAI-compatible provider setting.
    @return LiteLLM 连接参数 / LiteLLM connection parameters.
    """

    api_key = reveal_secret(settings.api_key)
    if not api_key and settings.api_base:
        api_key = "sk-no-key-required"
    if not api_key:
        raise RuntimeError("Missing ai.providers.openai.api_key configuration.")
    parameters: dict[str, object] = {"api_key": api_key}
    if settings.api_base:
        parameters["api_base"] = openai_compatible_api_base(settings.api_base)
    return parameters


def _openrouter_params(settings: OpenAICompatibleProviderSettings) -> dict[str, object]:
    """@brief 构造 OpenRouter 参数 / Build OpenRouter parameters.

    @param settings OpenRouter 设置 / OpenRouter settings.
    @return LiteLLM 连接参数 / LiteLLM connection parameters.
    """

    return {
        "api_key": _required_secret(
            settings.api_key, "ai.providers.openrouter.api_key"
        ),
        "api_base": openai_compatible_api_base(settings.api_base),
    }


def _gemini_params(settings: GeminiProviderSettings) -> dict[str, object]:
    """@brief 构造 Gemini 参数 / Build Gemini parameters.

    @param settings Gemini 设置 / Gemini settings.
    @return LiteLLM 连接参数 / LiteLLM connection parameters.
    @raise RuntimeError OpenAI-compatible Gemini 未给出端点时抛出 /
        Raised when OpenAI-compatible Gemini lacks an endpoint.
    """

    parameters: dict[str, object] = {
        "api_key": _required_secret(settings.api_key, "ai.providers.gemini.api_key")
    }
    if settings.openai_compatible and not settings.api_base:
        raise RuntimeError(
            "ai.providers.gemini.api_base is required when openai_compatible is true"
        )
    if settings.api_base:
        parameters["api_base"] = (
            openai_compatible_api_base(settings.api_base)
            if settings.openai_compatible
            else gemini_native_api_base(settings.api_base)
        )
    return parameters


def _zai_params(settings: OpenAICompatibleProviderSettings) -> dict[str, object]:
    """@brief 构造 Z.ai 参数 / Build Z.ai parameters.

    @param settings Z.ai 设置 / Z.ai settings.
    @return LiteLLM 连接参数 / LiteLLM connection parameters.
    """

    parameters: dict[str, object] = {
        "api_key": _required_secret(settings.api_key, "ai.providers.zai.api_key")
    }
    if settings.api_base:
        parameters["api_base"] = openai_compatible_api_base(settings.api_base)
    return parameters


def _siliconflow_params(
    settings: OpenAICompatibleProviderSettings,
) -> dict[str, object]:
    """@brief 构造 SiliconFlow 参数 / Build SiliconFlow parameters.

    @param settings SiliconFlow 设置 / SiliconFlow settings.
    @return LiteLLM 连接参数 / LiteLLM connection parameters.
    @raise RuntimeError API 根缺失时抛出 / Raised when the API root is missing.
    """

    api_base = openai_compatible_api_base(settings.api_base)
    if not api_base:
        raise RuntimeError("Missing ai.providers.siliconflow.api_base configuration.")
    return {
        "api_key": _required_secret(
            settings.api_key, "ai.providers.siliconflow.api_key"
        ),
        "api_base": api_base,
    }


def _azure_params(settings: AzureProviderSettings) -> dict[str, object]:
    """@brief 构造 Azure OpenAI 参数 / Build Azure OpenAI parameters.

    @param settings Azure OpenAI 设置 / Azure OpenAI settings.
    @return LiteLLM 连接参数 / LiteLLM connection parameters.
    @raise RuntimeError Azure 端点或 API 版本缺失时抛出 /
        Raised when the Azure endpoint or API version is missing.
    """

    endpoint = (settings.endpoint or "").rstrip("/")
    if not endpoint:
        raise RuntimeError("Missing ai.providers.azure.endpoint configuration.")
    if not settings.api_version:
        raise RuntimeError("Missing ai.providers.azure.api_version configuration.")
    return {
        "api_key": _required_secret(settings.api_key, "ai.providers.azure.api_key"),
        "api_base": endpoint,
        "api_version": settings.api_version,
    }


def _required_secret(value: SecretStr | None, field_name: str) -> str:
    """@brief 取得一个 provider 必需密钥 / Obtain a provider's required secret.

    @param value 掩码后的可选密钥 / Masked optional secret.
    @param field_name 面向操作者的配置路径 / Operator-facing configuration path.
    @return 非空原始密钥 / Non-empty raw secret.
    @raise RuntimeError 密钥为空或未设置时抛出 / Raised when the secret is empty or unset.
    """

    secret = reveal_secret(value)
    if not secret:
        raise RuntimeError(f"Missing {field_name} configuration.")
    return secret


__all__ = [
    "gemini_native_api_base",
    "openai_compatible_api_base",
    "provider_params",
]
