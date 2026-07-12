"""@brief 应用执行运行时公共接口 / Public application execution-runtime API."""

from fogmoe_bot.application.runtime.keyed_mailbox import (
    Accepted,
    AggregateIdentityPart,
    AggregateKey,
    AsyncOperation,
    KeyedMailboxRuntime,
    Overloaded,
    OverloadScope,
    RuntimeSnapshot,
    RuntimeState,
    RuntimeUnavailable,
    ShutdownMode,
    Submission,
    WorkPriority,
    WorkTicket,
)
from fogmoe_bot.application.runtime.clock import Jitter, SystemUtcClock, UtcClock
from fogmoe_bot.application.runtime.bot_runtime import (
    BackgroundService,
    BOT_RUNTIME_DATA_KEY,
    BotRuntime,
    BotRuntimeState,
    ServiceBinding,
)
from fogmoe_bot.application.runtime.rate_limit import ReplayAwareCooldownGate

EXECUTION_RUNTIME_DATA_KEY = "fogmoe.execution_runtime"
"""@brief 组合根保存执行运行时的稳定键 / Stable composition-root key for the execution runtime."""

__all__ = [
    "Accepted",
    "AggregateIdentityPart",
    "AggregateKey",
    "AsyncOperation",
    "BackgroundService",
    "BOT_RUNTIME_DATA_KEY",
    "BotRuntime",
    "BotRuntimeState",
    "EXECUTION_RUNTIME_DATA_KEY",
    "KeyedMailboxRuntime",
    "Jitter",
    "Overloaded",
    "OverloadScope",
    "RuntimeSnapshot",
    "RuntimeState",
    "RuntimeUnavailable",
    "ReplayAwareCooldownGate",
    "ServiceBinding",
    "ShutdownMode",
    "Submission",
    "SystemUtcClock",
    "UtcClock",
    "WorkPriority",
    "WorkTicket",
]
"""@brief 受支持的运行时公共符号 / Supported public runtime symbols."""
