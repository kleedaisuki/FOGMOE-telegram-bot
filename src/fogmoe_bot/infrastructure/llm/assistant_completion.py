"""@brief LiteLLM 异步 completion port / Asynchronous LiteLLM completion port."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from fogmoe_bot.application.assistant.completion import (
    AssistantCompletion,
    CompletionToolCall,
)
from fogmoe_bot.application.assistant.tools.catalog import ToolDefinition
from fogmoe_bot.domain.conversation.payloads import (
    JsonObject,
    JsonValue,
)
from fogmoe_bot.infrastructure.blocking import AsyncBlockingBulkhead

from .litellm_client import create_chat_completion
from .protocol import assistant_message_to_plain, normalise_tool_calls


class LiteLLMAssistantCompletion:
    """@brief 把同步 LiteLLM 限定在 thread adapter / Confine synchronous LiteLLM to a thread adapter."""

    def __init__(self, *, bulkhead: AsyncBlockingBulkhead) -> None:
        """@brief 注入独立的 provider 隔舱 / Inject a dedicated provider bulkhead.

        @param bulkhead 同步 LiteLLM 调用隔舱 / Synchronous LiteLLM call bulkhead.
        """

        self._bulkhead = bulkhead

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
        response = await self._bulkhead.call(
            lambda: create_chat_completion(
                provider,
                model,
                [dict(message) for message in messages],
                **kwargs,
            )
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
