"""@brief Assistant 基础设施装配凭据边界测试 / Assistant infrastructure-composition credential-boundary tests."""

from __future__ import annotations

import pytest

from fogmoe_bot.config import BotSettings
from fogmoe_bot.infrastructure.assistant.composition import (
    _image_api_token,
    _retrieval_api_key,
)


def _settings(
    *,
    embedding_key: str | None = None,
    image_token: str | None = None,
    chat_key: str | None = None,
) -> BotSettings:
    """@brief 构造带独立凭据的测试配置 / Build test settings with independent credentials.

    @param embedding_key embedding 专用密钥 / Dedicated embedding key.
    @param image_token 图片服务专用令牌 / Dedicated image-service token.
    @param chat_key 聊天 provider 密钥 / Chat-provider key.
    @return 已验证 Bot 设置 / Validated Bot settings.
    """

    return BotSettings.model_validate(
        {
            "assistant": {"retrieval": {"embedding": {"api_key": embedding_key}}},
            "integrations": {"image_generation": {"api_token": image_token}},
            "ai": {
                "providers": [
                    {
                        "id": "chat",
                        "label": "Chat",
                        "style": "openai",
                        "endpoint": "https://example.test/v1/chat/completions",
                        "auth": {"api_key": chat_key},
                    }
                ]
            },
        }
    )


def test_retrieval_requires_its_dedicated_key() -> None:
    """@brief 检索不得回退到聊天 provider 密钥 / Retrieval must not fall back to a chat-provider key.

    @return None / None.
    """

    with pytest.raises(RuntimeError, match=r"retrieval\.embedding\.api_key"):
        _retrieval_api_key(_settings(chat_key="chat-secret"))


def test_retrieval_uses_its_dedicated_key() -> None:
    """@brief 检索只读取自己的密钥 / Retrieval reads only its own key.

    @return None / None.
    """

    assert _retrieval_api_key(
        _settings(embedding_key="embedding-secret", chat_key="chat-secret")
    ) == "embedding-secret"


def test_image_service_does_not_reuse_chat_provider_key() -> None:
    """@brief 图片服务不得回退到聊天 provider 密钥 / Image service must not fall back to a chat-provider key.

    @return None / None.
    """

    assert _image_api_token(_settings(chat_key="chat-secret")) == ""
    assert _image_api_token(
        _settings(image_token="image-secret", chat_key="chat-secret")
    ) == "image-secret"
