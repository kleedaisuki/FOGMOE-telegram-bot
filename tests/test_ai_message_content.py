"""@brief Canonical message 多模态辅助函数测试 / Canonical-message multimodal helper tests."""

from fogmoe_bot.application.assistant.inference.message_content import (
    messages_have_images,
    strip_image_content,
)
from fogmoe_bot.domain.assistant.messages import (
    CanonicalMessage,
    ImagePart,
    TextPart,
    UrlImageSource,
)
from fogmoe_bot.domain.conversation.message import MessageRole


def test_messages_have_images_detects_canonical_image_parts() -> None:
    """@brief 检测规范图像片段 / Detect canonical image parts.

    @return None / None.
    """

    messages = [
        CanonicalMessage(MessageRole.USER, (TextPart("hello"),)),
        CanonicalMessage(
            MessageRole.USER,
            (
                TextPart("describe this"),
                ImagePart(UrlImageSource("https://example.test/a.png")),
            ),
        ),
    ]

    assert messages_have_images(messages) is True


def test_canonical_message_text_keeps_text_parts_in_order() -> None:
    """@brief 保留规范消息中的有序文本 / Keep ordered text in a canonical message.

    @return None / None.
    """

    message = CanonicalMessage(
        MessageRole.USER,
        (
            TextPart("first"),
            ImagePart(UrlImageSource("https://example.test/a.png")),
            TextPart("second"),
        ),
    )

    assert message.text == "first\nsecond"


def test_strip_image_content_returns_new_canonical_message() -> None:
    """@brief 降级图像消息且不修改原对象 / Downgrade image message without mutation.

    @return None / None.
    """

    original = CanonicalMessage(
        MessageRole.USER,
        (
            TextPart("caption"),
            ImagePart(UrlImageSource("https://example.test/a.png")),
        ),
    )
    messages = [CanonicalMessage(MessageRole.SYSTEM, (TextPart("system prompt"),)), original]

    stripped = strip_image_content(messages)

    assert stripped[0] == messages[0]
    assert stripped[1] == CanonicalMessage(MessageRole.USER, (TextPart("caption"),))
    assert original.has_images is True
    assert original.parts[1] == ImagePart(UrlImageSource("https://example.test/a.png"))
