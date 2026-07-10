import logging
from typing import NamedTuple

from .contracts import VisibleContentSink


class VisibleContentResult(NamedTuple):
    """@brief 可见内容发送结果 / Visible content emission result.

    @note content 是实际已发送或可恢复的内容；completed 表示 handler 是否完整完成 /
    content is the sent or recoverable content; completed tells whether the
    handler finished cleanly.
    """

    content: str
    completed: bool


def visible_content_was_sent(
    visible_content_handler: VisibleContentSink | None,
) -> bool:
    """@brief 判断是否已有内容发给用户 / Check whether content was sent to user.

    @param visible_content_handler 可见内容 handler / Visible content handler.
    @return True 表示已有文本或媒体消息发出 / True means text or media was sent.
    """
    if visible_content_handler is None:
        return False
    try:
        sent_count = int(getattr(visible_content_handler, "sent_count", 0))
    except (TypeError, ValueError):
        sent_count = 0
    if sent_count > 0:
        return True

    sent_messages = getattr(visible_content_handler, "sent_messages", [])
    if isinstance(sent_messages, list) and any(message is not None for message in sent_messages):
        return True

    contents = getattr(visible_content_handler, "sent_contents", [])
    if isinstance(contents, list) and any(str(content).strip() for content in contents):
        return True

    return bool(visible_content_events(visible_content_handler))


def visible_content_events(
    visible_content_handler: VisibleContentSink | None,
) -> list[dict[str, str]]:
    """@brief 读取已发送可见内容事件 / Read sent visible content events.

    @param visible_content_handler 可见内容 handler / Visible content handler.
    @return 可写入历史记录的事件列表 / Events suitable for chat history storage.
    """
    if visible_content_handler is None:
        return []
    visible_events = getattr(visible_content_handler, "visible_events", None)
    if callable(visible_events):
        try:
            events = visible_events()
            if isinstance(events, list):
                return [
                    event
                    for event in events
                    if isinstance(event, dict)
                    and str(event.get("content") or "").strip()
                ]
        except Exception:
            logging.exception("Failed to read visible content events")
            return []

    contents = getattr(visible_content_handler, "sent_contents", [])
    if not isinstance(contents, list):
        return []
    return [
        {
            "type": "assistant_visible",
            "content": str(content),
        }
        for content in contents
        if str(content).strip()
    ]


def last_visible_content(handler: VisibleContentSink) -> str:
    """@brief 读取最后一段可恢复内容 / Read the last recoverable content.

    @param handler 可见内容 handler / Visible content handler.
    @return 最后一段非空内容；没有则为空字符串 / Last non-empty content, or empty string.
    """
    events = visible_content_events(handler)
    for event in reversed(events):
        content = str(event.get("content") or "").strip()
        if content:
            return content
    return ""


def emit_visible_content(
    handler: VisibleContentSink,
    content: str,
    *,
    provider_name: str,
) -> VisibleContentResult:
    """@brief 发送 assistant 可见文本 / Emit assistant visible text.

    @param handler 可见内容 handler / Visible content handler.
    @param content 待发送内容 / Content to send.
    @param provider_name provider 名称，用于日志 / Provider name for logging.
    @return 发送结果 / Emission result.
    """
    if not content.strip():
        return VisibleContentResult("", True)

    try:
        visible_content = handler(content)
    except Exception as exc:
        logging.exception("%s visible content handler failed: %s", provider_name, exc)
        partial_content = last_visible_content(handler)
        if partial_content:
            return VisibleContentResult(partial_content, False)
        return VisibleContentResult("", True)

    if visible_content is None:
        partial_content = last_visible_content(handler)
        if partial_content:
            return VisibleContentResult(partial_content, False)
        return VisibleContentResult("", True)

    normalized = str(visible_content).strip()
    if normalized:
        return VisibleContentResult(normalized, True)

    partial_content = last_visible_content(handler)
    if partial_content:
        return VisibleContentResult(partial_content, False)
    return VisibleContentResult("", True)
