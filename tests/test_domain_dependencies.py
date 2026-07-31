"""@brief Domain 依赖方向测试 / Domain dependency-direction tests."""

import ast
import sys
from pathlib import Path


def test_domain_depends_only_on_domain_and_standard_library() -> None:
    """@brief Domain 只依赖自身与标准库 / Domain depends only on itself and the standard library.

    @return None / None.
    @note 第三方模型与 SDK 属于边界适配器；即使没有反向依赖，它们也不能进入领域层。/
        Third-party models and SDKs belong to boundary adapters and cannot enter the domain even
        when they do not introduce an inward project dependency.
    """

    domain_root = Path(__file__).parents[1] / "src" / "fogmoe_bot" / "domain"
    violations: list[str] = []
    for path in domain_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imported: tuple[str, ...]
            if isinstance(node, ast.ImportFrom):
                if node.level > 0:
                    continue
                imported = (node.module or "",)
            elif isinstance(node, ast.Import):
                imported = tuple(alias.name for alias in node.names)
            else:
                continue
            for module in imported:
                root = module.partition(".")[0]
                if root in sys.stdlib_module_names or module == "fogmoe_bot.domain":
                    continue
                if module.startswith("fogmoe_bot.domain."):
                    continue
                violations.append(
                    f"{path.relative_to(domain_root)}:{node.lineno}:{module}"
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
