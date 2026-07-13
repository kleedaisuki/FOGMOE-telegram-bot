"""@brief 出站代理传输边界 / Outbound proxy transport boundary.

此模块不读取 ``config.json``、不缓存配置对象。组合根把已验证的
``NetworkSettings`` 显式写入标准进程环境；随后各个既有 HTTP adapter 只消费该
传输边界，以避免把网络设置扩散成跨层的全局配置服务。
"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import SplitResult, urlsplit

import aiohttp
import requests
from aiohttp_socks import ProxyConnector

from fogmoe_bot.config import NetworkSettings


#: @brief 支持的代理协议 / Supported proxy schemes.
SUPPORTED_PROXY_SCHEMES = frozenset({"http", "https", "socks5", "socks5h"})
#: @brief SOCKS 代理协议集合 / SOCKS proxy schemes.
SOCKS_PROXY_SCHEMES = frozenset({"socks5", "socks5h"})
#: @brief 标准代理环境变量 / Standard proxy environment variable names.
PROXY_ENVIRONMENT_VARIABLES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)
#: @brief 会话工厂读取代理时的优先级 / Proxy lookup precedence for session factories.
_PROXY_LOOKUP_VARIABLES = (
    "ALL_PROXY",
    "all_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
)

#: @brief 本模块的结构化日志器 / Structured logger for this module.
logger = logging.getLogger(__name__)


def outbound_proxy_url(settings: NetworkSettings) -> str | None:
    """@brief 校验显式网络设置中的代理 / Validate the explicit network-settings proxy.

    @param settings 已由 Bot 配置边界解析的网络设置 / Network settings parsed by the Bot configuration boundary.
    @return 已配置的代理 URL；未配置时返回 ``None`` / Configured proxy URL, or ``None``.
    @raise ValueError 代理格式或协议不受支持 / Raised for an invalid or unsupported proxy URL.
    """

    return _validated_proxy_url(settings.proxy_url)


def configure_proxy_environment(settings: NetworkSettings) -> None:
    """@brief 显式导出第三方 SDK 所需代理环境 / Explicitly export proxy environment for third-party SDKs.

    @param settings 已解析的出站网络设置 / Parsed outbound network settings.
    @return None / None.
    @note 未设置代理时会移除继承的代理变量，令 ``config.json`` 成为唯一的
        进程代理输入 / When no proxy is configured, inherited proxy variables are removed so
        ``config.json`` is the sole process proxy input.
    """

    proxy_url = outbound_proxy_url(settings)
    if proxy_url is None:
        for variable_name in PROXY_ENVIRONMENT_VARIABLES:
            os.environ.pop(variable_name, None)
        logger.info("Configured direct outbound networking without a proxy")
        return

    for variable_name in PROXY_ENVIRONMENT_VARIABLES:
        os.environ[variable_name] = proxy_url
    logger.info(
        "Configured unified outbound proxy: %s", _redacted_proxy_endpoint(proxy_url)
    )


def create_requests_session() -> requests.Session:
    """@brief 创建由进程网络边界配置的 Requests 会话 / Create a Requests session configured by the process network boundary.

    @return 不继承任意额外环境变量的 Requests 会话 / Requests session that does not inherit arbitrary environment variables.
    @note 调用 ``configure_proxy_environment`` 后，本函数从其标准代理变量中恢复
        已验证的 URL；它绝不读取配置文件或缓存配置对象 / After
        ``configure_proxy_environment``, this function recovers the validated URL from standard
        proxy variables; it never reads a configuration file or caches settings.
    """

    session = requests.Session()
    session.trust_env = False
    proxy_url = _environment_proxy_url()
    if proxy_url is not None:
        session.proxies = {"http": proxy_url, "https": proxy_url}
    return session


def create_aiohttp_session(**kwargs: Any) -> aiohttp.ClientSession:
    """@brief 创建由进程网络边界配置的 aiohttp 会话 / Create an aiohttp session configured by the process network boundary.

    @param kwargs 传给 ``aiohttp.ClientSession`` 的额外参数 / Extra ClientSession arguments.
    @return 已绑定 HTTP 或 SOCKS 代理的异步会话 / Async session bound to the configured HTTP or SOCKS proxy.
    @raise ValueError 调用方提供与统一代理冲突的连接器或 proxy / Raised when a caller supplies a connector or proxy that conflicts with the unified proxy.
    """

    session_kwargs = {"trust_env": False, **kwargs}
    proxy_url = _environment_proxy_url()
    if proxy_url is None:
        return aiohttp.ClientSession(**session_kwargs)

    if "proxy" in session_kwargs:
        raise ValueError(
            "a configured proxy cannot be combined with a custom aiohttp proxy"
        )
    scheme = urlsplit(proxy_url).scheme.lower()
    if scheme in SOCKS_PROXY_SCHEMES:
        if "connector" in session_kwargs:
            raise ValueError(
                "a SOCKS proxy cannot be combined with a custom aiohttp connector"
            )
        connector = ProxyConnector.from_url(proxy_url)
        return aiohttp.ClientSession(connector=connector, **session_kwargs)
    return aiohttp.ClientSession(proxy=proxy_url, **session_kwargs)


def configure_litellm_proxy(litellm_module: Any, settings: NetworkSettings) -> None:
    """@brief 配置 LiteLLM 使用已注入的统一代理 / Configure LiteLLM to use the injected unified proxy.

    @param litellm_module 已导入的 LiteLLM 模块 / Imported LiteLLM module.
    @param settings 已解析的出站网络设置 / Parsed outbound network settings.
    @return None / None.
    @note 应在启动组合根调用一次；该函数不写环境变量，也不读取配置 / Call
        once from the startup composition root; this function neither writes environment variables
        nor reads configuration. SOCKS 走 HTTPX transport，以支持 ``socksio`` /
        SOCKS uses the HTTPX transport to support ``socksio``.
    """

    if outbound_proxy_url(settings) is None:
        return
    litellm_module.use_aiohttp_transport = False
    litellm_module.disable_aiohttp_transport = True


def _environment_proxy_url() -> str | None:
    """@brief 从已注入的标准环境恢复代理 / Recover a proxy from the injected standard environment.

    @return 已校验的代理 URL；不存在时返回 ``None`` / Validated proxy URL, or ``None`` when absent.
    @raise ValueError 环境中出现非法代理 URL / Raised when the injected environment has an invalid proxy URL.
    """

    for variable_name in _PROXY_LOOKUP_VARIABLES:
        proxy_url = os.environ.get(variable_name)
        if proxy_url:
            return _validated_proxy_url(proxy_url)
    return None


def _validated_proxy_url(value: str | None) -> str | None:
    """@brief 校验可选代理 URL / Validate an optional proxy URL.

    @param value 原始 URL 或空值 / Raw URL or empty value.
    @return 已去除首尾空白的 URL；空值返回 ``None`` / Trimmed URL, or ``None`` when empty.
    @raise ValueError URL 缺少主机、端口无效或协议不支持 / Raised when a host is missing, a port is invalid, or the scheme is unsupported.
    """

    proxy_url = (value or "").strip()
    if not proxy_url:
        return None
    parsed = urlsplit(proxy_url)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("network.proxy_url contains an invalid port") from error
    if parsed.scheme.lower() not in SUPPORTED_PROXY_SCHEMES or not parsed.hostname:
        supported = ", ".join(sorted(SUPPORTED_PROXY_SCHEMES))
        raise ValueError(
            f"network.proxy_url must use one of {supported} and include a hostname"
        )
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("network.proxy_url contains an invalid port")
    return proxy_url


def _redacted_proxy_endpoint(proxy_url: str) -> str:
    """@brief 生成不含凭据的代理端点 / Build a credential-free proxy endpoint.

    @param proxy_url 已校验的代理 URL / Validated proxy URL.
    @return 可安全记录的协议与主机 / Log-safe scheme and host.
    """

    parsed: SplitResult = urlsplit(proxy_url)
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    endpoint = f"{parsed.scheme}://{host}"
    if parsed.port is not None:
        endpoint = f"{endpoint}:{parsed.port}"
    return endpoint
