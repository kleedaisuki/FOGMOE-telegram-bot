"""@brief Assistant 旧计费与 kindness 退役的存储契约测试 / Storage-contract tests for retiring legacy Assistant billing and kindness."""

from __future__ import annotations

import re
from pathlib import Path

from fogmoe_dbctl.migrations import runner


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""@brief 仓库根目录 / Repository root directory."""


def test_0058_fails_closed_then_drops_only_retired_structures() -> None:
    """@brief 0058 先锁定并拒绝未闭合预留，再删除唯一的退役表/触发器 / 0058 locks and rejects unresolved reservations before dropping only retired tables and trigger.

    @return None / None.
    """

    migration_path = (
        _PROJECT_ROOT
        / "src/fogmoe_dbctl/migrations/sql/postgresql/0058_retire_assistant_legacy_billing.sql"
    )
    version_path = (
        _PROJECT_ROOT
        / "src/fogmoe_dbctl/migrations/versions/0058_retire_assistant_legacy_billing.py"
    )
    sections = runner._sections(
        migration_path.read_text(encoding="utf-8"), migration_path
    )
    upgrade = sections["up"]
    version = version_path.read_text(encoding="utf-8")

    for marker in (
        "LOCK TABLE",
        "assistant.billing_reservations",
        "economy.kindness_gifts",
        "IN ACCESS EXCLUSIVE MODE",
        "reservation.status NOT IN ('settled', 'released')",
        "requires manual audit",
        "DROP TRIGGER IF EXISTS assistant_billing_reservations_retired_tr",
        "DROP FUNCTION IF EXISTS bank.forbid_legacy_assistant_billing_mutation()",
        "DROP TABLE assistant.billing_reservations",
        "DROP TABLE economy.kindness_gifts",
    ):
        assert marker in upgrade

    assert "UPDATE identity.users" not in upgrade
    assert "INSERT INTO bank.ledger" not in upgrade
    assert 'revision = "0058_retire_assistant_legacy_billing"' in version
    assert 'down_revision = "0057_billing_renewal_order_integrity"' in version
    assert "irreversible" in sections["down"]


def test_schema_snapshot_has_current_head_without_retired_structures() -> None:
    """@brief DDL 快照停在当前 head 且不声明退役结构 / DDL snapshot ends at the current head and declares no retired structures.

    @return None / None.
    """

    snapshot = (_PROJECT_ROOT / "src/fogmoe_dbctl/schema.sql").read_text(
        encoding="utf-8"
    )

    assert re.search(r"^-- Alembic head: \S+$", snapshot, flags=re.MULTILINE)
    for retired_structure in (
        "assistant.billing_reservations",
        "assistant_billing_reservations_retired_tr",
        "bank.forbid_legacy_assistant_billing_mutation",
        "economy.kindness_gifts",
        "idx_kindness_recipient_created",
        "assistant.asset_action_confirmations",
    ):
        assert retired_structure not in snapshot


def test_0059_is_retained_then_0060_safely_retires_it() -> None:
    """@brief 已部署 revision 可解析，前向迁移在表为空时才退役 / A deployed revision resolves and a forward migration retires it only when empty.

    @return None / None.
    """

    migration_path = (
        _PROJECT_ROOT
        / "src/fogmoe_dbctl/migrations/sql/postgresql/0059_asset_action_confirmations.sql"
    )
    version_path = (
        _PROJECT_ROOT
        / "src/fogmoe_dbctl/migrations/versions/0059_asset_action_confirmations.py"
    )
    retirement_path = (
        _PROJECT_ROOT
        / "src/fogmoe_dbctl/migrations/sql/postgresql/0060_retire_asset_action_confirmations.sql"
    )
    retirement_version_path = (
        _PROJECT_ROOT
        / "src/fogmoe_dbctl/migrations/versions/0060_retire_asset_action_confirmations.py"
    )

    version = version_path.read_text(encoding="utf-8")
    upgrade = runner._sections(
        migration_path.read_text(encoding="utf-8"), migration_path
    )["up"]
    retirement_sections = runner._sections(
        retirement_path.read_text(encoding="utf-8"), retirement_path
    )
    retirement_version = retirement_version_path.read_text(encoding="utf-8")

    assert 'revision = "0059_asset_action_confirmations"' in version
    assert 'down_revision = "0058_retire_assistant_legacy_billing"' in version
    assert "CREATE TABLE assistant.asset_action_confirmations" in upgrade
    for marker in (
        "SET LOCAL lock_timeout = '5s'",
        "SET LOCAL statement_timeout = '30s'",
        "LOCK TABLE assistant.asset_action_confirmations IN ACCESS EXCLUSIVE MODE",
        "FROM assistant.asset_action_confirmations",
        "requires manual audit and archival",
        "DROP TABLE assistant.asset_action_confirmations",
    ):
        assert marker in retirement_sections["up"]
    assert 'revision = "0060_retire_asset_action_confirmations"' in retirement_version
    assert 'down_revision = "0059_asset_action_confirmations"' in retirement_version
    assert "irreversible" in retirement_sections["down"]


def test_runtime_has_no_legacy_assistant_billing_or_kindness_implementation() -> None:
    """@brief 运行时已删除旧计费适配器、币价字段与 no-op kindness 工具 / Runtime deletes legacy billing adapter, price field, and no-op kindness tool.

    @return None / None.
    """

    source_root = _PROJECT_ROOT / "src/fogmoe_bot"
    deleted_paths = (
        source_root / "domain/economy/assistant_billing.py",
        source_root / "infrastructure/database/assistant_billing.py",
        source_root / "infrastructure/assistant/tool_operations/social.py",
    )
    for path in deleted_paths:
        assert not path.exists()

    runtime_sources = (
        source_root / "application/conversation/assistant_ingress.py",
        source_root / "application/conversation/translation_ingress.py",
        source_root / "presentation/telegram/assistant_update_models.py",
        source_root / "infrastructure/database/assistant_turn_acceptance.py",
        source_root / "infrastructure/database/conversation_reset.py",
        source_root / "infrastructure/database/conversation_workflow/inference.py",
        source_root / "infrastructure/database/conversation_workflow/turn.py",
        source_root / "application/assistant/tools/catalog.py",
        source_root / "infrastructure/assistant/tool_operations/dispatcher.py",
    )
    source_text = "\n".join(
        path.read_text(encoding="utf-8") for path in runtime_sources
    )
    for retired_symbol in (
        "coin_cost",
        "AssistantBilling",
        "assistant.billing_reservations",
        "kindness_gift",
        "KindnessGiftArgs",
        "account.kindness_gift",
    ):
        assert retired_symbol not in source_text
