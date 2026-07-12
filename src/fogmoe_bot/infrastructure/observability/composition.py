"""@brief 进程级可观测性装配 / Process-level observability composition."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from fogmoe_bot.application.observability.telemetry import (
    Telemetry,
    TelemetryBuffer,
    TelemetryRuntime,
)
from fogmoe_bot.domain.observability.signals import Resource, freeze_attributes
from fogmoe_bot.infrastructure import config

from .postgres import DiscardTelemetrySink, PostgresTelemetrySink


@dataclass(frozen=True, slots=True)
class ObservabilityAssembly:
    """@brief 入口与 BotRuntime 共享的可观测性资源 / Observability resources shared by the entry point and BotRuntime.

    @param telemetry 同步非阻塞记录 API / Synchronous non-blocking recording API.
    @param runtime PostgreSQL 导出生命周期 / PostgreSQL export lifecycle.
    @param resource 当前进程资源 / Current process resource.
    """

    telemetry: Telemetry
    runtime: TelemetryRuntime
    resource: Resource


def build_observability() -> ObservabilityAssembly:
    """@brief 自顶向下装配进程可观测性 / Compose process observability top-down.

    @return 单一共享装配 / Sole shared assembly.
    """

    instance_id = str(uuid4())
    resource = Resource(
        resource_id=uuid4(),
        service_name="fogmoe-telegram-bot",
        service_version="0.1.0",
        deployment_environment=config.OBSERVABILITY_ENVIRONMENT,
        service_instance_id=instance_id,
        started_at=datetime.now(UTC),
        attributes=freeze_attributes(
            {
                "host.name": socket.gethostname(),
                "process.pid": os.getpid(),
                "telemetry.sdk.name": "fogmoe-native",
                "telemetry.sdk.language": "python",
            }
        ),
    )
    buffer = TelemetryBuffer(config.OBSERVABILITY_QUEUE_CAPACITY)
    telemetry = Telemetry(buffer)
    sink = (
        PostgresTelemetrySink(
            dsn=config.asyncpg_database_uri(),
            resource=resource,
            command_timeout=config.OBSERVABILITY_DB_COMMAND_TIMEOUT_SECONDS,
            retention_days=config.OBSERVABILITY_RETENTION_DAYS,
        )
        if config.OBSERVABILITY_ENABLED
        else DiscardTelemetrySink()
    )
    runtime = TelemetryRuntime(
        buffer=buffer,
        sink=sink,
        batch_size=config.OBSERVABILITY_BATCH_SIZE,
        flush_interval=config.OBSERVABILITY_FLUSH_INTERVAL_SECONDS,
        retry_max_delay=config.OBSERVABILITY_RETRY_MAX_DELAY_SECONDS,
        shutdown_flush_timeout=config.OBSERVABILITY_SHUTDOWN_FLUSH_TIMEOUT_SECONDS,
    )
    return ObservabilityAssembly(telemetry, runtime, resource)


__all__ = ["ObservabilityAssembly", "build_observability"]
