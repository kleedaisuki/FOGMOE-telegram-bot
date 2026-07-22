"""@brief Bot 配置输入边界 / Typed configuration input boundary for the bot.

该模块只定义 Telegram Bot 所拥有的配置投影，并从根 ``config.json`` 的用户语义化
字段读取它。它不读取环境变量、不缓存配置，也不向其他可执行程序提供设置服务。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Final, Literal, TypeAlias
from urllib.parse import quote_plus, urlsplit

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from fogmoe_bot.domain.temporal import TimeZoneId
from fogmoe_config.jsonc import JsoncDecodeError, JSONValue, load_jsonc

#: @brief 当前支持的根配置契约版本 / Root configuration contract version supported by this package.
SCHEMA_VERSION: Final[int] = 2
#: @brief Compose 强制终止前允许的最大运行时排空秒数 / Maximum runtime drain seconds before Compose escalation.
MAX_SHUTDOWN_GRACE_SECONDS: Final[int] = 190


#: @brief 配置允许的日志级别 / Allowed logging levels.
LogLevel: TypeAlias = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
#: @brief 正整数配置值 / Positive configuration integer.
PositiveInt: TypeAlias = Annotated[int, Field(gt=0)]
#: @brief 非负整数配置值 / Non-negative configuration integer.
NonNegativeInt: TypeAlias = Annotated[int, Field(ge=0)]
#: @brief 正浮点配置值 / Positive configuration floating point value.
PositiveFloat: TypeAlias = Annotated[float, Field(gt=0, allow_inf_nan=False)]
#: @brief 非负浮点配置值 / Non-negative configuration floating point value.
NonNegativeFloat: TypeAlias = Annotated[float, Field(ge=0, allow_inf_nan=False)]


def _json_array_to_tuple(value: object) -> object:
    """@brief 将 JSON 数组转换为不可变元组 / Convert a JSON array to an immutable tuple.

    @param value JSON 解码后的原始值 / Raw value after JSON decoding.
    @return 元组或未改动值 / Tuple, or the original value when it is not a list.
    """

    return tuple(value) if isinstance(value, list) else value


#: @brief 来自 JSON 数组的不可变字符串序列 / Immutable string sequence decoded from a JSON array.
StringTuple: TypeAlias = Annotated[
    tuple[str, ...],
    BeforeValidator(_json_array_to_tuple),
]
class ConfigurationError(ValueError):
    """@brief Bot 配置语义错误 / Bot configuration semantic error.

    @note 错误消息只暴露路径与约束，不会回显密钥值。/
        Error messages expose paths and constraints only, never secret values.
    """


class _FrozenSettings(BaseModel):
    """@brief 严格不可变配置模型基类 / Base class for strict immutable settings models."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
        validate_default=True,
    )


class AdministratorSettings(_FrozenSettings):
    """@brief 管理员身份设置 / Administrator identity settings."""

    user_id: PositiveInt = 1002288404
    contact_name: str | None = None

    @field_validator("contact_name")
    @classmethod
    def _normalize_contact_name(cls, value: str | None) -> str | None:
        """@brief 规范化管理员展示名 / Normalize the administrator display name.

        @param value 原始展示名 / Raw display name.
        @return 去除空白后的名称或 None / Trimmed name, or None when blank.
        """

        return value.strip() or None if value is not None else None


class IdentitySettings(_FrozenSettings):
    """@brief 人员与权限配置 / Identity and authorization settings."""

    administrator: AdministratorSettings = Field(default_factory=AdministratorSettings)


class TelegramHttpSettings(_FrozenSettings):
    """@brief Telegram HTTP 客户端超时 / Telegram HTTP client timeouts."""

    connect_timeout_seconds: PositiveFloat = 10.0
    read_timeout_seconds: PositiveFloat = 30.0
    write_timeout_seconds: PositiveFloat = 30.0
    pool_timeout_seconds: PositiveFloat = 10.0


class TelegramPollingSettings(_FrozenSettings):
    """@brief Telegram long-polling 设置 / Telegram long-polling settings."""

    get_updates_timeout_seconds: PositiveInt = 30
    get_updates_connect_timeout_seconds: PositiveFloat = 10.0
    get_updates_read_timeout_seconds: PositiveFloat = 35.0
    get_updates_write_timeout_seconds: PositiveFloat = 30.0
    get_updates_pool_timeout_seconds: PositiveFloat = 10.0
    get_updates_connection_pool_size: PositiveInt = 2
    retry_initial_delay_seconds: NonNegativeFloat = 1.0
    retry_max_delay_seconds: NonNegativeFloat = 30.0

    @model_validator(mode="after")
    def _validate_retry_window(self) -> TelegramPollingSettings:
        """@brief 确保退避上限不小于初值 / Ensure retry maximum is not below initial delay.

        @return 已验证的 polling 设置 / Validated polling settings.
        @raise ValueError 最大退避小于初始值时抛出 / Raised when maximum delay is below initial delay.
        """

        if self.retry_max_delay_seconds < self.retry_initial_delay_seconds:
            raise ValueError(
                "retry_max_delay_seconds must be >= retry_initial_delay_seconds"
            )
        return self


class TelegramSettings(_FrozenSettings):
    """@brief Telegram Bot 接入配置 / Telegram Bot integration settings."""

    bot_token: SecretStr | None = None
    http: TelegramHttpSettings = Field(default_factory=TelegramHttpSettings)
    polling: TelegramPollingSettings = Field(default_factory=TelegramPollingSettings)


class MailboxRuntimeSettings(_FrozenSettings):
    """@brief Keyed mailbox 运行时容量 / Keyed mailbox runtime capacities."""

    max_concurrency: PositiveInt = 32
    global_capacity: PositiveInt = 512
    per_key_capacity: PositiveInt = 8
    idle_ttl_seconds: PositiveFloat = 300.0
    shutdown_grace_seconds: Annotated[
        float,
        Field(ge=1, le=MAX_SHUTDOWN_GRACE_SECONDS, allow_inf_nan=False),
    ] = 180.0

    @model_validator(mode="after")
    def _validate_capacities(self) -> MailboxRuntimeSettings:
        """@brief 校验局部容量不超过全局容量 / Validate local capacity does not exceed global capacity.

        @return 已验证的 mailbox 设置 / Validated mailbox settings.
        @raise ValueError 容量关系无效时抛出 / Raised for invalid capacity relationships.
        """

        if self.per_key_capacity > self.global_capacity:
            raise ValueError("per_key_capacity must be <= global_capacity")
        return self


class SchedulingRuntimeSettings(_FrozenSettings):
    """@brief 定时任务 worker 设置 / Scheduling worker settings."""

    poll_interval_seconds: PositiveFloat = 1.0
    worker_count: PositiveInt = 3
    attempt_timeout_seconds: PositiveInt = 10
    lease_seconds: PositiveInt = 30
    max_attempts: PositiveInt = 5
    retry_base_seconds: PositiveFloat = 1.0
    retry_cap_seconds: PositiveFloat = 60.0

    @model_validator(mode="after")
    def _validate_worker_bounds(self) -> SchedulingRuntimeSettings:
        """@brief 校验调度 lease 与重试窗口 / Validate schedule lease and retry bounds.

        @return 已验证设置 / Validated settings.
        @raise ValueError lease 不覆盖尝试或 retry cap 过小时抛出 /
            Raised when the lease does not outlive an attempt or the retry cap is too small.
        """

        if self.lease_seconds <= self.attempt_timeout_seconds:
            raise ValueError("lease_seconds must be > attempt_timeout_seconds")
        if self.retry_cap_seconds < self.retry_base_seconds:
            raise ValueError("retry_cap_seconds must be >= retry_base_seconds")
        return self


class _AdaptivePollingSettings(_FrozenSettings):
    """@brief 自适应空闲轮询配置基类 / Base settings for adaptive idle polling."""

    poll_interval_seconds: PositiveFloat
    max_poll_interval_seconds: PositiveFloat

    @model_validator(mode="after")
    def _validate_polling_bounds(self) -> _AdaptivePollingSettings:
        """@brief 确保退避上限不小于基础间隔 / Ensure the backoff cap is not below the base interval.

        @return 已验证设置 / Validated settings.
        @raise ValueError 上限小于基础间隔时抛出 / Raised when the cap is below the base interval.
        """

        if self.max_poll_interval_seconds < self.poll_interval_seconds:
            raise ValueError(
                "max_poll_interval_seconds must be >= poll_interval_seconds"
            )
        return self


def _validate_provider_attempt_lease(
    *,
    provider_timeout_seconds: int,
    attempt_timeout_seconds: int,
    lease_seconds: int,
) -> None:
    """@brief 校验外部调用、完整尝试与 fencing lease 的嵌套截止时间 / Validate nested provider, attempt, and fencing-lease deadlines.

    @param provider_timeout_seconds 单个 provider 调用上限 / Single-provider call limit.
    @param attempt_timeout_seconds 包含 fallback 的完整尝试上限 / Whole-attempt limit including fallback.
    @param lease_seconds durable claim lease / Durable claim lease.
    @return None / None.
    @raise ValueError 未满足 ``provider < attempt < lease`` 时抛出 /
        Raised unless ``provider < attempt < lease``.
    """

    if lease_seconds <= attempt_timeout_seconds:
        raise ValueError("lease_seconds must be > attempt_timeout_seconds")
    if attempt_timeout_seconds <= provider_timeout_seconds:
        raise ValueError("attempt_timeout_seconds must be > provider_timeout_seconds")


class InboxRuntimeSettings(_AdaptivePollingSettings):
    """@brief Durable inbox worker 设置 / Durable inbox worker settings."""

    worker_count: PositiveInt = 16
    poll_interval_seconds: PositiveFloat = 0.1
    max_poll_interval_seconds: PositiveFloat = 0.5
    lease_seconds: PositiveInt = 60


class InferenceRuntimeSettings(_AdaptivePollingSettings):
    """@brief 推理 worker 设置 / Inference worker settings."""

    worker_count: PositiveInt = 8
    poll_interval_seconds: PositiveFloat = 0.25
    max_poll_interval_seconds: PositiveFloat = 0.5
    provider_timeout_seconds: PositiveInt = 90
    lease_seconds: PositiveInt = 180
    attempt_timeout_seconds: PositiveInt = 120

    @model_validator(mode="after")
    def _validate_lease(self) -> InferenceRuntimeSettings:
        """@brief 确保 provider、attempt 与 lease 严格嵌套 / Strictly nest provider, attempt, and lease deadlines.

        @return 已验证的推理设置 / Validated inference settings.
        @raise ValueError deadline 顺序无效时抛出 / Raised for an invalid deadline order.
        """

        _validate_provider_attempt_lease(
            provider_timeout_seconds=self.provider_timeout_seconds,
            attempt_timeout_seconds=self.attempt_timeout_seconds,
            lease_seconds=self.lease_seconds,
        )
        return self


class OutboxRuntimeSettings(_AdaptivePollingSettings):
    """@brief Durable outbox worker 设置 / Durable outbox worker settings."""

    worker_count: PositiveInt = 16
    poll_interval_seconds: PositiveFloat = 0.1
    max_poll_interval_seconds: PositiveFloat = 0.5
    lease_seconds: PositiveInt = 60
    attempt_timeout_seconds: PositiveInt = 25

    @model_validator(mode="after")
    def _validate_lease(self) -> OutboxRuntimeSettings:
        """@brief 确保 lease 严格长于投递尝试 / Ensure the lease strictly outlives delivery attempts.

        @return 已验证的 outbox 设置 / Validated outbox settings.
        @raise ValueError lease 过短时抛出 / Raised when the lease is too short.
        """

        if self.lease_seconds <= self.attempt_timeout_seconds:
            raise ValueError("lease_seconds must be > attempt_timeout_seconds")
        return self


class CompactionRuntimeSettings(_AdaptivePollingSettings):
    """@brief 上下文压缩 worker 设置 / Context-compaction worker settings."""

    worker_count: PositiveInt = 2
    poll_interval_seconds: PositiveFloat = 0.5
    max_poll_interval_seconds: PositiveFloat = 5.0
    provider_timeout_seconds: PositiveInt = 30
    attempt_timeout_seconds: PositiveInt = 120
    lease_seconds: PositiveInt = 180

    @model_validator(mode="after")
    def _validate_lease(self) -> CompactionRuntimeSettings:
        """@brief 确保压缩 provider、attempt 与 lease 严格嵌套 / Strictly nest compaction provider, attempt, and lease deadlines.

        @return 已验证的压缩设置 / Validated compaction settings.
        @raise ValueError deadline 顺序无效时抛出 / Raised for an invalid deadline order.
        """

        _validate_provider_attempt_lease(
            provider_timeout_seconds=self.provider_timeout_seconds,
            attempt_timeout_seconds=self.attempt_timeout_seconds,
            lease_seconds=self.lease_seconds,
        )
        return self


class DreamingRuntimeSettings(_AdaptivePollingSettings):
    """@brief 用户画像整合 worker 设置 / User-profile consolidation worker settings."""

    worker_count: PositiveInt = 2
    batch_size: PositiveInt = 4
    source_batch_size: PositiveInt = 32
    max_events_per_job: PositiveInt = 64
    max_evidence_characters: PositiveInt = 60_000
    poll_interval_seconds: PositiveFloat = 1.0
    max_poll_interval_seconds: PositiveFloat = 5.0
    refresh_seconds: PositiveInt = 21_600
    provider_timeout_seconds: PositiveInt = 60
    attempt_timeout_seconds: PositiveInt = 90
    lease_seconds: PositiveInt = 120
    max_attempts: PositiveInt = 5

    @model_validator(mode="after")
    def _validate_lease(self) -> DreamingRuntimeSettings:
        """@brief 确保 provider、attempt 与 lease 的截止顺序 / Order provider, attempt, and lease deadlines.

        @return 已验证的 dreaming 设置 / Validated dreaming settings.
        @raise ValueError 任一 deadline 不严格先于其外层边界时抛出 /
            Raised when a deadline does not strictly precede its enclosing boundary.
        """

        _validate_provider_attempt_lease(
            provider_timeout_seconds=self.provider_timeout_seconds,
            attempt_timeout_seconds=self.attempt_timeout_seconds,
            lease_seconds=self.lease_seconds,
        )
        return self


class RetrievalWorkerSettings(_AdaptivePollingSettings):
    """@brief 语义检索 worker 设置 / Semantic-retrieval worker settings."""

    worker_count: PositiveInt = 2
    batch_size: PositiveInt = 16
    poll_interval_seconds: PositiveFloat = 0.5
    max_poll_interval_seconds: PositiveFloat = 2.0
    lease_seconds: PositiveInt = 120


class RuntimeSettings(_FrozenSettings):
    """@brief 进程并发与 durable worker 设置 / Process concurrency and durable-worker settings."""

    mailbox: MailboxRuntimeSettings = Field(default_factory=MailboxRuntimeSettings)
    scheduling: SchedulingRuntimeSettings = Field(
        default_factory=SchedulingRuntimeSettings
    )
    inbox: InboxRuntimeSettings = Field(default_factory=InboxRuntimeSettings)
    inference: InferenceRuntimeSettings = Field(
        default_factory=InferenceRuntimeSettings
    )
    outbox: OutboxRuntimeSettings = Field(default_factory=OutboxRuntimeSettings)
    compaction: CompactionRuntimeSettings = Field(
        default_factory=CompactionRuntimeSettings
    )
    dreaming: DreamingRuntimeSettings = Field(default_factory=DreamingRuntimeSettings)


AiTaskName: TypeAlias = Literal["chat", "summary", "dreaming", "translation"]
"""@brief 受支持的 AI 任务名称 / Supported AI task names."""


def _non_blank(value: str, *, field_name: str) -> str:
    """@brief 规范化非空配置字符串 / Normalize a non-blank configuration string.

    @param value 原始配置值 / Raw configuration value.
    @param field_name 报错中使用的字段名 / Field name used in validation errors.
    @return 去除首尾空白的值 / Value with surrounding whitespace removed.
    @raise ValueError 值为空时抛出 / Raised when the value is blank.
    """

    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be blank")
    return normalized


class ProviderAuthSettings(_FrozenSettings):
    """@brief Provider API 密钥认证规则 / Provider API-key authentication rule.

    ``header`` 与 ``prefix`` 使 Bearer、x-api-key 等认证成为配置数据，而不是
    provider 名称分支。/
    ``header`` and ``prefix`` keep Bearer, x-api-key, and similar authentication as
    configuration data rather than provider-name branches.
    """

    api_key: SecretStr | None = None
    header: str = "Authorization"
    prefix: str = "Bearer "

    @field_validator("header")
    @classmethod
    def _validate_header(cls, value: str) -> str:
        """@brief 校验 HTTP header 名 / Validate an HTTP header name.

        @param value 原始 header 名 / Raw header name.
        @return 规范 header 名 / Normalized header name.
        @raise ValueError header 含控制字符时抛出 / Raised when the header has control characters.
        """

        normalized = _non_blank(value, field_name="auth.header")
        if any(character in normalized for character in "\r\n:"):
            raise ValueError("auth.header must be a single HTTP header name")
        return normalized

    @field_validator("prefix")
    @classmethod
    def _validate_prefix(cls, value: str) -> str:
        """@brief 拒绝 header injection 前缀 / Reject header-injection prefixes.

        @param value 原始认证前缀 / Raw authentication prefix.
        @return 原样前缀 / Original prefix.
        @raise ValueError 含 CR/LF 时抛出 / Raised when CR/LF is present.
        """

        if "\r" in value or "\n" in value:
            raise ValueError("auth.prefix must not contain CR or LF")
        return value


class _ProviderSettings(_FrozenSettings):
    """@brief 两种 wire style 共用的 provider 连接设置 / Provider connection settings shared by both wire styles."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    label: str = Field(min_length=1, max_length=120)
    endpoint: str = Field(min_length=1, max_length=2_048)
    auth: ProviderAuthSettings = Field(default_factory=ProviderAuthSettings)
    headers: Mapping[str, str] = Field(default_factory=dict)

    @field_validator("id", "label")
    @classmethod
    def _validate_identity(cls, value: str, info: object) -> str:
        """@brief 规范化 provider 标识与显示名 / Normalize provider ID and display label.

        @param value 原始字段值 / Raw field value.
        @param info Pydantic field 上下文 / Pydantic field context.
        @return 规范后的值 / Normalized value.
        """

        field_name = getattr(info, "field_name", "provider field")
        return _non_blank(value, field_name=str(field_name))

    @field_validator("endpoint")
    @classmethod
    def _validate_complete_endpoint(cls, value: str) -> str:
        """@brief 校验完整 HTTP 请求 endpoint / Validate a complete HTTP request endpoint.

        @param value 原始 URL / Raw URL.
        @return 规范 URL / Normalized URL.
        @raise ValueError URL 不是完整 HTTP 请求地址时抛出 /
            Raised when the URL is not a complete HTTP request endpoint.
        @note endpoint 必须带路径，例如 ``/v1/chat/completions``；客户端不得拼接 provider-specific 路径。/
            The endpoint must carry a path such as ``/v1/chat/completions``; clients must not
            append provider-specific paths.
        """

        normalized = _non_blank(value, field_name="endpoint")
        parts = urlsplit(normalized)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.netloc
            or parts.path in {"", "/"}
            or parts.query
            or parts.fragment
        ):
            raise ValueError(
                "endpoint must be a complete http(s) request URL without query or fragment"
            )
        return normalized

    @field_validator("headers")
    @classmethod
    def _validate_headers(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        """@brief 校验用户自定义 HTTP headers / Validate user-defined HTTP headers.

        @param value 原始 headers 映射 / Raw headers mapping.
        @return 去除名称空白后的 headers / Headers with normalized names.
        @raise ValueError header 名或值不安全时抛出 / Raised for unsafe header names or values.
        """

        normalized: dict[str, str] = {}
        for name, header_value in value.items():
            clean_name = _non_blank(name, field_name="headers key")
            if any(character in clean_name for character in "\r\n:"):
                raise ValueError("headers keys must be single HTTP header names")
            if "\r" in header_value or "\n" in header_value:
                raise ValueError("headers values must not contain CR or LF")
            if clean_name.casefold() in {
                "authorization",
                "x-api-key",
                "anthropic-version",
            }:
                raise ValueError(
                    "authentication and protocol headers must use provider auth/style fields"
                )
            normalized[clean_name] = header_value
        return normalized


class OpenAIProviderSettings(_ProviderSettings):
    """@brief OpenAI-style completion endpoint 设置 / OpenAI-style completion endpoint settings."""

    style: Literal["openai"]


class AnthropicProviderSettings(_ProviderSettings):
    """@brief Anthropic Messages-style endpoint 设置 / Anthropic Messages-style endpoint settings."""

    style: Literal["anthropic"]
    api_version: str = Field(min_length=1, max_length=128)

    @field_validator("api_version")
    @classmethod
    def _validate_api_version(cls, value: str) -> str:
        """@brief 校验 Anthropic API 版本 / Validate the Anthropic API version.

        @param value 原始版本字符串 / Raw version string.
        @return 规范版本字符串 / Normalized version string.
        """

        return _non_blank(value, field_name="api_version")


AiProviderSettings: TypeAlias = Annotated[
    OpenAIProviderSettings | AnthropicProviderSettings,
    Field(discriminator="style"),
]
"""@brief 按 wire style 判别的 provider 联合 / Provider union discriminated by wire style."""

#: @brief JSON 数组解码的不可变 provider 列表 / Immutable provider list decoded from a JSON array.
AiProviderSettingsTuple: TypeAlias = Annotated[
    tuple[AiProviderSettings, ...],
    BeforeValidator(_json_array_to_tuple),
]


class AiRouteModelSettings(_FrozenSettings):
    """@brief 一个 route 内可回退模型 / One fallback-capable model inside a route."""

    name: str = Field(min_length=1, max_length=512)
    accepts_images: bool = False

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        """@brief 规范化模型名 / Normalize a model name.

        @param value 原始模型名 / Raw model name.
        @return 规范模型名 / Normalized model name.
        """

        return _non_blank(value, field_name="models.name")


#: @brief JSON 数组解码的不可变 route 模型列表 / Immutable route-model list decoded from a JSON array.
AiRouteModelTuple: TypeAlias = Annotated[
    tuple[AiRouteModelSettings, ...],
    BeforeValidator(_json_array_to_tuple),
]
#: @brief JSON 数组解码的不可变工具名列表 / Immutable tool-name list decoded from a JSON array.
ToolNameTuple: TypeAlias = Annotated[
    tuple[str, ...],
    BeforeValidator(_json_array_to_tuple),
]


class AiRouteSettings(_FrozenSettings):
    """@brief 一个任务候选路由 / One candidate route for an AI task.

    route 把模型 fallback、能力和 protocol metadata 放在同一个配置节点，因而没有
    ``provider_order`` 与独立 model catalog 之间的隐式 join。/
    A route colocates model fallback, capabilities, and protocol metadata, so there is no
    implicit join between ``provider_order`` and a separate model catalog.
    """

    provider: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    models: AiRouteModelTuple = ()
    supports_tools: bool = True
    strict_tools: bool = False
    disabled_tools: ToolNameTuple = ()
    safety_block_is_terminal: bool = False
    meta: Mapping[str, str] = Field(default_factory=dict)

    @field_validator("provider")
    @classmethod
    def _validate_provider(cls, value: str) -> str:
        """@brief 规范化 provider 引用 / Normalize a provider reference.

        @param value 原始 provider ID / Raw provider ID.
        @return 规范 provider ID / Normalized provider ID.
        """

        return _non_blank(value, field_name="route.provider")

    @field_validator("disabled_tools")
    @classmethod
    def _validate_disabled_tools(cls, value: ToolNameTuple) -> ToolNameTuple:
        """@brief 校验禁用工具列表 / Validate disabled tool names.

        @param value 原始工具名序列 / Raw tool-name sequence.
        @return 去重后的工具名序列 / Deduplicated tool-name sequence.
        @raise ValueError 工具名为空时抛出 / Raised when a tool name is blank.
        """

        normalized = tuple(
            _non_blank(tool, field_name="disabled_tools item") for tool in value
        )
        if len(set(normalized)) != len(normalized):
            raise ValueError("disabled_tools must not contain duplicates")
        return normalized

    @field_validator("meta")
    @classmethod
    def _validate_meta(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        """@brief 校验 protocol metadata 的基础安全性 / Validate basic protocol-metadata safety.

        @param value 原始 metadata 映射 / Raw metadata mapping.
        @return 规范 metadata 映射 / Normalized metadata mapping.
        @raise ValueError key 或 value 含 CR/LF 时抛出 / Raised when a key or value contains CR/LF.
        """

        normalized: dict[str, str] = {}
        for key, item in value.items():
            clean_key = _non_blank(key, field_name="meta key")
            if "\r" in clean_key or "\n" in clean_key:
                raise ValueError("meta keys must not contain CR or LF")
            if "\r" in item or "\n" in item:
                raise ValueError("meta values must not contain CR or LF")
            normalized[clean_key] = item
        return normalized


#: @brief JSON 数组解码的不可变任务 route 列表 / Immutable task-route list decoded from a JSON array.
AiRouteTuple: TypeAlias = Annotated[
    tuple[AiRouteSettings, ...],
    BeforeValidator(_json_array_to_tuple),
]


class AiTaskRoutingSettings(_FrozenSettings):
    """@brief 一个 AI 任务的有序路由集 / Ordered route set for one AI task."""

    routes: AiRouteTuple = ()


class AiRoutingSettings(_FrozenSettings):
    """@brief 四类 AI 任务的统一路由配置 / Uniform route configuration for four AI tasks."""

    chat: AiTaskRoutingSettings = Field(default_factory=AiTaskRoutingSettings)
    summary: AiTaskRoutingSettings = Field(default_factory=AiTaskRoutingSettings)
    dreaming: AiTaskRoutingSettings = Field(default_factory=AiTaskRoutingSettings)
    translation: AiTaskRoutingSettings = Field(default_factory=AiTaskRoutingSettings)

    def for_task(self, task: AiTaskName) -> AiTaskRoutingSettings:
        """@brief 返回指定任务的 route 集 / Return the route set for one task.

        @param task 推理任务 / Inference task.
        @return 对应的不可变 route 集 / Corresponding immutable route set.
        """

        match task:
            case "chat":
                return self.chat
            case "summary":
                return self.summary
            case "dreaming":
                return self.dreaming
            case "translation":
                return self.translation


class AiSettings(_FrozenSettings):
    """@brief Provider 连接和任务 route 设置 / Provider connection and task-route settings.

    默认值故意不隐含任一商业 provider 或模型；部署配置必须明确声明它实际要调用的
    endpoint 与 routes。/
    Defaults intentionally imply no commercial provider or model; deployment configuration must
    explicitly declare the endpoint and routes it will call.
    """

    providers: AiProviderSettingsTuple = ()
    routing: AiRoutingSettings = Field(default_factory=AiRoutingSettings)

    @model_validator(mode="after")
    def _validate_graph(self) -> AiSettings:
        """@brief 校验 provider 与 route 的完整配置图 / Validate the complete provider-and-route configuration graph.

        @return 已验证的 AI 设置 / Validated AI settings.
        @raise ValueError provider ID 重复、route 引用缺失或能力契约非法时抛出 /
            Raised for duplicate provider IDs, missing route references, or invalid capability contracts.
        """

        provider_by_id: dict[str, AiProviderSettings] = {}
        for provider in self.providers:
            if provider.id in provider_by_id:
                raise ValueError(f"ai.providers contains duplicate provider id {provider.id!r}")
            provider_by_id[provider.id] = provider
        for task in ("chat", "summary", "dreaming", "translation"):
            task_routes = self.routing.for_task(task)
            for index, route in enumerate(task_routes.routes):
                location = f"ai.routing.{task}.routes[{index}]"
                referenced_provider = provider_by_id.get(route.provider)
                if referenced_provider is None:
                    raise ValueError(
                        f"{location}.provider references unknown provider {route.provider!r}"
                    )
                if not route.models:
                    raise ValueError(f"{location}.models must contain at least one model")
                model_names = tuple(model.name for model in route.models)
                if len(set(model_names)) != len(model_names):
                    raise ValueError(f"{location}.models must not contain duplicate names")
                if route.strict_tools and not route.supports_tools:
                    raise ValueError(
                        f"{location}.strict_tools requires supports_tools to be true"
                    )
                if (
                    referenced_provider.style == "anthropic"
                    and set(route.meta) - {"user_id"}
                ):
                    raise ValueError(
                        f"{location}.meta only supports user_id for anthropic routes"
                    )
        return self

    def provider_for(self, provider_id: str) -> AiProviderSettings:
        """@brief 按动态 ID 查找 provider / Look up a provider by dynamic ID.

        @param provider_id route 引用的 provider ID / Provider ID referenced by a route.
        @return 已验证 provider / Validated provider.
        @raise KeyError ID 未注册时抛出 / Raised when the ID is not registered.
        """

        for provider in self.providers:
            if provider.id == provider_id:
                return provider
        raise KeyError(provider_id)


class ContextWindowSettings(_FrozenSettings):
    """@brief 对话上下文 token 预算 / Conversation-context token budget."""

    warning_tokens: PositiveInt = 114_000
    hard_tokens: PositiveInt = 120_000
    reserved_tokens: NonNegativeInt = 8192

    @model_validator(mode="after")
    def _validate_budget(self) -> ContextWindowSettings:
        """@brief 校验 token 阈值关系 / Validate token-threshold relationships.

        @return 已验证的上下文预算 / Validated context budget.
        @raise ValueError 阈值关系无效时抛出 / Raised for invalid threshold relationships.
        """

        if self.warning_tokens > self.hard_tokens:
            raise ValueError("warning_tokens must be <= hard_tokens")
        if self.reserved_tokens >= self.hard_tokens:
            raise ValueError("reserved_tokens must be < hard_tokens")
        return self


class WorkingMemorySettings(_FrozenSettings):
    """@brief Working Memory 检索设置 / Working-memory retrieval settings."""

    result_limit: PositiveInt = 64
    reserved_tokens: NonNegativeInt = 16_384
    timeout_seconds: PositiveFloat = 5.0
    failure_cooldown_seconds: PositiveFloat = 60.0


class RetrievalEmbeddingSettings(_FrozenSettings):
    """@brief Embedding provider 设置 / Embedding provider settings."""

    api_key: SecretStr | None = None
    api_base: str = "https://openrouter.ai/api/v1"
    model: str = "qwen/qwen3-embedding-8b"
    space_id: str = "qwen3-embedding-8b.1024.episodic-v1"
    dimensions: Literal[1024] = 1024
    timeout_seconds: PositiveFloat = 30.0
    query_instruction: str = (
        "Retrieve prior conversation evidence relevant to the user's current question, "
        "including events, decisions, preferences, corrections, and temporal context."
    )


class RetrievalSettings(_FrozenSettings):
    """@brief Episodic retrieval 设置 / Episodic-retrieval settings."""

    worker: RetrievalWorkerSettings = Field(default_factory=RetrievalWorkerSettings)
    embedding: RetrievalEmbeddingSettings = Field(
        default_factory=RetrievalEmbeddingSettings
    )

    @model_validator(mode="after")
    def _validate_embedding_timeout_before_lease(self) -> RetrievalSettings:
        """@brief 确保 provider deadline 先于 vector lease / Ensure the provider deadline precedes the vector lease.

        @return 已验证的 retrieval 设置 / Validated retrieval settings.
        @raise ValueError embedding timeout 不严格短于 lease 时抛出 / Raised when the embedding timeout is not strictly shorter than the lease.
        """

        if self.embedding.timeout_seconds >= self.worker.lease_seconds:
            raise ValueError("embedding.timeout_seconds must be < worker.lease_seconds")
        return self


class HistoryCacheSettings(_FrozenSettings):
    """@brief 会话历史缓存设置 / Conversation-history cache settings."""

    capacity: PositiveInt = 256
    ttl_seconds: PositiveFloat = 900.0


class TimeSettings(_FrozenSettings):
    """@brief Assistant 时间语义设置 / Assistant temporal-semantics settings."""

    default_timezone: str = "Asia/Shanghai"
    """@brief 未指定时使用的 IANA 时区 / IANA zone used when a request omits one."""

    @field_validator("default_timezone")
    @classmethod
    def _validate_default_timezone(cls, value: str) -> str:
        """@brief 规范并验证默认 IANA 时区 / Normalize and validate the default IANA time zone.

        @param value 配置中的时区名 / Configured time-zone name.
        @return 规范时区名 / Canonicalized zone name.
        @raise ValueError 时区不存在时抛出 / Raised when the zone is unknown.
        """

        return TimeZoneId(value).value


class AssistantSettings(_FrozenSettings):
    """@brief Assistant 记忆、上下文与检索设置 / Assistant memory, context, and retrieval settings."""

    context_window: ContextWindowSettings = Field(default_factory=ContextWindowSettings)
    working_memory: WorkingMemorySettings = Field(default_factory=WorkingMemorySettings)
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    history_cache: HistoryCacheSettings = Field(default_factory=HistoryCacheSettings)
    time: TimeSettings = Field(default_factory=TimeSettings)


class DatabaseEndpointSettings(_FrozenSettings):
    """@brief PostgreSQL 部署端点 / PostgreSQL deployment endpoint."""

    host: str = "localhost"
    port: PositiveInt = 5432
    name: str = "fogmoe"


class ApplicationDatabaseSettings(_FrozenSettings):
    """@brief 机器人运行时数据库访问 / Bot runtime database access."""

    username: str = "fogmoe-bot"
    password: SecretStr | None = None
    pool_size: PositiveInt = 5
    max_overflow: NonNegativeInt = 10
    pool_recycle_seconds: PositiveInt = 1800
    connect_timeout_seconds: PositiveInt = 10
    search_path: StringTuple = (
        "identity",
        "conversation",
        "context_window",
        "retrieval",
        "user_profile",
        "assistant",
        "bank",
        "billing",
        "town",
        "chance",
        "personal_rpg",
        "economy",
        "moderation",
        "crypto",
        "game",
        "media",
        "admin",
        "public",
    )


class BotDatabaseSettings(_FrozenSettings):
    """@brief Bot 所需的数据库投影 / Database projection required by the bot."""

    endpoint: DatabaseEndpointSettings = Field(default_factory=DatabaseEndpointSettings)
    application: ApplicationDatabaseSettings = Field(
        default_factory=ApplicationDatabaseSettings
    )

    def sqlalchemy_url(self) -> str:
        """@brief 构造 asyncpg SQLAlchemy URL / Build an asyncpg SQLAlchemy URL.

        @return 已转义的 SQLAlchemy URL / Escaped SQLAlchemy URL.
        """

        password = reveal_secret(self.application.password)
        user = quote_plus(self.application.username)
        auth = user if not password else f"{user}:{quote_plus(password)}"
        endpoint = self.endpoint
        return f"postgresql+asyncpg://{auth}@{endpoint.host}:{endpoint.port}/{endpoint.name}"

    def asyncpg_url(self) -> str:
        """@brief 构造 asyncpg 原生 URL / Build a native asyncpg URL.

        @return 不含 SQLAlchemy driver 标记的 URL / URL without SQLAlchemy driver marker.
        """

        return self.sqlalchemy_url().replace(
            "postgresql+asyncpg://", "postgresql://", 1
        )


class NetworkSettings(_FrozenSettings):
    """@brief 出站网络设置 / Outbound-network settings."""

    proxy_url: str | None = None


class SearchIntegrationSettings(_FrozenSettings):
    """@brief 搜索工具凭据 / Search-tool credentials."""

    serpapi_api_key: SecretStr | None = None


class CodeExecutionIntegrationSettings(_FrozenSettings):
    """@brief 代码执行工具设置 / Code-execution tool settings."""

    judge0_api_url: str = "https://ce.judge0.com"
    judge0_api_key: SecretStr | None = None


class ImageGenerationIntegrationSettings(_FrozenSettings):
    """@brief 图片生成工具设置 / Image-generation tool settings."""

    api_url: str | None = None
    api_token: SecretStr | None = None
    model: str | None = None
    timeout_seconds: PositiveInt = 30


class AudioIntegrationSettings(_FrozenSettings):
    """@brief Fish Audio 工具设置 / Fish Audio tool settings."""

    api_key: SecretStr | None = None
    model: str = "s2.1-pro-free"
    reference_id: str = "dc020cb237df4248907565718715b20b"


class IntegrationsSettings(_FrozenSettings):
    """@brief 外部工具与 API 设置 / External tool and API settings."""

    search: SearchIntegrationSettings = Field(default_factory=SearchIntegrationSettings)
    code_execution: CodeExecutionIntegrationSettings = Field(
        default_factory=CodeExecutionIntegrationSettings
    )
    image_generation: ImageGenerationIntegrationSettings = Field(
        default_factory=ImageGenerationIntegrationSettings
    )
    audio: AudioIntegrationSettings = Field(default_factory=AudioIntegrationSettings)


class EconomySettings(_FrozenSettings):
    """@brief 经济系统启动参数 / Economy-system bootstrap settings."""

    new_user_bonus_coins: NonNegativeInt = 10


class LoggingSettings(_FrozenSettings):
    """@brief 文件与队列日志设置 / File and queue logging settings."""

    level: LogLevel = "INFO"
    directory: str = "logs"
    file_max_bytes: PositiveInt = 1_048_576
    file_backup_count: NonNegativeInt = 5
    queue_capacity: PositiveInt = 10_000


class ObservabilitySettings(_FrozenSettings):
    """@brief PostgreSQL 遥测设置 / PostgreSQL telemetry settings."""

    enabled: bool = True
    environment: str = "production"
    queue_capacity: PositiveInt = 20_000
    batch_size: PositiveInt = 250
    flush_interval_seconds: PositiveFloat = 1.0
    retry_max_delay_seconds: PositiveFloat = 30.0
    shutdown_flush_timeout_seconds: PositiveFloat = 3.0
    database_command_timeout_seconds: PositiveFloat = 2.0
    metric_interval_seconds: PositiveFloat = 15.0
    retention_days: PositiveInt = 30


class BotSettings(_FrozenSettings):
    """@brief Bot 组合根拥有的完整配置投影 / Complete configuration projection owned by the bot composition root."""

    identity: IdentitySettings = Field(default_factory=IdentitySettings)
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    ai: AiSettings = Field(default_factory=AiSettings)
    assistant: AssistantSettings = Field(default_factory=AssistantSettings)
    database: BotDatabaseSettings = Field(default_factory=BotDatabaseSettings)
    network: NetworkSettings = Field(default_factory=NetworkSettings)
    integrations: IntegrationsSettings = Field(default_factory=IntegrationsSettings)
    economy: EconomySettings = Field(default_factory=EconomySettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)


def reveal_secret(value: SecretStr | None) -> str | None:
    """@brief 在外部 SDK 边界取出 secret / Reveal a secret at an external SDK boundary.

    @param value 被掩码的可选 secret / Masked optional secret.
    @return 原始字符串或 None / Raw string, or None.
    @note 调用方不得记录返回值。/ Callers must never log the returned value.
    """

    return value.get_secret_value() if value is not None else None


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


def read_bot_settings(path: Path | None = None) -> BotSettings:
    """@brief 从 JSONC 文档读取 Bot 所需配置 / Read the Bot configuration projection from JSONC.

    @param path 可选 config.json 路径 / Optional config.json path.
    @return 严格、不可变的 Bot 设置 / Strict immutable Bot settings.
    @raise ConfigurationError JSONC 或 Bot 拥有字段无效时抛出 /
        Raised when JSONC or Bot-owned fields are invalid.
    """

    source_path = path or default_config_path()
    try:
        document = load_jsonc(source_path)
        payload = _bot_payload(document)
        return BotSettings.model_validate(payload)
    except JsoncDecodeError as error:
        raise ConfigurationError(str(error)) from error
    except ValidationError as error:
        details = "; ".join(
            ".".join(str(part) for part in item["loc"]) + ": " + item["msg"]
            for item in error.errors(include_input=False)
        )
        raise ConfigurationError(
            f"{source_path}: invalid bot configuration: {details}"
        ) from error


def _bot_payload(document: Mapping[str, JSONValue]) -> dict[str, object]:
    """@brief 提取 Bot 拥有的语义路径 / Extract semantic paths owned by the Bot.

    @param document 完整 JSONC 文档 / Complete JSONC document.
    @return 供 BotSettings 验证的投影 / Projection for BotSettings validation.
    @raise ConfigurationError 某个所需路径不是对象时抛出 /
        Raised when a required path is not an object.
    """

    _require_schema_version(document)
    database = _object_at(document, "database")
    observability = _object_at(document, "observability")
    return {
        "identity": _object_at(document, "identity"),
        "telegram": _object_at(document, "telegram"),
        "runtime": _object_at(document, "runtime"),
        "ai": _object_at(document, "ai"),
        "assistant": _object_at(document, "assistant"),
        "database": {
            "endpoint": _object_at(database, "endpoint"),
            "application": _object_at(database, "application"),
        },
        "network": _object_at(document, "network"),
        "integrations": _object_at(document, "integrations"),
        "economy": _object_at(document, "economy"),
        "logging": _object_at(document, "logging"),
        "observability": {
            key: value for key, value in observability.items() if key != "dashboard"
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
    "AdministratorSettings",
    "AiProviderSettings",
    "AiProviderSettingsTuple",
    "AiRouteModelSettings",
    "AiRouteSettings",
    "AiRouteTuple",
    "AiRoutingSettings",
    "AiSettings",
    "AiTaskName",
    "AiTaskRoutingSettings",
    "ApplicationDatabaseSettings",
    "AssistantSettings",
    "AnthropicProviderSettings",
    "BotDatabaseSettings",
    "BotSettings",
    "ConfigurationError",
    "ContextWindowSettings",
    "DatabaseEndpointSettings",
    "EconomySettings",
    "IdentitySettings",
    "IntegrationsSettings",
    "LoggingSettings",
    "MAX_SHUTDOWN_GRACE_SECONDS",
    "NetworkSettings",
    "OpenAIProviderSettings",
    "ObservabilitySettings",
    "ProviderAuthSettings",
    "RuntimeSettings",
    "SCHEMA_VERSION",
    "TelegramSettings",
    "default_config_path",
    "read_bot_settings",
    "reveal_secret",
]
