"""@brief 类型化 AI 设置到 provider route 的映射 / Map typed AI settings to provider routes."""

from __future__ import annotations

from typing import Literal, TypeAlias, cast

from fogmoe_bot.config import AiSettings, ProviderName
from fogmoe_bot.domain.assistant.routing.models import ProviderRoute
from fogmoe_bot.infrastructure.llm.litellm_models import normalize_provider


#: @brief Assistant 支持的推理任务 / Inference tasks supported by the Assistant.
TaskName: TypeAlias = Literal["chat", "summary", "dreaming", "translation"]
#: @brief 所有配置 provider 的稳定构造顺序 / Stable construction order for configured providers.
_SERVICE_NAMES: tuple[ProviderName, ...] = (
    "openai",
    "openrouter",
    "gemini",
    "azure",
    "siliconflow",
    "zai",
)
#: @brief 面向日志的稳定 provider 显示名 / Stable provider display names for logs.
_DISPLAY_NAMES: dict[ProviderName, str] = {
    "openai": "OpenAI",
    "openrouter": "OpenRouter",
    "gemini": "Gemini",
    "azure": "Azure",
    "siliconflow": "SiliconFlow",
    "zai": "Z.ai",
}


def get_provider_order_for_task(
    settings: AiSettings,
    task: TaskName | str,
) -> tuple[ProviderName, ...]:
    """@brief 读取任务的 provider 优先级 / Read a task's provider priority.

    @param settings 已验证的 AI 设置 / Validated AI settings.
    @param task chat、summary、dreaming 或 translation / Chat, summary, dreaming, or translation.
    @return 规范且不可变的 provider 顺序 / Normalized immutable provider order.
    @raise RuntimeError 任务不受支持时抛出 / Raised when the task is unsupported.
    """

    return settings.routing.for_task(_normalize_task(task))


def provider_model_for_task(
    settings: AiSettings,
    provider: ProviderName | str,
    task: TaskName | str,
) -> str | None:
    """@brief 返回 provider 的任务主模型 / Return a provider's primary model for a task.

    @param settings 已验证的 AI 设置 / Validated AI settings.
    @param provider provider 名称 / Provider name.
    @param task 推理任务 / Inference task.
    @return 主模型；未配置时为 None / Primary model, or None when unset.
    """

    models = settings.providers.for_name(normalize_provider(provider)).models
    task_name = _normalize_task(task)
    match task_name:
        case "chat":
            return models.chat
        case "summary":
            return models.summary
        case "dreaming":
            return models.dreaming or models.summary
        case "translation":
            return models.translation


def provider_fallback_model_for_task(
    settings: AiSettings,
    provider: ProviderName | str,
    task: TaskName | str,
) -> str | None:
    """@brief 返回 provider 的同 provider 回退模型 / Return a provider's intra-provider fallback model.

    @param settings 已验证的 AI 设置 / Validated AI settings.
    @param provider provider 名称 / Provider name.
    @param task 推理任务 / Inference task.
    @return 回退模型；该任务无回退时为 None / Fallback model, or None when the task has none.
    """

    models = settings.providers.for_name(normalize_provider(provider)).models
    match _normalize_task(task):
        case "chat":
            return models.chat_fallback
        case "summary":
            return models.summary_fallback
        case "dreaming" | "translation":
            return None


def get_models_for_task(
    settings: AiSettings,
    provider: ProviderName | str,
    task: TaskName | str,
) -> tuple[str, ...]:
    """@brief 返回 provider/task 模型尝试链 / Return a provider/task model attempt chain.

    @param settings 已验证的 AI 设置 / Validated AI settings.
    @param provider provider 名称 / Provider name.
    @param task 推理任务 / Inference task.
    @return 去重、保序的模型链 / Deduplicated model chain preserving order.
    @note chat 链会在主/回退模型后附加独立 vision 模型；推理层会在含图消息时优先它。/
        The chat chain appends an independent vision model after primary/fallback models; the
        inference layer prioritizes it for image-bearing messages.
    """

    models = settings.providers.for_name(normalize_provider(provider)).models
    return models.for_task(_normalize_task(task))


def completion_kwargs_for_task(
    settings: AiSettings,
    provider: ProviderName | str,
    task: TaskName | str,
) -> dict[str, object]:
    """@brief 构造 provider/task 的补充 completion 参数 / Build extra completion arguments for a provider/task.

    @param settings 已验证的 AI 设置 / Validated AI settings.
    @param provider provider 名称 / Provider name.
    @param task 推理任务 / Inference task.
    @return provider-neutral completion 参数 / Provider-neutral completion arguments.
    """

    provider_name = normalize_provider(provider)
    task_name = _normalize_task(task)
    if (
        provider_name == "gemini"
        and not settings.providers.gemini.openai_compatible
        and task_name != "translation"
    ):
        return {"reasoning_effort": "high"}
    return {}


def build_provider_profiles(
    settings: AiSettings,
    task: TaskName | str = "chat",
) -> dict[str, ProviderRoute]:
    """@brief 构造任务特定的 provider routes / Build task-specific provider routes.

    @param settings 已验证的 AI 设置 / Validated AI settings.
    @param task 推理任务 / Inference task.
    @return provider 到不可变 route 的映射 / Mapping from provider to immutable route.
    """

    task_name = _normalize_task(task)
    return {
        service_name: ProviderRoute(
            service_name=service_name,
            provider_name=service_name,
            display_name=_DISPLAY_NAMES[service_name],
            models=get_models_for_task(settings, service_name, task_name),
            completion_kwargs=completion_kwargs_for_task(
                settings,
                service_name,
                task_name,
            ),
            skip_tools=("web_search", "web_browser") if service_name == "zai" else (),
            safety_block_on_error=service_name == "gemini",
        )
        for service_name in _SERVICE_NAMES
    }


def configured_service_order(
    settings: AiSettings,
    task: TaskName | str = "chat",
) -> tuple[ProviderName, ...]:
    """@brief 返回任务的已配置服务优先级 / Return configured service priority for a task.

    @param settings 已验证的 AI 设置 / Validated AI settings.
    @param task 推理任务 / Inference task.
    @return 已配置服务的不可变优先级 / Immutable priority of configured services.
    """

    return get_provider_order_for_task(settings, task)


def _normalize_task(task: TaskName | str) -> TaskName:
    """@brief 验证任务名称 / Validate an inference task name.

    @param task 外部传入的任务名称 / Task name supplied by a caller.
    @return 受支持的规范任务名称 / Supported normalized task name.
    @raise RuntimeError 任务不受支持时抛出 / Raised when the task is unsupported.
    """

    normalized = task.strip().casefold()
    if normalized not in {"chat", "summary", "dreaming", "translation"}:
        raise RuntimeError(f"Unsupported AI task: {task}")
    return cast(TaskName, normalized)


__all__ = [
    "TaskName",
    "build_provider_profiles",
    "completion_kwargs_for_task",
    "configured_service_order",
    "get_models_for_task",
    "get_provider_order_for_task",
    "provider_fallback_model_for_task",
    "provider_model_for_task",
]
