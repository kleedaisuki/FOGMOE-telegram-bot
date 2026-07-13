"""@brief 数据库控制面配置输入边界 / Configuration input boundary for the database control plane.

本模块只读取控制面拥有的数据库职责：维护、迁移与 bootstrap。它从同一份用户
语义化 ``config.json`` 取得字段，但不依赖 Bot 或 Dashboard 的配置服务。
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from math import isfinite
from pathlib import Path
from typing import Annotated, Final, Literal, Never, cast

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError


type JSONValue = (
    None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]
)
"""@brief JSONC 可表示的递归值 / Recursive value representable by JSONC."""
#: @brief 当前支持的根配置契约版本 / Root configuration contract version supported by this package.
SCHEMA_VERSION: Final[int] = 1


class JsoncDecodeError(ValueError):
    """@brief JSONC 文档无效 / JSONC document is invalid."""


def _load_jsonc(path: Path) -> dict[str, JSONValue]:
    """@brief 读取 dbctl 本地 JSONC 文档 / Read the dbctl-local JSONC document.

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
    """@brief 解析 dbctl 的严格 JSONC / Parse dbctl's strict JSONC.

    @param source 已解码 JSONC 文本 / Decoded JSONC text.
    @return 严格 JSON 顶层对象 / Strict JSON top-level object.
    @raise JsoncDecodeError 注释或 JSON 语法无效时抛出 /
        Raised for invalid comments or JSON syntax.
    @note 仅允许 ``//``、``/* ... */`` 和标准 JSON；不接受 JSON5 扩展。/
        Only ``//`` and ``/* ... */`` comments plus standard JSON are accepted;
        other JSON5 extensions are rejected.
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
    @note ``json.loads`` 会把合法词法形式 ``1e999`` 转成 ``inf``；dbctl 的端口、
        超时等配置不能接受该非有限结果。/
        ``json.loads`` turns lexically valid ``1e999`` into ``inf``; dbctl values
        such as ports and timeouts must not accept that non-finite result.
    """

    value = float(token)
    if not isfinite(value):
        raise JsoncDecodeError(
            f"non-finite JSON numeric value {token!r} is not allowed"
        )
    return value


#: @brief 正整数数据库配置 / Positive database configuration integer.
PositiveInt = Annotated[int, Field(gt=0)]


class ConfigurationError(ValueError):
    """@brief dbctl 配置语义错误 / dbctl configuration semantic error."""


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
    """@brief PostgreSQL 端点 / PostgreSQL endpoint."""

    host: str = "localhost"
    port: PositiveInt = 5432
    name: str = "fogmoe"


class RuntimeRoleSettings(_FrozenSettings):
    """@brief 应用运行角色 / Application runtime role."""

    username: str = "fogmoe-bot"
    password: SecretStr | None = None


class MaintenanceRoleSettings(_FrozenSettings):
    """@brief 迁移与受控运维角色 / Migration and controlled-operations role."""

    username: str = "fogmoe-maintenance"
    password: SecretStr | None = None
    migration_schema: str = "infra"


class BootstrapSettings(_FrozenSettings):
    """@brief PostgreSQL bootstrap 设置 / PostgreSQL bootstrap settings."""

    system_user: str = "postgres"


class AdministratorSettings(_FrozenSettings):
    """@brief 初始管理员身份 / Initial administrator identity."""

    user_id: PositiveInt = 1002288404


class DbctlSettings(_FrozenSettings):
    """@brief dbctl 所有者视角的配置投影 / Configuration projection owned by dbctl."""

    endpoint: EndpointSettings = Field(default_factory=EndpointSettings)
    application: RuntimeRoleSettings = Field(default_factory=RuntimeRoleSettings)
    maintenance: MaintenanceRoleSettings = Field(
        default_factory=MaintenanceRoleSettings
    )
    bootstrap: BootstrapSettings = Field(default_factory=BootstrapSettings)
    administrator: AdministratorSettings = Field(default_factory=AdministratorSettings)


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


def reveal_secret(value: SecretStr | None, *, field_name: str) -> str:
    """@brief 取出必需密钥 / Reveal a required secret.

    @param value 掩码后的可选密钥 / Masked optional secret.
    @param field_name 人类可读字段路径 / Human-readable field path.
    @return 原始密钥 / Raw secret.
    @raise ConfigurationError 配置未提供密钥时抛出 / Raised when the secret is absent.
    """

    if value is None:
        raise ConfigurationError(f"{field_name} is required for this dbctl operation")
    return value.get_secret_value()


def read_dbctl_settings(path: Path | None = None) -> DbctlSettings:
    """@brief 从 JSONC 文档读取 dbctl 配置 / Read the dbctl configuration projection from JSONC.

    @param path 可选 config.json 路径 / Optional config.json path.
    @return 严格不可变的 dbctl 设置 / Strict immutable dbctl settings.
    @raise ConfigurationError JSONC 或控制面字段无效时抛出 /
        Raised when JSONC or control-plane fields are invalid.
    """

    source_path = path or default_config_path()
    try:
        document = _load_jsonc(source_path)
        return DbctlSettings.model_validate(_dbctl_payload(document))
    except JsoncDecodeError as error:
        raise ConfigurationError(str(error)) from error
    except ValidationError as error:
        details = "; ".join(
            ".".join(str(part) for part in item["loc"]) + ": " + item["msg"]
            for item in error.errors(include_input=False)
        )
        raise ConfigurationError(
            f"{source_path}: invalid dbctl configuration: {details}"
        ) from error


def _dbctl_payload(document: Mapping[str, JSONValue]) -> dict[str, object]:
    """@brief 提取 dbctl 所有的语义路径 / Extract semantic paths owned by dbctl.

    @param document 完整 JSONC 文档 / Complete JSONC document.
    @return dbctl 模型验证输入 / dbctl model-validation input.
    @raise ConfigurationError 必需字段不是对象时抛出 / Raised when a required field is not an object.
    """

    _require_schema_version(document)
    database = _object_at(document, "database")
    identity = _object_at(document, "identity")
    application = _object_at(database, "application")
    administrator = _object_at(identity, "administrator")
    return {
        "endpoint": _object_at(database, "endpoint"),
        "application": {
            key: value
            for key, value in application.items()
            if key in {"username", "password"}
        },
        "maintenance": _object_at(database, "maintenance"),
        "bootstrap": _object_at(database, "bootstrap"),
        "administrator": {
            key: value for key, value in administrator.items() if key == "user_id"
        },
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
    "BootstrapSettings",
    "ConfigurationError",
    "DbctlSettings",
    "EndpointSettings",
    "MaintenanceRoleSettings",
    "RuntimeRoleSettings",
    "SCHEMA_VERSION",
    "default_config_path",
    "read_dbctl_settings",
    "reveal_secret",
]
