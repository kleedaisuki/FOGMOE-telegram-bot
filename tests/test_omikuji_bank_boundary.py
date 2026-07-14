"""@brief 御神签银行边界契约 / Bank-boundary contract for Omikuji."""

from __future__ import annotations

import ast
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""@brief 项目根目录 / Project root directory."""

OMIKUJI_ADAPTER = (
    PROJECT_ROOT
    / "src"
    / "fogmoe_bot"
    / "infrastructure"
    / "database"
    / "game_operations"
    / "omikuji.py"
)
"""@brief 御神签 PostgreSQL adapter 路径 / Omikuji PostgreSQL adapter path."""


def _string_literals(source: str) -> tuple[str, ...]:
    """@brief 提取 Python 源码中的字符串字面量 / Extract string literals from Python source.

    @param source Python 源码 / Python source.
    @return 所有字符串字面量 / All string literals.
    """

    tree = ast.parse(source)
    return tuple(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


def test_omikuji_uses_bank_free_wallet_as_its_only_balance_source() -> None:
    """@brief 御神签不再锁定或读写 identity 金币投影 / Omikuji no longer locks or reads/writes the identity token projection.

    @return None / None.
    """

    source = OMIKUJI_ADAPTER.read_text(encoding="utf-8")
    strings = _string_literals(source)
    identity_queries = tuple(text for text in strings if "identity.users" in text)

    assert identity_queries == ("SELECT 1 FROM identity.users WHERE id = %s",)
    assert "_lock_account" not in source
    assert "_AccountOperations" not in source
    assert "UPDATE identity.users" not in source
    assert "lock_bank_account_balances" in source
    assert "TokenBucket.FREE" in source
    assert "post_bank_transfer" in source
