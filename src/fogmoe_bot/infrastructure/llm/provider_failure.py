"""@brief 原生 LLM adapter 的 application failure re-export / Re-export application failures for native LLM adapters.

失败分类属于 completion port 的应用层契约，durable inference 必须能够在不依赖
infrastructure 的情况下消费它。此模块保留旧的 adapter-local import 路径，避免
调用方为了实现细节而复制异常类型。/
Failure classification belongs to the application completion-port contract so durable inference
can consume it without depending on infrastructure. This module retains the adapter-local import
path and prevents callers from copying implementation-specific exception types.
"""

from fogmoe_bot.application.assistant.errors import (
    ProviderContractError,
    ProviderFailure,
    ProviderFailureKind,
)

__all__ = [
    "ProviderContractError",
    "ProviderFailure",
    "ProviderFailureKind",
]
