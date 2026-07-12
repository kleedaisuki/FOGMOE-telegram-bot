from datetime import datetime, timezone, timedelta

from fogmoe_bot.application.conversation.assistant_ingress import (
    normalize_assistant_personal_info as normalize_personal_info,
    normalize_assistant_impression as normalize_user_impression,
)
from fogmoe_bot.domain.context import (
    ChatMessageContext,
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


def test_context_tools_render_chat_message_metadata_and_escape_content():
    result = render_chat_message(
        ChatMessageContext(
            chat_type="supergroup",
            chat_title="Fog <Lab>",
            timestamp="2026-07-06 20:10:00",
            user_name="kc",
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
        'timestamp="2026-07-06 20:10:00" user="@kc"'
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
            impression="likes CS",
            personal_info="Klee",
            diary_exists=True,
        )
    )

    tool_context = build_tool_context(
        ConversationScope(user_id=42, is_group=True, group_id=-100, message_id=12),
    )

    assert '<user_state coins="7" user_plan="paid" permission="2"' in user_state_prompt
    assert 'permission_label="Premium"' in user_state_prompt
    assert 'diary_exists="true"' in user_state_prompt
    assert tool_context == {
        "is_group": True,
        "group_id": -100,
        "message_id": 12,
        "user_id": 42,
    }


def test_context_state_builds_model_messages_with_runtime_replacements():
    persisted_content = "<message>[photo]</message>"
    runtime_message = {
        "role": "user",
        "content": [
            {"type": "text", "text": "<message>[photo]</message>"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,abc"}},
        ],
    }
    history = [
        {"role": "user", "content": "older"},
        {"role": "user", "content": persisted_content},
    ]

    user_state = UserState(
        coins=7,
        plan="paid",
        permission=2,
        impression="likes CS",
    )
    context_state = build_context_state(
        system_prompt="base system policy",
        history_messages=history,
        scope=ConversationScope(
            user_id=42, is_group=True, group_id=-100, message_id=12
        ),
        user_state=user_state,
        runtime_replacements=[
            RuntimeMessageReplacement(
                persisted_content=persisted_content,
                runtime_message=runtime_message,
            )
        ],
        text_fallback_messages=history,
    )

    assert context_state.messages == [
        {
            "role": "system",
            "content": "base system policy\n\n"
            '<user_state coins="7" user_plan="paid" permission="2" '
            'permission_label="Premium" diary_exists="false" />\n'
            "<user_profile>\n  <impression>likes CS</impression>\n</user_profile>",
        },
        {"role": "user", "content": "older"},
        runtime_message,
    ]
    assert context_state.text_fallback_messages == [
        context_state.messages[0],
        *history,
    ]
    assert context_state.tool_context == {
        "is_group": True,
        "group_id": -100,
        "message_id": 12,
        "user_id": 42,
    }
    assert context_state.scope.user_id == 42
    assert context_state.user_state is user_state


def test_context_tools_ignore_empty_runtime_replacement():
    assert (
        create_runtime_replacement(
            persisted_content="persisted",
            runtime_message=None,
        )
        is None
    )


def test_user_state_normalizers_keep_prompt_inputs_bounded():
    long_impression = "a\n" + ("b" * 600)
    assert normalize_user_impression(None) == "Not recorded"
    assert "\n" not in normalize_user_impression(long_impression)
    assert len(normalize_user_impression(long_impression)) == 500

    assert normalize_personal_info("x" * 600) == "x" * 500
