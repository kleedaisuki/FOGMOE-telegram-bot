"""@brief 测试用内存可观测性装配 / In-memory observability assembly for tests."""

from datetime import UTC, datetime
from uuid import uuid4

from fogmoe_bot.application.observability.telemetry import (
    Telemetry,
    TelemetryBuffer,
    TelemetryRuntime,
)
from fogmoe_bot.domain.observability.signals import Resource, freeze_attributes
from fogmoe_bot.infrastructure.observability.composition import ObservabilityAssembly
from fogmoe_bot.infrastructure.observability.postgres import DiscardTelemetrySink


def make_telemetry(*, capacity: int = 4096) -> Telemetry:
    """@brief 创建不连接外部系统的 recorder / Create a recorder with no external connection.

    @param capacity 测试缓冲容量 / Test buffer capacity.
    @return typed telemetry / Typed telemetry.
    """

    return Telemetry(TelemetryBuffer(capacity))


def make_observability() -> ObservabilityAssembly:
    """@brief 创建可完整启停的丢弃式装配 / Create a fully lifecycle-capable discarding assembly.

    @return 测试装配 / Test assembly.
    """

    resource = Resource(
        resource_id=uuid4(),
        service_name="fogmoe-test",
        service_version="test",
        deployment_environment="test",
        service_instance_id=str(uuid4()),
        started_at=datetime.now(UTC),
        attributes=freeze_attributes(),
    )
    buffer = TelemetryBuffer(4096)
    telemetry = Telemetry(buffer)
    runtime = TelemetryRuntime(
        buffer=buffer,
        sink=DiscardTelemetrySink(),
        batch_size=256,
        flush_interval=0.01,
        retry_max_delay=0.1,
        shutdown_flush_timeout=0.1,
    )
    return ObservabilityAssembly(telemetry, runtime, resource)


__all__ = ["make_observability", "make_telemetry"]
