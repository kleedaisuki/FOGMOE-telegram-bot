"""@brief Assistant 任务配置到 provider route 的唯一映射 / Sole mapping from Assistant task configuration to provider routes."""

from __future__ import annotations

from collections.abc import Iterable

from fogmoe_bot.domain.assistant.routing.models import ProviderRoute
from fogmoe_bot.infrastructure import config
from fogmoe_bot.infrastructure.llm.litellm_models import normalize_provider


_TASKS = frozenset({"chat", "summary", "translate"})
"""@brief 支持的推理任务 / Supported inference tasks."""

_TASK_PROVIDER_CONFIG_PREFIXES = {
    "summary": "AI_SUMMARY",
    "translate": "AI_TRANSLATE",
}
"""@brief 子任务 provider 配置前缀 / Provider-setting prefixes for subtasks."""

_PROVIDER_MODEL_CONFIG_PATTERNS = {
    "openai": "OPENAI_{task}_MODEL",
    "openrouter": "OPENROUTER_{task}_MODEL",
    "siliconflow": "SILICONFLOW_{task}_MODEL",
    "gemini": "GEMINI_{task}_MODEL",
    "zai": "ZHIPU_{task}_MODEL",
    "azure": "AZURE_OPENAI_{task}_MODEL",
}
"""@brief provider 到模型配置名的映射 / Provider-to-model-setting mapping."""

_PROVIDER_FALLBACK_MODEL_CONFIGS = {
    ("gemini", "chat"): "GEMINI_CHAT_FALLBACK_MODEL",
    ("gemini", "summary"): "GEMINI_SUMMARY_FALLBACK_MODEL",
}
"""@brief 明确支持双模型的配置项 / Settings with explicit secondary models."""

_GEMINI_NATIVE_REASONING_EXCLUDED_TASKS = frozenset({"translate"})
"""@brief 不启用 Gemini native reasoning 的任务 / Tasks that exclude Gemini native reasoning."""

_SERVICE_NAMES = (
    "openai",
    "openrouter",
    "gemini",
    "azure",
    "siliconflow",
    "zhipu",
    "zai",
)
"""@brief 可配置服务名的稳定顺序 / Stable order of configurable service names."""

_DISPLAY_NAMES = {
    "openai": "OpenAI",
    "openrouter": "OpenRouter",
    "gemini": "Gemini",
    "azure": "Azure",
    "siliconflow": "SiliconFlow",
    "zhipu": "Z.ai",
    "zai": "Z.ai",
}
"""@brief 日志显示名 / Display names used in logs."""


def _dedupe(values: Iterable[str | None], *, casefold: bool = False) -> list[str]:
    """@brief 保序去除空值与重复值 / Remove blanks and duplicates while preserving order.

    @param values 可选字符串序列 / Sequence of optional strings.
    @param casefold 是否按 Unicode 无大小写规则规范 / Whether to normalize with Unicode case-folding.
    @return 规范后的唯一字符串 / Normalized unique strings.
    """

    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = (value or "").strip()
        if not normalized:
            continue
        key = normalized.casefold() if casefold else normalized
        if key in seen:
            continue
        seen.add(key)
        result.append(key if casefold else normalized)
    return result


def get_provider_order_for_task(task: str) -> list[str]:
    """@brief 读取 task-specific provider 优先级 / Read task-specific provider priority.

    @param task chat、translate 等任务名 / Task name such as chat or translate.
    @return 规范 provider 顺序的副本 / Copy of the normalized provider order.
    @raise RuntimeError 任务不受支持 / The task is unsupported.
    """

    task_name = task.strip().casefold()
    if task_name == "chat":
        return list(config.AI_SERVICE_ORDER)
    if task_name not in _TASKS:
        raise RuntimeError(f"Unsupported AI task: {task}")
    prefix = _TASK_PROVIDER_CONFIG_PREFIXES[task_name]
    return _dedupe(
        (
            getattr(config, f"{prefix}_PROVIDER", None),
            getattr(config, f"{prefix}_FALLBACK_PROVIDER", None),
        ),
        casefold=True,
    )


def provider_model_for_task(provider: str, task: str) -> str | None:
    """@brief 读取 provider 的主模型 / Read a provider's primary model.

    @param provider 外部服务别名 / External service alias.
    @param task 推理任务 / Inference task.
    @return 模型名；无对应配置时为 None / Model name, or None without a matching setting.
    """

    provider_name = normalize_provider(provider)
    pattern = _PROVIDER_MODEL_CONFIG_PATTERNS.get(provider_name)
    if pattern is None:
        return None
    return getattr(config, pattern.format(task=task.strip().upper()), None)


def provider_fallback_model_for_task(provider: str, task: str) -> str | None:
    """@brief 读取 provider 的显式回退模型 / Read a provider's explicit fallback model.

    @param provider 外部服务别名 / External service alias.
    @param task 推理任务 / Inference task.
    @return 回退模型名或 None / Fallback model name or None.
    """

    provider_name = normalize_provider(provider)
    config_name = _PROVIDER_FALLBACK_MODEL_CONFIGS.get(
        (provider_name, task.strip().casefold())
    )
    return getattr(config, config_name, None) if config_name is not None else None


def get_models_for_task(provider: str, task: str) -> list[str]:
    """@brief 读取 provider/task 模型链 / Read a provider/task model chain.

    @param provider 外部服务别名 / External service alias.
    @param task 推理任务 / Inference task.
    @return 去重后的主模型与回退模型 / Deduplicated primary and fallback models.
    """

    return _dedupe(
        (
            provider_model_for_task(provider, task),
            provider_fallback_model_for_task(provider, task),
        )
    )


def completion_kwargs_for_task(provider: str, task: str) -> dict[str, object]:
    """@brief 生成 provider/task 的补充 completion 参数 / Build extra completion arguments for a provider/task pair.

    @param provider 外部服务别名 / External service alias.
    @param task 推理任务 / Inference task.
    @return provider-neutral 参数映射 / Provider-neutral argument mapping.
    """

    provider_name = normalize_provider(provider)
    task_name = task.strip().casefold()
    if (
        provider_name == "gemini"
        and not config.GEMINI_OPENAI_COMPATIBLE
        and task_name not in _GEMINI_NATIVE_REASONING_EXCLUDED_TASKS
    ):
        return {"reasoning_effort": "high"}
    return {}


def build_provider_profiles(task: str = "chat") -> dict[str, ProviderRoute]:
    """@brief 构造 task-specific provider routes / Build task-specific provider routes.

    @param task chat、translate 等任务名 / Task name such as chat or translate.
    @return 服务名到不可变 route 的映射 / Mapping from service names to immutable routes.
    """

    return {
        service_name: ProviderRoute(
            service_name=service_name,
            provider_name=(
                "zhipu" if service_name in {"zhipu", "zai"} else service_name
            ),
            display_name=_DISPLAY_NAMES[service_name],
            models=tuple(get_models_for_task(service_name, task)),
            completion_kwargs=completion_kwargs_for_task(service_name, task),
            skip_tools=(
                ("web_search", "web_browser")
                if service_name in {"zhipu", "zai"}
                else ()
            ),
            safety_block_on_error=service_name == "gemini",
        )
        for service_name in _SERVICE_NAMES
    }


def configured_service_order(task: str = "chat") -> tuple[str, ...]:
    """@brief 读取 task-specific 服务优先级 / Read task-specific service priority.

    @param task chat、translate 等任务名 / Task name such as chat or translate.
    @return 配置的服务优先级 / Configured service priority.
    """

    return tuple(get_provider_order_for_task(task))


__all__ = [
    "build_provider_profiles",
    "completion_kwargs_for_task",
    "configured_service_order",
    "get_models_for_task",
    "get_provider_order_for_task",
    "provider_fallback_model_for_task",
    "provider_model_for_task",
]
