"""@brief 应用执行运行时公共接口 / Public application execution-runtime API."""

from fogmoe_bot.application.runtime.adaptive_polling import (
    AdaptivePolling,
    AdaptivePollingPolicy,
    LeaseRecoveryCadence,
)
from fogmoe_bot.application.runtime.bot_runtime import (
    BOT_RUNTIME_DATA_KEY,
    BackgroundService,
    BotRuntime,
    BotRuntimeState,
    ServiceBinding,
)
from fogmoe_bot.application.runtime.clock import Jitter, SystemUtcClock, UtcClock
from fogmoe_bot.application.runtime.failure_circuit import (
    CircuitPermit,
    FailureCircuit,
    FailureCircuitPolicy,
)
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
from fogmoe_bot.application.runtime.rate_limit import ReplayAwareCooldownGate

EXECUTION_RUNTIME_DATA_KEY = "fogmoe.execution_runtime"
"""@brief 组合根保存执行运行时的稳定键 / Stable composition-root key for the execution runtime."""

__all__ = [
    "Accepted",
    "AdaptivePolling",
    "AdaptivePollingPolicy",
    "AggregateIdentityPart",
    "AggregateKey",
    "AsyncOperation",
    "BackgroundService",
    "BOT_RUNTIME_DATA_KEY",
    "BotRuntime",
    "BotRuntimeState",
    "CircuitPermit",
    "EXECUTION_RUNTIME_DATA_KEY",
    "FailureCircuit",
    "FailureCircuitPolicy",
    "KeyedMailboxRuntime",
    "Jitter",
    "LeaseRecoveryCadence",
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
