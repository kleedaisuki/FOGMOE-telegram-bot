"""@brief 统一出站代理测试 / Unified outbound proxy tests."""

import asyncio
import os

import pytest

from fogmoe_bot.infrastructure import config
from fogmoe_bot.infrastructure.network import proxy
from fogmoe_bot.presentation.telegram import bot_app


class _RecordingApplicationBuilder:
    """@brief 记录构建器调用的替身 / Builder double that records method calls."""

    def __init__(self) -> None:
        """@brief 初始化调用记录 / Initialize call records."""
        self.calls: dict[str, tuple[object, ...]] = {}

    def __getattr__(self, name: str):
        """@brief 记录链式构建器调用 / Record a chained builder invocation.

        @param name 构建器方法名 / Builder method name.
        @return 可链式调用的方法 / Chainable method.
        """
        def recorder(*args: object) -> "_RecordingApplicationBuilder":
            self.calls[name] = args
            return self

        return recorder

    def build(self) -> object:
        """@brief 返回哨兵应用对象 / Return a sentinel application object.

        @return 测试应用哨兵 / Test application sentinel.
        """
        return object()


def test_requests_session_is_direct_when_proxy_is_not_configured(monkeypatch):
    """@brief 未配置时禁用继承代理 / Disable inherited proxies when unset."""
    monkeypatch.setattr(config, "NETWORK_PROXY_URL", None)

    session = proxy.create_requests_session()

    assert session.trust_env is False
    assert session.proxies == {}


def test_telegram_builder_configures_api_and_polling_proxy(monkeypatch):
    """@brief Telegram API 与轮询使用同一代理 / Telegram API and polling share one proxy."""
    builder = _RecordingApplicationBuilder()
    proxy_url = "socks5://127.0.0.1:7891"
    monkeypatch.setattr(config, "NETWORK_PROXY_URL", proxy_url)
    monkeypatch.setattr(bot_app, "ApplicationBuilder", lambda: builder)
    monkeypatch.setattr(bot_app, "register_handlers", lambda application: None)

    bot_app.create_application()

    assert builder.calls["proxy"] == (proxy_url,)
    assert builder.calls["get_updates_proxy"] == (proxy_url,)


def test_requests_session_uses_configured_socks_proxy(monkeypatch):
    """@brief Requests 使用显式 SOCKS 代理 / Requests uses explicit SOCKS proxy."""
    proxy_url = "socks5://127.0.0.1:7891"
    monkeypatch.setattr(config, "NETWORK_PROXY_URL", proxy_url)

    session = proxy.create_requests_session()

    assert session.trust_env is False
    assert session.proxies == {"http": proxy_url, "https": proxy_url}


def test_aiohttp_session_uses_socks_connector(monkeypatch):
    """@brief aiohttp 使用 SOCKS Connector / aiohttp uses a SOCKS connector."""
    monkeypatch.setattr(config, "NETWORK_PROXY_URL", "socks5://127.0.0.1:7891")

    async def create_and_check() -> None:
        session = proxy.create_aiohttp_session()
        try:
            assert session._connector.__class__.__name__ == "ProxyConnector"
            assert session.trust_env is False
        finally:
            await session.close()

    asyncio.run(create_and_check())


def test_aiohttp_session_uses_http_proxy(monkeypatch):
    """@brief aiohttp 使用 HTTP 代理 / aiohttp uses an HTTP proxy."""
    proxy_url = "http://127.0.0.1:7890"
    monkeypatch.setattr(config, "NETWORK_PROXY_URL", proxy_url)

    async def create_and_check() -> None:
        session = proxy.create_aiohttp_session()
        try:
            assert str(session._default_proxy) == proxy_url
            assert session.trust_env is False
        finally:
            await session.close()

    asyncio.run(create_and_check())


def test_configure_proxy_environment_overrides_all_standard_variables(monkeypatch):
    """@brief 标准环境变量被统一覆盖 / Standard environment variables are unified."""
    proxy_url = "socks5h://127.0.0.1:7891"
    monkeypatch.setattr(config, "NETWORK_PROXY_URL", proxy_url)
    for variable_name in proxy.PROXY_ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(variable_name, raising=False)

    proxy.configure_proxy_environment()

    assert {
        variable_name: os.environ[variable_name]
        for variable_name in proxy.PROXY_ENVIRONMENT_VARIABLES
    } == {variable_name: proxy_url for variable_name in proxy.PROXY_ENVIRONMENT_VARIABLES}


@pytest.mark.parametrize(
    "proxy_url",
    ["127.0.0.1:7890", "ftp://127.0.0.1:7890", "socks5://"],
)
def test_invalid_proxy_url_is_rejected_early(monkeypatch, proxy_url):
    """@brief 非法代理配置快速失败 / Invalid proxy configuration fails early."""
    monkeypatch.setattr(config, "NETWORK_PROXY_URL", proxy_url)

    with pytest.raises(ValueError, match="NETWORK_PROXY_URL"):
        proxy.outbound_proxy_url()
