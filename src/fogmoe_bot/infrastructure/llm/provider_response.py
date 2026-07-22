"""@brief Provider codec 的已解码响应 / Decoded responses from provider codecs."""

from __future__ import annotations

from dataclasses import dataclass

from fogmoe_bot.domain.assistant.messages import CanonicalMessage


@dataclass(frozen=True, slots=True)
class DecodedProviderCompletion:
    """@brief 传输无关的已解码 provider completion / Transport-neutral decoded provider completion.

    @param message 可持久化的 Canonical Message V2 / Persistable Canonical Message V2.
    @param input_tokens 可选输入 token 计数 / Optional input-token count.
    @param output_tokens 可选输出 token 计数 / Optional output-token count.
    """

    message: CanonicalMessage
    input_tokens: int | None
    output_tokens: int | None

__all__ = ["DecodedProviderCompletion"]
