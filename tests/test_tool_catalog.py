"""@brief 纯 typed tool catalog 测试 / Tests for the pure typed tool catalog."""

from collections.abc import Mapping

import pytest
from pydantic import Field

from fogmoe_bot.application.assistant.tools.catalog import (
    DEFAULT_TOOL_CATALOG,
    DuplicateToolNameError,
    InvalidToolArguments,
    ToolArguments,
    ToolCatalog,
    ToolDefinition,
    UnknownTool,
    ValidatedToolInvocation,
    define_tool,
)
from fogmoe_bot.infrastructure.llm.tool_serialization import serialize_tool_definition


class _EchoArgs(ToolArguments):
    """@brief 测试参数 / Test arguments."""

    value: str = Field(min_length=2, max_length=8)


def _definition(name: str = "echo") -> ToolDefinition:
    """@brief 构造无 handler 定义 / Build a handler-free definition."""

    return define_tool(
        name=name, description="Echo one value", arguments_model=_EchoArgs
    )


def test_schema_is_generated_and_catalog_executes_no_handler() -> None:
    """@brief Catalog 只校验与分类 / The catalog only validates and classifies."""

    definition = _definition()
    schema = definition.parameters_schema
    properties = schema["properties"]
    assert isinstance(properties, Mapping)
    value_schema = properties["value"]
    assert isinstance(value_schema, Mapping)
    assert value_schema["minLength"] == 2
    serialized = serialize_tool_definition(definition)
    function = serialized["function"]
    assert isinstance(function, Mapping)
    assert function["name"] == "echo"
    result = ToolCatalog((definition,)).validate("echo", {"value": "Klee"})
    assert isinstance(result, ValidatedToolInvocation)
    assert result.mutating is False
    assert result.effect_kind == "read.echo"


def test_catalog_returns_typed_unknown_validation_and_duplicate_results() -> None:
    """@brief Unknown、invalid 与 duplicate 都显式类型化 / Unknown, invalid, and duplicate cases are typed."""

    catalog = ToolCatalog((_definition(),))
    assert isinstance(catalog.validate("missing", {}), UnknownTool)
    invalid = catalog.validate("echo", {"value": "x", "extra": True})
    assert isinstance(invalid, InvalidToolArguments)
    assert {issue.field for issue in invalid.issues} == {"value", "extra"}
    with pytest.raises(DuplicateToolNameError):
        ToolCatalog((_definition(), _definition()))


def test_default_catalog_excludes_stateful_sandbox_and_classifies_action_tools() -> (
    None
):
    """@brief 默认目录不暴露 sessionful sandbox / Default catalog does not expose a sessionful sandbox."""

    names = [definition.name for definition in DEFAULT_TOOL_CATALOG]
    assert "linux_sandbox" not in names
    assert "fetch_group_context" in names
    group_context = DEFAULT_TOOL_CATALOG.validate(
        "fetch_group_context", {"window_size": 512}
    )
    assert isinstance(group_context, ValidatedToolInvocation)
    assert not group_context.mutating
    group_default = DEFAULT_TOOL_CATALOG.validate("fetch_group_context", {})
    assert isinstance(group_default, ValidatedToolInvocation)
    assert group_default.arguments.model_dump()["window_size"] == 256
    memory_default = DEFAULT_TOOL_CATALOG.validate(
        "search_memory", {"query": "old discussion"}
    )
    assert isinstance(memory_default, ValidatedToolInvocation)
    assert memory_default.arguments.model_dump()["limit"] == 64
    assert isinstance(
        DEFAULT_TOOL_CATALOG.validate(
            "search_memory", {"query": "old discussion", "limit": 128}
        ),
        ValidatedToolInvocation,
    )
    assert isinstance(
        DEFAULT_TOOL_CATALOG.validate(
            "search_memory", {"query": "old discussion", "limit": 129}
        ),
        InvalidToolArguments,
    )
    diary_read = DEFAULT_TOOL_CATALOG.validate("user_diary", {"action": "read"})
    diary_write = DEFAULT_TOOL_CATALOG.validate(
        "user_diary",
        {"action": "append", "content": "note"},
    )
    assert isinstance(diary_read, ValidatedToolInvocation) and not diary_read.mutating
    assert isinstance(diary_write, ValidatedToolInvocation) and diary_write.mutating
    assert diary_write.effect_kind == "diary.append"
    schedule_list = DEFAULT_TOOL_CATALOG.validate(
        "schedule_ai_message", {"action": "list"}
    )
    schedule_create = DEFAULT_TOOL_CATALOG.validate(
        "schedule_ai_message",
        {
            "action": "create",
            "cadence": {
                "kind": "calendar_daily",
                "first_at": "2030-01-01T09:00:00",
                "every_days": 1,
            },
            "timezone": "Asia/Shanghai",
            "trigger_reason": "test",
            "instruction": "hello",
        },
    )
    assert isinstance(schedule_list, ValidatedToolInvocation)
    assert schedule_list.mutating is False
    assert isinstance(schedule_create, ValidatedToolInvocation)
    assert schedule_create.mutating is True
    assert schedule_create.effect_kind == "schedule.create"
    schedule_update = DEFAULT_TOOL_CATALOG.validate(
        "schedule_ai_message",
        {
            "action": "update",
            "schedule_id": 7,
            "cadence": {
                "kind": "fixed_interval",
                "first_at": "2030-01-01T00:00:00Z",
                "every_seconds": 3600,
            },
            "trigger_reason": "updated test",
            "instruction": "hello later",
        },
    )
    assert isinstance(schedule_update, ValidatedToolInvocation)
    assert schedule_update.effect_kind == "schedule.update"
    assert isinstance(
        DEFAULT_TOOL_CATALOG.validate(
            "schedule_ai_message",
            {
                "action": "create",
                "cadence": {
                    "kind": "calendar_weekly",
                    "first_at": "2030-01-01T09:00:00",
                    "weekdays": [1, 1],
                },
                "trigger_reason": "duplicate weekday",
                "instruction": "must fail",
            },
        ),
        InvalidToolArguments,
    )
    media = DEFAULT_TOOL_CATALOG.validate("generate_image", {"prompt": "Klee"})
    assert isinstance(media, ValidatedToolInvocation)
    assert media.mutating is True
    assert media.effect_kind == "media.generate_image"


def test_send_sticker_is_a_bounded_mutation_without_file_id_escape_hatch() -> None:
    """@brief sticker tool 只接受有界 pack/emoji 语义 / The sticker tool accepts only bounded pack-and-emoji semantics."""

    invocation = DEFAULT_TOOL_CATALOG.validate(
        "send_sticker",
        {"pack_name": "BanG_Dream_Its_MyGO", "emoji": "👍"},
    )
    assert isinstance(invocation, ValidatedToolInvocation)
    assert invocation.mutating is True
    assert invocation.effect_kind == "telegram.send_sticker"

    arbitrary_file = DEFAULT_TOOL_CATALOG.validate(
        "send_sticker",
        {
            "pack_name": "WhiteWind",
            "emoji": "😊",
            "file_id": "attacker-controlled",
        },
    )
    assert isinstance(arbitrary_file, InvalidToolArguments)
    assert {issue.field for issue in arbitrary_file.issues} == {"file_id"}

    assert isinstance(
        DEFAULT_TOOL_CATALOG.validate(
            "send_sticker",
            {"pack_name": "../unsafe", "emoji": "😊"},
        ),
        InvalidToolArguments,
    )
    assert isinstance(
        DEFAULT_TOOL_CATALOG.validate(
            "send_sticker",
            {"pack_name": "WhiteWind", "emoji": " "},
        ),
        InvalidToolArguments,
    )
