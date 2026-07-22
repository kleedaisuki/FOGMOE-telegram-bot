"""@brief LiteLLM 异步 completion port / Asynchronous LiteLLM completion port."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from fogmoe_bot.application.assistant.completion import (
    AssistantCompletion,
    CompletionToolCall,
)
from fogmoe_bot.application.assistant.tools.catalog import ToolDefinition
from fogmoe_bot.application.observability.telemetry import Telemetry
from fogmoe_bot.domain.conversation.payloads import (
    JsonObject,
    JsonValue,
)
from fogmoe_bot.domain.observability.conventions import MetricName, Outcome
from fogmoe_bot.domain.observability.signals import SpanKind
from fogmoe_bot.infrastructure.blocking import AsyncBlockingBulkhead

from .litellm_client import LiteLLMChatClient
from .protocol import assistant_message_to_plain, normalise_tool_calls


class LiteLLMAssistantCompletion:
    """@brief 把同步 LiteLLM 限定在 thread adapter / Confine synchronous LiteLLM to a thread adapter."""

    def __init__(
        self,
        *,
        bulkhead: AsyncBlockingBulkhead,
        telemetry: Telemetry,
        client: LiteLLMChatClient,
    ) -> None:
        """@brief 注入独立的 provider 隔舱 / Inject a dedicated provider bulkhead.

        @param bulkhead 同步 LiteLLM 调用隔舱 / Synchronous LiteLLM call bulkhead.
        @param telemetry 进程 typed telemetry / Process typed telemetry.
        @param client 绑定显式 provider 设置的 LiteLLM 客户端 /
            LiteLLM client bound to explicit provider settings.
        @return None / None.
        """

        self._bulkhead = bulkhead
        self._telemetry = telemetry
        self._client = client

    async def complete(
        self,
        *,
        provider: str,
        model: str,
        messages: Sequence[JsonObject],
        tools: Sequence[ToolDefinition],
        tool_choice: str | JsonObject | None,
        max_tokens: int,
        request_options: Mapping[str, JsonValue],
    ) -> AssistantCompletion:
        """@brief 在线程边界请求并归一化完成 / Request and normalize a completion behind a thread boundary.

        @param provider provider 名称 / Provider name.
        @param model 模型名称 / Model name.
        @param messages 规范消息 / Canonical messages.
        @param tools typed tools / Typed tools.
        @param tool_choice 工具策略 / Tool policy.
        @param max_tokens 输出上限 / Output limit.
        @param request_options route 选项 / Route options.
        @return provider-neutral completion / Provider-neutral completion.
        """

        kwargs: dict[str, object] = dict(request_options)
        kwargs["max_tokens"] = max_tokens
        if tools:
            kwargs["tools"] = tuple(tools)
            kwargs["tool_choice"] = tool_choice
        attributes = {
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": provider,
            "gen_ai.request.model": model,
            "gen_ai.request.max_tokens": max_tokens,
        }
        with self._telemetry.span(
            "chat",
            kind=SpanKind.CLIENT,
            attributes=attributes,
        ) as span:
            try:
                response = await self._bulkhead.call(
                    lambda: self._client.complete(
                        provider,
                        model,
                        [dict(message) for message in messages],
                        **kwargs,
                    )
                )
            except Exception:
                self._telemetry.counter(
                    MetricName.LLM_OUTCOMES,
                    attributes={
                        "outcome": Outcome.FAILURE,
                        "gen_ai.provider.name": provider,
                        "gen_ai.request.model": model,
                    },
                )
                raise
            usage = getattr(response, "usage", None)
            for attribute_name, response_name in (
                ("gen_ai.usage.input_tokens", "prompt_tokens"),
                ("gen_ai.usage.output_tokens", "completion_tokens"),
            ):
                value = getattr(usage, response_name, None)
                if (
                    isinstance(value, int)
                    and not isinstance(value, bool)
                    and value >= 0
                ):
                    span.set_attribute(attribute_name, value)
                    self._telemetry.counter(
                        "gen_ai.client.token.usage",
                        float(value),
                        unit="{token}",
                        attributes={
                            "gen_ai.provider.name": provider,
                            "gen_ai.request.model": model,
                            "gen_ai.token.type": (
                                "input"
                                if response_name == "prompt_tokens"
                                else "output"
                            ),
                        },
                    )
            self._telemetry.counter(
                MetricName.LLM_OUTCOMES,
                attributes={
                    "outcome": Outcome.SUCCESS,
                    "gen_ai.provider.name": provider,
                    "gen_ai.request.model": model,
                },
            )
        choices = getattr(response, "choices", None)
        if not isinstance(choices, Sequence) or not choices:
            raise ValueError("Provider response contains no choices")
        message = getattr(choices[0], "message", None)
        if message is None:
            raise ValueError("Provider response contains no Assistant message")
        content_value = getattr(message, "content", "")
        content = "" if content_value is None else str(content_value)
        raw_calls = getattr(message, "tool_calls", None)
        plain_calls = normalise_tool_calls(raw_calls)
        calls: list[CompletionToolCall] = []
        for value in plain_calls:
            function = value.get("function")
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            if not isinstance(name, str) or not name:
                continue
            call_id = value.get("id")
            calls.append(
                CompletionToolCall(
                    provider_call_id=str(call_id) if call_id is not None else None,
                    name=name,
                    arguments=function.get("arguments", "{}"),
                )
            )
        plain_message = assistant_message_to_plain(
            message,
            content=content,
            tool_calls=plain_calls,
        )
        return AssistantCompletion(content, plain_message, tuple(calls))


__all__ = ["LiteLLMAssistantCompletion"]
