"""@brief Domain 依赖方向测试 / Domain dependency-direction tests."""

import ast
from pathlib import Path


def test_domain_never_imports_application_layer() -> None:
    """@brief Domain 不得反向依赖 application / Domain must never depend on application."""
    domain_root = Path(__file__).parents[1] / "src" / "fogmoe_bot" / "domain"
    violations: list[str] = []
    for path in domain_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "fogmoe_bot.application"
            ):
                violations.append(f"{path.relative_to(domain_root)}:{node.lineno}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("fogmoe_bot.application"):
                        violations.append(f"{path.relative_to(domain_root)}:{node.lineno}")

    assert violations == []
