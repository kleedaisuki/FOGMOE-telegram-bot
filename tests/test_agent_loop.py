from copy import deepcopy

from fogmoe_bot.application.assistant import agent_loop
from fogmoe_bot.domain.context import ContextState, ConversationScope, UserState


class _Message:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, message):
        self.message = message


class _Response:
    def __init__(self, message):
        self.choices = [_Choice(message)]


def _context(messages):
    return ContextState(
        scope=ConversationScope(user_id=42),
        user_state=UserState(coins=10, plan="free", permission=0, impression="Not recorded"),
        messages=messages,
        tool_context={"user_id": 42, "is_group": False, "group_id": None, "message_id": None},
    )


def test_agent_execution_state_separates_context_from_configuration():
    context = _context([{"role": "user", "content": "search example"}])
    config = agent_loop.AgentExecutionConfig(
        provider="test_provider",
        model="test_model",
        provider_name="Test",
        skip_tools=frozenset({"generate_image"}),
        completion_kwargs={"temperature": 0.2},
    )
    state = agent_loop.AgentExecutionState.from_context(context, config)
    state.events.append({"type": "tool_result", "tool_name": "google_search", "result": {"count": 1}})
    state.iteration = 2

    assert state.context is context
    assert state.config is config
    assert state.messages == [{"role": "user", "content": "search example"}]
    assert state.events[0]["tool_name"] == "google_search"
    assert state.iteration == 2


def test_agent_loop_does_not_synthesize_tool_result_reply(monkeypatch):
    responses = [
        _Response(
            _Message(
                "",
                [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "google_search",
                            "arguments": '{"query": "example"}',
                        },
                    }
                ],
            )
        ),
        _Response(_Message("", None)),
    ]

    calls = []

    def fake_create_chat_completion(*args, **kwargs):
        calls.append(deepcopy(kwargs))
        return responses.pop(0)

    monkeypatch.setitem(
        agent_loop.DEFAULT_AGENT_RUNTIME.handlers,
        "google_search",
        lambda **kwargs: {
            "organic_results": [
                {
                    "title": "Example result",
                    "link": "https://example.test",
                    "snippet": "Example snippet",
                }
            ]
        },
    )

    loop = agent_loop.AgentLoop(
        runtime=agent_loop.DEFAULT_AGENT_RUNTIME,
        completion_client=fake_create_chat_completion,
    )
    message, tool_logs = loop.run(
        _context([{"role": "user", "content": "search example"}]),
        agent_loop.AgentExecutionConfig(
            provider="test_provider",
            model="test_model",
            provider_name="Test",
        )
    )

    assert message == ""
    assert any(
        log.get("type") == "tool_result"
        and log.get("tool_name") == "google_search"
        for log in tool_logs
    )
    assert calls[0]["messages"] == [{"role": "user", "content": "search example"}]


def test_agent_loop_generates_final_reply_after_tool_limit(monkeypatch):
    responses = [
        _Response(
            _Message(
                "",
                [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "google_search",
                            "arguments": '{"query": "example"}',
                        },
                    }
                ],
            )
        ),
        _Response(_Message("根据已有搜索结果，Example result 是相关结果。", None)),
    ]
    calls = []

    def fake_create_chat_completion(*args, **kwargs):
        calls.append(kwargs)
        return responses.pop(0)

    monkeypatch.setitem(
        agent_loop.DEFAULT_AGENT_RUNTIME.handlers,
        "google_search",
        lambda **kwargs: {
            "organic_results": [
                {
                    "title": "Example result",
                    "link": "https://example.test",
                    "snippet": "Example snippet",
                }
            ]
        },
    )

    loop = agent_loop.AgentLoop(
        runtime=agent_loop.DEFAULT_AGENT_RUNTIME,
        completion_client=fake_create_chat_completion,
    )
    message, tool_logs = loop.run(
        _context([
            {
                "role": "system",
                "content": "at most 10 tool-calling rounds",
            },
            {"role": "user", "content": "search example"},
        ]),
        agent_loop.AgentExecutionConfig(
            provider="test_provider",
            model="test_model",
            provider_name="Test",
            max_iterations=1,
        )
    )

    assert message == "根据已有搜索结果，Example result 是相关结果。"
    assert "抱歉，处理您的请求时遇到了问题" not in message
    assert len(calls) == 2
    assert "tools" in calls[0]
    assert "tool_choice" in calls[0]
    assert "tools" not in calls[1]
    assert "tool_choice" not in calls[1]
    assert "Tool calling has reached the maximum allowed iterations" not in calls[1]["messages"][0]["content"]
    assert "at most 10 tool-calling rounds" in calls[1]["messages"][0]["content"]
    assert any(message["role"] == "tool" for message in calls[1]["messages"])
    assert any(
        log.get("type") == "tool_result"
        and log.get("tool_name") == "google_search"
        for log in tool_logs
    )


def test_agent_loop_sends_generated_voice_immediately(monkeypatch):
    responses = [
        _Response(
            _Message(
                "",
                [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "generate_voice",
                            "arguments": '{"text": "hello"}',
                        },
                    }
                ],
            )
        ),
        _Response(_Message("", None)),
    ]

    def fake_create_chat_completion(*args, **kwargs):
        return responses.pop(0)

    class _VisibleHandler:
        def __init__(self):
            self.calls = []

        def send_media(self, tool_name, result):
            self.calls.append((tool_name, result))
            return ["sent_message"]

    visible_handler = _VisibleHandler()
    monkeypatch.setitem(
        agent_loop.DEFAULT_AGENT_RUNTIME.handlers,
        "generate_voice",
        lambda **kwargs: {
            "status": "generated",
            "count": 1,
            "audios": [{"audio_id": "secret-audio-id"}],
        },
    )

    loop = agent_loop.AgentLoop(
        runtime=agent_loop.DEFAULT_AGENT_RUNTIME,
        completion_client=fake_create_chat_completion,
    )
    message, tool_logs = loop.run(
        _context([{"role": "user", "content": "say hello"}]),
        agent_loop.AgentExecutionConfig(
            provider="test_provider",
            model="test_model",
            provider_name="Test",
        ),
        visible_content_handler=visible_handler,
    )

    voice_results = [
        log
        for log in tool_logs
        if log.get("type") == "tool_result"
        and log.get("tool_name") == "generate_voice"
    ]

    assert message == ""
    assert visible_handler.calls == [
        (
            "generate_voice",
            {
                "status": "generated",
                "count": 1,
                "audios": [{"audio_id": "secret-audio-id"}],
            },
        )
    ]
    assert voice_results[0]["media_sent"] is True
    assert voice_results[0]["sent_message_count"] == 1
    assert voice_results[0]["result"]["message"] == "Generated audio has been sent to Telegram."
    assert "forward" not in str(voice_results[0]["result"]).lower()
