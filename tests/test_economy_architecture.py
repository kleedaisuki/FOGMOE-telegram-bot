"""@brief Economy 领域所有权与依赖方向测试 / Economy domain ownership and dependency-direction tests."""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""@brief 项目根目录 / Project root directory."""

SRC_ROOT = PROJECT_ROOT / "src" / "fogmoe_bot"
"""@brief FogMoe Bot 源码根目录 / FogMoe Bot source root."""


def _imported_modules(path: Path) -> tuple[str, ...]:
    """@brief 提取一个 Python 模块的绝对 import / Extract absolute imports from one Python module.

    @param path 待解析 Python 文件 / Python file to parse.
    @return 模块名元组 / Tuple of imported module names.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.append(node.module)
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
    return tuple(modules)


def test_check_in_lifecycle_has_one_domain_owner_and_no_reward_facade() -> None:
    """@brief 签到生命周期归领域且旧混合奖励 facade 被物理删除 /
    Check-in lifecycle belongs to Domain and the mixed reward facade is physically removed.

    @return None / None.
    """

    domain_path = SRC_ROOT / "domain" / "economy" / "check_in.py"
    application_path = SRC_ROOT / "application" / "economy" / "check_in.py"
    adapter_path = SRC_ROOT / "infrastructure" / "database" / "economy" / "check_in.py"
    retired_paths = (
        SRC_ROOT / "application" / "economy" / "rewards.py",
        SRC_ROOT / "infrastructure" / "database" / "economy" / "rewards.py",
    )

    assert domain_path.is_file()
    assert application_path.is_file()
    assert adapter_path.is_file()
    assert [path for path in retired_paths if path.exists()] == []

    domain_source = domain_path.read_text(encoding="utf-8")
    application_source = application_path.read_text(encoding="utf-8")
    adapter_source = adapter_path.read_text(encoding="utf-8")
    for symbol in (
        "class CheckInStreak",
        "class CheckInStreakLength",
        "class CheckInReward",
        "def claim(",
    ):
        assert symbol in domain_source
    assert "// 5" not in application_source
    assert "timedelta(days=1)" not in adapter_source
    assert "streak.claim(command.day)" in adapter_source

    forbidden_imports = {
        "fogmoe_bot.application.economy.rewards",
        "fogmoe_bot.infrastructure.database.economy.rewards",
    }
    offenders: list[str] = []
    for root in (SRC_ROOT, PROJECT_ROOT / "tests"):
        for path in root.rglob("*.py"):
            for module in _imported_modules(path):
                if module in forbidden_imports:
                    offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{module}")
    assert offenders == []


def test_daily_lottery_lifecycle_and_prize_policy_have_domain_ownership() -> None:
    """@brief 抽奖资格与概率规则归领域，随机生成和 SQL 仅由外层适配 /
    Lottery eligibility and prize policy belong to Domain while randomness and SQL stay outside.

    @return None / None.
    """

    domain_path = SRC_ROOT / "domain" / "economy" / "lottery.py"
    application_path = SRC_ROOT / "application" / "economy" / "lottery.py"
    service_path = SRC_ROOT / "application" / "economy" / "service.py"
    adapter_path = SRC_ROOT / "infrastructure" / "database" / "economy" / "lottery.py"
    randomness_path = SRC_ROOT / "infrastructure" / "economy" / "randomness.py"

    assert domain_path.is_file()
    assert application_path.is_file()
    assert adapter_path.is_file()
    assert randomness_path.is_file()
    domain_source = domain_path.read_text(encoding="utf-8")
    application_source = application_path.read_text(encoding="utf-8")
    service_source = service_path.read_text(encoding="utf-8")
    adapter_source = adapter_path.read_text(encoding="utf-8")
    randomness_source = randomness_path.read_text(encoding="utf-8")

    for symbol in (
        "class DailyLottery",
        "class LotteryClaimInstant",
        "class LotteryPrize",
        "class LotteryRandomness",
        "def draw_lottery_prize(",
        "def claim(",
    ):
        assert symbol in domain_source
    assert "timedelta(hours=24)" in domain_source
    assert "class LotteryClaimTransaction" in application_source
    assert "class PostgresLotteryClaimTransaction" in adapter_source
    assert "PostgresLotteryOperations" not in adapter_source
    assert "lottery.claim(" in adapter_source
    assert "timedelta" not in adapter_source
    assert "command.cooldown" not in adapter_source
    assert "import random" in randomness_source
    assert "import random" not in domain_source
    assert "import random" not in application_source
    assert "import random" not in service_source
    assert "cooldown:" not in application_source
    assert "prize: int | None" not in service_source


def test_economy_domain_and_application_do_not_depend_on_outer_adapters() -> None:
    """@brief Economy 核心只允许依赖内层代码 / Economy core may depend only on inner-layer code.

    @return None / None.
    """

    roots = (
        SRC_ROOT / "domain" / "economy",
        SRC_ROOT / "application" / "economy",
    )
    forbidden_prefixes = (
        "fogmoe_bot.infrastructure",
        "fogmoe_bot.presentation",
        "telegram",
        "sqlalchemy",
        "pydantic",
    )
    offenders: list[str] = []
    for root in roots:
        for path in root.rglob("*.py"):
            for module in _imported_modules(path):
                if module.startswith(forbidden_prefixes):
                    offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{module}")
    assert offenders == []
