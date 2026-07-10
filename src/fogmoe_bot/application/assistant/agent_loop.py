"""@brief Assistant Agent 回合驱动 / Assistant Agent turn driver."""

import json
import logging
from typing import Any, Dict, Iterable, List, Optional

from fogmoe_bot.domain.agent_runtime import DEFAULT_AGENT_RUNTIME, ToolTask
from fogmoe_bot.domain.agent_runtime.events import RuntimeEvent
from fogmoe_bot.domain.agent_runtime.protocol import (
    assistant_message_to_plain,
    normalise_tool_calls,
)
from fogmoe_bot.infrastructure.llm.litellm_client import create_chat_completion

from .agent_response import AgentResponse
from .delivery.contracts import VisibleContentSink
from .delivery.visible_content import emit_visible_content
from .errors import PartialAgentResponseError


def _return_final_text_response(
    *,
    content_text: str,
    tool_logs: List[RuntimeEvent],
    visible_content_handler: Optional[VisibleContentSink],
    provider_name: str,
) -> AgentResponse:
    """@brief 处理 Agent 最终文本 / Handle final Agent text.

    @param content_text Agent 最终文本 / Final Agent text.
    @param tool_logs Runtime 审计事件 / Runtime audit events.
    @param visible_content_handler 可见文本输出端口 / Visible text output port.
    @param provider_name provider 名称 / Provider name.
    @return 回复文本和事件 / Reply text and events.
    """
    if content_text.strip():
        if visible_content_handler:
            visible_result = emit_visible_content(
                visible_content_handler,
                content_text,
                provider_name=provider_name,
            )
            if visible_result.content:
                tool_logs.append({"type": "assistant_visible", "content": visible_result.content})
                return AgentResponse("", tool_logs)
            if not visible_result.completed:
                return AgentResponse("", tool_logs)
        return AgentResponse(content_text, tool_logs)
    if tool_logs:
        logging.warning("%s 工具调用后最终回复为空。", provider_name)
    return AgentResponse(content_text, tool_logs)


def _parse_tool_arguments(raw_args: Any, *, provider_name: str) -> Any:
    """@brief 解析 Agent 工具参数 / Parse Agent tool arguments.

    @param raw_args provider 返回的原始参数 / Raw provider arguments.
    @param provider_name provider 名称 / Provider name.
    @return 可提交给 Runtime 的参数 / Arguments suitable for Runtime submission.
    """
    if isinstance(raw_args, (dict, list)):
        return raw_args
    try:
        return json.loads(raw_args or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        logging.error("%s 工具参数解析失败: %s", provider_name, exc)
        return {}


def run_agent_loop(
    provider: str,
    model: str,
    messages: List[Dict[str, Any]],
    *,
    provider_name: str = "AI",
    tool_choice: str | Dict[str, object] = "auto",
    max_tokens: int = 4096,
    max_iterations: int = 10,
    skip_tools: Optional[Iterable[str]] = None,
    completion_kwargs: Optional[Dict[str, Any]] = None,
    visible_content_handler: Optional[VisibleContentSink] = None,
) -> AgentResponse:
    """@brief 驱动一个 Agent 回合 / Drive an Agent turn.

    Agent 只将 provider 的调用意图转换为 ToolTask，随后提交并消费
    AgentRuntime 的完成结果；参数校验、handler 选择、媒体副作用及公开结果
    投影均由 Runtime 负责。

    @param provider LiteLLM provider 名称 / LiteLLM provider name.
    @param model 模型名称 / Model name.
    @param messages 初始上下文消息 / Initial context messages.
    @param provider_name 面向日志的名称 / Name used in logs.
    @param tool_choice 工具选择策略 / Tool-choice policy.
    @param max_tokens 最大输出 token 数 / Maximum output tokens.
    @param max_iterations 最大 Agent 工具轮次 / Maximum Agent tool rounds.
    @param skip_tools 要忽略的能力名称 / Capability names to ignore.
    @param completion_kwargs provider 补充参数 / Extra provider parameters.
    @param visible_content_handler 用户可见内容输出端口 / User-visible content output port.
    @return 文本回复和 Runtime 事件 / Text reply and Runtime events.
    """
    filtered_messages = [
        message for message in messages if message.get("content") is not None or message.get("tool_calls")
    ]
    tool_logs: List[RuntimeEvent] = []
    skip_set = set(skip_tools or [])

    for iteration in range(max_iterations):
        try:
            response = create_chat_completion(
                provider,
                model,
                messages=filtered_messages,
                tools=DEFAULT_AGENT_RUNTIME.tool_definitions,
                tool_choice=tool_choice,
                max_tokens=max_tokens,
                **(completion_kwargs or {}),
            )
        except Exception as exc:
            if tool_logs:
                raise PartialAgentResponseError(str(exc), tool_logs) from exc
            raise

        assistant_message = response.choices[0].message
        raw_tool_calls = getattr(assistant_message, "tool_calls", None)
        assistant_content = assistant_message.content or ""
        if not raw_tool_calls:
            logging.info("%s 第 %s 轮：无工具调用，直接返回答案", provider_name, iteration + 1)
            return _return_final_text_response(
                content_text=assistant_content,
                tool_logs=tool_logs,
                visible_content_handler=visible_content_handler,
                provider_name=provider_name,
            )

        tool_calls = normalise_tool_calls(raw_tool_calls)
        logging.info("%s 第 %s 轮：检测到 %s 个工具调用", provider_name, iteration + 1, len(tool_calls))
        assistant_content_for_model = assistant_content
        if visible_content_handler and assistant_content.strip():
            visible_result = emit_visible_content(
                visible_content_handler,
                assistant_content,
                provider_name=provider_name,
            )
            if visible_result.content:
                assistant_content_for_model = visible_result.content
                tool_logs.append({"type": "assistant_visible", "content": visible_result.content})
                if not visible_result.completed:
                    return AgentResponse("", tool_logs)
            elif not visible_result.completed:
                return AgentResponse("", tool_logs)

        assistant_model_message = assistant_message_to_plain(
            assistant_message,
            content=assistant_content_for_model,
            tool_calls=tool_calls,
        )
        filtered_messages.append(assistant_model_message)
        assistant_message_logged = False

        for tool_call in tool_calls:
            function_payload = tool_call.get("function") or {}
            function_name = function_payload.get("name")
            if not function_name:
                logging.warning("%s 返回的工具调用缺少函数名: %s", provider_name, tool_call)
                continue
            if function_name in skip_set:
                continue

            result = DEFAULT_AGENT_RUNTIME.consume(
                DEFAULT_AGENT_RUNTIME.submit(
                    ToolTask(
                        name=function_name,
                        arguments=_parse_tool_arguments(
                            function_payload.get("arguments"),
                            provider_name=provider_name,
                        ),
                        invocation_id=tool_call.get("id"),
                        producer_name=provider_name,
                    )
                ),
                visible_content_handler=visible_content_handler,
            )
            call_event: RuntimeEvent = {
                "type": "assistant_tool_call",
                "tool_name": result.name,
                "arguments": result.logged_arguments,
                "tool_call_id": result.invocation_id,
            }
            if result.validation_error is not None:
                call_event["validation_error"] = result.validation_error
            if not assistant_message_logged:
                call_event["assistant_message"] = assistant_model_message
                assistant_message_logged = True
            tool_logs.append(call_event)

            filtered_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": result.invocation_id,
                    "name": result.name,
                    "content": json.dumps(result.public_result, ensure_ascii=False),
                }
            )
            result_event: RuntimeEvent = {
                "type": "tool_result",
                "tool_name": result.name,
                "arguments": result.arguments,
                "result": result.public_result,
                "tool_call_id": result.invocation_id,
                "internal_result": result.internal_result,
            }
            if result.media_sent:
                result_event["media_sent"] = True
                result_event["sent_message_count"] = result.sent_message_count
            tool_logs.append(result_event)

    logging.warning("%s 工具调用次数超限（%s轮）", provider_name, max_iterations)
    try:
        response = create_chat_completion(
            provider,
            model,
            messages=filtered_messages,
            max_tokens=max_tokens,
            **(completion_kwargs or {}),
        )
    except Exception as exc:
        if tool_logs:
            raise PartialAgentResponseError(str(exc), tool_logs) from exc
        raise

    assistant_message = response.choices[0].message
    if getattr(assistant_message, "tool_calls", None):
        logging.warning("%s 工具调用超限后的最终回复仍包含工具调用，忽略工具调用。", provider_name)
    return _return_final_text_response(
        content_text=assistant_message.content or "",
        tool_logs=tool_logs,
        visible_content_handler=visible_content_handler,
        provider_name=provider_name,
    )
