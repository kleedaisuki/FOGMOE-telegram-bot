from collections.abc import Iterable, Mapping


def content_has_image(content: object) -> bool:
    """@brief 判断消息内容是否包含图像项 / Check whether message content contains an image item.

    @param content provider-neutral 消息内容 / Provider-neutral message content.
    @return 存在 image_url 项时为 True / True when an image_url item is present.
    """

    if not isinstance(content, list):
        return False
    return any(
        isinstance(item, Mapping) and item.get("type") == "image_url"
        for item in content
    )


def messages_have_images(messages: Iterable[Mapping[str, object]]) -> bool:
    """@brief 判断消息序列是否包含图像 / Check whether a message sequence contains images.

    @param messages 已通过边界校验的消息序列 / Boundary-validated message sequence.
    @return 任一消息含图像时为 True / True when any message contains an image.
    """

    return any(content_has_image(message.get("content")) for message in messages)


def content_to_text(content: object) -> str:
    """@brief 将多模态内容降级为纯文本 / Reduce multimodal content to plain text.

    @param content provider-neutral 消息内容 / Provider-neutral message content.
    @return 按原顺序连接的文本项 / Text items joined in their original order.
    """

    if not isinstance(content, list):
        return content if isinstance(content, str) else str(content or "")

    text_parts: list[str] = []
    for item in content:
        if not isinstance(item, Mapping):
            continue
        if item.get("type") == "text":
            text = item.get("text")
            if text:
                text_parts.append(str(text))
    return "\n".join(text_parts)


def strip_image_content(
    messages: Iterable[Mapping[str, object]],
) -> list[dict[str, object]]:
    """@brief 生成移除图像后的消息副本 / Copy messages with image items removed.

    @param messages 已通过边界校验的消息序列 / Boundary-validated message sequence.
    @return 适用于纯文本模型的消息列表 / Message list suitable for text-only models.
    @note 不修改输入消息 / Does not mutate input messages.
    """

    stripped_messages: list[dict[str, object]] = []
    for message in messages:
        content = message.get("content")
        stripped_message = dict(message)
        if not content_has_image(content):
            stripped_messages.append(stripped_message)
            continue
        stripped_message["content"] = content_to_text(content)
        stripped_messages.append(stripped_message)
    return stripped_messages
