import pytest

from fogmoe_bot.infrastructure import config
from fogmoe_bot.infrastructure.assistant import routing_config


def test_get_provider_order_for_chat_returns_configured_order_copy(monkeypatch):
    monkeypatch.setattr(config, "AI_SERVICE_ORDER", ["openai", "zhipu"])

    order = routing_config.get_provider_order_for_task("chat")
    order.append("gemini")

    assert order == ["openai", "zhipu", "gemini"]
    assert config.AI_SERVICE_ORDER == ["openai", "zhipu"]


def test_get_provider_order_for_subtask_uses_primary_and_fallback(monkeypatch):
    monkeypatch.setattr(config, "AI_SUMMARY_PROVIDER", " OpenAI ")
    monkeypatch.setattr(config, "AI_SUMMARY_FALLBACK_PROVIDER", " ZHIPU ")

    assert routing_config.get_provider_order_for_task("summary") == [
        "openai",
        "zhipu",
    ]


def test_get_provider_order_for_subtask_deduplicates_case_insensitively(monkeypatch):
    monkeypatch.setattr(config, "AI_TRANSLATE_PROVIDER", "OpenAI")
    monkeypatch.setattr(config, "AI_TRANSLATE_FALLBACK_PROVIDER", "openai")

    assert routing_config.get_provider_order_for_task("translate") == ["openai"]


@pytest.mark.parametrize("task", ["embedding", "vision", "classifier"])
def test_get_provider_order_for_unknown_task_fails(task):
    with pytest.raises(RuntimeError, match="Unsupported AI task"):
        routing_config.get_provider_order_for_task(task)


@pytest.mark.parametrize(
    ("provider", "config_name"),
    [
        ("openai", "OPENAI_TRANSLATE_MODEL"),
        ("openrouter", "OPENROUTER_TRANSLATE_MODEL"),
        ("siliconflow", "SILICONFLOW_TRANSLATE_MODEL"),
        ("gemini", "GEMINI_TRANSLATE_MODEL"),
        ("zhipu", "ZHIPU_TRANSLATE_MODEL"),
        ("zai", "ZHIPU_TRANSLATE_MODEL"),
        ("azure", "AZURE_OPENAI_TRANSLATE_MODEL"),
    ],
)
def test_get_models_for_task_uses_provider_specific_model_config(
    monkeypatch,
    provider,
    config_name,
):
    monkeypatch.setattr(config, config_name, f"{provider}-translate-model")

    assert routing_config.get_models_for_task(provider, "translate") == [
        f"{provider}-translate-model"
    ]


def test_get_models_for_task_includes_gemini_task_fallback(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_SUMMARY_MODEL", "gemini-summary-primary")
    monkeypatch.setattr(
        config, "GEMINI_SUMMARY_FALLBACK_MODEL", "gemini-summary-fallback"
    )

    assert routing_config.get_models_for_task("gemini", "summary") == [
        "gemini-summary-primary",
        "gemini-summary-fallback",
    ]


def test_get_models_for_task_deduplicates_identical_primary_and_fallback(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_CHAT_MODEL", "gemini-chat")
    monkeypatch.setattr(config, "GEMINI_CHAT_FALLBACK_MODEL", "gemini-chat")

    assert routing_config.get_models_for_task("gemini", "chat") == ["gemini-chat"]


def test_completion_kwargs_adds_reasoning_effort_for_native_gemini(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_OPENAI_COMPATIBLE", False)

    assert routing_config.completion_kwargs_for_task("gemini", "summary") == {
        "reasoning_effort": "high"
    }
    assert routing_config.completion_kwargs_for_task("gemini", "translate") == {}


def test_completion_kwargs_omits_reasoning_effort_for_openai_compatible_gemini(
    monkeypatch,
):
    monkeypatch.setattr(config, "GEMINI_OPENAI_COMPATIBLE", True)

    assert routing_config.completion_kwargs_for_task("gemini", "summary") == {}
