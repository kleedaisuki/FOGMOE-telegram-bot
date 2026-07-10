"""@brief 无工具推理任务执行器 / Tool-free inference task runner."""

import logging
from collections.abc import Callable
from typing import Any

from fogmoe_bot.infrastructure.llm.litellm_client import create_chat_completion

from .task_profiles import (
    completion_kwargs_for_task,
    get_models_for_task,
    get_provider_order_for_task,
)


CompletionClient = Callable[..., Any]
"""@brief 同步模型完成调用 / Synchronous model-completion call."""


class InferenceTaskRunner:
    """@brief 执行 summary、translate、vision 等无工具推理任务 / Run tool-free inference tasks.

    该对象没有跨请求业务状态，但集中持有任务 route 解析与模型调用依赖，
    使任务执行可替换、可注入、可独立测试。
    / This object has no cross-request business state, but centralizes task-route
    resolution and model invocation dependencies for replacement, injection and testing.
    """

    def __init__(
        self,
        *,
        completion_client: CompletionClient = create_chat_completion,
        provider_order_resolver: Callable[[str], list[str]] = get_provider_order_for_task,
        model_resolver: Callable[[str, str], list[str]] = get_models_for_task,
        completion_kwargs_resolver: Callable[[str, str], dict[str, Any]] = completion_kwargs_for_task,
    ) -> None:
        """@brief 初始化任务执行器 / Initialize the task runner.

        @param completion_client 同步模型调用依赖 / Synchronous model-call dependency.
        @param provider_order_resolver 任务 provider 顺序解析器 / Task provider-order resolver.
        @param model_resolver provider 模型解析器 / Provider model resolver.
        @param completion_kwargs_resolver 调用参数解析器 / Completion-argument resolver.
        """
        self._completion_client = completion_client
        self._provider_order_resolver = provider_order_resolver
        self._model_resolver = model_resolver
        self._completion_kwargs_resolver = completion_kwargs_resolver

    def run(
        self,
        task_name: str,
        messages: list[dict[str, Any]],
        **overrides: Any,
    ) -> Any:
        """@brief 执行带 provider/model 回退的推理任务 / Run an inference task with provider/model fallback.

        @param task_name 任务名称 / Task name.
        @param messages 模型消息 / Model messages.
        @param overrides 覆盖默认调用参数 / Override default completion parameters.
        @return provider 返回的完成结果 / Completion result returned by the provider.
        @raise RuntimeError 当全部 route 都失败 / When every route fails.
        """
        last_error: Exception | None = None
        for provider_name in self._provider_order_resolver(task_name):
            models = self._model_resolver(provider_name, task_name)
            if not models:
                logging.warning("Inference task %s skipped provider %s: no model configured", task_name, provider_name)
                continue
            for model in models:
                try:
                    request_kwargs = {
                        **self._completion_kwargs_resolver(provider_name, task_name),
                        **overrides,
                    }
                    return self._completion_client(provider_name, model, messages, **request_kwargs)
                except Exception as exc:
                    logging.warning(
                        "Inference task %s failed via provider=%s model=%s: %s",
                        task_name,
                        provider_name,
                        model,
                        exc,
                    )
                    last_error = exc
        raise RuntimeError(f"All providers failed for inference task: {task_name}") from last_error


INFERENCE_TASK_RUNNER = InferenceTaskRunner()
"""@brief 进程共享任务执行器 / Process-shared inference task runner."""
