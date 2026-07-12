"""@brief Domain 依赖方向测试 / Domain dependency-direction tests."""

import ast
from pathlib import Path


def test_domain_depends_only_on_domain_and_standard_or_third_party_libraries() -> None:
    """@brief Domain 不得依赖外层包 / Domain must not depend on outer layers."""
    domain_root = Path(__file__).parents[1] / "src" / "fogmoe_bot" / "domain"
    forbidden_prefixes = (
        "fogmoe_bot.application",
        "fogmoe_bot.infrastructure",
        "fogmoe_bot.presentation",
    )
    violations: list[str] = []
    for path in domain_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                forbidden_prefixes
            ):
                violations.append(f"{path.relative_to(domain_root)}:{node.lineno}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(forbidden_prefixes):
                        violations.append(
                            f"{path.relative_to(domain_root)}:{node.lineno}"
                        )

    assert violations == []


def test_application_depends_on_ports_not_adapters_or_transport_sdks() -> None:
    """@brief Application 仅依赖内层类型与端口 / Application depends on inner types and ports, not adapters or transport SDKs."""

    application_root = Path(__file__).parents[1] / "src" / "fogmoe_bot" / "application"
    forbidden_prefixes = (
        "fogmoe_bot.infrastructure",
        "fogmoe_bot.presentation",
        "telegram",
        "sqlalchemy",
        "aiohttp",
        "requests",
        "litellm",
        "e2b",
    )
    violations: list[str] = []
    for path in application_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                forbidden_prefixes
            ):
                violations.append(f"{path.relative_to(application_root)}:{node.lineno}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(forbidden_prefixes):
                        violations.append(
                            f"{path.relative_to(application_root)}:{node.lineno}"
                        )

    assert violations == []
