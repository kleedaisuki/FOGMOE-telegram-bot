import asyncio

from fogmoe_bot.application.assistant.agent_loop import AgentResponse
from fogmoe_bot.application.assistant.inference.service import AssistantInferenceService
from fogmoe_bot.domain.context import ContextState, ConversationScope, UserState
from fogmoe_bot.domain.agent_routing import ProviderCircuit, ProviderRoute


def _route(
    service_name: str,
    *,
    model: str = "model",
) -> ProviderRoute:
    return ProviderRoute(
        service_name=service_name,
        provider_name=service_name,
        display_name=service_name,
        models=(model,),
        completion_kwargs={},
    )


def _service(*, order, profiles, runner, text_only_patterns=()):
    class _AgentLoop:
        def run(self, context, config, *, visible_content_handler=None):
            return runner(
                config.provider,
                config.model,
                context.messages,
                provider_name=config.provider_name,
                skip_tools=config.skip_tools,
                completion_kwargs=config.completion_kwargs,
                visible_content_handler=visible_content_handler,
            )

    return AssistantInferenceService(
        service_order=order,
        profiles=profiles,
        circuit=ProviderCircuit(
            failure_threshold=3,
            window_seconds=300,
            cooldown_seconds=1800,
        ),
        text_only_model_patterns=text_only_patterns,
        agent_loop=_AgentLoop(),
    )


def _context(messages, *, text_fallback_messages=None):
    return ContextState(
        scope=ConversationScope(user_id=123),
        user_state=UserState(coins=10, plan="free", permission=0, impression="Not recorded"),
        messages=messages,
        tool_context={"user_id": 123, "is_group": False, "group_id": None, "message_id": None},
        text_fallback_messages=text_fallback_messages,
    )


def test_inference_retries_image_messages_as_text_after_all_routes_fail():
    image_messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe this image"},
                {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}},
            ],
        }
    ]
    calls = []

    def runner(provider, model, messages, **kwargs):
        calls.append(messages)
        if len(calls) == 1:
            raise RuntimeError("provider failed")
        return AgentResponse("text fallback response", [])

    service = _service(order=("openai",), profiles={"openai": _route("openai")}, runner=runner)

    response = asyncio.run(service.infer(_context(image_messages)))

    assert response == ("text fallback response", [])
    assert calls == [
        image_messages,
        [{"role": "user", "content": "describe this image"}],
    ]


def test_text_only_route_uses_vision_text_fallback_messages():
    image_messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "runtime message without description"},
                {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}},
            ],
        }
    ]
    text_fallback_messages = [{"role": "user", "content": "a cat on a desk"}]
    calls = []

    def runner(provider, model, messages, **kwargs):
        calls.append(messages)
        return AgentResponse("ok", [])

    service = _service(
        order=("siliconflow",),
        profiles={"siliconflow": _route("siliconflow", model="vendor/text-small")},
        runner=runner,
        text_only_patterns=("vendor/text-*",),
    )

    assert asyncio.run(
        service.infer(_context(image_messages, text_fallback_messages=text_fallback_messages))
    ) == ("ok", [])
    assert calls == [text_fallback_messages]


def test_vision_capable_route_keeps_multimodal_messages():
    image_messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.test/a.png"}}]}]
    calls = []

    def runner(provider, model, messages, **kwargs):
        calls.append(messages)
        return AgentResponse("ok", [])

    service = _service(order=("openai",), profiles={"openai": _route("openai", model="gpt-4o")}, runner=runner)

    assert asyncio.run(service.infer(_context(image_messages))) == ("ok", [])
    assert calls == [image_messages]


def test_provider_circuit_opens_after_three_failures_and_resets_on_success():
    circuit = ProviderCircuit(failure_threshold=3, window_seconds=300, cooldown_seconds=1800)
    circuit.record_failure("gemini", now=100.0)
    circuit.record_failure("gemini", now=200.0)
    circuit.record_success("gemini")
    circuit.record_failure("gemini", now=300.0)

    assert circuit.is_open("gemini", now=300.0) is False
    assert circuit.failure_streaks["gemini"] == [300.0]

    circuit.record_failure("gemini", now=301.0)
    circuit.record_failure("gemini", now=302.0)

    assert circuit.is_open("gemini", now=302.0) is True
    assert circuit.is_open("gemini", now=2102.0) is False


def test_open_circuit_skips_to_next_route(monkeypatch):
    calls = []

    def runner(provider, model, messages, **kwargs):
        calls.append(provider)
        return AgentResponse("ok", [])

    service = _service(
        order=("gemini", "siliconflow"),
        profiles={"gemini": _route("gemini"), "siliconflow": _route("siliconflow")},
        runner=runner,
    )
    monkeypatch.setattr(service.circuit, "is_open", lambda name: name == "gemini")

    assert asyncio.run(service.infer(_context([]))) == ("ok", [])
    assert calls == ["siliconflow"]
