"""@brief Assistant 路由值对象 / Assistant routing value objects.

路由是一次模型尝试所需的完整、不可变配置投影。它不再依赖配置名称、全局 provider
表或 client 内的 provider 分支。/
Routes are complete immutable configuration projections for model attempts. They no longer depend
on configuration names, global provider tables, or provider branches inside a client.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, TypeAlias
from urllib.parse import urlsplit

from fogmoe_bot.domain.assistant.request_metadata import (
    RequestMetaError,
    normalize_request_meta,
)

#: @brief 已支持的 provider wire protocol 风格 / Supported provider wire-protocol styles.
ProviderStyle: TypeAlias = Literal["openai", "anthropic"]

#: @brief 必须由 route 语义字段控制的 HTTP headers / HTTP headers controlled by route semantic fields.
_RESERVED_HEADERS = frozenset({"authorization", "x-api-key", "anthropic-version"})


def _freeze_mapping(values: Mapping[str, str]) -> Mapping[str, str]:
    """@brief 复制并冻结字符串映射 / Copy and freeze a string mapping.

    @param values 待冻结的映射 / Mapping to freeze.
    @return 不可变映射视图 / Immutable mapping view.
    """

    return MappingProxyType(dict(values))


def _validated_headers(values: Mapping[str, str]) -> Mapping[str, str]:
    """@brief 校验并冻结用户自定义 HTTP headers / Validate and freeze user-defined HTTP headers.

    @param values 认证之外的自定义 header 映射 / Custom header mapping excluding authentication.
    @return 已校验的不可变 header 映射 / Validated immutable header mapping.
    @raise ValueError header 会覆盖协议字段或含 CR/LF 时抛出 /
        Raised when a header overrides a protocol field or contains CR/LF.
    """

    normalized: dict[str, str] = {}
    for name, value in values.items():
        if not isinstance(name, str) or not name.strip() or any(
            character in name for character in "\r\n:"
        ):
            raise ValueError("ProviderRoute.headers keys must be HTTP header names")
        if not isinstance(value, str) or "\r" in value or "\n" in value:
            raise ValueError("ProviderRoute.headers values must not contain CR or LF")
        if name.casefold() in _RESERVED_HEADERS:
            raise ValueError(
                "ProviderRoute.headers cannot override authentication or protocol headers"
            )
        normalized[name] = value
    return _freeze_mapping(normalized)


def _validated_meta(values: Mapping[str, str]) -> Mapping[str, str]:
    """@brief 校验并冻结 route metadata / Validate and freeze route metadata.

    @param values 用户定义的 metadata 映射 / User-defined metadata mapping.
    @return 已校验的不可变 metadata 映射 / Validated immutable metadata mapping.
    @raise ValueError metadata 不是安全字符串映射时抛出 /
        Raised when metadata is not a safe string mapping.
    """

    try:
        return normalize_request_meta(values)
    except RequestMetaError as error:
        raise ValueError(f"ProviderRoute.meta {error}") from error


@dataclass(frozen=True, slots=True)
class ProviderAuth:
    """@brief Provider 请求认证材料 / Provider request authentication material.

    @param api_key 可选 API key；repr 中隐藏 / Optional API key, hidden from repr.
    @param header 携带 key 的 HTTP header / HTTP header carrying the key.
    @param prefix key 之前的字面前缀 / Literal prefix preceding the key.
    """

    api_key: str | None = field(default=None, repr=False)
    header: str = "Authorization"
    prefix: str = "Bearer "

    def __post_init__(self) -> None:
        """@brief 校验认证 header 的传输安全性 / Validate authentication-header transport safety.

        @return None / None.
        @raise ValueError header 或 prefix 可注入额外 header 时抛出 /
            Raised when header or prefix can inject another header.
        """

        if not isinstance(self.header, str) or not self.header.strip() or any(
            character in self.header for character in "\r\n:"
        ):
            raise ValueError("ProviderAuth.header must be one HTTP header name")
        if not isinstance(self.prefix, str) or "\r" in self.prefix or "\n" in self.prefix:
            raise ValueError("ProviderAuth.prefix must not contain CR or LF")
        if self.api_key is not None and not isinstance(self.api_key, str):
            raise ValueError("ProviderAuth.api_key must be a string or None")


@dataclass(frozen=True, slots=True)
class RouteModel:
    """@brief 同一 provider 内的一个模型候选 / One model candidate within one provider.

    @param name provider 可识别的模型名称 / Model name understood by the provider.
    @param accepts_images 是否可接收图像内容块 / Whether the model accepts image content blocks.
    """

    name: str
    accepts_images: bool = False

    def __post_init__(self) -> None:
        """@brief 校验模型名 / Validate the model name.

        @return None / None.
        @raise ValueError 模型名为空时抛出 / Raised when the model name is blank.
        """

        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("RouteModel.name must not be blank")


@dataclass(frozen=True, slots=True)
class ProviderRoute:
    """@brief 一个自包含、可回退的 provider 路由 / One self-contained fallback-capable provider route.

    @param route_id task 内稳定候选标识；供 circuit 与 telemetry 使用 /
        Stable candidate identity inside a task, used by circuit and telemetry.
    @param provider_id 操作员配置的动态 provider ID / Dynamic provider ID configured by the operator.
    @param provider_label 面向日志与用户的显示名 / Display name for logs and users.
    @param style HTTP wire protocol 风格 / HTTP wire-protocol style.
    @param endpoint 完整请求 URL；client 不拼接路径 / Complete request URL; clients append no path.
    @param auth API key header 认证材料 / API-key header authentication material.
    @param headers 除认证与协议 header 外的自定义 headers / Custom headers apart from auth and protocol headers.
    @param api_version Anthropic 请求必需的 API version / API version required by Anthropic requests.
    @param models 保序的同 provider 模型 fallback 链 / Ordered same-provider model fallback chain.
    @param supports_tools endpoint 是否支持 tools / Whether the endpoint supports tools.
    @param strict_tools 是否请求严格工具 schema / Whether to request strict tool schemas.
    @param disabled_tools 即使支持 tools 也要排除的工具名 / Tools excluded despite tool support.
    @param safety_block_is_terminal safety block 是否禁止继续 fallback /
        Whether a safety block forbids further fallback.
    @param meta 协议风格限定的请求 metadata / Wire-style-constrained request metadata.
    """

    route_id: str
    provider_id: str
    provider_label: str
    style: ProviderStyle
    endpoint: str
    auth: ProviderAuth
    models: tuple[RouteModel, ...]
    headers: Mapping[str, str] = field(default_factory=dict)
    api_version: str | None = None
    supports_tools: bool = True
    strict_tools: bool = False
    disabled_tools: tuple[str, ...] = ()
    safety_block_is_terminal: bool = False
    meta: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """@brief 强化 route 的领域不变量 / Strengthen domain invariants for a route.

        @return None / None.
        @raise ValueError route 不完整或协议语义冲突时抛出 /
            Raised when a route is incomplete or its protocol semantics conflict.
        """

        for field_name, value in (
            ("route_id", self.route_id),
            ("provider_id", self.provider_id),
            ("provider_label", self.provider_label),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"ProviderRoute.{field_name} must not be blank")
        if self.style not in {"openai", "anthropic"}:
            raise ValueError("ProviderRoute.style must be openai or anthropic")
        if not isinstance(self.auth, ProviderAuth):
            raise ValueError("ProviderRoute.auth must be ProviderAuth")
        if not isinstance(self.endpoint, str):
            raise ValueError("ProviderRoute.endpoint must be a string")
        if not isinstance(self.headers, Mapping):
            raise ValueError("ProviderRoute.headers must be a string mapping")
        if not isinstance(self.meta, Mapping):
            raise ValueError("ProviderRoute.meta must be a string mapping")
        endpoint = urlsplit(self.endpoint)
        if (
            endpoint.scheme not in {"http", "https"}
            or not endpoint.netloc
            or endpoint.path in {"", "/"}
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError("ProviderRoute.endpoint must be a complete HTTP request URL")
        if not self.models:
            raise ValueError("ProviderRoute.models must not be empty")
        if not all(isinstance(model, RouteModel) for model in self.models):
            raise ValueError("ProviderRoute.models must contain RouteModel values")
        names = tuple(model.name for model in self.models)
        if len(set(names)) != len(names):
            raise ValueError("ProviderRoute.models must not contain duplicate names")
        if self.strict_tools and not self.supports_tools:
            raise ValueError("ProviderRoute.strict_tools requires supports_tools")
        if not all(isinstance(tool, str) for tool in self.disabled_tools):
            raise ValueError("ProviderRoute.disabled_tools must contain strings")
        normalized_disabled = tuple(tool.strip() for tool in self.disabled_tools)
        if not all(normalized_disabled):
            raise ValueError("ProviderRoute.disabled_tools must not contain blank names")
        if len(set(normalized_disabled)) != len(normalized_disabled):
            raise ValueError("ProviderRoute.disabled_tools must not contain duplicates")
        if self.style == "anthropic":
            if (
                not isinstance(self.api_version, str)
                or not self.api_version.strip()
            ):
                raise ValueError("Anthropic ProviderRoute requires api_version")
            if set(self.meta) - {"user_id"}:
                raise ValueError("Anthropic ProviderRoute.meta only supports user_id")
        elif self.api_version is not None:
            raise ValueError("OpenAI ProviderRoute must not set api_version")
        object.__setattr__(self, "headers", _validated_headers(self.headers))
        object.__setattr__(self, "meta", _validated_meta(self.meta))
        object.__setattr__(self, "disabled_tools", normalized_disabled)


__all__ = ["ProviderAuth", "ProviderRoute", "ProviderStyle", "RouteModel"]
