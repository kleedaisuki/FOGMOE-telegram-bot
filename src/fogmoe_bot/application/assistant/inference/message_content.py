"""@brief Canonical message 的多模态选择 / Multimodal selection for canonical messages."""

from __future__ import annotations

from collections.abc import Iterable

from fogmoe_bot.domain.assistant.messages import CanonicalMessage


def message_has_image(message: CanonicalMessage) -> bool:
    """@brief 判断 canonical 消息是否包含图像 / Check whether a canonical message contains an image.

    @param message canonical V2 消息 / Canonical V2 message.
    @return 存在 ImagePart 时为 True / True when an ImagePart is present.
    """

    return message.has_images


def messages_have_images(messages: Iterable[CanonicalMessage]) -> bool:
    """@brief 判断消息序列是否包含图像 / Check whether a message sequence contains an image.

    @param messages canonical V2 消息序列 / Canonical V2 message sequence.
    @return 任一消息含 ImagePart 时为 True / True when any message contains an ImagePart.
    """

    return any(message_has_image(message) for message in messages)


def strip_image_content(
    messages: Iterable[CanonicalMessage],
) -> list[CanonicalMessage]:
    """@brief 生成移除图像后的消息副本 / Build message copies with image parts removed.

    @param messages canonical V2 消息序列 / Canonical V2 message sequence.
    @return 适用于纯文本模型的 canonical 消息列表 / Canonical messages suitable for text-only models.
    @note 不修改输入消息 / Does not mutate input messages.
    """

    return [
        message.without_images() if message_has_image(message) else message
        for message in messages
    ]


__all__ = ["message_has_image", "messages_have_images", "strip_image_content"]
