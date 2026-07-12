"""@brief LLM provider 工具定义序列化边界 / LLM-provider tool-definition serialization boundary.

应用层只持有 provider-neutral 的不可变 ``ToolDefinition``。本模块是把它转换成
OpenAI-compatible 字典的唯一位置，防止第三方协议结构反向渗入工具目录。
/ The application layer owns only provider-neutral immutable ``ToolDefinition``
objects. This module is the sole boundary converting them into OpenAI-compatible
dictionaries, preventing a third-party protocol shape from leaking into the catalog.
"""

from collections.abc import Mapping, Sequence

from fogmoe_bot.application.assistant.tools.catalog import (
    FrozenSchemaValue,
    ToolDefinition,
)

from .protocol import JsonValue, ProviderPayload


def _schema_to_json(value: FrozenSchemaValue) -> JsonValue:
    """@brief 解冻 schema 节点 / Thaw one schema node.

    @param value 深度不可变 schema 值 / Deeply immutable schema value.
    @return JSON 安全的独立副本 / Independent JSON-safe copy.
    """

    if isinstance(value, Mapping):
        return {str(key): _schema_to_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_schema_to_json(item) for item in value]
    return value


def serialize_tool_definition(definition: ToolDefinition) -> ProviderPayload:
    """@brief 序列化一个工具定义 / Serialize one tool definition.

    @param definition 应用层权威定义 / Authoritative application definition.
    @return OpenAI-compatible function-tool 字典 / OpenAI-compatible function-tool dictionary.
    """

    parameters = _schema_to_json(definition.parameters_schema)
    if not isinstance(parameters, dict):
        raise TypeError("Tool parameters schema must serialize to an object")
    return {
        "type": "function",
        "function": {
            "name": definition.name,
            "description": definition.description,
            "parameters": parameters,
        },
    }


def serialize_tool_definitions(
    definitions: Sequence[ToolDefinition],
) -> list[ProviderPayload]:
    """@brief 保序序列化工具目录 / Serialize tool definitions in order.

    @param definitions provider-neutral 定义序列 / Provider-neutral definition sequence.
    @return 新建的 provider 字典列表 / Fresh list of provider dictionaries.
    """

    return [serialize_tool_definition(definition) for definition in definitions]


__all__ = ["serialize_tool_definition", "serialize_tool_definitions"]
