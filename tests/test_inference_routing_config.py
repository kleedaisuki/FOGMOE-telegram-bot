"""@brief 类型化 AI 路由配置测试 / Tests for typed AI routing configuration."""

import pytest

from fogmoe_bot.config import AiSettings
from fogmoe_bot.infrastructure.assistant import routing_config


def _settings(payload: dict[str, object]) -> AiSettings:
    """@brief 从显式测试负载构造严格 AI 设置 / Build strict AI settings from an explicit test payload.

    @param payload ``ai`` 配置投影 / ``ai`` configuration projection.
    @return 已验证的不可变 AI 设置 / Validated immutable AI settings.
    """

    return AiSettings.model_validate(payload)


def test_get_provider_order_for_chat_returns_typed_immutable_order() -> None:
    """@brief 验证 chat 顺序来自类型化配置 / Verify chat order comes from typed configuration."""

    settings = _settings({"routing": {"chat": {"provider_order": ["openai", "zai"]}}})

    order = routing_config.get_provider_order_for_task(settings, "chat")

    assert order == ("openai", "zai")
    assert isinstance(order, tuple)


def test_get_provider_order_for_subtask_uses_primary_and_fallback() -> None:
    """@brief 验证后台任务按主/回退 provider 排序 / Verify background task uses primary/fallback order."""

    settings = _settings(
        {
            "routing": {
                "summary": {
                    "provider": "openai",
                    "fallback_provider": "zai",
                }
            }
        }
    )

    assert routing_config.get_provider_order_for_task(settings, "summary") == (
        "openai",
        "zai",
    )


def test_get_provider_order_for_translation_deduplicates_provider() -> None:
    """@brief 验证 translation 路由去重 / Verify translation route deduplicates a provider."""

    settings = _settings(
        {
            "routing": {
                "translation": {
                    "provider": "zai",
                    "fallback_provider": "zai",
                }
            }
        }
    )

    assert routing_config.get_provider_order_for_task(settings, "translation") == (
        "zai",
    )


@pytest.mark.parametrize("task", ["embedding", "vision", "classifier", "translate"])
def test_get_provider_order_for_unknown_task_fails(task: str) -> None:
    """@brief 验证旧别名及未知任务被拒绝 / Verify legacy aliases and unknown tasks are rejected."""

    with pytest.raises(RuntimeError, match="Unsupported AI task"):
        routing_config.get_provider_order_for_task(_settings({}), task)


@pytest.mark.parametrize(
    "provider",
    ["openai", "openrouter", "siliconflow", "gemini", "zai", "azure"],
)
def test_get_models_for_translation_uses_provider_specific_model(
    provider: str,
) -> None:
    """@brief 验证 translation 使用各 provider 的模型 / Verify translation uses a provider-specific model."""

    settings = _settings(
        {
            "providers": {
                provider: {"models": {"translation": f"{provider}-translation-model"}}
            }
        }
    )

    assert routing_config.get_models_for_task(settings, provider, "translation") == (
        f"{provider}-translation-model",
    )


def test_get_models_for_task_includes_gemini_task_fallback() -> None:
    """@brief 验证 Gemini summary 主/回退模型链 / Verify Gemini summary primary/fallback model chain."""

    settings = _settings(
        {
            "providers": {
                "gemini": {
                    "models": {
                        "summary": "gemini-summary-primary",
                        "summary_fallback": "gemini-summary-fallback",
                    }
                }
            }
        }
    )

    assert routing_config.get_models_for_task(settings, "gemini", "summary") == (
        "gemini-summary-primary",
        "gemini-summary-fallback",
    )


def test_get_models_for_chat_includes_configured_openrouter_vision_model() -> None:
    """@brief 验证 chat 链包含独立视觉模型 / Verify chat chain includes the independent vision model."""

    settings = _settings(
        {
            "providers": {
                "openrouter": {
                    "models": {"chat": "deepseek-chat", "vision": "qwen-vision"}
                }
            }
        }
    )

    assert routing_config.get_models_for_task(settings, "openrouter", "chat") == (
        "deepseek-chat",
        "qwen-vision",
    )


def test_get_models_for_task_deduplicates_identical_primary_and_fallback() -> None:
    """@brief 验证同一模型不会被重复尝试 / Verify an identical model is not retried."""

    settings = _settings(
        {
            "providers": {
                "gemini": {
                    "models": {
                        "chat": "gemini-chat",
                        "chat_fallback": "gemini-chat",
                    }
                }
            }
        }
    )

    assert routing_config.get_models_for_task(settings, "gemini", "chat") == (
        "gemini-chat",
    )


def test_completion_kwargs_adds_reasoning_effort_for_native_gemini() -> None:
    """@brief 验证原生 Gemini 的 reasoning 参数 / Verify reasoning parameters for native Gemini."""

    settings = _settings({"providers": {"gemini": {"openai_compatible": False}}})

    assert routing_config.completion_kwargs_for_task(settings, "gemini", "summary") == {
        "reasoning_effort": "high"
    }
    assert (
        routing_config.completion_kwargs_for_task(settings, "gemini", "translation")
        == {}
    )


def test_completion_kwargs_omits_reasoning_effort_for_openai_compatible_gemini() -> (
    None
):
    """@brief 验证 OpenAI-compatible Gemini 不注入原生参数 / Verify compatible Gemini has no native-only parameters."""

    settings = _settings({"providers": {"gemini": {"openai_compatible": True}}})

    assert (
        routing_config.completion_kwargs_for_task(settings, "gemini", "summary") == {}
    )
