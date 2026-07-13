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
from fogmoe_bot.config import BotDatabaseSettings, ObservabilitySettings
from fogmoe_bot.domain.observability.signals import Resource, freeze_attributes

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


def build_observability(
    *,
    settings: ObservabilitySettings,
    database: BotDatabaseSettings,
) -> ObservabilityAssembly:
    """@brief 自顶向下装配进程可观测性 / Compose process observability top-down.

    @param settings 已验证的遥测设置 / Validated telemetry settings.
    @param database 已验证的 Bot 数据库设置 / Validated Bot database settings.
    @return 单一共享装配 / Sole shared assembly.
    """

    instance_id = str(uuid4())
    resource = Resource(
        resource_id=uuid4(),
        service_name="fogmoe-telegram-bot",
        service_version="0.1.0",
        deployment_environment=settings.environment,
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
    buffer = TelemetryBuffer(settings.queue_capacity)
    telemetry = Telemetry(buffer)
    sink = (
        PostgresTelemetrySink(
            dsn=database.asyncpg_url(),
            resource=resource,
            command_timeout=settings.database_command_timeout_seconds,
            retention_days=settings.retention_days,
        )
        if settings.enabled
        else DiscardTelemetrySink()
    )
    runtime = TelemetryRuntime(
        buffer=buffer,
        sink=sink,
        batch_size=settings.batch_size,
        flush_interval=settings.flush_interval_seconds,
        retry_max_delay=settings.retry_max_delay_seconds,
        shutdown_flush_timeout=settings.shutdown_flush_timeout_seconds,
    )
    return ObservabilityAssembly(telemetry, runtime, resource)


__all__ = ["ObservabilityAssembly", "build_observability"]
