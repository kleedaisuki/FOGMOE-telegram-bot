"""@brief 类型化 AI 设置到自包含 route 的映射 / Map typed AI settings to self-contained routes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, TypeAlias, cast

from fogmoe_bot.config import (
    AiRouteModelSettings,
    AiSettings,
    AiTaskName,
    AnthropicProviderSettings,
    reveal_secret,
)
from fogmoe_bot.domain.assistant.routing.models import (
    ProviderAuth,
    ProviderRoute,
    RouteModel,
)

#: @brief Assistant 支持的推理任务 / Inference tasks supported by the Assistant.
TaskName: TypeAlias = Literal["chat", "summary", "dreaming", "translation"]


def build_provider_routes(
    settings: AiSettings,
    task: TaskName | str = "chat",
) -> tuple[ProviderRoute, ...]:
    """@brief 构造一个任务的有序自包含 provider routes / Build ordered self-contained provider routes for one task.

    @param settings 已验证的 AI 设置 / Validated AI settings.
    @param task 推理任务 / Inference task.
    @return 按 operator 配置顺序的 route 元组 / Route tuple in operator-configured order.
    @raise RuntimeError 任务名称未知时抛出 / Raised when the task name is unknown.
    @note 同一 route 中的 ``models`` 是同 provider fallback 链；路由之间的顺序是
        provider/endpoint fallback 链。/
        ``models`` within a route are a same-provider fallback chain; route order is the
        provider/endpoint fallback chain.
    """

    task_name = _normalize_task(task)
    task_routes = settings.routing.for_task(task_name).routes
    return tuple(
        _to_provider_route(
            task=task_name,
            ordinal=ordinal,
            settings=settings,
            provider_id=route.provider,
            models=route.models,
            supports_tools=route.supports_tools,
            strict_tools=route.strict_tools,
            disabled_tools=route.disabled_tools,
            safety_block_is_terminal=route.safety_block_is_terminal,
            meta=route.meta,
        )
        for ordinal, route in enumerate(task_routes)
    )


def _to_provider_route(
    *,
    task: TaskName,
    ordinal: int,
    settings: AiSettings,
    provider_id: str,
    models: tuple[AiRouteModelSettings, ...],
    supports_tools: bool,
    strict_tools: bool,
    disabled_tools: tuple[str, ...],
    safety_block_is_terminal: bool,
    meta: Mapping[str, str],
) -> ProviderRoute:
    """@brief 将一个已验证配置 route 投影为领域 route / Project one validated config route into a domain route.

    @param task 归属任务 / Owning task.
    @param ordinal 任务内配置序号 / Configured ordinal inside the task.
    @param settings 完整 AI 设置 / Complete AI settings.
    @param provider_id provider 引用 / Provider reference.
    @param models 已验证模型列表 / Validated model list.
    @param supports_tools 是否支持 tools / Whether tools are supported.
    @param strict_tools 是否严格工具 schema / Whether tool schemas are strict.
    @param disabled_tools 禁用工具 / Disabled tools.
    @param safety_block_is_terminal safety block 是否终止 fallback / Whether a safety block terminates fallback.
    @param meta 用户配置 metadata / Operator-configured metadata.
    @return 自包含领域 route / Self-contained domain route.
    """

    # AiSettings graph validator proves every provider reference and model shape before this
    # infrastructure projection runs.
    provider = settings.provider_for(provider_id)
    return ProviderRoute(
        route_id=f"{task}:{ordinal}:{provider.id}",
        provider_id=provider.id,
        provider_label=provider.label,
        style=provider.style,
        endpoint=provider.endpoint,
        auth=ProviderAuth(
            api_key=reveal_secret(provider.auth.api_key),
            header=provider.auth.header,
            prefix=provider.auth.prefix,
        ),
        headers=provider.headers,
        api_version=(
            provider.api_version
            if isinstance(provider, AnthropicProviderSettings)
            else None
        ),
        models=tuple(
            RouteModel(
                name=model.name,
                accepts_images=model.accepts_images,
            )
            for model in models
        ),
        supports_tools=supports_tools,
        strict_tools=strict_tools,
        disabled_tools=disabled_tools,
        safety_block_is_terminal=safety_block_is_terminal,
        meta=meta,
    )


def _normalize_task(task: TaskName | str) -> AiTaskName:
    """@brief 验证任务名称 / Validate an inference task name.

    @param task 外部传入任务名 / Task name supplied by a caller.
    @return 受支持的规范任务名 / Supported normalized task name.
    @raise RuntimeError 任务不受支持时抛出 / Raised when the task is unsupported.
    """

    normalized = task.strip().casefold()
    if normalized not in {"chat", "summary", "dreaming", "translation"}:
        raise RuntimeError(f"Unsupported AI task: {task}")
    return cast(AiTaskName, normalized)


__all__ = ["TaskName", "build_provider_routes"]
