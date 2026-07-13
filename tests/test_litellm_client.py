"""@brief 注入式 LiteLLM 客户端测试 / Tests for the injected LiteLLM client."""

import pytest
import litellm

from fogmoe_bot.application.assistant.tools.catalog import ToolArguments, define_tool
from fogmoe_bot.config import AiProvidersSettings
from fogmoe_bot.infrastructure.llm import litellm_client
from fogmoe_bot.infrastructure.llm.litellm_message_sanitizer import (
    sanitize_message_for_provider,
)
from fogmoe_bot.infrastructure.llm.litellm_models import normalize_provider
from fogmoe_bot.infrastructure.llm.litellm_provider_config import (
    gemini_native_api_base,
    openai_compatible_api_base,
    provider_params,
)


def _providers(payload: dict[str, object] | None = None) -> AiProvidersSettings:
    """@brief 从显式负载构造严格 provider 设置 / Build strict provider settings from an explicit payload.

    @param payload ``ai.providers`` 配置投影 / ``ai.providers`` configuration projection.
    @return 已验证的不可变 provider 设置 / Validated immutable provider settings.
    """

    return AiProvidersSettings.model_validate(payload or {})


def _client(
    payload: dict[str, object] | None = None,
) -> litellm_client.LiteLLMChatClient:
    """@brief 构造注入式 LiteLLM 客户端 / Build an injected LiteLLM client.

    @param payload ``ai.providers`` 配置投影 / ``ai.providers`` configuration projection.
    @return 不读取环境变量的客户端 / Client that does not read environment variables.
    """

    return litellm_client.LiteLLMChatClient(providers=_providers(payload))


def test_openai_compatible_api_base_strips_chat_completions_suffix() -> None:
    """@brief 验证 OpenAI-compatible 根路径标准化 / Verify OpenAI-compatible root normalization."""

    assert (
        openai_compatible_api_base("https://example.test/v1/chat/completions/")
        == "https://example.test/v1"
    )


def test_gemini_native_api_base_strips_models_suffix() -> None:
    """@brief 验证原生 Gemini 根路径标准化 / Verify native Gemini root normalization."""

    assert (
        gemini_native_api_base(
            "https://generativelanguage.googleapis.com/v1beta/models"
        )
        == "https://generativelanguage.googleapis.com/v1beta"
    )


def test_provider_params_uses_dummy_openai_key_for_custom_base() -> None:
    """@brief 验证无密钥兼容端点使用占位符 / Verify keyless compatible endpoint uses a placeholder key."""

    providers = _providers(
        {"openai": {"api_base": "https://openai-compatible.test/v1"}}
    )

    assert provider_params("openai", providers=providers) == {
        "api_key": "sk-no-key-required",
        "api_base": "https://openai-compatible.test/v1",
    }


def test_provider_params_requires_openai_key_or_base() -> None:
    """@brief 验证 OpenAI 必须有密钥或兼容端点 / Verify OpenAI requires a key or compatible endpoint."""

    with pytest.raises(RuntimeError, match="ai.providers.openai.api_key"):
        provider_params("openai", providers=_providers())


def test_provider_params_builds_openrouter_params() -> None:
    """@brief 验证 OpenRouter 参数来自注入配置 / Verify OpenRouter parameters come from injected settings."""

    providers = _providers(
        {
            "openrouter": {
                "api_key": "openrouter-key",
                "api_base": "https://openrouter.test/api/v1/chat/completions",
            }
        }
    )

    assert provider_params("openrouter", providers=providers) == {
        "api_key": "openrouter-key",
        "api_base": "https://openrouter.test/api/v1",
    }


def test_provider_params_requires_openrouter_key() -> None:
    """@brief 验证 OpenRouter 密钥不能为空 / Verify an OpenRouter key is required."""

    with pytest.raises(RuntimeError, match="ai.providers.openrouter.api_key"):
        provider_params("openrouter", providers=_providers())


def test_provider_params_requires_gemini_base_for_openai_compatible_mode() -> None:
    """@brief 验证兼容模式 Gemini 需要端点 / Verify compatible Gemini requires an endpoint."""

    providers = _providers(
        {
            "gemini": {
                "api_key": "gemini-key",
                "openai_compatible": True,
            }
        }
    )

    with pytest.raises(RuntimeError, match="openai_compatible is true"):
        provider_params("gemini", providers=providers)


def test_provider_params_builds_native_gemini_base() -> None:
    """@brief 验证原生 Gemini 使用 models 之前的根路径 / Verify native Gemini uses the root before models."""

    providers = _providers(
        {
            "gemini": {
                "api_key": "gemini-key",
                "openai_compatible": False,
                "api_base": "https://generativelanguage.googleapis.com/v1beta/models",
            }
        }
    )

    assert provider_params("gemini", providers=providers) == {
        "api_key": "gemini-key",
        "api_base": "https://generativelanguage.googleapis.com/v1beta",
    }


def test_provider_params_builds_azure_params() -> None:
    """@brief 验证 Azure 参数只使用语义 endpoint / Verify Azure parameters use only the semantic endpoint."""

    providers = _providers(
        {
            "azure": {
                "api_key": "azure-key",
                "endpoint": "https://azure.test/",
                "api_version": "2024-12-01-preview",
            }
        }
    )

    assert provider_params("azure", providers=providers) == {
        "api_key": "azure-key",
        "api_base": "https://azure.test",
        "api_version": "2024-12-01-preview",
    }


def test_provider_params_builds_zai_params_under_its_canonical_name() -> None:
    """@brief 验证 Z.ai 使用 zai 而非历史别名 / Verify Z.ai uses zai rather than a legacy alias."""

    providers = _providers(
        {
            "zai": {
                "api_key": "zai-key",
                "api_base": "https://zai.test/v4/chat/completions",
            }
        }
    )

    assert provider_params("zai", providers=providers) == {
        "api_key": "zai-key",
        "api_base": "https://zai.test/v4",
    }
    assert normalize_provider("zai") == "zai"
    with pytest.raises(RuntimeError, match="Unsupported AI provider"):
        normalize_provider("zhipu")


def test_sanitize_message_removes_provider_specific_fields_for_non_gemini() -> None:
    """@brief 验证非 Gemini 消息移除 provider 字段 / Verify non-Gemini messages remove provider fields."""

    message = {
        "role": "assistant",
        "content": "",
        "provider_specific_fields": {"x": 1},
        "tool_calls": [
            {
                "id": "call-1",
                "provider_specific_fields": {"x": 1},
                "function": {"name": "tool"},
            }
        ],
    }

    assert sanitize_message_for_provider(message, "openai") == {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call-1",
                "function": {"name": "tool"},
            }
        ],
    }


def test_sanitize_message_applies_native_gemini_tool_message_rules() -> None:
    """@brief 验证原生 Gemini 的工具消息规则 / Verify native Gemini tool-message rules."""

    assistant_message = {
        "role": "assistant",
        "content": "",
        "provider_specific_fields": {"x": 1},
        "tool_calls": [
            {
                "id": "call-1",
                "provider_specific_fields": {"x": 1},
                "function": {"name": "tool"},
            }
        ],
    }
    tool_message = {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": "result",
    }

    assert sanitize_message_for_provider(assistant_message, "gemini") == {
        "role": "assistant",
        "provider_specific_fields": {"x": 1},
        "tool_calls": [
            {
                "provider_specific_fields": {"x": 1},
                "function": {"name": "tool"},
            }
        ],
    }
    assert sanitize_message_for_provider(tool_message, "gemini") == {
        "role": "tool",
        "content": "result",
    }


def test_client_normalizes_zai_and_filters_none_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 验证客户端用注入设置调用 Z.ai / Verify client calls Z.ai with injected settings."""

    calls: list[dict[str, object]] = []
    messages = [{"role": "user", "content": "hello"}]

    def fake_completion(**kwargs: object) -> str:
        """@brief 记录 LiteLLM 调用 / Record a LiteLLM call.

        @param kwargs LiteLLM 参数 / LiteLLM parameters.
        @return 固定测试响应 / Fixed test response.
        """

        calls.append(kwargs)
        return "ok"

    monkeypatch.setattr(litellm, "completion", fake_completion)

    assert (
        _client(
            {"zai": {"api_key": "zai-key", "api_base": "https://zai.test/v4"}}
        ).complete(
            "zai",
            "glm-test",
            messages,
            temperature=None,
            max_tokens=128,
        )
        == "ok"
    )
    assert calls == [
        {
            "model": "zai/glm-test",
            "messages": messages,
            "api_key": "zai-key",
            "api_base": "https://zai.test/v4",
            "max_tokens": 128,
            "drop_params": True,
        }
    ]


def test_client_serializes_typed_tools_at_provider_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 验证 typed tool 仅在 provider 边界序列化 / Verify typed tools serialize only at the provider boundary."""

    calls: list[dict[str, object]] = []
    definition = define_tool(
        name="ping",
        description="Return pong",
        arguments_model=ToolArguments,
    )

    def fake_completion(**kwargs: object) -> str:
        """@brief 记录 LiteLLM 调用 / Record a LiteLLM call.

        @param kwargs LiteLLM 参数 / LiteLLM parameters.
        @return 固定测试响应 / Fixed test response.
        """

        calls.append(kwargs)
        return "ok"

    monkeypatch.setattr(litellm, "completion", fake_completion)

    result = _client({"zai": {"api_key": "zai-key"}}).complete(
        "zai",
        "glm-test",
        [{"role": "user", "content": "hello"}],
        tools=(definition,),
    )

    assert result == "ok"
    assert calls[0]["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "ping",
                "description": "Return pong",
                "parameters": {
                    "additionalProperties": False,
                    "properties": {},
                    "type": "object",
                },
            },
        }
    ]


def test_client_rejects_prebuilt_provider_tool_dicts() -> None:
    """@brief 验证客户端拒绝越过类型边界的工具字典 / Verify client rejects tool dictionaries crossing the typed boundary."""

    with pytest.raises(TypeError, match="ToolDefinition"):
        _client({"zai": {"api_key": "zai-key"}}).complete(
            "zai",
            "glm-test",
            [{"role": "user", "content": "hello"}],
            tools=[{"type": "function"}],
        )


def test_client_uses_openrouter_provider_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 验证客户端为 OpenRouter 添加 provider 前缀 / Verify client adds the OpenRouter provider prefix."""

    calls: list[dict[str, object]] = []
    messages = [{"role": "user", "content": "hello"}]

    def fake_completion(**kwargs: object) -> str:
        """@brief 记录 LiteLLM 调用 / Record a LiteLLM call.

        @param kwargs LiteLLM 参数 / LiteLLM parameters.
        @return 固定测试响应 / Fixed test response.
        """

        calls.append(kwargs)
        return "ok"

    monkeypatch.setattr(litellm, "completion", fake_completion)

    assert (
        _client(
            {
                "openrouter": {
                    "api_key": "openrouter-key",
                    "api_base": "https://openrouter.test/api/v1",
                }
            }
        ).complete("openrouter", "anthropic/claude-sonnet-4.5", messages)
        == "ok"
    )
    assert calls == [
        {
            "model": "openrouter/anthropic/claude-sonnet-4.5",
            "messages": messages,
            "api_key": "openrouter-key",
            "api_base": "https://openrouter.test/api/v1",
            "drop_params": True,
        }
    ]


def test_client_uses_openai_history_shape_for_compatible_gemini(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 验证兼容 Gemini 使用 OpenAI 历史格式 / Verify compatible Gemini uses OpenAI history shape."""

    calls: list[dict[str, object]] = []
    messages = [
        {
            "role": "assistant",
            "content": "",
            "provider_specific_fields": {"x": 1},
            "tool_calls": [
                {
                    "id": "call-1",
                    "provider_specific_fields": {"x": 1},
                    "function": {"name": "tool"},
                }
            ],
        }
    ]

    def fake_completion(**kwargs: object) -> str:
        """@brief 记录 LiteLLM 调用 / Record a LiteLLM call.

        @param kwargs LiteLLM 参数 / LiteLLM parameters.
        @return 固定测试响应 / Fixed test response.
        """

        calls.append(kwargs)
        return "ok"

    monkeypatch.setattr(litellm, "completion", fake_completion)

    assert (
        _client(
            {
                "gemini": {
                    "api_key": "gemini-key",
                    "openai_compatible": True,
                    "api_base": "https://gemini-compatible.test/v1",
                }
            }
        ).complete("gemini", "gemini-test", messages)
        == "ok"
    )
    assert calls[0]["model"] == "openai/gemini-test"
    assert calls[0]["messages"] == [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {"name": "tool"},
                }
            ],
        }
    ]
