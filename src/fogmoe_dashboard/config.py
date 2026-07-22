"""@brief Dashboard 配置输入边界 / Configuration input boundary for the Dashboard.

Dashboard 只读取报表访问与观测查询相关字段；它不复用 Bot 或 dbctl 的配置对象，
也不通过环境变量或 libpq service 文件取得凭据。
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from math import isfinite
from pathlib import Path
from typing import Annotated, Final, Literal, Never, Self, cast
from urllib.parse import quote_plus

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    model_validator,
)


type JSONValue = (
    None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]
)
"""@brief JSONC 可表示的递归值 / Recursive value representable by JSONC."""
#: @brief 当前支持的根配置契约版本 / Root configuration contract version supported by this package.
SCHEMA_VERSION: Final[int] = 1


class JsoncDecodeError(ValueError):
    """@brief JSONC 文档无效 / JSONC document is invalid."""


def _load_jsonc(path: Path) -> dict[str, JSONValue]:
    """@brief 读取 Dashboard 本地 JSONC 文档 / Read the Dashboard-local JSONC document.

    @param path 配置文件路径 / Configuration-file path.
    @return 严格 JSON 顶层对象 / Strict JSON top-level object.
    @raise JsoncDecodeError 文件无法读取或格式无效时抛出 /
        Raised when the file cannot be read or its format is invalid.
    """

    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise JsoncDecodeError(f"cannot read JSONC file {path}: {error}") from error
    try:
        return _parse_jsonc(source)
    except JsoncDecodeError as error:
        raise JsoncDecodeError(f"invalid JSONC file {path}: {error}") from error


def _parse_jsonc(source: str) -> dict[str, JSONValue]:
    """@brief 解析 Dashboard 的严格 JSONC / Parse Dashboard's strict JSONC.

    @param source 已解码 JSONC 文本 / Decoded JSONC text.
    @return 严格 JSON 顶层对象 / Strict JSON top-level object.
    @raise JsoncDecodeError 注释或 JSON 语法无效时抛出 /
        Raised for invalid comments or JSON syntax.
    @note 仅接受 ``//``、``/* ... */`` 与标准 JSON；不接受 JSON5 扩展。/
        Only comments plus standard JSON are accepted; JSON5 extensions are rejected.
    """

    try:
        value = cast(
            JSONValue,
            json.loads(
                _strip_jsonc_comments(source),
                object_pairs_hook=_object_without_duplicate_keys,
                parse_constant=_reject_non_json_number,
                parse_float=_parse_finite_json_float,
            ),
        )
    except JsoncDecodeError:
        raise
    except json.JSONDecodeError as error:
        raise JsoncDecodeError(
            f"invalid JSON at line {error.lineno}, column {error.colno}: {error.msg}"
        ) from error
    if not isinstance(value, dict):
        raise JsoncDecodeError("the top-level JSONC value must be an object")
    return value


def _strip_jsonc_comments(source: str) -> str:
    """@brief 用本地状态机替换 JSONC 注释 / Replace JSONC comments with a local state machine.

    @param source 原始 JSONC 文本 / Raw JSONC text.
    @return 位置不变的严格 JSON 文本 / Strict JSON text with unchanged positions.
    @raise JsoncDecodeError 块注释未闭合时抛出 / Raised when a block comment is unterminated.
    """

    characters = list(source)
    state: Literal["normal", "string", "escape", "line", "block"] = "normal"
    block_start: int | None = None
    index = 0
    while index < len(characters):
        character = characters[index]
        following = characters[index + 1] if index + 1 < len(characters) else ""
        if state == "string":
            state = (
                "escape"
                if character == "\\"
                else "normal"
                if character == '"'
                else "string"
            )
        elif state == "escape":
            state = "string"
        elif state == "line":
            if character in "\r\n":
                state = "normal"
            else:
                characters[index] = " "
        elif state == "block":
            if character == "*" and following == "/":
                characters[index] = " "
                characters[index + 1] = " "
                state = "normal"
                index += 1
            elif character not in "\r\n":
                characters[index] = " "
        elif character == '"':
            state = "string"
        elif character == "/" and following == "/":
            characters[index] = " "
            characters[index + 1] = " "
            state = "line"
            index += 1
        elif character == "/" and following == "*":
            characters[index] = " "
            characters[index + 1] = " "
            block_start = index
            state = "block"
            index += 1
        index += 1
    if state == "block":
        assert block_start is not None
        line = source.count("\n", 0, block_start) + 1
        column = block_start - source.rfind("\n", 0, block_start)
        raise JsoncDecodeError(
            f"unterminated block comment at line {line}, column {column}"
        )
    return "".join(characters)


def _object_without_duplicate_keys(
    pairs: list[tuple[str, JSONValue]],
) -> dict[str, JSONValue]:
    """@brief 构造无重复键对象 / Build an object without duplicate keys.

    @param pairs JSON 成员对 / JSON member pairs.
    @return 键唯一对象 / Object with unique keys.
    @raise JsoncDecodeError 存在重复键时抛出 / Raised when a duplicate key exists.
    """

    result: dict[str, JSONValue] = {}
    for key, value in pairs:
        if key in result:
            raise JsoncDecodeError(f"duplicate object key {key!r}")
        result[key] = value
    return result


def _reject_non_json_number(token: str) -> Never:
    """@brief 拒绝非 JSON 数值常量 / Reject non-JSON numeric constants.

    @param token 非标准 token / Non-standard token.
    @raise JsoncDecodeError 始终抛出 / Always raised.
    """

    raise JsoncDecodeError(
        f"non-standard JSON numeric constant {token!r} is not allowed"
    )


def _parse_finite_json_float(token: str) -> float:
    """@brief 解析且限制有限 JSON 浮点数 / Parse and require a finite JSON float.

    @param token JSON 数字 token / JSON numeric token.
    @return 有限浮点数 / Finite floating-point value.
    @raise JsoncDecodeError 指数溢出为无穷大时抛出 /
        Raised when an exponent overflows to infinity.
    @note ``json.loads`` 会把合法词法形式 ``1e999`` 转成 ``inf``；Dashboard 的
        查询预算不能接受该非有限结果。/
        ``json.loads`` turns lexically valid ``1e999`` into ``inf``; Dashboard query
        budgets must not accept that non-finite result.
    """

    value = float(token)
    if not isfinite(value):
        raise JsoncDecodeError(
            f"non-finite JSON numeric value {token!r} is not allowed"
        )
    return value


#: @brief 正整数 Dashboard 配置 / Positive Dashboard configuration integer.
PositiveInt = Annotated[int, Field(gt=0)]
#: @brief 正浮点 Dashboard 配置 / Positive Dashboard floating-point configuration.
PositiveFloat = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class ConfigurationError(ValueError):
    """@brief Dashboard 配置语义错误 / Dashboard configuration semantic error."""


class _FrozenSettings(BaseModel):
    """@brief 严格不可变模型基类 / Base class for strict immutable models."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
        validate_default=True,
    )


class EndpointSettings(_FrozenSettings):
    """@brief PostgreSQL 报表端点 / PostgreSQL reporting endpoint."""

    host: str = "localhost"
    port: PositiveInt = 5432
    name: str = "fogmoe"


class ReportingDatabaseSettings(_FrozenSettings):
    """@brief 只读报表数据库身份 / Read-only reporting database identity."""

    username: str = "fogmoe-dashboard"
    password: SecretStr | None = None


class DashboardQuerySettings(_FrozenSettings):
    """@brief Dashboard 查询资源预算 / Dashboard query resource budget."""

    pool_size: PositiveInt = 4
    command_timeout_seconds: PositiveFloat = 5.0
    heartbeat_interval_seconds: PositiveFloat = 15.0
    resource_stale_after_seconds: PositiveFloat = 90.0

    @model_validator(mode="after")
    def _validate_resource_liveness_budget(self) -> Self:
        """@brief 为心跳抖动保留三个采样周期 / Reserve three sampling periods for heartbeat jitter.

        @return 验证后的不可变设置 / Validated immutable settings.
        @raise ValueError 失活阈值小于三个心跳周期时抛出 /
            Raised when the stale threshold is shorter than three heartbeat periods.
        """

        minimum_stale_after = 3 * self.heartbeat_interval_seconds
        if self.resource_stale_after_seconds < minimum_stale_after:
            raise ValueError(
                "resource_stale_after_seconds must be >= 3 * heartbeat_interval_seconds"
            )
        return self


class DashboardSettings(_FrozenSettings):
    """@brief Dashboard 所有者视角的配置投影 / Configuration projection owned by the Dashboard."""

    endpoint: EndpointSettings = Field(default_factory=EndpointSettings)
    reporting: ReportingDatabaseSettings = Field(
        default_factory=ReportingDatabaseSettings
    )
    query: DashboardQuerySettings = Field(default_factory=DashboardQuerySettings)

    def database_url(self) -> str:
        """@brief 构造 asyncpg SQLAlchemy URL / Build an asyncpg SQLAlchemy URL.

        @return 已转义的数据库 URL / Escaped database URL.
        @raise ConfigurationError 缺少报表密码时抛出 / Raised when the reporting password is absent.
        """

        if self.reporting.password is None:
            raise ConfigurationError("database.reporting.password is required")
        endpoint = self.endpoint
        user = quote_plus(self.reporting.username)
        password = quote_plus(self.reporting.password.get_secret_value())
        return (
            f"postgresql+asyncpg://{user}:{password}@"
            f"{endpoint.host}:{endpoint.port}/{endpoint.name}"
        )


def default_config_path() -> Path:
    """@brief 返回默认根配置路径 / Return the default root configuration path.

    @return 项目根目录中的 config.json / ``config.json`` in the project root.

    @note 控制台入口可从任意工作目录启动；不能让调用者当前目录（current
        working directory）改变默认的部署配置。通过 ``config.py`` 在 src-layout
        中的位置定位项目根目录。已安装到不含源码树的环境时，调用方应通过
        ``--config`` 显式提供路径。/
        Console entry points may start from any directory, so the caller's current
        working directory must not change the default deployment configuration. The
        project root is located from ``config.py`` in the src-layout. In an installed
        environment without the source tree, callers should pass ``--config``.
    """

    return Path(__file__).resolve().parents[2] / "config.json"


def read_dashboard_settings(path: Path | None = None) -> DashboardSettings:
    """@brief 从 JSONC 文档读取 Dashboard 配置 / Read the Dashboard configuration projection from JSONC.

    @param path 可选 config.json 路径 / Optional config.json path.
    @return 严格不可变的 Dashboard 设置 / Strict immutable Dashboard settings.
    @raise ConfigurationError JSONC 或 Dashboard 拥有字段无效时抛出 /
        Raised when JSONC or Dashboard-owned fields are invalid.
    """

    source_path = path or default_config_path()
    try:
        document = _load_jsonc(source_path)
        return DashboardSettings.model_validate(_dashboard_payload(document))
    except JsoncDecodeError as error:
        raise ConfigurationError(str(error)) from error
    except ValidationError as error:
        details = "; ".join(
            ".".join(str(part) for part in item["loc"]) + ": " + item["msg"]
            for item in error.errors(include_input=False)
        )
        raise ConfigurationError(
            f"{source_path}: invalid dashboard configuration: {details}"
        ) from error


def _dashboard_payload(document: Mapping[str, JSONValue]) -> dict[str, object]:
    """@brief 提取 Dashboard 所有的语义路径 / Extract semantic paths owned by the Dashboard.

    @param document 完整 JSONC 文档 / Complete JSONC document.
    @return Dashboard 模型验证输入 / Dashboard model-validation input.
    @raise ConfigurationError 必需字段不是对象时抛出 / Raised when a required field is not an object.
    """

    _require_schema_version(document)
    database = _object_at(document, "database")
    observability = _object_at(document, "observability")
    query = dict(_object_at(observability, "dashboard"))
    if "metric_interval_seconds" in observability:
        query["heartbeat_interval_seconds"] = observability["metric_interval_seconds"]
    return {
        "endpoint": _object_at(database, "endpoint"),
        "reporting": _object_at(database, "reporting"),
        "query": query,
    }


def _object_at(document: Mapping[str, JSONValue], key: str) -> Mapping[str, JSONValue]:
    """@brief 读取必需对象字段 / Read a required object field.

    @param document 父对象 / Parent object.
    @param key 字段名 / Field name.
    @return 对象字段 / Object field.
    @raise ConfigurationError 字段缺失或不是对象时抛出 /
        Raised when the field is missing or is not an object.
    """

    value = document.get(key)
    if not isinstance(value, dict):
        raise ConfigurationError(f"config field {key!r} must be an object")
    return value


def _require_schema_version(document: Mapping[str, JSONValue]) -> None:
    """@brief 验证根配置版本 / Validate the root configuration version.

    @param document 完整 JSONC 文档 / Complete JSONC document.
    @return None / None.
    @raise ConfigurationError 版本缺失或不受支持时抛出 /
        Raised when the version is missing or unsupported.
    """

    version = document.get("schema_version")
    if type(version) is not int or version != SCHEMA_VERSION:
        raise ConfigurationError(
            f"schema_version must be the supported integer {SCHEMA_VERSION}"
        )


__all__ = [
    "ConfigurationError",
    "DashboardQuerySettings",
    "DashboardSettings",
    "EndpointSettings",
    "ReportingDatabaseSettings",
    "SCHEMA_VERSION",
    "default_config_path",
    "read_dashboard_settings",
]
