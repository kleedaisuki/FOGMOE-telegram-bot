from fogmoe_bot.infrastructure.assistant.routing_config import build_provider_profiles
from fogmoe_bot.domain.assistant.routing.policy import model_supports_vision
from fogmoe_bot.infrastructure import config


def test_model_supports_vision_respects_text_only_patterns():
    patterns = ["deepseek-ai/deepseek-v4-flash", "vendor/text-*"]

    assert model_supports_vision("deepseek-ai/DeepSeek-V4-Flash", patterns) is False
    assert model_supports_vision("vendor/text-small", patterns) is False
    assert model_supports_vision("gpt-4o", patterns) is True


def test_provider_profiles_resolve_configured_chat_model(monkeypatch):
    monkeypatch.setattr(config, "SILICONFLOW_CHAT_MODEL", "deepseek-ai/model")

    profile = build_provider_profiles()["siliconflow"]

    assert profile.provider_name == "siliconflow"
    assert profile.models == ("deepseek-ai/model",)


def test_provider_profiles_map_zai_alias_to_zhipu_runtime_provider(monkeypatch):
    monkeypatch.setattr(config, "ZHIPU_CHAT_MODEL", "glm-5")

    profile = build_provider_profiles()["zai"]

    assert profile.provider_name == "zhipu"
    assert profile.models == ("glm-5",)
    assert profile.skip_tools == ("web_search", "web_browser")
