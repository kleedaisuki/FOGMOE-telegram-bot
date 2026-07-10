from fogmoe_bot.domain.agent_runtime.runtime import AgentRuntime, ToolTask


def test_runtime_accepts_task_and_returns_public_result():
    runtime = AgentRuntime(
        tool_definitions=[],
        arg_models={},
        handlers={"echo": lambda value: {"value": value}},
    )

    handle = runtime.submit(
        ToolTask(
            name="echo",
            arguments={"value": "klee"},
            invocation_id="call_1",
            producer_name="test",
        )
    )
    result = runtime.consume(handle)

    assert result.task_id == handle.task_id
    assert result.invocation_id == "call_1"
    assert result.public_result == {"value": "klee"}
    assert result.internal_result == {"value": "klee"}


def test_runtime_projects_generated_media_without_exposing_artifact_id():
    runtime = AgentRuntime(
        tool_definitions=[],
        arg_models={},
        handlers={
            "generate_voice": lambda: {
                "status": "generated",
                "audios": [{"audio_id": "private-artifact"}],
            }
        },
    )

    result = runtime.consume(
        runtime.submit(
            ToolTask(
                name="generate_voice",
                arguments={},
                invocation_id="call_2",
                producer_name="test",
            )
        )
    )

    assert result.public_result["status"] == "generated"
    assert "private-artifact" not in str(result.public_result)
    assert result.internal_result["audios"][0]["audio_id"] == "private-artifact"
