"""@brief Durable Assistant tools 的基础设施 composition / Infrastructure composition for durable Assistant tools."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from fogmoe_bot.application.assistant.agent_loop import AgentLoop
from fogmoe_bot.application.assistant.durable_inference import (
    DurableAssistantInferenceAdapter,
)
from fogmoe_bot.application.assistant.inference.service import AssistantInferenceService
from fogmoe_bot.application.assistant.tool_runtime import AgentRuntime
from fogmoe_bot.application.assistant.tools.catalog import DEFAULT_TOOL_CATALOG
from fogmoe_bot.application.conversation.compaction_worker import (
    ConversationCompactionWorker,
)
from fogmoe_bot.application.conversation.history_projection import (
    ConversationHistoryProjector,
)
from fogmoe_bot.application.conversation.inference_worker import InferenceRuntimeLimits
from fogmoe_bot.domain.assistant.routing.circuit import ProviderCircuit
from fogmoe_bot.domain.conversation.retention import ContextTokenBudget, TokenCount
from fogmoe_bot.infrastructure import config
from fogmoe_bot.infrastructure.blocking import (
    AsyncBlockingBulkhead,
)
from fogmoe_bot.infrastructure.database.assistant_tool_effects import (
    PostgresAssistantToolStore,
)
from fogmoe_bot.infrastructure.database.conversation_retention import (
    PostgresConversationRetention,
)
from fogmoe_bot.infrastructure.database.group_message_projection import (
    PostgresGroupMessageProjection,
)
from fogmoe_bot.infrastructure.database.conversation_workflow.outbox import (
    PostgresOutboxRepository,
)
from fogmoe_bot.infrastructure.llm.assistant_completion import (
    LiteLLMAssistantCompletion,
)
from fogmoe_bot.infrastructure.llm.history_token_counter import (
    ConservativeHistoryTokenCounter,
)
from fogmoe_bot.infrastructure.media.file_artifact_store import FileArtifactStore
from fogmoe_bot.infrastructure.media.file_rate_limiter import FileSlidingWindowLimiter

from .compaction_summary import ProviderCompactionSummaryGenerator
from .external_reads import ExternalReadSettings, RequestsExternalReadTools
from .generated_media import GeneratedMediaSettings, RequestsGeneratedMediaTools
from .routing_config import build_provider_profiles, configured_service_order
from .sticker_catalog import TelegramStickerCatalogReader
from .tool_operations.dispatcher import AssistantToolOperationDispatcher


@dataclass(frozen=True, slots=True)
class DurableAssistantComposition:
    """@brief composition root 返回的共享 resources / Shared resources returned by the composition root.

    @param inference durable inference adapter / Durable inference adapter.
    @param compaction durable conversation compaction worker / Durable conversation-compaction worker.
    @param artifacts outbox delivery 共享 artifact store / Artifact store shared with outbox delivery.
    @param blocking_bulkheads 由顶层运行时关停的阻塞 SDK 隔舱 /
        Blocking SDK bulkheads closed by the top-level runtime.
    """

    inference: DurableAssistantInferenceAdapter
    compaction: ConversationCompactionWorker
    artifacts: FileArtifactStore
    blocking_bulkheads: tuple[AsyncBlockingBulkhead, ...]


def build_durable_assistant(
    *,
    system_prompt: str,
    runtime_limits: InferenceRuntimeLimits,
    retention: PostgresConversationRetention | None = None,
) -> DurableAssistantComposition:
    """@brief 组合 async Agent、receipts、adapters 与 durable inference / Compose the async Agent, receipts, adapters, and durable inference.

    @param system_prompt system prompt / System prompt.
    @param runtime_limits inference worker budgets / Inference-worker budgets.
    @param retention 可替换 retention repository / Replaceable retention repository.
    @return inference 与 artifact store / Inference and artifact store.
    """

    retention_repository = retention or PostgresConversationRetention()
    budget = _retention_budget()
    history = ConversationHistoryProjector(
        persistence=retention_repository,
        token_counter=ConservativeHistoryTokenCounter(guard_ratio=budget.guard_ratio),
        budget=budget,
    )
    artifact_root = config.BASE_DIR / "logs" / "generated_artifacts"
    rate_limit_root = config.BASE_DIR / "logs" / "media_rate_limits"
    artifacts = FileArtifactStore(artifact_root)
    external_settings = ExternalReadSettings(
        serpapi_key=config.SERPAPI_API_KEY or "",
        judge0_url=config.JUDGE0_API_URL,
        judge0_key=config.JUDGE0_API_KEY or "",
    )
    generated_settings = GeneratedMediaSettings(
        image_url=config.IMAGE_GEN_API_URL,
        image_token=config.IMAGE_GEN_API_TOKEN,
        fish_audio_key=config.FISH_AUDIO_API_KEY or "",
        fish_audio_model=config.FISH_AUDIO_MODEL,
        fish_audio_reference_id=config.FISH_AUDIO_REFERENCE_ID,
        image_timeout_seconds=config.IMAGE_GEN_TIMEOUT,
    )
    external_bulkhead = AsyncBlockingBulkhead(
        capacity=4,
        queue_timeout=2.0,
        call_timeout=max(30.0, float(external_settings.timeout_seconds + 5)),
        task_name="assistant-external-read",
    )
    media_bulkhead = AsyncBlockingBulkhead(
        capacity=2,
        queue_timeout=5.0,
        call_timeout=max(90.0, float(generated_settings.image_timeout_seconds + 30)),
        task_name="assistant-generated-media",
    )
    sticker_timeout_seconds = 20
    sticker_bulkhead = AsyncBlockingBulkhead(
        capacity=2,
        queue_timeout=2.0,
        call_timeout=max(60.0, float(sticker_timeout_seconds * 4)),
        task_name="assistant-sticker-catalog",
    )
    provider_bulkhead = AsyncBlockingBulkhead(
        capacity=4,
        queue_timeout=5.0,
        call_timeout=120.0,
        task_name="assistant-provider-completion",
    )
    generated_media = RequestsGeneratedMediaTools(
        settings=generated_settings,
        artifacts=artifacts,
        limiter=FileSlidingWindowLimiter(rate_limit_root),
        bulkhead=media_bulkhead,
    )
    operations = AssistantToolOperationDispatcher(
        help_text=config.HELP_TEXT,
        external_reads=RequestsExternalReadTools(
            external_settings,
            bulkhead=external_bulkhead,
        ),
        generated_media=generated_media,
        stickers=TelegramStickerCatalogReader(
            config_path=config.BASE_DIR / "resources" / "ai_sticker_packs.json",
            bot_token=config.TELEGRAM_BOT_TOKEN or "",
            timeout_seconds=sticker_timeout_seconds,
            bulkhead=sticker_bulkhead,
        ),
        outbox=PostgresOutboxRepository(),
        memory=retention_repository,
        groups=PostgresGroupMessageProjection(),
    )
    store = PostgresAssistantToolStore(operations=operations)
    completion = LiteLLMAssistantCompletion(bulkhead=provider_bulkhead)
    agent = AgentLoop(
        runtime=AgentRuntime(
            catalog=DEFAULT_TOOL_CATALOG,
            persistence=store,
        ),
        completion=completion,
        checkpoints=store,
    )
    circuit = ProviderCircuit(
        failure_threshold=3,
        window_seconds=5 * 60,
        cooldown_seconds=30 * 60,
    )
    service = AssistantInferenceService(
        service_order=configured_service_order(),
        profiles=build_provider_profiles(),
        circuit=circuit,
        text_only_model_patterns=config.AI_CHAT_TEXT_ONLY_MODELS,
        agent_loop=agent,
    )
    translation_service = AssistantInferenceService(
        service_order=configured_service_order("translate"),
        profiles=build_provider_profiles("translate"),
        circuit=circuit,
        text_only_model_patterns=config.AI_CHAT_TEXT_ONLY_MODELS,
        agent_loop=agent,
    )
    compaction = ConversationCompactionWorker(
        persistence=retention_repository,
        generator=ProviderCompactionSummaryGenerator(
            completion=completion,
            service_order=configured_service_order("summary"),
            profiles=build_provider_profiles("summary"),
            request_timeout_seconds=config.COMPACTION_PROVIDER_TIMEOUT_SECONDS,
            budget=budget,
        ),
        worker_count=config.COMPACTION_WORKER_COUNT,
        poll_interval=config.COMPACTION_POLL_INTERVAL,
        attempt_timeout=timedelta(seconds=config.COMPACTION_ATTEMPT_TIMEOUT_SECONDS),
        lease_for=timedelta(seconds=config.COMPACTION_LEASE_SECONDS),
    )
    return DurableAssistantComposition(
        inference=DurableAssistantInferenceAdapter(
            history=history,
            system_prompt=system_prompt,
            runtime_limits=runtime_limits,
            history_reserved_tokens=TokenCount(config.CHAT_RESERVED_TOKENS),
            inference=service,
            translation_inference=translation_service,
        ),
        compaction=compaction,
        artifacts=artifacts,
        blocking_bulkheads=(
            external_bulkhead,
            media_bulkhead,
            sticker_bulkhead,
            provider_bulkhead,
        ),
    )


def _retention_budget() -> ContextTokenBudget:
    """@brief 从显式配置构造严格 token budget / Build a strict token budget from explicit configuration.

    @return validated context token budget / Validated context token budget.
    """

    warning = config.CHAT_TOKEN_WARN_LIMIT
    return ContextTokenBudget(
        warning_tokens=TokenCount(warning),
        hard_tokens=TokenCount(config.CHAT_TOKEN_LIMIT),
        summary_output_tokens=TokenCount(2_500),
        segment_input_tokens=TokenCount(min(64_000, warning)),
    )


__all__ = ["DurableAssistantComposition", "build_durable_assistant"]
