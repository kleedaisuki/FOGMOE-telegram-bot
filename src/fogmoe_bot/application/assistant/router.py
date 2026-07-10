import asyncio
import logging
from typing import Dict, Optional

from fogmoe_bot.infrastructure import config

from .agent_response import AgentResponse
from .chat_capabilities import chat_model_for_service, chat_service_supports_vision
from .delivery.contracts import VisibleContentSink
from .delivery.visible_content import visible_content_events, visible_content_was_sent
from .errors import PartialAgentResponseError, SafetyBlockError
from .message_content import messages_have_images, strip_image_content
from fogmoe_bot.domain.agent_runtime.tools import (
    cleanup_linux_sandbox,
    clear_tool_request_context,
    set_tool_request_context,
)
from .providers import azure, gemini, openai, openrouter, siliconflow, zhipu
from fogmoe_bot.domain.agent_runtime.executor import EXECUTOR
from .routing.provider_circuit import ProviderCircuit

AI_SERVICE_MAP = {
    "openai": openai.get_ai_response,
    "openrouter": openrouter.get_ai_response,
    "gemini": gemini.get_ai_response,
    "azure": azure.get_ai_response,
    "siliconflow": siliconflow.get_ai_response,
    "zhipu": zhipu.get_ai_response,
    "zai": zhipu.get_ai_response,
}

AI_SERVICE_ORDER = config.AI_SERVICE_ORDER

AI_PROVIDER_CIRCUIT_FAILURE_THRESHOLD = 3
AI_PROVIDER_CIRCUIT_WINDOW_SECONDS = 5 * 60
AI_PROVIDER_CIRCUIT_COOLDOWN_SECONDS = 30 * 60
_provider_circuit = ProviderCircuit(
    failure_threshold=AI_PROVIDER_CIRCUIT_FAILURE_THRESHOLD,
    window_seconds=AI_PROVIDER_CIRCUIT_WINDOW_SECONDS,
    cooldown_seconds=AI_PROVIDER_CIRCUIT_COOLDOWN_SECONDS,
)

PARTIAL_AI_RESPONSE_ERROR_MESSAGE = (
    "看起来对话出现了一些小问题呢。"
    "您可以尝试使用 /clear 命令来清空聊天记录，"
    "然后我们重新开始对话吧！\n"
    "It seems there was a small issue with the conversation."
    "You can try using the /clear command to clear the chat history,"
    "and then we can start over!\n\n"
    "错误信息 Error message: \n\n"
    "问题类型：工具执行后回复生成失败。\n"
    "Issue type: response generation failed after tool execution.\n\n"
    "内部处理失败，详细信息已记录。\n"
    "Internal processing failed. Details have been logged.\n\n"
    "您可以发送给管理员 @ScarletKc 报告此问题。\n"
    "You can report this issue to the admin @ScarletKc."
)


def _call_service_with_context(
    service_name: str,
    messages,
    user_id: int,
    tool_context: Optional[Dict[str, object]],
    visible_content_handler: Optional[VisibleContentSink],
) -> AgentResponse:
    request_context = dict(tool_context or {})
    request_context.setdefault("user_id", user_id)
    set_tool_request_context(request_context)
    try:
        return AI_SERVICE_MAP[service_name](
            messages,
            user_id,
            visible_content_handler=visible_content_handler,
        )
    finally:
        try:
            cleanup_linux_sandbox()
        finally:
            clear_tool_request_context()


def _messages_for_service(
    service_name: str,
    messages,
    text_fallback_messages=None,
):
    if not messages_have_images(messages):
        return messages
    if chat_service_supports_vision(service_name):
        return messages

    model = chat_model_for_service(service_name)
    logging.info(
        "AI chat provider %s model=%s is configured as text-only; using vision text fallback",
        service_name,
        model,
    )
    if text_fallback_messages is not None:
        return list(text_fallback_messages)
    return strip_image_content(messages)


async def _try_ai_services(
    messages,
    user_id: int,
    tool_context: Optional[Dict[str, object]] = None,
    visible_content_handler: Optional[VisibleContentSink] = None,
    text_fallback_messages=None,
) -> tuple[AgentResponse | None, Exception | None]:
    last_error = None
    loop = asyncio.get_running_loop()

    for service_name in AI_SERVICE_ORDER:
        if _provider_circuit.is_open(service_name):
            logging.warning("%s 当前处于熔断冷却中，跳过调用", service_name)
            continue

        service_messages = _messages_for_service(
            service_name,
            messages,
            text_fallback_messages,
        )
        try:
            response = await loop.run_in_executor(
                EXECUTOR,
                lambda s=service_name, m=service_messages: _call_service_with_context(
                    s,
                    m.copy(),
                    user_id,
                    tool_context,
                    visible_content_handler,
                ),
            )
            _provider_circuit.record_success(service_name)
            return response, None
        except SafetyBlockError:
            if visible_content_was_sent(visible_content_handler):
                logging.warning(
                    "%s triggered safety block after sending visible content; not retrying",
                    service_name,
                )
                return AgentResponse("", visible_content_events(visible_content_handler)), None
            if service_name == "gemini":
                logging.warning("Gemini triggered safety block, trying next service")
                last_error = SafetyBlockError("SafetyBlockError")
                continue
            raise
        except PartialAgentResponseError as exc:
            logging.error(
                "%s failed after partial AI response; not retrying: %s",
                service_name,
                exc,
                exc_info=True,
            )
            if visible_content_was_sent(visible_content_handler):
                return AgentResponse("", exc.events), None
            return AgentResponse(PARTIAL_AI_RESPONSE_ERROR_MESSAGE, exc.events), None
        except Exception as exc:
            if visible_content_was_sent(visible_content_handler):
                logging.error(
                    "%s failed after sending visible content; not retrying: %s",
                    service_name,
                    exc,
                    exc_info=True,
                )
                return AgentResponse("", visible_content_events(visible_content_handler)), None
            logging.warning("%s 调用失败: %s", service_name, exc)
            _provider_circuit.record_failure(service_name)
            last_error = exc
            continue

    return None, last_error


async def get_ai_response(
    messages,
    user_id: int,
    tool_context: Optional[Dict[str, object]] = None,
    text_fallback_messages=None,
    visible_content_handler: Optional[VisibleContentSink] = None,
) -> AgentResponse:
    """
    统一AI响应异步接口，根据配置的顺序依次尝试不同的AI服务
    """
    response, last_error = await _try_ai_services(
        messages,
        user_id,
        tool_context,
        visible_content_handler,
        text_fallback_messages,
    )
    if response is not None:
        return response

    if messages_have_images(messages):
        logging.warning("多模态 AI 调用全部失败，降级为纯文本图片描述重试: %s", last_error)
        if text_fallback_messages is not None:
            text_messages = list(text_fallback_messages)
        else:
            text_messages = strip_image_content(messages)
        response, last_error = await _try_ai_services(
            text_messages,
            user_id,
            tool_context,
            visible_content_handler,
        )
        if response is not None:
            return response

    logging.error("所有AI服务均调用失败: %s", last_error)
    return AgentResponse(
        "抱歉喵，雾萌娘在处理你的请求时遇到了一点小问题！现在有点不舒服啦，请稍后再试吧～\n"
        "请联系管理员 @ScarletKc 反馈问题。",
        [],
    )
