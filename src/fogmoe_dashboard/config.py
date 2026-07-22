"""@brief Dashboard 配置输入边界 / Configuration input boundary for the Dashboard.

Dashboard 只读取报表访问与观测查询相关字段；它不复用 Bot 或 dbctl 的配置对象，
也不通过环境变量或 libpq service 文件取得凭据。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Final, Self
from urllib.parse import quote_plus

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    model_validator,
)

from fogmoe_config.jsonc import JsoncDecodeError, JSONValue, load_jsonc

#: @brief 当前支持的根配置契约版本 / Root configuration contract version supported by this package.
SCHEMA_VERSION: Final[int] = 1


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
        document = load_jsonc(source_path)
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
