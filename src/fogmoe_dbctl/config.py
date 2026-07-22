"""@brief 数据库控制面配置输入边界 / Configuration input boundary for the database control plane.

本模块只读取控制面拥有的数据库职责：应用、维护、报表、迁移与 bootstrap。它从
同一份用户语义化 ``config.json`` 取得字段，但不依赖 Bot 或 Dashboard 的配置服务。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Final, Self

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


class ReportingRoleSettings(_FrozenSettings):
    """@brief 只读报表登录角色 / Read-only reporting login role."""

    username: str = "fogmoe-dashboard"
    password: SecretStr | None = None


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
    reporting: ReportingRoleSettings = Field(default_factory=ReportingRoleSettings)
    bootstrap: BootstrapSettings = Field(default_factory=BootstrapSettings)
    administrator: AdministratorSettings = Field(default_factory=AdministratorSettings)

    @model_validator(mode="after")
    def require_distinct_login_roles(self) -> Self:
        """@brief 强制受管登录角色与 bootstrap 管理身份两两不同 / Require managed login roles and the bootstrap administrator to be pairwise distinct.

        @return 已验证设置 / Validated settings.
        @raise ValueError 任意两个角色名相同时抛出 /
            Raised when any two role names are equal.
        @note 角色分离是授权模型不变量，不能留给 bootstrap 执行顺序补救。/
            Role separation is an authorization-model invariant and must not be
            deferred to bootstrap execution order.
        """

        usernames = (
            self.application.username,
            self.maintenance.username,
            self.reporting.username,
            self.bootstrap.system_user,
        )
        if len(set(usernames)) != len(usernames):
            raise ValueError(
                "database.application.username, database.maintenance.username, "
                "database.reporting.username, and database.bootstrap.system_user "
                "must be pairwise distinct"
            )
        return self


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
        document = load_jsonc(source_path)
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
        "reporting": _object_at(database, "reporting"),
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
    "ReportingRoleSettings",
    "RuntimeRoleSettings",
    "SCHEMA_VERSION",
    "default_config_path",
    "read_dbctl_settings",
    "reveal_secret",
]
