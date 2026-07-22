"""@brief Durable Assistant inference adapter 测试 / Tests for the durable Assistant inference adapter."""

import asyncio
from datetime import datetime, timedelta, timezone
import pytest
from pydantic import ValidationError

from fogmoe_bot.application.assistant.agent_loop import AgentResponse
from fogmoe_bot.application.assistant.durable_inference import (
    TRANSLATION_SYSTEM_PROMPT,
    DurableAssistantInferenceAdapter,
)
from fogmoe_bot.application.assistant.inference_command import (
    DurableAssistantInferenceCommand,
    DurableAssistantScope,
    DurableAssistantUser,
    DurableProfileClaim,
    DurableUserProfile,
)
from fogmoe_bot.application.assistant.errors import AssistantInferenceUnavailableError
from fogmoe_bot.application.context_window.projection import (
    ContextWindowBounds,
    CompactionPending,
    ContextWindowRequest,
    ContextWindowResult,
    ContextWindowReady,
    project_conversation_message,
)
from fogmoe_bot.application.conversation.inference_worker import (
    InferenceErrorCategory,
    InferenceDependencyPending,
    InferenceOutputError,
    InferenceRuntimeLimits,
    PermanentInferenceError,
    RetryableInferenceError,
)
from fogmoe_bot.domain.context import ContextState
from fogmoe_bot.domain.accounts.plan import AccountPlan
from fogmoe_bot.domain.conversation.payloads import JsonObject
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    ConversationMessageId,
    MessageSequence,
    TurnId,
    UpdateId,
)
from fogmoe_bot.domain.conversation.message import (
    ConversationMessage,
    MessageDraft,
    MessageRole,
)
from fogmoe_bot.domain.conversation.outbox import SEND_TELEGRAM_MESSAGE
from fogmoe_bot.domain.context_window.compaction import CompactionId
from fogmoe_bot.domain.context_window.budget import TokenCount
from fogmoe_bot.domain.user_profile.models import (
    ProfileClaimKind,
    ProfileConfidence,
)


NOW = datetime(2026, 7, 11, 10, tzinfo=timezone.utc)
"""@brief 测试基准时间 / Test reference time."""


def _request(turn_id: TurnId, **overrides: object) -> JsonObject:
    """@brief 构造严格 durable request / Build a strict durable request.

    @param turn_id 当前 Turn / Current Turn.
    @param overrides 顶层字段覆盖 / Top-level field overrides.
    @return JSON request / JSON request.
    """

    request: JsonObject = {
        "schema_version": 2,
        "conversation_id": "assistant-group:-100:thread:9",
        "turn_id": str(turn_id),
        "delivery_stream_id": "telegram:bot:1:chat:-100:thread:9",
        "chat_id": -100,
        "reply_to_message_id": 42,
        "message_thread_id": 9,
        "user": {
            "user_id": 7,
            "username": "klee",
            "display_name": "Klee",
            "coins": 91,
            "plan": "free",
            "permission": 0,
            "profile": None,
            "personal_info": "",
            "diary_exists": False,
        },
        "scope": {
            "is_group": True,
            "group_id": -100,
            "message_id": 42,
            "message_thread_id": 9,
        },
        "disable_notification": False,
        "protect_content": False,
        "disable_web_page_preview": True,
    }
    request.update(overrides)  # type: ignore[arg-type]
    return request


def _private_command_with_profile(
    turn_id: TurnId,
) -> DurableAssistantInferenceCommand:
    """@brief 构造携带 Profile 时间与元组的私聊命令 / Build a private command carrying Profile datetimes and tuples.

    @param turn_id 当前 Turn / Current Turn.
    @return 将经过 JSONB 持久化的严格命令 / Strict command that will pass through JSONB persistence.
    """

    profile = DurableUserProfile(
        revision=3,
        observed_through_event_id=71,
        prompt_version=2,
        route_key="profile:test",
        created_at=NOW,
        updated_at=NOW + timedelta(minutes=1),
        claims=(
            DurableProfileClaim(
                key="preference.drink",
                kind=ProfileClaimKind.PREFERENCE,
                statement="Klee prefers tea.",
                confidence=ProfileConfidence.EXPLICIT,
                evidence_event_ids=(70, 71),
                observed_at=NOW + timedelta(seconds=30),
            ),
        ),
    )
    return DurableAssistantInferenceCommand(
        schema_version=2,
        conversation_id="assistant-user:7",
        turn_id=str(turn_id),
        delivery_stream_id="telegram:bot:1:chat:7:thread:0",
        chat_id=7,
        reply_to_message_id=None,
        message_thread_id=None,
        user=DurableAssistantUser(
            user_id=7,
            username="klee",
            display_name="Klee",
            coins=0,
            plan=AccountPlan.FREE,
            permission=0,
            profile=profile,
            personal_info="",
            diary_exists=False,
        ),
        scope=DurableAssistantScope(is_group=False),
    )


def _message(
    *,
    sequence: int,
    turn_id: TurnId,
    role: MessageRole,
    content: JsonObject,
) -> ConversationMessage:
    """@brief 构造规范会话消息 / Build a canonical conversation message.

    @param sequence 会话序号 / Conversation sequence.
    @param turn_id 所属 Turn / Owning Turn.
    @param role 消息角色 / Message role.
    @param content 结构内容 / Structured content.
    @return 规范消息 / Canonical message.
    """

    return ConversationMessage(
        draft=MessageDraft(
            message_id=ConversationMessageId.for_turn(
                turn_id,
                f"test.{sequence}.{role.value}",
            ),
            conversation_id=ConversationId("assistant-group:-100:thread:9"),
            turn_id=turn_id,
            source_update_id=(UpdateId(sequence) if role is MessageRole.USER else None),
            role=role,
            content=content,
            idempotency_key=f"test:{sequence}:{role.value}",
            created_at=NOW + timedelta(seconds=sequence),
        ),
        sequence=MessageSequence(sequence),
    )


class _History:
    """@brief 固定 token-aware 历史 projector / Fixed token-aware history projector."""

    def __init__(
        self,
        messages: tuple[ConversationMessage, ...],
        *,
        forced: ContextWindowResult | None = None,
    ) -> None:
        """@brief 保存消息 / Store messages.

        @param messages 固定消息 / Fixed messages.
        """

        self.messages = messages
        self.forced = forced
        self.calls: list[ContextWindowRequest] = []

    async def project(self, request: ContextWindowRequest) -> ContextWindowResult:
        """@brief 返回固定 summary+tail projection / Return a fixed summary-plus-tail projection."""

        self.calls.append(request)
        if self.forced is not None:
            return self.forced
        anchor = tuple(
            message
            for message in self.messages
            if message.draft.turn_id == request.through_turn_id
        )
        source = self.messages if request.include_history else anchor
        projected = tuple(
            model_message
            for message in source
            for model_message in project_conversation_message(message)
        )
        sequences = [
            int(message.sequence) for message in (anchor or self.messages)
        ] or [1]
        return ContextWindowReady(
            checkpoint_summary=None,
            messages=projected,
            estimated_tokens=TokenCount(1),
            bounds=ContextWindowBounds(
                request.conversation_id,
                request.through_turn_id,
                min(sequences),
                max(sequences),
                0,
            ),
            checkpoint=None,
            anchor_messages=anchor,
        )


class _Inference:
    """@brief 可注入 AgentResponse 或异常的 service 替身 / Service double returning an AgentResponse or exception."""

    def __init__(self, result: AgentResponse | Exception) -> None:
        """@brief 创建 service 替身 / Create the service double.

        @param result 固定响应或异常 / Fixed response or exception.
        """

        self.result = result
        self.context: ContextState | None = None
        self.kwargs: dict[str, object] = {}

    async def infer(
        self,
        context_state: ContextState,
        *,
        allow_tools: bool = True,
        request_timeout: float | None = None,
        tool_context: object | None = None,
    ) -> AgentResponse:
        """@brief 记录纯推理调用 / Record the pure inference call."""

        self.context = context_state
        self.kwargs = {
            "allow_tools": allow_tools,
            "request_timeout": request_timeout,
            "tool_context": tool_context,
        }
        if isinstance(self.result, Exception):
            raise self.result
        context_state.messages.extend(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "search", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call-1",
                    "name": "search",
                    "content": '{"answer":42}',
                },
                {"role": "assistant", "content": self.result.text},
            ]
        )
        return AgentResponse(
            self.result.text,
            self.result.events,
            context_state,
        )


def _adapter(
    history: _History,
    inference: _Inference,
) -> DurableAssistantInferenceAdapter:
    """@brief 构造测试 adapter / Build a test adapter."""

    return DurableAssistantInferenceAdapter(
        history=history,
        system_prompt="You are a careful assistant.",
        runtime_limits=InferenceRuntimeLimits(
            provider_timeout=timedelta(seconds=20),
            attempt_timeout=timedelta(seconds=30),
            lease_for=timedelta(seconds=45),
        ),
        inference=inference,
    )


def test_persisted_json_strictly_restores_profile_datetimes_and_tuples() -> None:
    """@brief JSONB Profile 载荷在严格模式下恢复时间和元组 / Strict JSONB parsing restores Profile datetimes and tuples.

    acceptance 使用 ``model_dump(mode=\"json\")``，因此 JSONB 中的时间为 ISO 8601
    字符串、元组为数组。适配器必须在 JSON 验证通道中重建命令，同时保留对非 JSON
    Python 强制转换的拒绝。/ Acceptance uses ``model_dump(mode=\"json\")``, so JSONB
    stores times as ISO 8601 strings and tuples as arrays. The adapter must rebuild the command
    through Pydantic's JSON-validation channel while retaining rejection of non-JSON Python
    coercions.
    """

    command = _private_command_with_profile(TurnId.new())
    persisted = command.to_json()
    stored_profile = persisted["user"]["profile"]
    assert isinstance(stored_profile, dict)
    assert isinstance(stored_profile["created_at"], str)
    assert isinstance(stored_profile["claims"], list)

    parsed = DurableAssistantInferenceAdapter._parse_request(persisted)

    assert parsed == command
    assert parsed.user.profile is not None
    assert parsed.user.profile.claims[0].evidence_event_ids == (70, 71)
    assert parsed.user.profile.claims[0].observed_at == NOW + timedelta(seconds=30)


def test_adapter_reads_cutoff_history_and_returns_ordered_durable_outbox_intents() -> (
    None
):
    """@brief Adapter 读取 Turn 截止历史并返回有序 durable outbox / Adapter reads Turn-cutoff history and returns ordered durable outbox intents."""

    async def scenario() -> None:
        turn_id = TurnId.new()
        prior_turn = TurnId.new()
        history = _History(
            (
                _message(
                    sequence=1,
                    turn_id=prior_turn,
                    role=MessageRole.ASSISTANT,
                    content={"text": "previous"},
                ),
                _message(
                    sequence=2,
                    turn_id=turn_id,
                    role=MessageRole.USER,
                    content={"text": "hello"},
                ),
            )
        )
        response = AgentResponse(
            "final answer",
            [
                {"type": "assistant_visible", "content": "progress"},
                {
                    "type": "tool_result",
                    "tool_name": "search",
                    "arguments": {},
                    "result": {"answer": 42},
                    "tool_call_id": "call-1",
                    "internal_result": {"secret": "must-not-persist"},
                },
            ],
        )
        inference = _Inference(response)

        result = await _adapter(history, inference).infer(_request(turn_id))

        assert len(history.calls) == 1
        assert history.calls[0].conversation_id == ConversationId(
            "assistant-group:-100:thread:9"
        )
        assert history.calls[0].through_turn_id == turn_id
        assert history.calls[0].include_history is True
        assert inference.context is not None
        system_message = inference.context.messages[0]
        assert system_message["role"] == "system"
        assert isinstance(system_message["content"], str)
        assert (
            '<user_identity trust="trusted_platform_metadata" display_name="Klee" '
            'username="klee" user_id="7" />'
        ) in system_message["content"]
        assert inference.context.messages[1:3] == [
            {"role": "assistant", "content": "previous"},
            {"role": "user", "content": "hello"},
        ]
        assert inference.kwargs == {
            "allow_tools": True,
            "request_timeout": 20.0,
            "tool_context": inference.kwargs["tool_context"],
        }
        assert inference.kwargs["tool_context"] is not None
        assert [intent.kind for intent in result.outbounds] == [
            SEND_TELEGRAM_MESSAGE,
            SEND_TELEGRAM_MESSAGE,
        ]
        assert result.outbounds[0].payload == {
            "chat_id": -100,
            "text": "progress",
            "parse_mode": "Markdown",
            "disable_notification": False,
            "protect_content": False,
            "disable_web_page_preview": True,
            "reply_to_message_id": 42,
            "message_thread_id": 9,
        }
        assert result.outbounds[1].payload == {
            "chat_id": -100,
            "text": "final answer",
            "parse_mode": "Markdown",
            "disable_notification": False,
            "protect_content": False,
            "disable_web_page_preview": True,
            "message_thread_id": 9,
        }
        runtime_events = result.assistant_content["runtime_events"]
        assert isinstance(runtime_events, list)
        assert "internal_result" not in runtime_events[1]
        history_messages = result.assistant_content["history_messages"]
        assert isinstance(history_messages, list)
        assert [item["role"] for item in history_messages] == [
            "assistant",
            "tool",
            "assistant",
        ]

    asyncio.run(scenario())


def test_translation_uses_dedicated_prompt_without_tools_and_marks_output_excluded() -> (
    None
):
    """@brief 翻译使用隔离 prompt、禁用工具并标记输出 / Translation uses an isolated prompt, disables tools, and marks its output."""

    async def scenario() -> None:
        """@brief 执行 durable 翻译 / Execute a durable translation."""

        turn_id = TurnId.new()
        prior_turn = TurnId.new()
        history = _History(
            (
                _message(
                    sequence=1,
                    turn_id=prior_turn,
                    role=MessageRole.ASSISTANT,
                    content={"text": "ordinary assistant history"},
                ),
                _message(
                    sequence=2,
                    turn_id=turn_id,
                    role=MessageRole.USER,
                    content={
                        "text": "你好",
                        "task_kind": "translation",
                        "exclude_from_assistant": True,
                    },
                ),
            )
        )
        chat_inference = _Inference(AssertionError("chat route must not run"))
        translation_inference = _Inference(AgentResponse("Hello, meow!", []))
        adapter = DurableAssistantInferenceAdapter(
            history=history,
            system_prompt="You are a careful assistant.",
            runtime_limits=InferenceRuntimeLimits(
                provider_timeout=timedelta(seconds=20),
                attempt_timeout=timedelta(seconds=30),
                lease_for=timedelta(seconds=45),
            ),
            inference=chat_inference,
            translation_inference=translation_inference,
        )

        result = await adapter.infer(
            _request(
                turn_id,
                task_kind="translation",
                translation_input="你好",
            )
        )

        assert chat_inference.context is None
        assert len(history.calls) == 1
        assert history.calls[0].include_history is False
        assert translation_inference.context is not None
        assert translation_inference.context.messages[:2] == [
            {"role": "system", "content": TRANSLATION_SYSTEM_PROMPT},
            {"role": "user", "content": "你好"},
        ]
        assert translation_inference.kwargs == {
            "allow_tools": False,
            "request_timeout": 20.0,
            "tool_context": None,
        }
        assert result.assistant_content["task_kind"] == "translation"
        assert result.assistant_content["exclude_from_assistant"] is True
        assert result.outbounds[0].payload["text"] == "Hello, meow!"
        assert result.outbounds[0].payload["parse_mode"] == "Markdown"

    asyncio.run(scenario())


def test_translation_rejects_activity_and_durable_message_input_drift() -> None:
    """@brief 翻译 activity 与持久消息输入漂移会永久失败 / Translation input drift between the activity and durable message fails permanently."""

    turn_id = TurnId.new()
    current = _message(
        sequence=1,
        turn_id=turn_id,
        role=MessageRole.USER,
        content={"text": "durable", "exclude_from_assistant": True},
    )
    with pytest.raises(PermanentInferenceError) as mismatch:
        asyncio.run(
            _adapter(
                _History((current,)),
                _Inference(AgentResponse("unused", [])),
            ).infer(
                _request(
                    turn_id,
                    task_kind="translation",
                    translation_input="different",
                )
            )
        )
    assert mismatch.value.category is InferenceErrorCategory.INVALID_REQUEST


def test_pending_compaction_uses_the_non_exhausting_dependency_gate() -> None:
    """@brief Hard budget 等待 compaction 时返回不耗尽 provider retry budget 的 durable gate / Waiting for hard-budget compaction returns a durable gate that does not exhaust provider retries."""

    turn_id = TurnId.new()
    bounds = ContextWindowBounds(
        ConversationId("assistant-user:7"),
        turn_id,
        1,
        1,
        0,
    )
    compaction_id = CompactionId.for_range(
        conversation_id=ConversationId("assistant-user:7"),
        epoch_floor_sequence=0,
        from_sequence=1,
        through_sequence=1,
        projection_version=1,
    )
    history = _History(
        (),
        forced=CompactionPending(compaction_id, TokenCount(120_001), bounds),
    )
    inference = _Inference(AgentResponse("must not run", []))

    with pytest.raises(InferenceDependencyPending) as pending:
        asyncio.run(_adapter(history, inference).infer(_request(turn_id)))

    assert pending.value.retry_after == timedelta(seconds=5)
    assert inference.context is None


def test_future_assistant_projection_ignores_translation_input_and_output() -> None:
    """@brief 后续 Assistant 永久忽略翻译输入和输出 / A later Assistant permanently ignores translation input and output."""

    async def scenario() -> None:
        """@brief 构造含翻译记录的后续对话 / Build a later conversation containing translation records."""

        ordinary_turn = TurnId.new()
        translation_turn = TurnId.new()
        current_turn = TurnId.new()
        history = _History(
            (
                _message(
                    sequence=1,
                    turn_id=ordinary_turn,
                    role=MessageRole.ASSISTANT,
                    content={"text": "ordinary"},
                ),
                _message(
                    sequence=2,
                    turn_id=translation_turn,
                    role=MessageRole.USER,
                    content={"text": "secret input", "exclude_from_assistant": True},
                ),
                _message(
                    sequence=3,
                    turn_id=translation_turn,
                    role=MessageRole.ASSISTANT,
                    content={
                        "text": "secret output",
                        "exclude_from_assistant": True,
                        "history_messages": [
                            {"role": "assistant", "content": "secret output"}
                        ],
                    },
                ),
                _message(
                    sequence=4,
                    turn_id=current_turn,
                    role=MessageRole.USER,
                    content={"text": "current"},
                ),
            )
        )
        inference = _Inference(AgentResponse("answer", []))

        await _adapter(history, inference).infer(_request(current_turn))

        assert inference.context is not None
        assert inference.context.messages[1:3] == [
            {"role": "assistant", "content": "ordinary"},
            {"role": "user", "content": "current"},
        ]
        assert "secret" not in str(inference.context.messages)

    asyncio.run(scenario())


def test_strict_command_rejects_unknown_and_coerced_fields() -> None:
    """@brief 严格 command 拒绝未知字段和字符串布尔值 / Strict command rejects unknown fields and string Booleans."""

    turn_id = TurnId.new()
    request = _request(turn_id, unexpected=True)
    with pytest.raises(PermanentInferenceError) as unknown:
        asyncio.run(
            _adapter(_History(()), _Inference(AgentResponse("ok", []))).infer(request)
        )
    assert unknown.value.category is InferenceErrorCategory.INVALID_REQUEST

    coerced = _request(turn_id, disable_notification="false")
    with pytest.raises(PermanentInferenceError) as mistyped:
        asyncio.run(
            _adapter(_History(()), _Inference(AgentResponse("ok", []))).infer(coerced)
        )
    assert mistyped.value.category is InferenceErrorCategory.INVALID_REQUEST


def test_command_model_is_frozen_and_validates_cross_field_scope() -> None:
    """@brief Command 冻结且拒绝不一致 group scope / Command is frozen and rejects inconsistent group scope."""

    turn_id = TurnId.new()
    strict_python_request = _request(turn_id)
    strict_python_request["user"] = {
        **strict_python_request["user"],  # type: ignore[dict-item]
        "plan": AccountPlan.FREE,
    }
    command = DurableAssistantInferenceCommand.model_validate(
        strict_python_request,
        strict=True,
    )
    with pytest.raises(ValidationError):
        command.chat_id = -200

    invalid_scope = _request(
        turn_id,
        scope={"is_group": True, "group_id": -200, "message_id": 42},
    )
    with pytest.raises(PermanentInferenceError):
        asyncio.run(
            _adapter(_History(()), _Inference(AgentResponse("ok", []))).infer(
                invalid_scope
            )
        )

    wrong_conversation = _request(
        turn_id,
        conversation_id="assistant-user:7",
    )
    with pytest.raises(PermanentInferenceError):
        asyncio.run(
            _adapter(_History(()), _Inference(AgentResponse("ok", []))).infer(
                wrong_conversation
            )
        )

    private_state_in_group = _request(
        turn_id,
        user={
            "user_id": 7,
            "username": "klee",
            "display_name": "Klee",
            "coins": 91,
            "plan": "free",
            "permission": 0,
            "profile": None,
            "personal_info": "private",
            "diary_exists": False,
        },
    )
    with pytest.raises(PermanentInferenceError):
        asyncio.run(
            _adapter(_History(()), _Inference(AgentResponse("ok", []))).infer(
                private_state_in_group
            )
        )

    missing_translation_input = _request(turn_id, task_kind="translation")
    with pytest.raises(PermanentInferenceError):
        asyncio.run(
            _adapter(_History(()), _Inference(AgentResponse("ok", []))).infer(
                missing_translation_input
            )
        )

    assistant_with_translation_input = _request(
        turn_id,
        translation_input="must not be accepted",
    )
    with pytest.raises(PermanentInferenceError):
        asyncio.run(
            _adapter(_History(()), _Inference(AgentResponse("ok", []))).infer(
                assistant_with_translation_input
            )
        )


def test_missing_current_user_message_and_oversized_output_fail_permanently() -> None:
    """@brief 缺少当前用户消息或输出过长均永久失败 / Missing current user message and oversized output fail permanently."""

    turn_id = TurnId.new()
    prior = _message(
        sequence=1,
        turn_id=TurnId.new(),
        role=MessageRole.USER,
        content={"text": "old"},
    )
    with pytest.raises(PermanentInferenceError) as missing:
        asyncio.run(
            _adapter(
                _History((prior,)),
                _Inference(AgentResponse("ok", [])),
            ).infer(_request(turn_id))
        )
    assert missing.value.category is InferenceErrorCategory.INVALID_REQUEST

    current = _message(
        sequence=2,
        turn_id=turn_id,
        role=MessageRole.USER,
        content={"text": "new"},
    )
    with pytest.raises(InferenceOutputError):
        asyncio.run(
            _adapter(
                _History((prior, current)),
                _Inference(AgentResponse("x" * 4097, [])),
            ).infer(_request(turn_id))
        )


@pytest.mark.parametrize(
    ("cause", "expected_type", "category"),
    (
        (
            type(
                "RateLimitError", (RuntimeError,), {"retry_after": timedelta(seconds=7)}
            )("busy"),
            RetryableInferenceError,
            InferenceErrorCategory.RATE_LIMIT,
        ),
        (
            type("AuthenticationError", (RuntimeError,), {})("bad key"),
            PermanentInferenceError,
            InferenceErrorCategory.AUTHENTICATION,
        ),
    ),
)
def test_provider_exhaustion_maps_to_worker_taxonomy(
    cause: Exception,
    expected_type: type[Exception],
    category: InferenceErrorCategory,
) -> None:
    """@brief Provider 耗尽映射 retry/permanent taxonomy / Provider exhaustion maps to retry/permanent taxonomy."""

    turn_id = TurnId.new()
    current = _message(
        sequence=1,
        turn_id=turn_id,
        role=MessageRole.USER,
        content={"text": "hello"},
    )
    error = AssistantInferenceUnavailableError("all failed", last_error=cause)
    with pytest.raises(expected_type) as captured:
        asyncio.run(
            _adapter(_History((current,)), _Inference(error)).infer(_request(turn_id))
        )
    assert isinstance(
        captured.value, (RetryableInferenceError, PermanentInferenceError)
    )
    assert captured.value.category is category
