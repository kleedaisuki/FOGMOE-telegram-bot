"""会话 JSON 载荷类型 / Conversation JSON payload types."""

type JsonScalar = None | bool | int | float | str
"""@brief JSON 标量值 / JSON scalar value."""

type JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
"""@brief 可持久化 JSON 值 / Persistable JSON value."""

type JsonObject = dict[str, JsonValue]
