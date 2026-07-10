import json
import logging
from typing import Any, Dict, Iterable, List, Optional

from .delivery.visible_content import emit_visible_content, send_tool_media
from .tool_calling.execution import execute_tool_call
from .tool_calling.protocol import (
    assistant_message_to_plain,
    normalise_tool_calls,
)
from .tools import OPENAI_TOOLS, AI_TOOL_ARG_MODELS, AI_TOOL_HANDLERS
from fogmoe_bot.infrastructure.llm.litellm_client import create_chat_completion
from .types import AIResponse, PartialAIResponseError, ToolLog, VisibleContentHandler


def _public_tool_result(
    tool_name: str,
    tool_result: Dict[str, Any],
    *,
    media_sent: bool = False,
) -> Dict[str, Any]:
    if tool_name not in {"generate_image", "generate_voice"} or not isinstance(tool_result, dict):
        return tool_result

    if tool_result.get("error"):
        public_result = {"error": tool_result.get("error")}
        for key in ("status_code", "details", "response_preview", "warnings", "retry_after_seconds"):
            if key in tool_result:
                public_result[key] = tool_result[key]
        return public_result

    if tool_name == "generate_image" and tool_result.get("status") == "generated":
        return {
            "status": "generated",
            "message": (
                "Generated image has been sent to Telegram."
                if media_sent
                else (
                    "Generated image is ready and will be sent to Telegram. "
                    "If you need to inspect the image yourself later, ask the user "
                    "to forward the sent image back to you."
                )
            ),
        }

    if tool_name == "generate_voice" and tool_result.get("status") == "generated":
        return {
            "status": "generated",
            "message": (
                "Generated audio has been sent to Telegram."
                if media_sent
                else "Generated audio is ready and will be sent to Telegram."
            ),
        }

    return {"status": tool_result.get("status") or "unknown"}


def _log_generate_image_result(provider_name: str, tool_result: Dict[str, Any]) -> None:
    if not isinstance(tool_result, dict):
        logging.warning("%s generate_image returned non-dict result: %s", provider_name, type(tool_result).__name__)
        return

    if tool_result.get("error"):
        logging.warning(
            "%s generate_image returned error: error=%s, status_code=%s, retry_after_seconds=%s, details=%s",
            provider_name,
            tool_result.get("error"),
            tool_result.get("status_code"),
            tool_result.get("retry_after_seconds"),
            str(tool_result.get("details") or tool_result.get("response_preview") or "")[:500],
        )
        return

    if tool_result.get("status") == "generated":
        images = [tool_result["image"]] if isinstance(tool_result.get("image"), dict) else []
        if not images and isinstance(tool_result.get("images"), list):
            images = tool_result["images"]
        logging.info(
            "%s generate_image generated %s image(s): count=%s, warnings=%s",
            provider_name,
            len(images),
            tool_result.get("count"),
            tool_result.get("warnings"),
        )
        return

    logging.info(
        "%s generate_image returned status=%s",
        provider_name,
        tool_result.get("status") or "unknown",
    )


def _log_generate_voice_result(provider_name: str, tool_result: Dict[str, Any]) -> None:
    if not isinstance(tool_result, dict):
        logging.warning("%s generate_voice returned non-dict result: %s", provider_name, type(tool_result).__name__)
        return

    if tool_result.get("error"):
        logging.warning(
            "%s generate_voice returned error: error=%s, status_code=%s, retry_after_seconds=%s, details=%s",
            provider_name,
            tool_result.get("error"),
            tool_result.get("status_code"),
            tool_result.get("retry_after_seconds"),
            str(tool_result.get("details") or tool_result.get("response_preview") or "")[:500],
        )
        return

    if tool_result.get("status") == "generated":
        audios = tool_result.get("audios") if isinstance(tool_result.get("audios"), list) else []
        logging.info(
            "%s generate_voice generated %s audio clip(s): count=%s, warnings=%s",
            provider_name,
            len(audios),
            tool_result.get("count"),
            tool_result.get("warnings"),
        )
        return

    logging.info(
        "%s generate_voice returned status=%s",
        provider_name,
        tool_result.get("status") or "unknown",
    )


def _return_final_text_response(
    *,
    content_text: str,
    tool_logs: List[ToolLog],
    visible_content_handler: Optional[VisibleContentHandler],
    provider_name: str,
) -> AIResponse:
    if content_text.strip():
        if visible_content_handler:
            visible_result = emit_visible_content(
                visible_content_handler,
                content_text,
                provider_name=provider_name,
            )
            if visible_result.content:
                tool_logs.append({
                    "type": "assistant_visible",
                    "content": visible_result.content,
                })
                return "", tool_logs
            if not visible_result.completed:
                return "", tool_logs
            return content_text, tool_logs
        return content_text, tool_logs
    if tool_logs:
        logging.warning("%s 工具调用后最终回复为空。", provider_name)
    return content_text, tool_logs


def run_tool_loop(
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
    visible_content_handler: Optional[VisibleContentHandler] = None,
) -> AIResponse:
    """Run a tool-calling loop through LiteLLM using OpenAI-format tools."""
    tools = OPENAI_TOOLS
    filtered_messages = [
        msg for msg in messages if msg.get("content") is not None or msg.get("tool_calls")
    ]

    tool_logs: List[ToolLog] = []
    skip_set = set(skip_tools or [])

    for iteration in range(max_iterations):
        request_tool_choice = tool_choice
        try:
            request_kwargs = {"max_tokens": max_tokens, **(completion_kwargs or {})}
            response = create_chat_completion(
                provider,
                model,
                messages=filtered_messages,
                tools=tools,
                tool_choice=request_tool_choice,
                **request_kwargs,
            )
        except Exception as exc:
            if tool_logs:
                raise PartialAIResponseError(str(exc), tool_logs) from exc
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
                tool_logs.append({
                    "type": "assistant_visible",
                    "content": visible_result.content,
                })
                if not visible_result.completed:
                    return "", tool_logs
            elif not visible_result.completed:
                return "", tool_logs

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

            raw_args = function_payload.get("arguments") or "{}"
            try:
                raw_function_args = json.loads(raw_args)
            except json.JSONDecodeError as exc:
                logging.error("%s 工具参数解析失败: %s", provider_name, exc)
                raw_function_args = {}

            execution = execute_tool_call(
                function_name=function_name,
                raw_function_args=raw_function_args,
                provider_name=provider_name,
                arg_models=AI_TOOL_ARG_MODELS,
                handlers=AI_TOOL_HANDLERS,
            )

            tool_call_id = tool_call.get("id")
            tool_log_entry = {
                "type": "assistant_tool_call",
                "tool_name": function_name,
                "arguments": execution.logged_args,
                "tool_call_id": tool_call_id,
            }
            if execution.validation_error is not None:
                tool_log_entry["validation_error"] = execution.validation_error
            if not assistant_message_logged:
                tool_log_entry["assistant_message"] = assistant_model_message
                assistant_message_logged = True
            tool_logs.append(tool_log_entry)

            function_args = execution.function_args
            internal_tool_result = execution.internal_result

            if function_name == "generate_image":
                _log_generate_image_result(provider_name, internal_tool_result)
            elif function_name == "generate_voice":
                _log_generate_voice_result(provider_name, internal_tool_result)

            sent_media_messages = send_tool_media(
                visible_content_handler=visible_content_handler,
                tool_name=function_name,
                tool_result=internal_tool_result,
                provider_name=provider_name,
            )
            media_sent = bool(sent_media_messages)

            tool_result = _public_tool_result(
                function_name,
                internal_tool_result,
                media_sent=media_sent,
            )

            filtered_messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "name": function_name,
                "content": json.dumps(tool_result, ensure_ascii=False),
            })
            tool_log_entry = {
                "type": "tool_result",
                "tool_name": function_name,
                "arguments": function_args,
                "result": tool_result,
                "tool_call_id": tool_call_id,
            }
            if function_name in {"generate_image", "generate_voice"}:
                tool_log_entry["internal_result"] = internal_tool_result
                if media_sent:
                    tool_log_entry["media_sent"] = True
                    tool_log_entry["sent_message_count"] = len(sent_media_messages)
            tool_logs.append(tool_log_entry)

    logging.warning("%s 工具调用次数超限（%s轮）", provider_name, max_iterations)
    try:
        request_kwargs = {"max_tokens": max_tokens, **(completion_kwargs or {})}
        response = create_chat_completion(
            provider,
            model,
            messages=filtered_messages,
            **request_kwargs,
        )
    except Exception as exc:
        if tool_logs:
            raise PartialAIResponseError(str(exc), tool_logs) from exc
        raise

    assistant_message = response.choices[0].message
    raw_tool_calls = getattr(assistant_message, "tool_calls", None)
    if raw_tool_calls:
        logging.warning(
            "%s 工具调用超限后的最终回复仍包含工具调用，忽略工具调用并使用文本内容。",
            provider_name,
        )
    return _return_final_text_response(
        content_text=assistant_message.content or "",
        tool_logs=tool_logs,
        visible_content_handler=visible_content_handler,
        provider_name=provider_name,
    )
