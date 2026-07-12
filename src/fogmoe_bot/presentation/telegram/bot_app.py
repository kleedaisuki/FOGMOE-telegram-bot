"""@brief FogMoe Telegram 进程的唯一组合根与异步生命周期 / Sole composition root and asynchronous lifecycle for the FogMoe Telegram process."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import signal
import time
from datetime import timedelta

import telegram.error
from telegram import Update
from telegram.ext import ApplicationBuilder

from fogmoe_bot.application.accounts.operations import (
    ACCOUNT_SERVICE_DATA_KEY,
    AccountService,
)
from fogmoe_bot.application.admin.models import (
    ADMIN_RUNTIME_DATA_KEY,
    ADMIN_SERVICE_DATA_KEY,
)
from fogmoe_bot.application.admin.runtime import AdminRuntime
from fogmoe_bot.application.admin.service import AdminService
from fogmoe_bot.application.conversation.assistant_ingress import (
    AssistantIngressCoordinator,
)
from fogmoe_bot.application.conversation.inbox_worker import InboxWorker
from fogmoe_bot.application.conversation.inference_worker import (
    InferenceRuntimeLimits,
    InferenceWorker,
)
from fogmoe_bot.application.conversation.outbox_worker import OutboxWorker
from fogmoe_bot.application.conversation.router import IngressRouter
from fogmoe_bot.application.conversation.translation_ingress import (
    TranslationIngressCoordinator,
)
from fogmoe_bot.application.conversation.workflow import (
    ConversationWorkflow,
)
from fogmoe_bot.application.crypto.market_monitor import (
    BTC_MONITOR_DATA_KEY,
    BtcPatternMonitor,
)
from fogmoe_bot.application.crypto.workflow import (
    CRYPTO_SERVICE_DATA_KEY,
    CryptoService,
)
from fogmoe_bot.application.economy.service import (
    ECONOMY_SERVICE_DATA_KEY,
    EconomyService,
)
from fogmoe_bot.application.games.rps_service import RPS_SERVICE_DATA_KEY, RpsService
from fogmoe_bot.application.games.runtime import GAMES_RUNTIME_DATA_KEY, GamesRuntime
from fogmoe_bot.application.moderation.verification_worker import (
    VERIFICATION_WORKER_DATA_KEY,
    VerificationTimeoutWorker,
)
from fogmoe_bot.application.runtime import (
    BOT_RUNTIME_DATA_KEY,
    EXECUTION_RUNTIME_DATA_KEY,
    BotRuntime,
    KeyedMailboxRuntime,
    ReplayAwareCooldownGate,
    ServiceBinding,
    ShutdownMode,
)
from fogmoe_bot.application.scheduling.dispatcher import ScheduleDispatcher
from fogmoe_bot.application.scheduling.prompt_turn import PromptTurnHandler
from fogmoe_bot.application.scheduling.runtime import SchedulingWorkLoop
from fogmoe_bot.application.observability.runtime_metrics import RuntimeMetricsService
from fogmoe_bot.infrastructure import config
from fogmoe_bot.infrastructure.blocking import (
    AsyncBlockingBulkhead,
    BlockingBulkheadLifecycle,
)
from fogmoe_bot.infrastructure.admin.announcements import (
    PostgresAdminAnnouncementOperations,
)
from fogmoe_bot.infrastructure.admin.log_reader import AsyncBoundedLogSource
from fogmoe_bot.infrastructure.admin.stats import PostgresAdminStatsProjection
from fogmoe_bot.infrastructure.assistant.composition import build_durable_assistant
from fogmoe_bot.infrastructure.crypto.binance_monitor import BinanceBtcPatternSource
from fogmoe_bot.infrastructure.database import db
from fogmoe_bot.infrastructure.database.standalone_outbound import (
    PostgresStandaloneOutboundCapability,
)
from fogmoe_bot.infrastructure.database.assistant_turn_acceptance import (
    PostgresAssistantTurnAcceptanceUoW,
)
from fogmoe_bot.infrastructure.database.conversation_reset import (
    PostgresConversationResetUoW,
)
from fogmoe_bot.infrastructure.database.conversation_workflow.inbox import (
    PostgresInboxRepository,
)
from fogmoe_bot.infrastructure.database.conversation_workflow.inference import (
    PostgresInferenceRepository,
)
from fogmoe_bot.infrastructure.database.conversation_workflow.outbox import (
    PostgresOutboxRepository,
)
from fogmoe_bot.infrastructure.database.conversation_workflow.turn import (
    PostgresTurnRepository,
)
from fogmoe_bot.infrastructure.database.repositories.schedule_repository import (
    ScheduleRepository,
)
from fogmoe_bot.infrastructure.database.scheduled_assistant_profile import (
    PostgresScheduledAssistantProfileReader,
)
from fogmoe_bot.infrastructure.observability.logging import current_log_file_path
from fogmoe_bot.infrastructure.observability.composition import ObservabilityAssembly
from fogmoe_bot.infrastructure.telegram.monitor_notification import (
    TelegramMonitorNotificationSink,
)
from fogmoe_bot.infrastructure.telegram.outbox_delivery import (
    TelegramOutboxDeliveryAdapter,
)

from .account_handlers import AccountTelegramCommandHandler
from .admin_handlers import (
    AdminTelegramCommandHandler,
    TelegramAnnouncementOutboundFactory,
)
from .assistant_primary_route import TelegramAssistantPrimaryRoute
from .basic_handlers import StaticTelegramCommandHandler
from .command_cooldown_guard import TelegramCommandCooldownGuard
from .command_route import TelegramDurableCommandPrimaryRoute
from .economy_basic_handlers import EconomyBasicTelegramCommandHandler
from .handler_catalog import (
    HANDLER_CATALOG,
    HandlerKind,
    TelegramApplication,
    install_error_policy,
)
from .handler_composition import (
    HANDLER_BLOCKING_BULKHEADS_DATA_KEY,
    assemble_handler_capabilities,
)
from .catalog_route import (
    TelegramCatalogDispatcher,
    TelegramCatalogPrimaryRoute,
)
from .listener import PollingBackoff, TelegramBotUpdateSource, TelegramPollingListener
from .moderation_composition import (
    MODERATION_CAPABILITY_DATA_KEY,
    TelegramModerationCapability,
)
from .reset_route import TelegramConversationResetPrimaryRoute
from .runtime_settings import (
    TELEGRAM_SETTINGS_DATA_KEY,
    TelegramRuntimeSettings,
    resolve_administrator_contact_name,
)
from .translation_handlers import TranslationTelegramCommandHandler


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _RuntimePrimitives:
    """@brief 跨运行时切片共享的进程级资源 / Process resources shared across runtime slices."""

    execution: KeyedMailboxRuntime
    inbox: PostgresInboxRepository
    turns: PostgresTurnRepository
    inference: PostgresInferenceRepository
    outbox_repository: PostgresOutboxRepository
    outbound: PostgresStandaloneOutboundCapability
    acceptance: PostgresAssistantTurnAcceptanceUoW
    inference_limits: InferenceRuntimeLimits
    admin_service: AdminService
    admin_runtime: AdminRuntime


@dataclass(frozen=True, slots=True)
class _ServiceAssembly:
    """@brief 已排序服务绑定及需发布的能力 / Ordered service bindings and capabilities to publish."""

    bindings: tuple[ServiceBinding, ...]
    btc_monitor: BtcPatternMonitor


def _required_capability[T](
    application: TelegramApplication,
    key: str,
    expected_type: type[T],
) -> T:
    """@brief 从组合根读取并校验一个 runtime capability / Read and validate one runtime capability from the composition root.

    @param application PTB Application / PTB Application.
    @param key ``bot_data`` 稳定键 / Stable ``bot_data`` key.
    @param expected_type capability 具体类型 / Concrete capability type.
    @return 已校验 capability / Validated capability.
    @raise RuntimeError capability 缺失或类型错误 / Missing or incorrectly typed capability.
    """

    value = application.bot_data.get(key)
    if not isinstance(value, expected_type):
        raise RuntimeError(
            f"Application capability {key!r} must be {expected_type.__name__}"
        )
    return value


def _compose_primitives(
    observability: ObservabilityAssembly,
) -> _RuntimePrimitives:
    """@brief 装配事务共享资源和执行器 / Compose transaction-sharing resources and the executor.

    @param observability 进程唯一可观测性装配 / Sole process observability assembly.
    @return 基础运行时资源 / Primitive runtime resources.
    """

    db.configure_observability(observability.telemetry)
    execution = KeyedMailboxRuntime(
        max_concurrency=config.RUNTIME_MAX_CONCURRENCY,
        global_capacity=config.RUNTIME_GLOBAL_CAPACITY,
        per_key_capacity=config.RUNTIME_PER_KEY_CAPACITY,
        idle_ttl=config.RUNTIME_MAILBOX_IDLE_TTL_SECONDS,
    )
    outbox_repository = PostgresOutboxRepository()
    turns = PostgresTurnRepository()
    outbound = PostgresStandaloneOutboundCapability(
        outbox_repository,
        telemetry=observability.telemetry,
    )
    admin_operations = PostgresAdminAnnouncementOperations()
    return _RuntimePrimitives(
        execution=execution,
        inbox=PostgresInboxRepository(),
        turns=turns,
        inference=PostgresInferenceRepository(outbox=outbox_repository),
        outbox_repository=outbox_repository,
        outbound=outbound,
        acceptance=PostgresAssistantTurnAcceptanceUoW(turns),
        inference_limits=InferenceRuntimeLimits(
            provider_timeout=timedelta(
                seconds=config.INFERENCE_PROVIDER_TIMEOUT_SECONDS
            ),
            attempt_timeout=timedelta(seconds=config.INFERENCE_ATTEMPT_TIMEOUT_SECONDS),
            lease_for=timedelta(seconds=config.INFERENCE_LEASE_SECONDS),
        ),
        admin_service=AdminService(
            administrator_id=config.ADMIN_USER_ID,
            stats=PostgresAdminStatsProjection(),
            logs=AsyncBoundedLogSource(current_log_file_path),
            announcements=admin_operations,
        ),
        admin_runtime=AdminRuntime(
            operations=admin_operations,
            outbound=outbound,
            factory=TelegramAnnouncementOutboundFactory(),
        ),
    )


def _compose_ingress(
    application: TelegramApplication,
    primitives: _RuntimePrimitives,
    *,
    bot_user_id: int,
    bot_username: str,
) -> IngressRouter:
    """@brief 装配 Update 的 guard、主路由与观察者 / Compose update guards, primary routes, and observers."""

    assistant_route = TelegramAssistantPrimaryRoute(
        coordinator=AssistantIngressCoordinator(
            acceptance=primitives.acceptance,
            feedback=primitives.outbound,
        ),
        bot_user_id=bot_user_id,
        bot_username=bot_username,
    )
    reset_route = TelegramConversationResetPrimaryRoute(
        persistence=PostgresConversationResetUoW(primitives.outbox_repository),
        bot_username=bot_username,
    )
    moderation = _required_capability(
        application,
        MODERATION_CAPABILITY_DATA_KEY,
        TelegramModerationCapability,
    )
    catalog_dispatcher = TelegramCatalogDispatcher(
        application=application,
        catalog=HANDLER_CATALOG,
    )
    durable_commands = TelegramDurableCommandPrimaryRoute(
        bot_username=bot_username,
        handlers=(
            StaticTelegramCommandHandler(
                outbound=primitives.outbound,
                help_text=config.HELP_TEXT,
            ),
            EconomyBasicTelegramCommandHandler(
                economy=_required_capability(
                    application,
                    ECONOMY_SERVICE_DATA_KEY,
                    EconomyService,
                ),
                outbound=primitives.outbound,
            ),
            AccountTelegramCommandHandler(
                accounts=_required_capability(
                    application,
                    ACCOUNT_SERVICE_DATA_KEY,
                    AccountService,
                ),
                outbound=primitives.outbound,
            ),
            TranslationTelegramCommandHandler(
                TranslationIngressCoordinator(
                    acceptance=primitives.acceptance,
                    feedback=primitives.outbound,
                )
            ),
            AdminTelegramCommandHandler(
                service=primitives.admin_service,
                outbound=primitives.outbound,
            ),
        ),
    )
    owned_commands = {
        definition.filter_namespace
        for definition in HANDLER_CATALOG
        if definition.kind is HandlerKind.COMMAND
    } | {"clear", "fogmoebot", *durable_commands.commands}
    cooldown = TelegramCommandCooldownGuard(
        gate=ReplayAwareCooldownGate(
            cooldown_seconds=1.0,
            max_entries=8192,
            retention_seconds=3600.0,
        ),
        outbound=primitives.outbound,
        bot_username=bot_username,
        commands=owned_commands,
    )
    return IngressRouter(
        runtime=primitives.execution,
        guards=(moderation.guard, cooldown),
        primary_routes=(
            reset_route,
            assistant_route,
            durable_commands,
            TelegramCatalogPrimaryRoute(
                dispatcher=catalog_dispatcher,
                excluded=assistant_route,
                additional_excluded=(reset_route, durable_commands),
            ),
        ),
        observers=(moderation.observer,),
    )


def _compose_services(
    application: TelegramApplication,
    primitives: _RuntimePrimitives,
    ingress: IngressRouter,
    observability: ObservabilityAssembly,
) -> _ServiceAssembly:
    """@brief 装配并按排空阶段排序所有长驻服务 / Compose and drain-order every resident service."""

    listener = TelegramPollingListener(
        source=TelegramBotUpdateSource(application.bot),
        sink=primitives.inbox,
        poll_timeout=float(config.TELEGRAM_GET_UPDATES_TIMEOUT),
        allowed_updates=Update.ALL_TYPES,
        backoff=PollingBackoff(
            initial_delay=config.TELEGRAM_POLLING_RETRY_INITIAL_DELAY,
            max_delay=config.TELEGRAM_POLLING_RETRY_MAX_DELAY,
        ),
    )
    inbox = InboxWorker(
        repository=primitives.inbox,
        router=ingress,
        worker_count=config.INBOX_WORKER_COUNT,
        poll_interval=config.INBOX_POLL_INTERVAL,
        lease_for=timedelta(seconds=config.INBOX_LEASE_SECONDS),
        telemetry=observability.telemetry,
    )
    assistant = build_durable_assistant(
        system_prompt=config.SYSTEM_PROMPT,
        runtime_limits=primitives.inference_limits,
        telemetry=observability.telemetry,
    )
    inference = InferenceWorker(
        repository=primitives.inference,
        inference=assistant.inference,
        worker_count=config.INFERENCE_WORKER_COUNT,
        poll_interval=config.INFERENCE_POLL_INTERVAL,
        runtime_limits=primitives.inference_limits,
        telemetry=observability.telemetry,
    )
    outbox = OutboxWorker(
        repository=primitives.outbox_repository,
        delivery=TelegramOutboxDeliveryAdapter(
            application.bot,
            artifacts=assistant.artifacts,
        ),
        worker_count=config.OUTBOX_WORKER_COUNT,
        poll_interval=config.OUTBOX_POLL_INTERVAL,
        lease_for=timedelta(seconds=config.OUTBOX_LEASE_SECONDS),
        attempt_timeout=timedelta(seconds=config.OUTBOX_ATTEMPT_TIMEOUT_SECONDS),
        telemetry=observability.telemetry,
    )
    scheduling = SchedulingWorkLoop(
        dispatcher=ScheduleDispatcher(
            repository=ScheduleRepository(),
            handlers=(
                PromptTurnHandler(
                    workflow=ConversationWorkflow(primitives.turns),
                    profiles=PostgresScheduledAssistantProfileReader(),
                ),
            ),
            batch_size=config.SCHEDULING_WORKER_COUNT,
            stale_after=timedelta(seconds=config.SCHEDULING_LEASE_SECONDS),
        ),
        maintenance=(),
        poll_interval=config.SCHEDULING_POLL_INTERVAL,
        worker_count=config.SCHEDULING_WORKER_COUNT,
    )
    btc_bulkhead = AsyncBlockingBulkhead(
        capacity=4,
        queue_timeout=2.0,
        call_timeout=15.0,
        task_name="binance-btc-pattern",
    )
    btc_monitor = BtcPatternMonitor(
        source=BinanceBtcPatternSource(bulkhead=btc_bulkhead),
        notifications=TelegramMonitorNotificationSink(application.bot),
    )
    handler_bulkheads = _required_blocking_bulkheads(application)
    blocking = BlockingBulkheadLifecycle(
        (*assistant.blocking_bulkheads, *handler_bulkheads, btc_bulkhead)
    )
    bindings = (
        ServiceBinding("telegram-listener", listener, shutdown_phase=0),
        ServiceBinding("inbox", inbox, shutdown_phase=10),
        ServiceBinding("scheduling", scheduling, shutdown_phase=10),
        ServiceBinding("assistant-inference", inference, shutdown_phase=20),
        ServiceBinding(
            "conversation-compaction",
            assistant.compaction,
            shutdown_phase=20,
        ),
        ServiceBinding(
            "rps",
            _required_capability(application, RPS_SERVICE_DATA_KEY, RpsService),
            shutdown_phase=20,
        ),
        ServiceBinding(
            "crypto-prediction",
            _required_capability(
                application,
                CRYPTO_SERVICE_DATA_KEY,
                CryptoService,
            ),
            shutdown_phase=20,
        ),
        ServiceBinding(
            "games",
            _required_capability(
                application,
                GAMES_RUNTIME_DATA_KEY,
                GamesRuntime,
            ),
            shutdown_phase=20,
        ),
        ServiceBinding(
            "verification",
            _required_capability(
                application,
                VERIFICATION_WORKER_DATA_KEY,
                VerificationTimeoutWorker,
            ),
            shutdown_phase=20,
        ),
        ServiceBinding(
            "admin-announcements",
            primitives.admin_runtime,
            shutdown_phase=20,
        ),
        ServiceBinding("btc-monitor", btc_monitor, shutdown_phase=20),
        ServiceBinding(
            "assistant-blocking-calls",
            blocking,
            shutdown_phase=25,
        ),
        ServiceBinding("outbox", outbox, shutdown_phase=30),
        ServiceBinding(
            "runtime-metrics",
            RuntimeMetricsService(
                telemetry=observability.telemetry,
                exporter=observability.runtime,
                execution=primitives.execution,
                interval=config.OBSERVABILITY_METRIC_INTERVAL_SECONDS,
            ),
            shutdown_phase=90,
        ),
        ServiceBinding(
            "telemetry-export",
            observability.runtime,
            shutdown_phase=100,
        ),
    )
    return _ServiceAssembly(bindings, btc_monitor)


def _required_blocking_bulkheads(
    application: TelegramApplication,
) -> tuple[AsyncBlockingBulkhead, ...]:
    value = application.bot_data.get(HANDLER_BLOCKING_BULKHEADS_DATA_KEY)
    if not isinstance(value, tuple) or not value:
        raise RuntimeError("Handler blocking bulkheads are not configured")
    if not all(isinstance(item, AsyncBlockingBulkhead) for item in value):
        raise RuntimeError("Handler blocking bulkhead configuration is invalid")
    return value


def compose_bot_runtime(
    application: TelegramApplication,
    observability: ObservabilityAssembly,
) -> BotRuntime:
    """@brief 装配 Listener、Conversation workers 与所有长驻服务 / Compose the Listener, Conversation workers, and every long-running service.

    @param application 已初始化且运行中的 PTB Application / Initialized and running PTB Application.
    @param observability 进程唯一可观测性装配 / Sole process observability assembly.
    @return 单一顶层 BotRuntime / Sole top-level BotRuntime.
    @raise RuntimeError Bot identity/capability 缺失或重复装配 / Missing identity/capability or duplicate composition.
    @note 所有服务共享一个 event loop、Bot 与数据库引擎；只有组合根可以依赖所有层。/
        Every service shares one event loop, Bot, and database engine; only the composition root
        may depend on every layer.
    """

    if BOT_RUNTIME_DATA_KEY in application.bot_data:
        raise RuntimeError("BotRuntime was composed more than once")

    bot_username = application.bot.username
    if not bot_username:
        raise RuntimeError("Initialized Telegram Bot requires a username")
    bot_user_id = application.bot.id

    primitives = _compose_primitives(observability)
    ingress = _compose_ingress(
        application,
        primitives,
        bot_user_id=bot_user_id,
        bot_username=bot_username,
    )
    services = _compose_services(application, primitives, ingress, observability)
    runtime = BotRuntime(
        execution_runtime=primitives.execution,
        services=services.bindings,
    )
    application.bot_data[BOT_RUNTIME_DATA_KEY] = runtime
    application.bot_data[EXECUTION_RUNTIME_DATA_KEY] = primitives.execution
    application.bot_data[BTC_MONITOR_DATA_KEY] = services.btc_monitor
    application.bot_data[ADMIN_SERVICE_DATA_KEY] = primitives.admin_service
    application.bot_data[ADMIN_RUNTIME_DATA_KEY] = primitives.admin_runtime
    return runtime


def create_application() -> TelegramApplication:
    """@brief 创建无 PTB Updater/JobQueue 的 handler capability 容器 / Create the handler-capability container without a PTB Updater or JobQueue.

    @return 已装配 capability 与 error policy 的 PTB Application /
        PTB Application with capabilities and error policy assembled.
    @raise ValueError Telegram token 缺失 / Missing Telegram token.
    @note Update 只能由 ``TelegramPollingListener`` 获取；``Application`` 不拥有第二个
        poller。/ Updates are fetched only by ``TelegramPollingListener``; ``Application`` owns
        no second poller.
    """

    token = config.TELEGRAM_BOT_TOKEN
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is required")
    builder = (
        ApplicationBuilder()
        .token(token)
        .connect_timeout(config.TELEGRAM_CONNECT_TIMEOUT)
        .read_timeout(config.TELEGRAM_READ_TIMEOUT)
        .write_timeout(config.TELEGRAM_WRITE_TIMEOUT)
        .pool_timeout(config.TELEGRAM_POOL_TIMEOUT)
        .get_updates_connect_timeout(config.TELEGRAM_GET_UPDATES_CONNECT_TIMEOUT)
        .get_updates_read_timeout(config.TELEGRAM_GET_UPDATES_READ_TIMEOUT)
        .get_updates_write_timeout(config.TELEGRAM_GET_UPDATES_WRITE_TIMEOUT)
        .get_updates_pool_timeout(config.TELEGRAM_GET_UPDATES_POOL_TIMEOUT)
        .get_updates_connection_pool_size(
            config.TELEGRAM_GET_UPDATES_CONNECTION_POOL_SIZE
        )
        .job_queue(None)
        .updater(None)
    )
    proxy_url = config.NETWORK_PROXY_URL
    if proxy_url:
        builder = builder.proxy(proxy_url).get_updates_proxy(proxy_url)
    application = builder.build()
    assemble_handler_capabilities(application)
    install_error_policy(application)
    return application


async def _resolve_administrator_contact(
    application: TelegramApplication,
) -> None:
    """@brief 启动时解析全局管理员展示名 / Resolve the global administrator display name at startup.

    @param application 已初始化的 Telegram Application / Initialized Telegram Application.
    @return None / None.
    @note Telegram API 暂时不可用时保留 ``ADMIN_CONTACT_NAME`` 回退值且不阻止启动 /
        Keep the ``ADMIN_CONTACT_NAME`` fallback and do not block startup when the Telegram API is unavailable.
    """

    settings = _required_capability(
        application,
        TELEGRAM_SETTINGS_DATA_KEY,
        TelegramRuntimeSettings,
    )
    try:
        resolved = await resolve_administrator_contact_name(application.bot, settings)
    except telegram.error.TelegramError as error:
        logger.warning(
            "Unable to resolve administrator contact for user_id=%s; using configured fallback",
            settings.administrator_id,
            exc_info=error,
        )
        return
    application.bot_data[TELEGRAM_SETTINGS_DATA_KEY] = resolved


async def _wait_for_stop_or_runtime_failure(
    runtime: BotRuntime,
    stop_event: asyncio.Event,
) -> None:
    """@brief 等待 OS 停止信号或后台服务 fail-fast / Wait for an OS stop signal or background-service fail-fast.

    @param runtime 已启动 BotRuntime / Started BotRuntime.
    @param stop_event 进程停止事件 / Process-stop event.
    @return None / None.
    @raise RuntimeError 任一后台服务提前终结 / Any background service terminates early.
    """

    stop_waiter = asyncio.create_task(stop_event.wait(), name="application-stop-signal")
    runtime_waiter = asyncio.create_task(
        runtime.wait_terminated(),
        name="application-runtime-termination",
    )
    try:
        done, _ = await asyncio.wait(
            (stop_waiter, runtime_waiter),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if runtime_waiter in done:
            await runtime_waiter
            failure = runtime.failure
            if failure is not None:
                raise RuntimeError("BotRuntime background service failed") from failure
            raise RuntimeError("BotRuntime terminated before a stop request")
    finally:
        for waiter in (stop_waiter, runtime_waiter):
            if not waiter.done():
                waiter.cancel()
        await asyncio.gather(stop_waiter, runtime_waiter, return_exceptions=True)


async def _shutdown_runtime(runtime: BotRuntime) -> None:
    """@brief 在 grace 期内分阶段排空，超时后强制取消 / Drain in phases within the grace period, then cancel on timeout.

    @param runtime 待停止运行时 / Runtime to stop.
    @return None / None.
    """

    try:
        async with asyncio.timeout(config.RUNTIME_SHUTDOWN_GRACE_SECONDS):
            await runtime.shutdown(ShutdownMode.DRAIN)
    except TimeoutError:
        logger.warning("BotRuntime drain timed out; forcing cancellation")
        await runtime.shutdown(ShutdownMode.CANCEL)


async def serve_application(
    application: TelegramApplication,
    stop_event: asyncio.Event,
    observability: ObservabilityAssembly,
) -> None:
    """@brief 在一个 event loop 中运行并完整清理 Application / Run and fully clean up the Application on one event loop.

    @param application 待运行 PTB Application / PTB Application to run.
    @param stop_event 外部停止事件 / External stop event.
    @param observability 进程唯一可观测性装配 / Sole process observability assembly.
    @return None / None.
    @raise BaseExceptionGroup 主流程与清理同时失败 / Main execution and cleanup both fail.
    @note 关停顺序是 Listener → Inbox/Scheduling → Inference/feature workers
        → blocking SDK calls → Outbox → keyed executor → PTB Bot → DB engine。/
        Shutdown order is Listener → Inbox/Scheduling → Inference/feature workers →
        blocking SDK calls → Outbox → keyed executor → PTB Bot → DB engine.
    """

    initialized = False
    started = False
    runtime: BotRuntime | None = None
    primary_failure: BaseException | None = None
    cleanup_failures: list[BaseException] = []
    try:
        await application.initialize()
        initialized = True
        await _resolve_administrator_contact(application)
        await application.start()
        started = True
        runtime = compose_bot_runtime(application, observability)
        await runtime.start()
        await _wait_for_stop_or_runtime_failure(runtime, stop_event)
    except BaseException as error:
        primary_failure = error

    if runtime is not None:
        try:
            await _shutdown_runtime(runtime)
        except BaseException as error:
            cleanup_failures.append(error)
    if started:
        try:
            await application.stop()
        except BaseException as error:
            cleanup_failures.append(error)
    if initialized:
        try:
            await application.shutdown()
        except BaseException as error:
            cleanup_failures.append(error)
    try:
        await db.dispose_current_engine()
    except BaseException as error:
        cleanup_failures.append(error)

    if primary_failure is not None and cleanup_failures:
        raise BaseExceptionGroup(
            "Telegram application execution and cleanup failed",
            [primary_failure, *cleanup_failures],
        )
    if primary_failure is not None:
        raise primary_failure
    if cleanup_failures:
        raise BaseExceptionGroup(
            "Telegram application cleanup failed",
            cleanup_failures,
        )


def _install_signal_handlers(stop_event: asyncio.Event) -> tuple[signal.Signals, ...]:
    """@brief 将进程信号映射到异步停止事件 / Map process signals to the asynchronous stop event.

    @param stop_event 接收信号的事件 / Event receiving stop signals.
    @return 成功注册的 signals / Successfully registered signals.
    """

    loop = asyncio.get_running_loop()
    registered: list[signal.Signals] = []
    for process_signal in (signal.SIGINT, signal.SIGTERM, signal.SIGABRT):
        try:
            loop.add_signal_handler(process_signal, stop_event.set)
        except NotImplementedError, RuntimeError:
            logger.warning(
                "Event loop cannot install signal handler %s", process_signal
            )
            continue
        registered.append(process_signal)
    return tuple(registered)


async def _run_application(
    application: TelegramApplication,
    observability: ObservabilityAssembly,
) -> None:
    """@brief 安装信号并运行一个 Application 实例 / Install signals and run one Application instance.

    @param application 待运行实例 / Instance to run.
    @param observability 进程唯一可观测性装配 / Sole process observability assembly.
    @return None / None.
    """

    stop_event = asyncio.Event()
    registered = _install_signal_handlers(stop_event)
    try:
        await serve_application(application, stop_event, observability)
    finally:
        loop = asyncio.get_running_loop()
        for process_signal in registered:
            loop.remove_signal_handler(process_signal)


def _is_recoverable_bootstrap_error(error: BaseException) -> bool:
    """@brief 判断 Application bootstrap 是否可通过重建恢复 / Decide whether rebuilding can recover an Application bootstrap error.

    @param error bootstrap 异常 / Bootstrap error.
    @return 瞬态 Telegram 网络错误为 True / True for transient Telegram network errors.
    """

    return isinstance(
        error,
        (
            telegram.error.NetworkError,
            telegram.error.TimedOut,
            telegram.error.RetryAfter,
        ),
    )


def _bootstrap_retry_delay(attempt: int) -> float:
    """@brief 计算 bootstrap capped exponential backoff / Calculate capped exponential backoff for bootstrap.

    @param attempt 从 1 开始的连续失败次数 / Consecutive failure count starting at one.
    @return 延迟秒数 / Delay in seconds.
    """

    initial_delay = max(0.0, float(config.TELEGRAM_POLLING_RETRY_INITIAL_DELAY))
    max_delay = max(initial_delay, float(config.TELEGRAM_POLLING_RETRY_MAX_DELAY))
    return float(min(max_delay, initial_delay * (2 ** max(0, attempt - 1))))


def run(observability: ObservabilityAssembly) -> None:
    """@brief 运行唯一自控 long-poll listener，并恢复瞬态 bootstrap 失败 / Run the sole self-controlled long-poll listener and recover transient bootstrap failures.

    @param observability 进程唯一可观测性装配 / Sole process observability assembly.
    @return None / None.
    """

    attempt = 0
    while True:
        application = create_application()
        try:
            asyncio.run(_run_application(application, observability))
            return
        except KeyboardInterrupt:
            logger.info("Bot shutdown requested by keyboard interrupt")
            return
        except Exception as error:
            if not _is_recoverable_bootstrap_error(error):
                raise
            attempt += 1
            delay = _bootstrap_retry_delay(attempt)
            logger.warning(
                "Telegram bootstrap failed transiently; rebuilding in %.1fs "
                "(attempt %s): %s",
                delay,
                attempt,
                error,
                exc_info=True,
            )
            time.sleep(delay)


__all__ = [
    "compose_bot_runtime",
    "create_application",
    "run",
    "serve_application",
]
