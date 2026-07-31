from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from fogmoe_bot.application.conversation.assistant_ingress import (
    normalize_assistant_personal_info as normalize_personal_info,
)
from fogmoe_bot.domain.context import (
    ChatMessageContext,
    ContextState,
    ConversationScope,
    RuntimeMessageReplacement,
    ScheduledTaskContext,
    UserState,
    build_context_state,
    build_tool_context,
    create_runtime_replacement,
    render_chat_message,
    render_scheduled_task,
    render_user_state,
)
from fogmoe_bot.domain.assistant.messages import (
    CanonicalMessage,
    ImagePart,
    TextPart,
    UrlImageSource,
    text_message,
)
from fogmoe_bot.domain.conversation.message import MessageRole
from fogmoe_bot.domain.user_profile.models import (
    ProfileClaim,
    ProfileClaimKind,
    ProfileConfidence,
    ProfileDocument,
    UserProfileSnapshot,
)


def _profile() -> UserProfileSnapshot:
    """@brief 构造 acceptance-pinned Profile / Build an acceptance-pinned Profile."""

    now = datetime(2026, 7, 6, tzinfo=timezone.utc)
    return UserProfileSnapshot(
        user_id=42,
        revision=3,
        document=ProfileDocument(
            (
                ProfileClaim(
                    key="interest.cs",
                    kind=ProfileClaimKind.PREFERENCE,
                    statement="喜欢计算机科学",
                    confidence=ProfileConfidence.EXPLICIT,
                    evidence_event_ids=(7,),
                    observed_at=now,
                ),
            )
        ),
        observed_through_event_id=7,
        created_at=now,
        updated_at=now,
        route_key="test:model",
        prompt_version=1,
    )


def test_context_tools_render_chat_message_metadata_and_escape_content():
    result = render_chat_message(
        ChatMessageContext(
            chat_type="supergroup",
            chat_title="Fog <Lab>",
            timestamp="2026-07-06 20:10:00",
            user_name="Klee",
            username="kc",
            message_text='hello <Klee> & "world"',
            message_id=1201,
            forward_type="channel",
            forward_chat="@some_channel",
            forward_message_id="456",
        )
    )

    lines = result.splitlines()
    assert lines[0].startswith(
        '<metadata type="supergroup" title="Fog &lt;Lab&gt;" '
        'timestamp="2026-07-06 20:10:00" user="Klee" username="@kc"'
    )
    assert 'message_id="1201"' in lines[0]
    assert (
        '<forward type="channel" chat="@some_channel" message_id="456" />' in lines[1]
    )
    assert "<message>hello &lt;Klee&gt; &amp; &quot;world&quot;</message>" in result


def test_context_tools_render_scheduled_task_with_utc_timestamps():
    scheduled_for = datetime(2026, 7, 10, 12, 30, tzinfo=timezone(timedelta(hours=8)))

    result = render_scheduled_task(
        ScheduledTaskContext(
            timestamp=datetime(2026, 7, 10, 4, 30, tzinfo=timezone.utc),
            scheduled_at=None,
            scheduled_for=scheduled_for,
            trigger_reason="check <in>",
            context_text="context & note",
            instruction="say hi",
        )
    )

    assert (
        '<metadata type="scheduler" timestamp="2026-07-10 04:30:00" '
        'origin="scheduled_task" scheduled_for="2026-07-10 04:30:00">'
    ) in result
    assert "<trigger>check &lt;in&gt;</trigger>" in result
    assert "<context>context &amp; note</context>" in result
    assert "<instruction>say hi</instruction>" in result


def test_context_tools_render_user_state_and_tool_context():
    user_state_prompt = render_user_state(
        UserState(
            coins=7,
            plan="paid",
            permission=2,
            profile=_profile(),
            personal_info="Klee",
            diary_exists=True,
            user_id=42,
            username="klee",
            display_name="Klee",
        )
    )

    tool_context = build_tool_context(
        ConversationScope(user_id=42, is_group=True, group_id=-100, message_id=12),
    )

    assert (
        '<user_identity trust="trusted_platform_metadata" display_name="Klee" '
        'username="klee" user_id="42" />'
    ) in user_state_prompt
    assert '<user_state coins="7" user_plan="paid" permission="2"' in user_state_prompt
    assert 'permission_label="Premium"' in user_state_prompt
    assert 'diary_exists="true"' in user_state_prompt
    assert tool_context == {
        "is_group": True,
        "group_id": -100,
        "message_id": 12,
        "message_thread_id": None,
        "user_id": 42,
    }


def test_context_state_builds_model_messages_with_runtime_replacements() -> None:
    """@brief 用 canonical V2 替换持久化图片占位文本 / Replace persisted image placeholder with canonical V2.

    @return None / None.
    """

    persisted_content = "<message>[photo]</message>"
    persisted_message = text_message(MessageRole.USER, persisted_content)
    runtime_message = CanonicalMessage(
        MessageRole.USER,
        (
            TextPart(persisted_content),
            ImagePart(UrlImageSource("data:image/jpeg;base64,abc")),
        ),
    )
    history = [
        text_message(MessageRole.USER, "older"),
        persisted_message,
    ]

    user_state = UserState(
        coins=7,
        plan="paid",
        permission=2,
        user_id=42,
        username="klee",
        display_name="Klee",
    )
    context_state = build_context_state(
        context_id=UUID("00000000-0000-4000-8000-000000000042"),
        system_prompt="base system policy",
        history_messages=history,
        scope=ConversationScope(
            user_id=42, is_group=True, group_id=-100, message_id=12
        ),
        user_state=user_state,
        runtime_replacements=[
            RuntimeMessageReplacement(
                persisted_message=persisted_message,
                runtime_message=runtime_message,
            )
        ],
        text_fallback_messages=history,
    )

    assert context_state.messages == (
        text_message(
            MessageRole.SYSTEM,
            "base system policy\n\n"
            '<conversation_scope kind="group" shared="true" group_id="-100" '
            'thread_id="0" current_user_id="42" />\n\n'
            '<user_identity trust="trusted_platform_metadata" display_name="Klee" '
            'username="klee" user_id="42" />\n'
            '<user_state coins="7" user_plan="paid" permission="2" '
            'permission_label="Premium" diary_exists="false" />',
        ),
        text_message(MessageRole.USER, "older"),
        runtime_message,
    )
    assert context_state.text_fallback_messages == (
        context_state.messages[0],
        *history,
    )
    assert context_state.tool_context == {
        "is_group": True,
        "group_id": -100,
        "message_id": 12,
        "message_thread_id": None,
        "user_id": 42,
    }
    assert context_state.scope.user_id == 42
    assert context_state.user_state is user_state


def test_group_context_rejects_private_profile_state() -> None:
    """@brief 群 Context 不能携带私人画像状态 / Group Context cannot carry private profile state."""

    with pytest.raises(ValueError, match="cannot contain private User Profile"):
        build_context_state(
            context_id=UUID("00000000-0000-4000-8000-000000000043"),
            system_prompt="base",
            history_messages=(),
            scope=ConversationScope(user_id=42, is_group=True, group_id=-100),
            user_state=UserState(
                coins=7,
                plan="paid",
                permission=2,
                profile=_profile(),
            ),
        )


def test_context_state_exposes_immutable_views_and_named_history_transitions() -> None:
    """@brief Context 聚合封闭可变状态并以具名动作演进 /
    The Context aggregate closes mutable state and evolves through named operations.

    @return None / None.
    """

    with pytest.raises(TypeError, match=r"ContextState\.create"):
        ContextState()

    original = build_context_state(
        context_id=UUID("00000000-0000-4000-8000-000000000044"),
        system_prompt="base",
        history_messages=(text_message(MessageRole.USER, "question"),),
        scope=ConversationScope(user_id=42),
        user_state=UserState(coins=7, plan="paid", permission=2),
    )
    original.define_stable_prefix(1)
    branch = original.fork_for_route()
    branch.record_agent_history(
        (*branch.messages, text_message(MessageRole.ASSISTANT, "answer"))
    )

    assert isinstance(original.messages, tuple)
    assert len(original.messages) == 2
    assert len(branch.messages) == 3
    with pytest.raises(TypeError):
        original.tool_context["user_id"] = 7  # type: ignore[index]
    with pytest.raises(ValueError, match="within messages"):
        original.select_model_messages(())
    assert len(original.messages) == 2

    original.adopt_route_history(branch)
    assert original.messages[-1].text == "answer"

    another = build_context_state(
        context_id=UUID("00000000-0000-4000-8000-000000000045"),
        system_prompt="base",
        history_messages=(),
        scope=ConversationScope(user_id=42),
        user_state=original.user_state,
    )
    with pytest.raises(ValueError, match="another ContextState"):
        original.adopt_route_history(another)


def test_context_tools_ignore_empty_runtime_replacement() -> None:
    """@brief 空运行时替换不会制造无效 canonical 消息 / Empty replacement creates no invalid canonical message.

    @return None / None.
    """

    assert (
        create_runtime_replacement(
            persisted_message=text_message(MessageRole.USER, "persisted"),
            runtime_message=None,
        )
        is None
    )


def test_user_state_normalizers_keep_prompt_inputs_bounded():
    assert normalize_personal_info("x" * 600) == "x" * 500
