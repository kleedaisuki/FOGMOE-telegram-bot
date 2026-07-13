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
from fogmoe_bot.application.memory.service import RetrievalWorkingMemory
from fogmoe_bot.application.retrieval import (
    EPISODIC_CORPUS_ID,
    EpisodicPassageRenderer,
    RetrievalWorker,
    SemanticRecall,
)
from fogmoe_bot.application.user_profile.worker import DreamingWorker
from fogmoe_bot.application.context_window.worker import (
    CompactionWorker,
)
from fogmoe_bot.application.context_window.projection import (
    ContextWindowProjector,
)
from fogmoe_bot.application.context_window.cache import ContextWindowCache
from fogmoe_bot.application.conversation.inference_worker import InferenceRuntimeLimits
from fogmoe_bot.domain.assistant.routing.circuit import ProviderCircuit
from fogmoe_bot.domain.context_window.budget import ContextTokenBudget, TokenCount
from fogmoe_bot.domain.retrieval import EmbeddingSpace
from fogmoe_bot.infrastructure import config
from fogmoe_bot.infrastructure.blocking import (
    AsyncBlockingBulkhead,
)
from fogmoe_bot.infrastructure.database.assistant_tool_effects import (
    PostgresAssistantToolStore,
)
from fogmoe_bot.infrastructure.database.context_window import (
    PostgresContextWindowStore,
)
from fogmoe_bot.infrastructure.database.retrieval import (
    PostgresEpisodicSource,
    PostgresRetrievalStore,
)
from fogmoe_bot.infrastructure.database.user_profile.source import (
    PostgresProfileEvidenceSource,
)
from fogmoe_bot.infrastructure.database.user_profile.store import PostgresUserProfileStore
from fogmoe_bot.infrastructure.database.group_message_projection import (
    PostgresGroupMessageProjection,
)
from fogmoe_bot.infrastructure.database.conversation_workflow.outbox import (
    PostgresOutboxRepository,
)
from fogmoe_bot.infrastructure.llm.assistant_completion import (
    LiteLLMAssistantCompletion,
)
from fogmoe_bot.infrastructure.context_window.token_counter import (
    ConservativeHistoryTokenCounter,
)
from fogmoe_bot.infrastructure.media.file_artifact_store import FileArtifactStore
from fogmoe_bot.infrastructure.media.file_rate_limiter import FileSlidingWindowLimiter
from fogmoe_bot.infrastructure.retrieval import OpenAICompatibleEmbeddings
from fogmoe_bot.infrastructure.user_profile.dreaming_model import ProviderDreamingModel

from fogmoe_bot.infrastructure.context_window.summary import (
    ProviderCompactionSummaryGenerator,
)
from .external_reads import ExternalReadSettings, RequestsExternalReadTools
from .generated_media import GeneratedMediaSettings, RequestsGeneratedMediaTools
from .routing_config import build_provider_profiles, configured_service_order
from .sticker_catalog import TelegramStickerCatalogReader
from .tool_operations.dispatcher import AssistantToolOperationDispatcher
from fogmoe_bot.application.observability.telemetry import Telemetry


@dataclass(frozen=True, slots=True)
class DurableAssistantComposition:
    """@brief composition root 返回的共享 resources / Shared resources returned by the composition root.

    @param inference durable inference adapter / Durable inference adapter.
    @param compaction durable conversation compaction worker / Durable conversation-compaction worker.
    @param retrieval durable episodic-retrieval worker / Durable episodic-retrieval worker.
    @param dreaming durable User Profile consolidation worker / Durable User Profile consolidation worker.
    @param embedding_client 共享 embedding HTTP client 与生命周期 / Shared embedding HTTP client and lifecycle.
    @param artifacts outbox delivery 共享 artifact store / Artifact store shared with outbox delivery.
    @param blocking_bulkheads 由顶层运行时关停的阻塞 SDK 隔舱 /
        Blocking SDK bulkheads closed by the top-level runtime.
    """

    inference: DurableAssistantInferenceAdapter
    compaction: CompactionWorker
    retrieval: RetrievalWorker
    dreaming: DreamingWorker
    embedding_client: OpenAICompatibleEmbeddings
    artifacts: FileArtifactStore
    blocking_bulkheads: tuple[AsyncBlockingBulkhead, ...]


def build_durable_assistant(
    *,
    system_prompt: str,
    runtime_limits: InferenceRuntimeLimits,
    context_window: PostgresContextWindowStore | None = None,
    telemetry: Telemetry,
) -> DurableAssistantComposition:
    """@brief 组合 async Agent、receipts、adapters 与 durable inference / Compose the async Agent, receipts, adapters, and durable inference.

    @param system_prompt system prompt / System prompt.
    @param runtime_limits inference worker budgets / Inference-worker budgets.
    @param context_window 可替换 Context Window store / Replaceable context-window store.
    @param telemetry 进程 typed telemetry / Process typed telemetry.
    @return inference 与 artifact store / Inference and artifact store.
    """

    context_window_store = context_window or PostgresContextWindowStore()
    retrieval_store = PostgresRetrievalStore()
    embedding_space = _retrieval_space()
    embedding_client = OpenAICompatibleEmbeddings(
        api_key=_retrieval_api_key(),
        api_base=config.RETRIEVAL_EMBEDDING_API_BASE,
        timeout_seconds=config.RETRIEVAL_EMBEDDING_TIMEOUT_SECONDS,
        telemetry=telemetry,
        proxy_url=config.NETWORK_PROXY_URL,
    )
    recall = SemanticRecall(
        embeddings=embedding_client,
        store=retrieval_store,
        space=embedding_space,
        corpus_id=EPISODIC_CORPUS_ID,
        telemetry=telemetry,
    )
    working_memory = RetrievalWorkingMemory(recall=recall)
    retrieval = RetrievalWorker(
        source=PostgresEpisodicSource(),
        store=retrieval_store,
        embeddings=embedding_client,
        space=embedding_space,
        renderer=EpisodicPassageRenderer(),
        telemetry=telemetry,
        worker_count=config.RETRIEVAL_WORKER_COUNT,
        batch_size=config.RETRIEVAL_BATCH_SIZE,
        poll_interval=config.RETRIEVAL_POLL_INTERVAL,
        lease_for=timedelta(seconds=config.RETRIEVAL_LEASE_SECONDS),
    )
    budget = _context_window_budget()
    history = ContextWindowProjector(
        persistence=context_window_store,
        token_counter=ConservativeHistoryTokenCounter(guard_ratio=budget.guard_ratio),
        budget=budget,
        cache=ContextWindowCache(
            capacity=config.CONVERSATION_HISTORY_CACHE_CAPACITY,
            ttl_seconds=config.CONVERSATION_HISTORY_CACHE_TTL_SECONDS,
        ),
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
        telemetry=telemetry,
    )
    operations = AssistantToolOperationDispatcher(
        help_text=config.HELP_TEXT,
        external_reads=RequestsExternalReadTools(
            external_settings,
            bulkhead=external_bulkhead,
            telemetry=telemetry,
        ),
        generated_media=generated_media,
        stickers=TelegramStickerCatalogReader(
            config_path=config.BASE_DIR / "resources" / "ai_sticker_packs.json",
            bot_token=config.TELEGRAM_BOT_TOKEN or "",
            timeout_seconds=sticker_timeout_seconds,
            bulkhead=sticker_bulkhead,
        ),
        outbox=PostgresOutboxRepository(),
        memory=working_memory,
        groups=PostgresGroupMessageProjection(),
    )
    store = PostgresAssistantToolStore(operations=operations)
    completion = LiteLLMAssistantCompletion(
        bulkhead=provider_bulkhead,
        telemetry=telemetry,
    )
    agent = AgentLoop(
        runtime=AgentRuntime(
            catalog=DEFAULT_TOOL_CATALOG,
            persistence=store,
        ),
        completion=completion,
        checkpoints=store,
        memory=working_memory,
        telemetry=telemetry,
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
        working_memory_limit=config.WORKING_MEMORY_RESULT_LIMIT,
        working_memory_max_tokens=config.WORKING_MEMORY_RESERVED_TOKENS,
        working_memory_enabled=True,
        agent_loop=agent,
    )
    translation_service = AssistantInferenceService(
        service_order=configured_service_order("translate"),
        profiles=build_provider_profiles("translate"),
        circuit=circuit,
        text_only_model_patterns=config.AI_CHAT_TEXT_ONLY_MODELS,
        working_memory_limit=config.WORKING_MEMORY_RESULT_LIMIT,
        working_memory_max_tokens=config.WORKING_MEMORY_RESERVED_TOKENS,
        working_memory_enabled=False,
        agent_loop=agent,
    )
    compaction = CompactionWorker(
        persistence=context_window_store,
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
    profile_store = PostgresUserProfileStore()
    dreaming = DreamingWorker(
        source=PostgresProfileEvidenceSource(),
        store=profile_store,
        model=ProviderDreamingModel(
            completion=completion,
            service_order=configured_service_order("dreaming"),
            profiles=build_provider_profiles("dreaming"),
            request_timeout_seconds=config.DREAMING_PROVIDER_TIMEOUT_SECONDS,
            telemetry=telemetry,
        ),
        telemetry=telemetry,
        worker_count=config.DREAMING_WORKER_COUNT,
        batch_size=config.DREAMING_BATCH_SIZE,
        source_batch_size=config.DREAMING_SOURCE_BATCH_SIZE,
        max_events_per_dream=config.DREAMING_MAX_EVENTS_PER_JOB,
        max_evidence_chars=config.DREAMING_MAX_EVIDENCE_CHARS,
        poll_interval=config.DREAMING_POLL_INTERVAL,
        refresh_after=timedelta(seconds=config.DREAMING_REFRESH_SECONDS),
        attempt_timeout=timedelta(seconds=config.DREAMING_ATTEMPT_TIMEOUT_SECONDS),
        lease_for=timedelta(seconds=config.DREAMING_LEASE_SECONDS),
        max_attempts=config.DREAMING_MAX_ATTEMPTS,
    )
    return DurableAssistantComposition(
        inference=DurableAssistantInferenceAdapter(
            history=history,
            system_prompt=system_prompt,
            runtime_limits=runtime_limits,
            history_reserved_tokens=TokenCount(
                config.CHAT_RESERVED_TOKENS
                + config.WORKING_MEMORY_RESERVED_TOKENS
            ),
            inference=service,
            translation_inference=translation_service,
        ),
        compaction=compaction,
        retrieval=retrieval,
        dreaming=dreaming,
        embedding_client=embedding_client,
        artifacts=artifacts,
        blocking_bulkheads=(
            external_bulkhead,
            media_bulkhead,
            sticker_bulkhead,
            provider_bulkhead,
        ),
    )


def _context_window_budget() -> ContextTokenBudget:
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


def _retrieval_api_key() -> str:
    """@brief 解析独立 embedding 凭据并允许显式复用 OpenRouter key / Resolve embedding credentials with explicit OpenRouter fallback.

    @return 非空 API key / Non-empty API key.
    @raise RuntimeError 未配置凭据 / Missing credentials.
    """

    key = config.RETRIEVAL_EMBEDDING_API_KEY or config.OPENROUTER_API_KEY
    if not key:
        raise RuntimeError(
            "RETRIEVAL_EMBEDDING_API_KEY or OPENROUTER_API_KEY is required"
        )
    return key


def _retrieval_space() -> EmbeddingSpace:
    """@brief 从显式配置构造活跃 embedding space / Build the active embedding space from explicit configuration.

    @return 已验证空间 / Validated space.
    """

    return EmbeddingSpace(
        space_id=config.RETRIEVAL_EMBEDDING_SPACE_ID,
        model=config.RETRIEVAL_EMBEDDING_MODEL,
        dimensions=config.RETRIEVAL_EMBEDDING_DIMENSIONS,
        query_instruction=config.RETRIEVAL_QUERY_INSTRUCTION,
        passage_format_version=1,
    )


__all__ = ["DurableAssistantComposition", "build_durable_assistant"]
