"""@brief 统一出站代理测试 / Unified outbound proxy tests."""

import asyncio
import os
from types import SimpleNamespace
from collections.abc import Coroutine
from typing import Any

import pytest
import telegram.error

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
        return SimpleNamespace(bot=object(), bot_data={})


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
    monkeypatch.setattr(
        bot_app,
        "assemble_handler_capabilities",
        lambda application: None,
    )
    monkeypatch.setattr(bot_app, "install_error_policy", lambda application: None)

    application = bot_app.create_application()

    assert builder.calls["proxy"] == (proxy_url,)
    assert builder.calls["get_updates_proxy"] == (proxy_url,)
    assert builder.calls["job_queue"] == (None,)
    assert builder.calls["updater"] == (None,)
    assert "concurrent_updates" not in builder.calls
    assert application.bot_data == {}


def test_telegram_builder_configures_polling_connection_pool(monkeypatch):
    """@brief 轮询清理请求拥有独立连接槽位 / Polling cleanup has a spare connection slot."""
    builder = _RecordingApplicationBuilder()
    monkeypatch.setattr(config, "NETWORK_PROXY_URL", None)
    monkeypatch.setattr(config, "TELEGRAM_GET_UPDATES_CONNECTION_POOL_SIZE", 2)
    monkeypatch.setattr(bot_app, "ApplicationBuilder", lambda: builder)
    monkeypatch.setattr(
        bot_app,
        "assemble_handler_capabilities",
        lambda application: None,
    )
    monkeypatch.setattr(bot_app, "install_error_policy", lambda application: None)

    application = bot_app.create_application()

    assert builder.calls["get_updates_connection_pool_size"] == (2,)
    assert application.bot_data == {}


def test_run_rebuilds_application_after_transient_bootstrap_failure(monkeypatch):
    """@brief 临时网络错误后退避并重建 Application / Rebuild the Application after a transient bootstrap error."""

    first = object()
    second = object()
    applications = iter([first, second])
    delays: list[float] = []
    attempts = 0

    def run_once(coroutine: Coroutine[Any, Any, None]) -> None:
        """@brief 编排 asyncio.run 结果并关闭未执行 coroutine / Script asyncio.run outcomes and close the unexecuted coroutine.

        @param coroutine 未执行应用 coroutine / Unexecuted application coroutine.
        @return None / None.
        """

        nonlocal attempts
        attempts += 1
        coroutine.close()
        if attempts == 1:
            raise telegram.error.NetworkError("proxy unavailable")

    monkeypatch.setattr(bot_app, "create_application", lambda: next(applications))
    monkeypatch.setattr(bot_app.asyncio, "run", run_once)
    monkeypatch.setattr(bot_app.time, "sleep", delays.append)
    monkeypatch.setattr(config, "TELEGRAM_POLLING_RETRY_INITIAL_DELAY", 1.0)
    monkeypatch.setattr(config, "TELEGRAM_POLLING_RETRY_MAX_DELAY", 30.0)

    bot_app.run()

    assert delays == [1.0]
    assert attempts == 2


def test_run_propagates_nonrecoverable_bootstrap_failure(monkeypatch):
    """@brief 无效 token 等永久错误不应无限重试 / Permanent errors such as bad tokens must not retry forever."""
    application = object()

    def fail(coroutine: Coroutine[Any, Any, None]) -> None:
        """@brief 关闭 coroutine 并抛永久错误 / Close the coroutine and raise a permanent error.

        @param coroutine 未执行应用 coroutine / Unexecuted application coroutine.
        @return None / None.
        """

        coroutine.close()
        raise telegram.error.InvalidToken("bad token")

    monkeypatch.setattr(bot_app, "create_application", lambda: application)
    monkeypatch.setattr(bot_app.asyncio, "run", fail)

    with pytest.raises(telegram.error.InvalidToken):
        bot_app.run()


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
    } == {
        variable_name: proxy_url for variable_name in proxy.PROXY_ENVIRONMENT_VARIABLES
    }


@pytest.mark.parametrize(
    "proxy_url",
    ["127.0.0.1:7890", "ftp://127.0.0.1:7890", "socks5://"],
)
def test_invalid_proxy_url_is_rejected_early(monkeypatch, proxy_url):
    """@brief 非法代理配置快速失败 / Invalid proxy configuration fails early."""
    monkeypatch.setattr(config, "NETWORK_PROXY_URL", proxy_url)

    with pytest.raises(ValueError, match="NETWORK_PROXY_URL"):
        proxy.outbound_proxy_url()
