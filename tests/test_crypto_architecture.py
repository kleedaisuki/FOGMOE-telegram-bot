"""@brief Crypto 领域与 adapter 静态边界测试 / Crypto domain and adapter static-boundary tests."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""@brief 项目根目录 / Project root directory."""

SRC_ROOT = PROJECT_ROOT / "src" / "fogmoe_bot"
"""@brief Python 源码包根目录 / Python source-package root."""


def test_btc_market_pattern_is_domain_owned_and_provider_neutral() -> None:
    """@brief BTC 行情规则归领域且不泄漏 Binance 细节 / BTC market rules are domain-owned without Binance leakage."""

    domain_path = SRC_ROOT / "domain" / "crypto" / "market_pattern.py"
    application_path = SRC_ROOT / "application" / "crypto" / "market_monitor.py"
    adapter_path = SRC_ROOT / "infrastructure" / "crypto" / "binance_monitor.py"
    domain_source = domain_path.read_text(encoding="utf-8")
    application_source = application_path.read_text(encoding="utf-8")
    adapter_source = adapter_path.read_text(encoding="utf-8")

    assert "class MarketCandle" in domain_source
    assert "class PatternTrigger" in domain_source
    assert "class RedRedGreenPattern" in domain_source
    assert "class PatternTrigger" not in application_source
    assert "class _Candle" not in adapter_source
    assert "def _body_ratio" not in adapter_source
    assert "def _price_change" not in adapter_source
    assert "body_ratio_threshold" not in adapter_source
    assert "green_vs_red_ratio" not in adapter_source
    for provider_term in ("Binance", "BTCUSDT", "ClientError", "UMFutures"):
        assert provider_term not in domain_source
