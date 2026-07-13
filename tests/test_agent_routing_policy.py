"""@brief Agent provider 路由策略测试 / Tests for Agent provider-routing policy."""

from fogmoe_bot.config import AiSettings
from fogmoe_bot.domain.assistant.routing.policy import model_supports_vision
from fogmoe_bot.infrastructure.assistant.routing_config import build_provider_profiles


def test_model_supports_vision_respects_text_only_patterns() -> None:
    """@brief 验证文本模型模式会禁用视觉 / Verify text-only patterns disable vision."""

    patterns = ["deepseek-ai/deepseek-v4-flash", "vendor/text-*"]

    assert model_supports_vision("deepseek-ai/DeepSeek-V4-Flash", patterns) is False
    assert model_supports_vision("vendor/text-small", patterns) is False
    assert model_supports_vision("gpt-4o", patterns) is True


def test_provider_profiles_resolve_configured_chat_model() -> None:
    """@brief 验证 provider profile 使用显式 chat 模型 / Verify provider profile uses an explicit chat model."""

    settings = AiSettings.model_validate(
        {"providers": {"siliconflow": {"models": {"chat": "deepseek-ai/model"}}}}
    )

    profile = build_provider_profiles(settings)["siliconflow"]

    assert profile.provider_name == "siliconflow"
    assert profile.models == ("deepseek-ai/model",)


def test_provider_profiles_use_zai_without_a_legacy_zhipu_alias() -> None:
    """@brief 验证 Z.ai 是唯一配置与运行时名称 / Verify Z.ai is the sole configuration and runtime name."""

    settings = AiSettings.model_validate(
        {
            "routing": {"translation": {"provider": "zai"}},
            "providers": {"zai": {"models": {"translation": "glm-5"}}},
        }
    )

    profile = build_provider_profiles(settings, "translation")["zai"]

    assert profile.service_name == "zai"
    assert profile.provider_name == "zai"
    assert profile.models == ("glm-5",)
    assert profile.skip_tools == ("web_search", "web_browser")
