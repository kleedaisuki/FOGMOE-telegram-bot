"""@brief 类型化 AI provider 与 route 配置测试 / Tests for typed AI provider and route configuration."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from fogmoe_bot.config import (
    AiSettings,
    AnthropicProviderSettings,
    OpenAIProviderSettings,
)
from fogmoe_bot.infrastructure.assistant.routing_config import build_provider_routes


def _provider(
    *,
    provider_id: str = "gateway",
    style: str = "openai",
    endpoint: str | None = None,
) -> dict[str, object]:
    """@brief 构造最小 provider 配置 / Build a minimal provider configuration.

    @param provider_id 动态 provider ID / Dynamic provider ID.
    @param style wire protocol 风格 / Wire-protocol style.
    @param endpoint 可选完整请求 URL / Optional complete request URL.
    @return 可供 Pydantic 验证的 provider payload / Provider payload suitable for Pydantic validation.
    """

    if style == "anthropic":
        return {
            "id": provider_id,
            "label": provider_id.title(),
            "style": "anthropic",
            "endpoint": endpoint or "https://api.example.test/v1/messages",
            "api_version": "2023-06-01",
            "auth": {
                "api_key": "test-key",
                "header": "x-api-key",
                "prefix": "",
            },
            "headers": {"X-Tenant": "thought-lab"},
        }
    return {
        "id": provider_id,
        "label": provider_id.title(),
        "style": "openai",
        "endpoint": endpoint or "https://api.example.test/v1/chat/completions",
        "auth": {
            "api_key": "test-key",
            "header": "Authorization",
            "prefix": "Bearer ",
        },
        "headers": {"X-Tenant": "thought-lab"},
    }


def _route(
    *,
    provider: str = "gateway",
    models: list[dict[str, object]] | None = None,
    supports_tools: bool = True,
    strict_tools: bool = False,
    disabled_tools: list[str] | None = None,
    safety_block_is_terminal: bool = False,
    meta: dict[str, str] | None = None,
) -> dict[str, object]:
    """@brief 构造最小任务 route 配置 / Build a minimal task-route configuration.

    @param provider route 引用的 provider / Provider referenced by the route.
    @param models 有序模型 fallback 链 / Ordered model fallback chain.
    @param supports_tools provider 是否支持 tools / Whether the provider supports tools.
    @param strict_tools 是否启用严格工具 schema / Whether strict tool schemas are enabled.
    @param disabled_tools 禁用工具列表 / Disabled-tool list.
    @param safety_block_is_terminal safety block 是否停止 fallback / Whether a safety block stops fallback.
    @param meta 用户定义的 request metadata / User-defined request metadata.
    @return 可供 Pydantic 验证的 route payload / Route payload suitable for Pydantic validation.
    """

    return {
        "provider": provider,
        "models": models
        if models is not None
        else [{"name": "model-primary", "accepts_images": True}],
        "supports_tools": supports_tools,
        "strict_tools": strict_tools,
        "disabled_tools": disabled_tools or [],
        "safety_block_is_terminal": safety_block_is_terminal,
        "meta": meta or {},
    }


def _settings(
    *,
    providers: list[dict[str, object]] | None = None,
    chat_routes: list[dict[str, object]] | None = None,
    summary_routes: list[dict[str, object]] | None = None,
    dreaming_routes: list[dict[str, object]] | None = None,
    translation_routes: list[dict[str, object]] | None = None,
) -> AiSettings:
    """@brief 构造四任务完整测试设置 / Build complete test settings for all four tasks.

    @param providers provider payload 列表 / Provider payload list.
    @param chat_routes chat routes / Chat routes.
    @param summary_routes summary routes / Summary routes.
    @param dreaming_routes dreaming routes / Dreaming routes.
    @param translation_routes translation routes / Translation routes.
    @return 已验证 AI 设置 / Validated AI settings.
    """

    selected_providers = providers or [_provider()]
    selected_chat = chat_routes or [_route()]
    selected_summary = summary_routes or [_route(supports_tools=False)]
    selected_dreaming = dreaming_routes or [_route(supports_tools=False)]
    selected_translation = translation_routes or [_route(supports_tools=False)]
    return AiSettings.model_validate(
        {
            "providers": selected_providers,
            "routing": {
                "chat": {"routes": selected_chat},
                "summary": {"routes": selected_summary},
                "dreaming": {"routes": selected_dreaming},
                "translation": {"routes": selected_translation},
            },
        }
    )


def test_provider_union_uses_style_as_the_only_discriminator() -> None:
    """@brief OpenAI/Anthropic 使用 style 判别联合 / OpenAI/Anthropic use a style-discriminated union."""

    settings = _settings(
        providers=[
            _provider(provider_id="gateway"),
            _provider(provider_id="claude", style="anthropic"),
        ],
        chat_routes=[_route(provider="gateway"), _route(provider="claude")],
        summary_routes=[_route(provider="gateway", supports_tools=False)],
        dreaming_routes=[_route(provider="gateway", supports_tools=False)],
        translation_routes=[_route(provider="gateway", supports_tools=False)],
    )

    assert isinstance(settings.provider_for("gateway"), OpenAIProviderSettings)
    assert isinstance(settings.provider_for("claude"), AnthropicProviderSettings)


def test_build_provider_routes_projects_connection_and_custom_meta() -> None:
    """@brief route 包含完整连接信息和用户 metadata / Routes carry complete connection data and user metadata."""

    settings = _settings(
        chat_routes=[
            _route(
                models=[
                    {"name": "text-model", "accepts_images": False},
                    {"name": "vision-model", "accepts_images": True},
                ],
                strict_tools=True,
                disabled_tools=["web_search"],
                safety_block_is_terminal=True,
                meta={"trace": "chat-42", "operator": "Klee"},
            )
        ]
    )

    routes = build_provider_routes(settings, "chat")

    assert len(routes) == 1
    route = routes[0]
    assert route.route_id == "chat:0:gateway"
    assert route.provider_id == "gateway"
    assert route.provider_label == "Gateway"
    assert route.style == "openai"
    assert route.endpoint == "https://api.example.test/v1/chat/completions"
    assert route.auth.api_key == "test-key"
    assert route.auth.header == "Authorization"
    assert route.headers == {"X-Tenant": "thought-lab"}
    assert tuple((model.name, model.accepts_images) for model in route.models) == (
        ("text-model", False),
        ("vision-model", True),
    )
    assert route.strict_tools is True
    assert route.disabled_tools == ("web_search",)
    assert route.safety_block_is_terminal is True
    assert route.meta == {"trace": "chat-42", "operator": "Klee"}


def test_anthropic_route_accepts_only_user_id_meta() -> None:
    """@brief Anthropic route 仅接受官方 metadata user_id / Anthropic routes accept only official metadata user_id."""

    settings = _settings(
        providers=[_provider(provider_id="claude", style="anthropic")],
        chat_routes=[_route(provider="claude", meta={"user_id": "telegram:42"})],
        summary_routes=[_route(provider="claude", supports_tools=False)],
        dreaming_routes=[_route(provider="claude", supports_tools=False)],
        translation_routes=[_route(provider="claude", supports_tools=False)],
    )

    route = build_provider_routes(settings)[0]

    assert route.style == "anthropic"
    assert route.api_version == "2023-06-01"
    assert route.meta == {"user_id": "telegram:42"}


def test_route_meta_defaults_to_an_empty_mapping() -> None:
    """@brief 未指定 meta 时保持空映射 / Meta remains empty when it is not specified."""

    route = build_provider_routes(_settings())[0]

    assert route.meta == {}


@pytest.mark.parametrize(
    ("payload", "match"),
    (
        (
            lambda: {
                "providers": [_provider(), _provider()],
                "routing": {
                    task: {"routes": [_route(supports_tools=task == "chat")]}
                    for task in ("chat", "summary", "dreaming", "translation")
                },
            },
            "duplicate provider id",
        ),
        (
            lambda: {
                "providers": [_provider()],
                "routing": {
                    "chat": {"routes": [_route(provider="missing")]},
                    "summary": {"routes": [_route(supports_tools=False)]},
                    "dreaming": {"routes": [_route(supports_tools=False)]},
                    "translation": {"routes": [_route(supports_tools=False)]},
                },
            },
            "references unknown provider",
        ),
        (
            lambda: {
                "providers": [_provider()],
                "routing": {
                    "chat": {"routes": [_route(models=[])]},
                    "summary": {"routes": [_route(supports_tools=False)]},
                    "dreaming": {"routes": [_route(supports_tools=False)]},
                    "translation": {"routes": [_route(supports_tools=False)]},
                },
            },
            "models must contain at least one model",
        ),
        (
            lambda: {
                "providers": [_provider()],
                "routing": {
                    "chat": {
                        "routes": [
                            _route(supports_tools=False, strict_tools=True)
                        ]
                    },
                    "summary": {"routes": [_route(supports_tools=False)]},
                    "dreaming": {"routes": [_route(supports_tools=False)]},
                    "translation": {"routes": [_route(supports_tools=False)]},
                },
            },
            "strict_tools requires supports_tools",
        ),
        (
            lambda: {
                "providers": [_provider(provider_id="claude", style="anthropic")],
                "routing": {
                    "chat": {
                        "routes": [
                            _route(provider="claude", meta={"trace": "nope"})
                        ]
                    },
                    "summary": {
                        "routes": [_route(provider="claude", supports_tools=False)]
                    },
                    "dreaming": {
                        "routes": [_route(provider="claude", supports_tools=False)]
                    },
                    "translation": {
                        "routes": [_route(provider="claude", supports_tools=False)]
                    },
                },
            },
            "only supports user_id",
        ),
    ),
)
def test_ai_settings_rejects_invalid_provider_route_graph(
    payload: Callable[[], dict[str, object]],
    match: str,
) -> None:
    """@brief AiSettings 在边界拒绝无效配置图 / AiSettings rejects invalid configuration graphs at the boundary.

    @param payload 延迟构造的无效 payload / Lazily constructed invalid payload.
    @param match 预期验证错误片段 / Expected validation-error fragment.
    @return None / None.
    """

    with pytest.raises(ValueError, match=match):
        AiSettings.model_validate(payload())


@pytest.mark.parametrize("task", ("embedding", "vision", "classifier", "translate"))
def test_build_provider_routes_rejects_unknown_task(task: str) -> None:
    """@brief 旧别名与未知任务被拒绝 / Legacy aliases and unknown tasks are rejected.

    @param task 待拒绝任务名 / Task name to reject.
    @return None / None.
    """

    with pytest.raises(RuntimeError, match="Unsupported AI task"):
        build_provider_routes(_settings(), task)
