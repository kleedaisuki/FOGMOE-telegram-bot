"""@brief Bot 配置输入边界 / Typed configuration input boundary for the bot.

该模块只定义 Telegram Bot 所拥有的配置投影，并从根 ``config.json`` 的用户语义化
字段读取它。它不读取环境变量、不缓存配置，也不向其他可执行程序提供设置服务。
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum, auto
import json
from math import isfinite
from pathlib import Path
from typing import Annotated, Final, Literal, Never, TypeAlias, cast
from urllib.parse import quote_plus

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


type JSONValue = (
    None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]
)
"""@brief JSONC 可表示的递归值 / Recursive value representable by JSONC."""
#: @brief 当前支持的根配置契约版本 / Root configuration contract version supported by this package.
SCHEMA_VERSION: Final[int] = 1


class JsoncDecodeError(ValueError):
    """@brief JSONC 文档无效 / JSONC document is invalid."""


class _JsoncScanState(Enum):
    """@brief JSONC 注释扫描状态 / JSONC comment-scanning states."""

    NORMAL = auto()
    """@brief 常规 JSON 语法位置 / Ordinary JSON syntax position."""

    STRING = auto()
    """@brief 双引号字符串内部 / Inside a double-quoted string."""

    ESCAPE = auto()
    """@brief 字符串转义字符之后 / Immediately after a string escape."""

    LINE_COMMENT = auto()
    """@brief 行注释内部 / Inside a line comment."""

    BLOCK_COMMENT = auto()
    """@brief 块注释内部 / Inside a block comment."""


def _load_jsonc(path: Path) -> dict[str, JSONValue]:
    """@brief 读取 Bot 本地 JSONC 文档 / Read the Bot-local JSONC document.

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
    """@brief 解析严格 JSON 加注释 / Parse strict JSON plus comments.

    @param source 已解码的 JSONC 文本 / Decoded JSONC text.
    @return 严格 JSON 顶层对象 / Strict JSON top-level object.
    @raise JsoncDecodeError 注释或 JSON 语法无效时抛出 /
        Raised for invalid comments or JSON syntax.
    @note 仅允许 ``//`` 和 ``/* ... */`` 注释，不接受 JSON5 其他扩展。/
        Only ``//`` and ``/* ... */`` comments are accepted; other JSON5 extensions are rejected.
    """

    try:
        value = cast(
            JSONValue,
            json.loads(
                _strip_jsonc_comments(source),
                object_pairs_hook=_json_object_without_duplicate_keys,
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
    """@brief 用状态机替换 JSONC 注释 / Replace JSONC comments with whitespace using a state machine.

    @param source 原始 JSONC 文本 / Raw JSONC text.
    @return 与原文位置对齐的严格 JSON 文本 / Strict JSON text aligned with the source positions.
    @raise JsoncDecodeError 块注释未闭合时抛出 / Raised when a block comment is unterminated.
    """

    characters = list(source)
    state = _JsoncScanState.NORMAL
    block_start: int | None = None
    index = 0
    while index < len(characters):
        character = characters[index]
        following = characters[index + 1] if index + 1 < len(characters) else ""
        if state is _JsoncScanState.STRING:
            if character == "\\":
                state = _JsoncScanState.ESCAPE
            elif character == '"':
                state = _JsoncScanState.NORMAL
            index += 1
            continue
        if state is _JsoncScanState.ESCAPE:
            state = _JsoncScanState.STRING
            index += 1
            continue
        if state is _JsoncScanState.LINE_COMMENT:
            if character in "\r\n":
                state = _JsoncScanState.NORMAL
            else:
                characters[index] = " "
            index += 1
            continue
        if state is _JsoncScanState.BLOCK_COMMENT:
            if character == "*" and following == "/":
                characters[index] = " "
                characters[index + 1] = " "
                state = _JsoncScanState.NORMAL
                index += 2
                continue
            if character not in "\r\n":
                characters[index] = " "
            index += 1
            continue
        if character == '"':
            state = _JsoncScanState.STRING
            index += 1
            continue
        if character == "/" and following == "/":
            characters[index] = " "
            characters[index + 1] = " "
            state = _JsoncScanState.LINE_COMMENT
            index += 2
            continue
        if character == "/" and following == "*":
            characters[index] = " "
            characters[index + 1] = " "
            block_start = index
            state = _JsoncScanState.BLOCK_COMMENT
            index += 2
            continue
        index += 1
    if state is _JsoncScanState.BLOCK_COMMENT:
        assert block_start is not None
        line = source.count("\n", 0, block_start) + 1
        column = block_start - source.rfind("\n", 0, block_start)
        raise JsoncDecodeError(
            f"unterminated block comment at line {line}, column {column}"
        )
    return "".join(characters)


def _json_object_without_duplicate_keys(
    pairs: list[tuple[str, JSONValue]],
) -> dict[str, JSONValue]:
    """@brief 构造无重复键的 JSON 对象 / Build a JSON object without duplicate keys.

    @param pairs JSON 解码器给出的成员对 / Member pairs emitted by the JSON decoder.
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
    """@brief 拒绝 NaN 和 Infinity / Reject NaN and Infinity.

    @param token 非标准数值 token / Non-standard numeric token.
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
    @note ``json.loads`` 会把合法词法形式 ``1e999`` 转成 ``inf``；配置中的
        超时、容量等数值不能接受该非有限结果。/
        ``json.loads`` turns lexically valid ``1e999`` into ``inf``; configuration
        values such as timeouts and capacities must not accept that non-finite result.
    """

    value = float(token)
    if not isfinite(value):
        raise JsoncDecodeError(
            f"non-finite JSON numeric value {token!r} is not allowed"
        )
    return value


#: @brief AI provider 的受限名称 / Closed set of AI provider names.
ProviderName: TypeAlias = Literal[
    "openai",
    "openrouter",
    "siliconflow",
    "gemini",
    "zai",
    "azure",
]
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
#: @brief 来自 JSON 数组的不可变 provider 序列 / Immutable provider sequence decoded from a JSON array.
ProviderTuple: TypeAlias = Annotated[
    tuple[ProviderName, ...],
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
    shutdown_grace_seconds: PositiveFloat = 30.0

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
    lease_seconds: PositiveInt = 1800


class InboxRuntimeSettings(_FrozenSettings):
    """@brief Durable inbox worker 设置 / Durable inbox worker settings."""

    worker_count: PositiveInt = 16
    poll_interval_seconds: PositiveFloat = 0.1
    lease_seconds: PositiveInt = 60


class InferenceRuntimeSettings(_FrozenSettings):
    """@brief 推理 worker 设置 / Inference worker settings."""

    worker_count: PositiveInt = 8
    poll_interval_seconds: PositiveFloat = 0.25
    provider_timeout_seconds: PositiveInt = 90
    lease_seconds: PositiveInt = 180
    attempt_timeout_seconds: PositiveInt = 120

    @model_validator(mode="after")
    def _validate_lease(self) -> InferenceRuntimeSettings:
        """@brief 确保 lease 覆盖一次尝试 / Ensure the lease covers one attempt.

        @return 已验证的推理设置 / Validated inference settings.
        @raise ValueError lease 过短时抛出 / Raised when the lease is too short.
        """

        if self.lease_seconds < self.attempt_timeout_seconds:
            raise ValueError("lease_seconds must be >= attempt_timeout_seconds")
        return self


class OutboxRuntimeSettings(_FrozenSettings):
    """@brief Durable outbox worker 设置 / Durable outbox worker settings."""

    worker_count: PositiveInt = 16
    poll_interval_seconds: PositiveFloat = 0.1
    lease_seconds: PositiveInt = 60
    attempt_timeout_seconds: PositiveInt = 25

    @model_validator(mode="after")
    def _validate_lease(self) -> OutboxRuntimeSettings:
        """@brief 确保 lease 覆盖投递尝试 / Ensure the lease covers delivery attempts.

        @return 已验证的 outbox 设置 / Validated outbox settings.
        @raise ValueError lease 过短时抛出 / Raised when the lease is too short.
        """

        if self.lease_seconds < self.attempt_timeout_seconds:
            raise ValueError("lease_seconds must be >= attempt_timeout_seconds")
        return self


class CompactionRuntimeSettings(_FrozenSettings):
    """@brief 上下文压缩 worker 设置 / Context-compaction worker settings."""

    worker_count: PositiveInt = 2
    poll_interval_seconds: PositiveFloat = 0.5
    provider_timeout_seconds: PositiveInt = 30
    attempt_timeout_seconds: PositiveInt = 120
    lease_seconds: PositiveInt = 180

    @model_validator(mode="after")
    def _validate_lease(self) -> CompactionRuntimeSettings:
        """@brief 确保压缩 lease 覆盖尝试 / Ensure compaction lease covers attempts.

        @return 已验证的压缩设置 / Validated compaction settings.
        @raise ValueError lease 过短时抛出 / Raised when the lease is too short.
        """

        if self.lease_seconds < self.attempt_timeout_seconds:
            raise ValueError("lease_seconds must be >= attempt_timeout_seconds")
        return self


class DreamingRuntimeSettings(_FrozenSettings):
    """@brief 用户画像整合 worker 设置 / User-profile consolidation worker settings."""

    worker_count: PositiveInt = 2
    batch_size: PositiveInt = 4
    source_batch_size: PositiveInt = 32
    max_events_per_job: PositiveInt = 64
    max_evidence_characters: PositiveInt = 60_000
    poll_interval_seconds: PositiveFloat = 1.0
    refresh_seconds: PositiveInt = 21_600
    provider_timeout_seconds: PositiveInt = 60
    attempt_timeout_seconds: PositiveInt = 90
    lease_seconds: PositiveInt = 120
    max_attempts: PositiveInt = 5

    @model_validator(mode="after")
    def _validate_lease(self) -> DreamingRuntimeSettings:
        """@brief 确保 profile lease 覆盖尝试 / Ensure profile lease covers attempts.

        @return 已验证的 dreaming 设置 / Validated dreaming settings.
        @raise ValueError lease 过短时抛出 / Raised when the lease is too short.
        """

        if self.lease_seconds < self.attempt_timeout_seconds:
            raise ValueError("lease_seconds must be >= attempt_timeout_seconds")
        return self


class RetrievalWorkerSettings(_FrozenSettings):
    """@brief 语义检索 worker 设置 / Semantic-retrieval worker settings."""

    worker_count: PositiveInt = 2
    batch_size: PositiveInt = 16
    poll_interval_seconds: PositiveFloat = 0.5
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


class ProviderModels(_FrozenSettings):
    """@brief 单个 provider 的任务模型目录 / Task-model catalog for one provider."""

    chat: str | None = None
    chat_fallback: str | None = None
    vision: str | None = None
    summary: str | None = None
    summary_fallback: str | None = None
    dreaming: str | None = None
    translation: str | None = None

    def for_task(
        self, task: Literal["chat", "summary", "dreaming", "translation"]
    ) -> tuple[str, ...]:
        """@brief 返回任务的主/回退模型链 / Return primary and fallback model chain for a task.

        @param task 推理任务 / Inference task.
        @return 去除空值与重复项后的模型元组 / Tuple of non-empty, deduplicated models.
        """

        values: tuple[str | None, ...]
        match task:
            case "chat":
                primary, fallback = self.chat, self.chat_fallback
                values = (primary, fallback, self.vision)
            case "summary":
                primary, fallback = self.summary, self.summary_fallback
                values = (primary, fallback)
            case "dreaming":
                primary, fallback = self.dreaming or self.summary, None
                values = (primary, fallback)
            case "translation":
                primary, fallback = self.translation, None
                values = (primary, fallback)
        configured = tuple(value for value in values if value)
        return tuple(dict.fromkeys(configured))


class OpenAICompatibleProviderSettings(_FrozenSettings):
    """@brief OpenAI-compatible provider 设置 / OpenAI-compatible provider settings."""

    api_key: SecretStr | None = None
    api_base: str | None = None
    models: ProviderModels = Field(default_factory=ProviderModels)


class GeminiProviderSettings(OpenAICompatibleProviderSettings):
    """@brief Gemini provider 设置 / Gemini provider settings."""

    openai_compatible: bool = False


class AzureProviderSettings(_FrozenSettings):
    """@brief Azure OpenAI provider 设置 / Azure OpenAI provider settings."""

    api_key: SecretStr | None = None
    endpoint: str | None = None
    api_version: str | None = None
    deployment: str | None = None
    models: ProviderModels = Field(default_factory=ProviderModels)


class AiProvidersSettings(_FrozenSettings):
    """@brief 所有 AI provider 凭据与模型 / Credentials and models for all AI providers."""

    openai: OpenAICompatibleProviderSettings = Field(
        default_factory=lambda: OpenAICompatibleProviderSettings(
            models=ProviderModels(
                chat="gpt-4o",
                summary="gpt-4o-mini",
                dreaming="gpt-4o-mini",
                translation="gpt-4o-mini",
            )
        )
    )
    openrouter: OpenAICompatibleProviderSettings = Field(
        default_factory=lambda: OpenAICompatibleProviderSettings(
            api_base="https://openrouter.ai/api/v1",
            models=ProviderModels(
                chat="anthropic/claude-sonnet-4.5",
                summary="openai/gpt-4o-mini",
                dreaming="openai/gpt-4o-mini",
                translation="openai/gpt-4o-mini",
            ),
        )
    )
    siliconflow: OpenAICompatibleProviderSettings = Field(
        default_factory=lambda: OpenAICompatibleProviderSettings(
            api_base="https://api.siliconflow.cn/v1",
            models=ProviderModels(
                chat="deepseek-ai/DeepSeek-V4-Flash",
                summary="deepseek-ai/DeepSeek-V4-Flash",
                translation="deepseek-ai/DeepSeek-V4-Flash",
            ),
        )
    )
    gemini: GeminiProviderSettings = Field(
        default_factory=lambda: GeminiProviderSettings(
            models=ProviderModels(
                chat="gemini-3.5-flash",
                chat_fallback="gemini-2.5-flash-lite",
                summary="gemini-3-flash-preview",
                summary_fallback="gemini-2.5-flash-lite",
            )
        )
    )
    zai: OpenAICompatibleProviderSettings = Field(
        default_factory=lambda: OpenAICompatibleProviderSettings(
            api_base="https://open.bigmodel.cn/api/paas/v4",
            models=ProviderModels(
                chat="glm-4.7-flash",
                translation="glm-4.7-flash",
            ),
        )
    )
    azure: AzureProviderSettings = Field(
        default_factory=lambda: AzureProviderSettings(api_version="2024-12-01-preview")
    )

    def for_name(
        self, provider: ProviderName
    ) -> OpenAICompatibleProviderSettings | AzureProviderSettings:
        """@brief 获取指定 provider 的设置 / Get settings for a named provider.

        @param provider 受支持的 provider 名称 / Supported provider name.
        @return 对应 provider 的不可变设置 / Corresponding immutable provider settings.
        """

        match provider:
            case "openai":
                return self.openai
            case "openrouter":
                return self.openrouter
            case "siliconflow":
                return self.siliconflow
            case "gemini":
                return self.gemini
            case "zai":
                return self.zai
            case "azure":
                return self.azure


class AiTaskRouteSettings(_FrozenSettings):
    """@brief 单个后台 AI 任务的路由 / Route for one background AI task."""

    provider: ProviderName | None = None
    fallback_provider: ProviderName | None = None

    def ordered_providers(self) -> ProviderTuple:
        """@brief 返回去重后的 provider 顺序 / Return deduplicated provider order.

        @return 主 provider 后接回退 provider / Primary provider followed by fallback provider.
        """

        values = tuple(
            value for value in (self.provider, self.fallback_provider) if value
        )
        return cast(ProviderTuple, tuple(dict.fromkeys(values)))


class AiChatRouteSettings(_FrozenSettings):
    """@brief 主聊天 AI 路由 / Primary chat AI route."""

    provider_order: ProviderTuple = ("gemini", "zai", "siliconflow")
    text_only_models: StringTuple = ("deepseek-ai/DeepSeek-V4-Flash",)

    @field_validator("provider_order")
    @classmethod
    def _reject_duplicate_providers(cls, value: ProviderTuple) -> ProviderTuple:
        """@brief 拒绝重复 provider / Reject duplicate providers.

        @param value provider 顺序 / Provider order.
        @return 已验证顺序 / Validated order.
        @raise ValueError 出现重复 provider 时抛出 / Raised when a provider repeats.
        """

        if len(set(value)) != len(value):
            raise ValueError("provider_order must not contain duplicates")
        return value


class AiRoutingSettings(_FrozenSettings):
    """@brief AI 任务路由配置 / AI task-routing configuration."""

    chat: AiChatRouteSettings = Field(default_factory=AiChatRouteSettings)
    summary: AiTaskRouteSettings = Field(
        default_factory=lambda: AiTaskRouteSettings(provider="gemini")
    )
    dreaming: AiTaskRouteSettings = Field(
        default_factory=lambda: AiTaskRouteSettings(provider="gemini")
    )
    translation: AiTaskRouteSettings = Field(
        default_factory=lambda: AiTaskRouteSettings(provider="zai")
    )

    def for_task(
        self,
        task: Literal["chat", "summary", "dreaming", "translation"],
    ) -> ProviderTuple:
        """@brief 返回指定任务的 provider 顺序 / Return provider order for a task.

        @param task 推理任务 / Inference task.
        @return 已验证、不可变的 provider 顺序 / Validated immutable provider order.
        """

        if task == "chat":
            return self.chat.provider_order
        match task:
            case "summary":
                return self.summary.ordered_providers()
            case "dreaming":
                order = self.dreaming.ordered_providers()
                return order or self.summary.ordered_providers()
            case "translation":
                return self.translation.ordered_providers()


class AiSettings(_FrozenSettings):
    """@brief AI provider、模型和路由设置 / AI provider, model, and routing settings."""

    routing: AiRoutingSettings = Field(default_factory=AiRoutingSettings)
    providers: AiProvidersSettings = Field(default_factory=AiProvidersSettings)


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


class HistoryCacheSettings(_FrozenSettings):
    """@brief 会话历史缓存设置 / Conversation-history cache settings."""

    capacity: PositiveInt = 256
    ttl_seconds: PositiveFloat = 900.0


class AssistantSettings(_FrozenSettings):
    """@brief Assistant 记忆、上下文与检索设置 / Assistant memory, context, and retrieval settings."""

    context_window: ContextWindowSettings = Field(default_factory=ContextWindowSettings)
    working_memory: WorkingMemorySettings = Field(default_factory=WorkingMemorySettings)
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    history_cache: HistoryCacheSettings = Field(default_factory=HistoryCacheSettings)


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

    @return 当前工作目录中的 config.json / ``config.json`` in the current working directory.
    """

    return Path.cwd() / "config.json"


def read_bot_settings(path: Path | None = None) -> BotSettings:
    """@brief 从 JSONC 文档读取 Bot 所需配置 / Read the Bot configuration projection from JSONC.

    @param path 可选 config.json 路径 / Optional config.json path.
    @return 严格、不可变的 Bot 设置 / Strict immutable Bot settings.
    @raise ConfigurationError JSONC 或 Bot 拥有字段无效时抛出 /
        Raised when JSONC or Bot-owned fields are invalid.
    """

    source_path = path or default_config_path()
    try:
        document = _load_jsonc(source_path)
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
    "AiProvidersSettings",
    "AiSettings",
    "AiTaskRouteSettings",
    "ApplicationDatabaseSettings",
    "AssistantSettings",
    "AzureProviderSettings",
    "BotDatabaseSettings",
    "BotSettings",
    "ConfigurationError",
    "ContextWindowSettings",
    "DatabaseEndpointSettings",
    "EconomySettings",
    "IdentitySettings",
    "IntegrationsSettings",
    "LoggingSettings",
    "NetworkSettings",
    "ObservabilitySettings",
    "ProviderModels",
    "ProviderName",
    "RuntimeSettings",
    "SCHEMA_VERSION",
    "TelegramSettings",
    "default_config_path",
    "read_bot_settings",
    "reveal_secret",
]
