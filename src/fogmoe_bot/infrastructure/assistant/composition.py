"""@brief Durable Assistant 的基础设施装配 / Infrastructure composition for the durable Assistant."""

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
from fogmoe_bot.application.context_window.cache import ContextWindowCache
from fogmoe_bot.application.context_window.projection import ContextWindowProjector
from fogmoe_bot.application.context_window.worker import CompactionWorker
from fogmoe_bot.application.conversation.inference_worker import InferenceRuntimeLimits
from fogmoe_bot.application.memory.service import RetrievalWorkingMemory
from fogmoe_bot.application.observability.telemetry import Telemetry
from fogmoe_bot.application.retrieval import (
    EPISODIC_CORPUS_ID,
    EpisodicPassageRenderer,
    RetrievalWorker,
    SemanticRecall,
)
from fogmoe_bot.application.runtime import (
    AdaptivePollingPolicy,
    FailureCircuit,
    FailureCircuitPolicy,
)
from fogmoe_bot.application.scheduling.service import SchedulingService
from fogmoe_bot.application.timekeeping.service import TimeService
from fogmoe_bot.application.user_profile.worker import DreamingWorker
from fogmoe_bot.config import AssistantSettings, BotSettings, reveal_secret
from fogmoe_bot.domain.context_window.budget import ContextTokenBudget, TokenCount
from fogmoe_bot.domain.retrieval import EmbeddingSpace
from fogmoe_bot.domain.temporal import TimeZoneId
from fogmoe_bot.infrastructure.assistant.external_reads import (
    ExternalReadSettings,
    RequestsExternalReadTools,
)
from fogmoe_bot.infrastructure.assistant.generated_media import (
    GeneratedMediaSettings,
    RequestsGeneratedMediaTools,
)
from fogmoe_bot.infrastructure.assistant.routing_config import (
    build_provider_profiles,
    configured_service_order,
)
from fogmoe_bot.infrastructure.assistant.sticker_catalog import (
    TelegramStickerCatalogReader,
)
from fogmoe_bot.infrastructure.assistant.tool_operations.dispatcher import (
    AssistantToolOperationDispatcher,
)
from fogmoe_bot.infrastructure.blocking import AsyncBlockingBulkhead
from fogmoe_bot.infrastructure.context_window.summary import (
    ProviderCompactionSummaryGenerator,
)
from fogmoe_bot.infrastructure.context_window.token_counter import (
    ConservativeHistoryTokenCounter,
)
from fogmoe_bot.infrastructure.database.assistant_tool_effects import (
    PostgresAssistantToolStore,
)
from fogmoe_bot.infrastructure.database.context_window import PostgresContextWindowStore
from fogmoe_bot.infrastructure.database.conversation_workflow.outbox import (
    PostgresOutboxRepository,
)
from fogmoe_bot.infrastructure.database.group_message_projection import (
    PostgresGroupMessageProjection,
)
from fogmoe_bot.infrastructure.database.retrieval import (
    PostgresEpisodicSource,
    PostgresRetrievalStore,
)
from fogmoe_bot.infrastructure.database.temporal_memory import (
    PostgresTemporalMemoryReader,
)
from fogmoe_bot.infrastructure.database.user_profile.source import (
    PostgresProfileEvidenceSource,
)
from fogmoe_bot.infrastructure.database.user_profile.store import (
    PostgresUserProfileStore,
)
from fogmoe_bot.infrastructure.llm.assistant_completion import (
    LiteLLMAssistantCompletion,
)
from fogmoe_bot.infrastructure.llm.litellm_client import LiteLLMChatClient
from fogmoe_bot.infrastructure.media.file_artifact_store import FileArtifactStore
from fogmoe_bot.infrastructure.media.file_rate_limiter import FileSlidingWindowLimiter
from fogmoe_bot.infrastructure.retrieval import OpenAICompatibleEmbeddings
from fogmoe_bot.infrastructure.user_profile.dreaming_model import ProviderDreamingModel
from fogmoe_bot.resources import BotResources


@dataclass(frozen=True, slots=True)
class DurableAssistantComposition:
    """@brief 顶层运行时拥有的 Assistant 资源 / Assistant resources owned by the top-level runtime.

    @param inference durable inference adapter / Durable inference adapter.
    @param compaction durable conversation-compaction worker / Durable conversation-compaction worker.
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
    settings: BotSettings,
    resources: BotResources,
    context_window: PostgresContextWindowStore | None = None,
    telemetry: Telemetry,
) -> DurableAssistantComposition:
    """@brief 装配 durable Assistant 及其外部 adapters / Compose the durable Assistant and its external adapters.

    @param settings Bot 配置边界验证后的不可变投影 /
        Immutable projection validated by the Bot configuration boundary.
    @param resources 组合根加载的只读资源 / Read-only resources loaded by the composition root.
    @param context_window 可替换 Context Window store / Replaceable Context Window store.
    @param telemetry 进程 typed telemetry / Process typed telemetry.
    @return 推理、后台 worker 与需关停资源 / Inference, background workers, and resources requiring shutdown.
    @note 本函数是外层 composition，不读取文件或环境；secret 仅在第三方 SDK 边界揭示。/
        This outer composition reads no files or environment; secrets are revealed only at third-party SDK boundaries.
    """

    assistant_settings = settings.assistant
    runtime = settings.runtime
    context_window_store = context_window or PostgresContextWindowStore()
    retrieval_store = PostgresRetrievalStore()
    embedding_space = _retrieval_space(assistant_settings)
    embedding_client = OpenAICompatibleEmbeddings(
        api_key=_retrieval_api_key(settings),
        api_base=assistant_settings.retrieval.embedding.api_base,
        timeout_seconds=assistant_settings.retrieval.embedding.timeout_seconds,
        telemetry=telemetry,
        proxy_url=settings.network.proxy_url,
    )
    recall_circuit = FailureCircuit[tuple[str, str]](
        FailureCircuitPolicy(
            failure_threshold=1,
            failure_window_seconds=(
                assistant_settings.working_memory.failure_cooldown_seconds
            ),
            cooldown_seconds=(
                assistant_settings.working_memory.failure_cooldown_seconds
            ),
        )
    )
    recall = SemanticRecall(
        embeddings=embedding_client,
        store=retrieval_store,
        space=embedding_space,
        corpus_id=EPISODIC_CORPUS_ID,
        telemetry=telemetry,
        query_timeout_seconds=assistant_settings.working_memory.timeout_seconds,
        failure_circuit=recall_circuit,
    )
    working_memory = RetrievalWorkingMemory(recall=recall)
    retrieval_worker = assistant_settings.retrieval.worker
    retrieval = RetrievalWorker(
        source=PostgresEpisodicSource(),
        store=retrieval_store,
        embeddings=embedding_client,
        space=embedding_space,
        renderer=EpisodicPassageRenderer(),
        telemetry=telemetry,
        worker_count=retrieval_worker.worker_count,
        batch_size=retrieval_worker.batch_size,
        polling_policy=AdaptivePollingPolicy(
            retrieval_worker.poll_interval_seconds,
            retrieval_worker.max_poll_interval_seconds,
        ),
        lease_for=timedelta(seconds=retrieval_worker.lease_seconds),
    )
    budget = _context_window_budget(assistant_settings)
    history = ContextWindowProjector(
        persistence=context_window_store,
        token_counter=ConservativeHistoryTokenCounter(guard_ratio=budget.guard_ratio),
        budget=budget,
        cache=ContextWindowCache(
            capacity=assistant_settings.history_cache.capacity,
            ttl_seconds=assistant_settings.history_cache.ttl_seconds,
        ),
    )
    artifacts = FileArtifactStore(resources.generated_artifact_directory)
    external_settings = ExternalReadSettings(
        serpapi_key=reveal_secret(settings.integrations.search.serpapi_api_key) or "",
        judge0_url=settings.integrations.code_execution.judge0_api_url,
        judge0_key=reveal_secret(settings.integrations.code_execution.judge0_api_key)
        or "",
    )
    image_settings = settings.integrations.image_generation
    image_model = image_settings.model or ""
    generated_settings = GeneratedMediaSettings(
        image_url=image_settings.api_url or "",
        image_token=_image_api_token(settings),
        image_model=image_model,
        fish_audio_key=reveal_secret(settings.integrations.audio.api_key) or "",
        fish_audio_model=settings.integrations.audio.model,
        fish_audio_reference_id=settings.integrations.audio.reference_id,
        image_timeout_seconds=image_settings.timeout_seconds,
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
        limiter=FileSlidingWindowLimiter(resources.media_rate_limit_directory),
        bulkhead=media_bulkhead,
        telemetry=telemetry,
    )
    operations = AssistantToolOperationDispatcher(
        help_text=resources.help_text,
        external_reads=RequestsExternalReadTools(
            external_settings,
            bulkhead=external_bulkhead,
            telemetry=telemetry,
        ),
        generated_media=generated_media,
        stickers=TelegramStickerCatalogReader(
            config_path=resources.sticker_catalog_path,
            bot_token=reveal_secret(settings.telegram.bot_token) or "",
            timeout_seconds=sticker_timeout_seconds,
            bulkhead=sticker_bulkhead,
        ),
        outbox=PostgresOutboxRepository(),
        memory=working_memory,
        temporal_memory=PostgresTemporalMemoryReader(
            corpus_id=EPISODIC_CORPUS_ID,
            format_version=embedding_space.passage_format_version,
        ),
        groups=PostgresGroupMessageProjection(),
        time=TimeService(
            default_time_zone=TimeZoneId(assistant_settings.time.default_timezone)
        ),
        scheduling=SchedulingService(),
    )
    store = PostgresAssistantToolStore(operations=operations)
    completion = LiteLLMAssistantCompletion(
        bulkhead=provider_bulkhead,
        telemetry=telemetry,
        client=LiteLLMChatClient(providers=settings.ai.providers),
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
    circuit = FailureCircuit[str](
        FailureCircuitPolicy(
            failure_threshold=3,
            failure_window_seconds=5 * 60,
            cooldown_seconds=30 * 60,
        )
    )
    service = AssistantInferenceService(
        service_order=configured_service_order(settings.ai),
        profiles=build_provider_profiles(settings.ai),
        circuit=circuit,
        text_only_model_patterns=settings.ai.routing.chat.text_only_models,
        working_memory_limit=assistant_settings.working_memory.result_limit,
        working_memory_max_tokens=assistant_settings.working_memory.reserved_tokens,
        working_memory_enabled=True,
        agent_loop=agent,
    )
    translation_service = AssistantInferenceService(
        service_order=configured_service_order(settings.ai, "translation"),
        profiles=build_provider_profiles(settings.ai, "translation"),
        circuit=circuit,
        text_only_model_patterns=settings.ai.routing.chat.text_only_models,
        working_memory_limit=assistant_settings.working_memory.result_limit,
        working_memory_max_tokens=assistant_settings.working_memory.reserved_tokens,
        working_memory_enabled=False,
        agent_loop=agent,
    )
    compaction_runtime = runtime.compaction
    compaction = CompactionWorker(
        persistence=context_window_store,
        generator=ProviderCompactionSummaryGenerator(
            completion=completion,
            service_order=configured_service_order(settings.ai, "summary"),
            profiles=build_provider_profiles(settings.ai, "summary"),
            request_timeout_seconds=compaction_runtime.provider_timeout_seconds,
            budget=budget,
        ),
        worker_count=compaction_runtime.worker_count,
        polling_policy=AdaptivePollingPolicy(
            compaction_runtime.poll_interval_seconds,
            compaction_runtime.max_poll_interval_seconds,
        ),
        attempt_timeout=timedelta(seconds=compaction_runtime.attempt_timeout_seconds),
        lease_for=timedelta(seconds=compaction_runtime.lease_seconds),
    )
    dreaming_runtime = runtime.dreaming
    profile_store = PostgresUserProfileStore()
    dreaming = DreamingWorker(
        source=PostgresProfileEvidenceSource(),
        store=profile_store,
        model=ProviderDreamingModel(
            completion=completion,
            service_order=configured_service_order(settings.ai, "dreaming"),
            profiles=build_provider_profiles(settings.ai, "dreaming"),
            request_timeout_seconds=dreaming_runtime.provider_timeout_seconds,
            telemetry=telemetry,
        ),
        telemetry=telemetry,
        polling_policy=AdaptivePollingPolicy(
            dreaming_runtime.poll_interval_seconds,
            dreaming_runtime.max_poll_interval_seconds,
        ),
        worker_count=dreaming_runtime.worker_count,
        batch_size=dreaming_runtime.batch_size,
        source_batch_size=dreaming_runtime.source_batch_size,
        max_events_per_dream=dreaming_runtime.max_events_per_job,
        max_evidence_chars=dreaming_runtime.max_evidence_characters,
        refresh_after=timedelta(seconds=dreaming_runtime.refresh_seconds),
        attempt_timeout=timedelta(seconds=dreaming_runtime.attempt_timeout_seconds),
        lease_for=timedelta(seconds=dreaming_runtime.lease_seconds),
        max_attempts=dreaming_runtime.max_attempts,
    )
    inference_runtime = runtime.inference
    return DurableAssistantComposition(
        inference=DurableAssistantInferenceAdapter(
            history=history,
            system_prompt=resources.system_prompt,
            runtime_limits=InferenceRuntimeLimits(
                provider_timeout=timedelta(
                    seconds=inference_runtime.provider_timeout_seconds
                ),
                attempt_timeout=timedelta(
                    seconds=inference_runtime.attempt_timeout_seconds
                ),
                lease_for=timedelta(seconds=inference_runtime.lease_seconds),
            ),
            history_reserved_tokens=TokenCount(
                assistant_settings.context_window.reserved_tokens
                + assistant_settings.working_memory.reserved_tokens
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


def _context_window_budget(settings: AssistantSettings) -> ContextTokenBudget:
    """@brief 从 Assistant 设置构造 token budget / Build a token budget from Assistant settings.

    @param settings 已验证的 Assistant 设置 / Validated Assistant settings.
    @return 已验证的上下文 token budget / Validated context token budget.
    """

    warning = settings.context_window.warning_tokens
    return ContextTokenBudget(
        warning_tokens=TokenCount(warning),
        hard_tokens=TokenCount(settings.context_window.hard_tokens),
        summary_output_tokens=TokenCount(2_500),
        segment_input_tokens=TokenCount(min(64_000, warning)),
    )


def _retrieval_api_key(settings: BotSettings) -> str:
    """@brief 解析独立 embedding 凭据 / Resolve the independent embedding credential.

    @param settings 已验证的 Bot 设置 / Validated Bot settings.
    @return 非空 embedding API key / Non-empty embedding API key.
    @raise RuntimeError embedding 与 OpenRouter 均未提供密钥时抛出 /
        Raised when neither embedding nor OpenRouter provides a key.
    """

    key = reveal_secret(
        settings.assistant.retrieval.embedding.api_key
    ) or reveal_secret(settings.ai.providers.openrouter.api_key)
    if not key:
        raise RuntimeError(
            "assistant.retrieval.embedding.api_key or ai.providers.openrouter.api_key "
            "is required"
        )
    return key


def _image_api_token(settings: BotSettings) -> str:
    """@brief 解析图片服务令牌 / Resolve the image-service token.

    @param settings 已验证的 Bot 设置 / Validated Bot settings.
    @return 图片 API 令牌；未配置时为空字符串 / Image API token, or an empty string when unset.
    @note 选择 OpenRouter 图片模型时，未单独给出令牌会复用 OpenRouter 密钥。/
        When an OpenRouter image model is selected, its key is reused if no dedicated token exists.
    """

    dedicated = reveal_secret(settings.integrations.image_generation.api_token)
    if dedicated:
        return dedicated
    if settings.integrations.image_generation.model:
        return reveal_secret(settings.ai.providers.openrouter.api_key) or ""
    return ""


def _retrieval_space(settings: AssistantSettings) -> EmbeddingSpace:
    """@brief 从 Assistant 设置构造 embedding space / Build the active embedding space from Assistant settings.

    @param settings 已验证的 Assistant 设置 / Validated Assistant settings.
    @return 已验证的 embedding space / Validated embedding space.
    """

    embedding = settings.retrieval.embedding
    return EmbeddingSpace(
        space_id=embedding.space_id,
        model=embedding.model,
        dimensions=embedding.dimensions,
        query_instruction=embedding.query_instruction,
        passage_format_version=1,
    )


__all__ = ["DurableAssistantComposition", "build_durable_assistant"]
