"""@brief 统一出站代理适配 / Unified outbound proxy adapter."""

import logging
import os
from typing import Any
from urllib.parse import urlsplit

import aiohttp
import requests
from aiohttp_socks import ProxyConnector

from fogmoe_bot.infrastructure import config


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

logger = logging.getLogger(__name__)


def outbound_proxy_url() -> str | None:
    """@brief 获取并校验统一出站代理 / Get and validate outbound proxy.

    @return 已配置的代理 URL；未配置时返回 ``None`` / Configured proxy URL, or ``None``.
    @raise ValueError 配置格式或协议不受支持 / Invalid or unsupported proxy URL.
    """
    proxy_url = (config.NETWORK_PROXY_URL or "").strip()
    if not proxy_url:
        return None

    parsed = urlsplit(proxy_url)
    scheme = parsed.scheme.lower()
    if scheme not in SUPPORTED_PROXY_SCHEMES or not parsed.hostname:
        supported = ", ".join(sorted(SUPPORTED_PROXY_SCHEMES))
        raise ValueError(
            f"NETWORK_PROXY_URL must use one of {supported} and include a hostname."
        )
    return proxy_url


def _redacted_proxy_endpoint(proxy_url: str) -> str:
    """@brief 生成不含凭据的代理端点 / Build a credential-free proxy endpoint.

    @param proxy_url 已校验的代理 URL / Validated proxy URL.
    @return 可安全记录的协议与主机 / Log-safe scheme and host.
    """
    parsed = urlsplit(proxy_url)
    endpoint = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port is not None:
        endpoint = f"{endpoint}:{parsed.port}"
    return endpoint


def configure_proxy_environment() -> None:
    """@brief 为第三方 SDK 导出标准代理变量 / Export proxy for third-party SDKs.

    @note 显式会覆盖进程继承的代理变量 / Explicit configuration overrides inherited values.
    """
    proxy_url = outbound_proxy_url()
    if proxy_url is None:
        return

    for variable_name in PROXY_ENVIRONMENT_VARIABLES:
        os.environ[variable_name] = proxy_url
    logger.info(
        "Configured unified outbound proxy: %s", _redacted_proxy_endpoint(proxy_url)
    )


def create_requests_session() -> requests.Session:
    """@brief 创建统一代理 Requests 会话 / Create a uniformly proxied Requests session.

    @return 不受环境变量干扰的 Requests 会话 / Requests session with deterministic proxy settings.
    """
    session = requests.Session()
    session.trust_env = False
    proxy_url = outbound_proxy_url()
    if proxy_url is not None:
        session.proxies = {"http": proxy_url, "https": proxy_url}
    return session


def create_aiohttp_session(**kwargs: Any) -> aiohttp.ClientSession:
    """@brief 创建统一代理 aiohttp 会话 / Create a uniformly proxied aiohttp session.

    @param kwargs 传给 ``aiohttp.ClientSession`` 的额外参数 / Extra ClientSession arguments.
    @return 已绑定 HTTP 或 SOCKS 代理的异步会话 / Async session bound to the configured proxy.
    @raise ValueError 调用方同时传入连接器时无法安全替换 / A connector is supplied with SOCKS proxy.
    """
    session_kwargs = {"trust_env": False, **kwargs}
    proxy_url = outbound_proxy_url()
    if proxy_url is None:
        return aiohttp.ClientSession(**session_kwargs)

    scheme = urlsplit(proxy_url).scheme.lower()
    if scheme in SOCKS_PROXY_SCHEMES:
        if "connector" in session_kwargs:
            raise ValueError(
                "A SOCKS proxy cannot be combined with a custom aiohttp connector."
            )
        connector = ProxyConnector.from_url(proxy_url)
        return aiohttp.ClientSession(connector=connector, **session_kwargs)
    return aiohttp.ClientSession(proxy=proxy_url, **session_kwargs)


def configure_litellm_proxy(litellm_module: Any) -> None:
    """@brief 配置 LiteLLM 使用统一代理 / Configure LiteLLM to use the unified proxy.

    @param litellm_module 已导入的 LiteLLM 模块 / Imported LiteLLM module.
    @note 代理环境变量由应用启动阶段设置一次；此处仅选择 LiteLLM transport。/
    Proxy environment variables are configured once during application startup; this function only selects the LiteLLM transport.
    SOCKS 走 HTTPX transport，以支持 ``socksio`` / SOCKS uses the HTTPX transport.
    """
    if outbound_proxy_url() is None:
        return

    litellm_module.use_aiohttp_transport = False
    litellm_module.disable_aiohttp_transport = True
