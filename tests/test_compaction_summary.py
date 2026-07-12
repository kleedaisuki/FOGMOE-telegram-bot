"""@brief Provider compaction summary adapter tests / Provider compaction summary adapter tests."""

import asyncio
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

import pytest

from fogmoe_bot.application.assistant.completion import AssistantCompletion
from fogmoe_bot.application.assistant.tools.catalog import ToolDefinition
from fogmoe_bot.application.conversation.compaction_worker import (
    RetryableCompactionError,
)
from fogmoe_bot.domain.assistant.routing.models import ProviderRoute
from fogmoe_bot.domain.conversation.payloads import (
    JsonObject,
    JsonValue,
)
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    LeaseToken,
    TurnId,
)
from fogmoe_bot.domain.conversation.retention import (
    ContextTokenBudget,
    RetentionSegment,
    RetentionSegmentDraft,
    TokenCount,
)
from fogmoe_bot.infrastructure.assistant.compaction_summary import (
    ProviderCompactionSummaryGenerator,
)


NOW = datetime(2030, 1, 1, tzinfo=UTC)
"""@brief 确定性测试时刻 / Deterministic test instant."""


class _Completion:
    """@brief 可配置 route 失败的 completion fake / Completion fake with configurable route failures."""

    def __init__(self, *, failures: set[str], content: str) -> None:
        """@brief 保存 route 行为 / Store route behavior."""

        self.failures = failures
        self.content = content
        self.calls: list[
            tuple[str, str, Sequence[JsonObject], Sequence[ToolDefinition], int]
        ] = []

    async def complete(
        self,
        *,
        provider: str,
        model: str,
        messages: Sequence[JsonObject],
        tools: Sequence[ToolDefinition],
        tool_choice: str | JsonObject | None,
        max_tokens: int,
        request_options: Mapping[str, JsonValue],
    ) -> AssistantCompletion:
        """@brief 记录无工具请求并返回或失败 / Record the tool-free request and return or fail."""

        del tool_choice, request_options
        self.calls.append((provider, model, messages, tools, max_tokens))
        if provider in self.failures:
            raise RuntimeError(f"{provider} unavailable")
        return AssistantCompletion(
            self.content,
            {"role": "assistant", "content": self.content},
        )


def _claim() -> RetentionSegment:
    """@brief 构造带前序累计 memory 的 processing claim / Build a processing claim carrying prior cumulative memory."""

    draft = RetentionSegmentDraft.compaction(
        conversation_id=ConversationId("assistant-user:7"),
        owner_user_id=7,
        epoch_floor_sequence=0,
        from_sequence=1,
        through_sequence=2,
        anchor_turn_id=TurnId.new(),
        predecessor_segment_id=None,
        projection_version=1,
        source_snapshot=(
            {"role": "system", "content": "prior cumulative memory"},
            {"role": "user", "content": "new delta"},
        ),
        source_row_count=2,
        source_token_count=TokenCount(10),
        created_at=NOW,
    )
    return RetentionSegment.pending(draft).claim(
        token=LeaseToken.new(),
        claimed_at=NOW + timedelta(seconds=1),
        lease_for=timedelta(seconds=30),
    )


def _route(name: str) -> ProviderRoute:
    """@brief 构造单模型 summary route / Build a single-model summary route."""

    return ProviderRoute(
        service_name=name,
        provider_name=name,
        display_name=name,
        models=(f"{name}-summary",),
        completion_kwargs={},
    )


def test_summary_routes_without_tools_and_bounds_provider_output() -> None:
    """@brief 第一 route 失败后回退，snapshot 作为非指令 JSON 且输出有界 / The adapter falls back, wraps the snapshot as untrusted JSON, and bounds output."""

    async def scenario() -> None:
        """@brief 执行 route fallback / Execute route fallback."""

        completion = _Completion(failures={"first"}, content="a" * 100)
        generator = ProviderCompactionSummaryGenerator(
            completion=completion,
            service_order=("first", "second"),
            profiles={"first": _route("first"), "second": _route("second")},
            request_timeout_seconds=5,
            budget=ContextTokenBudget(
                warning_tokens=TokenCount(100),
                hard_tokens=TokenCount(120),
                summary_output_tokens=TokenCount(10),
                segment_input_tokens=TokenCount(50),
            ),
        )

        summary = await generator.summarize(_claim())

        assert summary.route_key == "second:second-summary"
        assert int(summary.token_count) <= 10
        assert [call[0] for call in completion.calls] == ["first", "second"]
        assert all(call[3] == () for call in completion.calls)
        assert all(call[4] == 10 for call in completion.calls)
        provider_messages = completion.calls[-1][2]
        assert len(provider_messages) == 2
        assert provider_messages[0]["role"] == "system"
        assert "<conversation_snapshot_json>" in str(provider_messages[1]["content"])
        assert "prior cumulative memory" in str(provider_messages[1]["content"])

    asyncio.run(scenario())


def test_provider_value_errors_remain_retryable_worker_failures() -> None:
    """@brief Provider 形状错误被 adapter 分类为 retryable 而非 source corruption / Provider shape errors remain retryable rather than source corruption."""

    completion = _Completion(failures={"only"}, content="unused")
    generator = ProviderCompactionSummaryGenerator(
        completion=completion,
        service_order=("only",),
        profiles={"only": _route("only")},
        request_timeout_seconds=5,
    )

    with pytest.raises(RetryableCompactionError, match="routes failed"):
        asyncio.run(generator.summarize(_claim()))
