"""@brief Telegram 组合根与自控 listener 生命周期测试 / Tests for the Telegram composition root and self-controlled listener lifecycle."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from telegram import User
from telegram.ext import ApplicationBuilder

from fogmoe_bot.config import BotSettings
from fogmoe_bot.application.runtime import (
    BOT_RUNTIME_DATA_KEY,
    EXECUTION_RUNTIME_DATA_KEY,
    BotRuntime,
    KeyedMailboxRuntime,
    ServiceBinding,
)
from fogmoe_bot.presentation.telegram import bot_app
from fogmoe_bot.presentation.telegram.handler_catalog import install_error_policy
from fogmoe_bot.presentation.telegram.handler_composition import (
    assemble_handler_capabilities,
)
from fogmoe_bot.resources import BotResources, load_resources
from observability_testkit import make_observability


def _settings() -> BotSettings:
    """@brief 构造最小可装配 Bot 设置 / Build the minimum Bot settings needed for composition.

    @return 含测试 token 与 embedding 凭据的设置 / Settings containing test token and embedding credential.
    """

    return BotSettings.model_validate(
        {
            "telegram": {"bot_token": "123456:ABCDEF_test_token"},
            "assistant": {"retrieval": {"embedding": {"api_key": "test-key"}}},
        }
    )


def _resources(tmp_path: Path) -> BotResources:
    """@brief 加载测试用只读资源 / Load read-only resources for tests.

    @param tmp_path pytest 隔离目录 / Pytest isolated directory.
    @return 绑定隔离日志目录的资源 / Resources bound to an isolated log directory.
    """

    return load_resources(log_directory=tmp_path / "logs")


def _offline_application(tmp_path: Path):
    """@brief 创建带离线 Bot identity 的 Application / Build an Application with an offline Bot identity.

    @return 无 Updater/JobQueue 的 Application / Application without an Updater or JobQueue.
    """

    application = (
        ApplicationBuilder()
        .token("123456:ABCDEF_test_token")
        .job_queue(None)
        .updater(None)
        .build()
    )
    object.__setattr__(
        application.bot,
        "_bot_user",
        User(id=999, first_name="Fog", is_bot=True, username="FogMoeBot"),
    )
    assemble_handler_capabilities(
        application,
        telemetry=make_observability().telemetry,
        settings=_settings(),
        resources=_resources(tmp_path),
    )
    install_error_policy(application)
    return application


def test_composition_has_one_listener_and_complete_phased_runtime(
    tmp_path: Path,
) -> None:
    """@brief 组合根只拥有一个 listener 并声明完整阶段 / The composition root owns one listener and declares every phase."""

    application = _offline_application(tmp_path)

    observability = make_observability()
    runtime = bot_app.compose_bot_runtime(
        application,
        observability,
        settings=_settings(),
        resources=_resources(tmp_path),
    )

    assert application.updater is None
    assert application.handlers == {}
    assert runtime.service_names == (
        "telegram-listener",
        "inbox",
        "scheduling",
        "assistant-inference",
        "conversation-compaction",
        "episodic-retrieval",
        "user-profile-dreaming",
        "verification",
        "admin-announcements",
        "btc-monitor",
        "assistant-blocking-calls",
        "embedding-http-client",
        "outbox",
        "runtime-metrics",
        "telemetry-export",
    )
    assert application.bot_data[BOT_RUNTIME_DATA_KEY] is runtime
    assert application.bot_data[EXECUTION_RUNTIME_DATA_KEY] is runtime.execution_runtime
    with pytest.raises(RuntimeError, match="more than once"):
        bot_app.compose_bot_runtime(
            application,
            observability,
            settings=_settings(),
            resources=_resources(tmp_path),
        )


class _LifecycleApplication:
    """@brief 记录 PTB 生命周期顺序的替身 / Double recording PTB lifecycle ordering."""

    def __init__(self, events: list[str]) -> None:
        """@brief 创建记录器 / Create the recorder.

        @param events 共享顺序记录 / Shared ordering log.
        """

        self.events = events

    async def initialize(self) -> None:
        """@brief 记录初始化 / Record initialization."""

        self.events.append("application.initialize")

    async def start(self) -> None:
        """@brief 记录启动 / Record startup."""

        self.events.append("application.start")

    async def stop(self) -> None:
        """@brief 记录停止 / Record stopping."""

        self.events.append("application.stop")

    async def shutdown(self) -> None:
        """@brief 记录释放 / Record shutdown."""

        self.events.append("application.shutdown")


class _LifecycleService:
    """@brief 记录 BotRuntime service drain 的替身 / Double recording BotRuntime service drain."""

    def __init__(self, events: list[str]) -> None:
        """@brief 创建记录器 / Create the recorder.

        @param events 共享顺序记录 / Shared ordering log.
        """

        self.events = events

    async def run(self, stop_event: asyncio.Event) -> None:
        """@brief 等待并记录 drain / Wait for and record drain.

        @param stop_event runtime 停止事件 / Runtime stop event.
        @return None / None.
        """

        self.events.append("runtime.start")
        await stop_event.wait()
        self.events.append("runtime.stop")


def test_serve_application_drains_runtime_before_bot_and_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """@brief runtime 在 Bot 与数据库之前排空 / Runtime drains before the Bot and database are released."""

    async def scenario() -> None:
        """@brief 执行完整生命周期 / Execute the complete lifecycle.

        @return None / None.
        """

        events: list[str] = []
        application = _LifecycleApplication(events)
        runtime = BotRuntime(
            execution_runtime=KeyedMailboxRuntime(
                max_concurrency=1,
                global_capacity=2,
                per_key_capacity=1,
            ),
            services=(ServiceBinding("test", _LifecycleService(events)),),
        )

        async def dispose() -> None:
            """@brief 记录数据库释放 / Record database disposal."""

            events.append("database.dispose")

        monkeypatch.setattr(bot_app.db, "dispose_current_engine", dispose)
        monkeypatch.setattr(
            bot_app,
            "assemble_handler_capabilities",
            lambda application, *, telemetry, settings, resources: None,
        )

        async def skip_contact(application: object) -> None:
            """@brief 跳过无 capability fake 的管理员解析 / Skip admin resolution for a capability-free fake."""

        monkeypatch.setattr(bot_app, "_resolve_administrator_contact", skip_contact)
        monkeypatch.setattr(
            bot_app,
            "compose_bot_runtime",
            lambda value, observability, *, settings, resources: runtime,
        )
        stop_event = asyncio.Event()
        stop_event.set()

        await bot_app.serve_application(  # type: ignore[arg-type]
            application,
            stop_event,
            make_observability(),
            settings=_settings(),
            resources=_resources(tmp_path),
        )

        assert events == [
            "application.initialize",
            "application.start",
            "runtime.start",
            "runtime.stop",
            "application.stop",
            "application.shutdown",
            "database.dispose",
        ]

    asyncio.run(scenario())
