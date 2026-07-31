"""@brief 0072 AttachmentImportIntent migration 的静态 CTest / Static CTest for the 0072 AttachmentImportIntent migration."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
"""@brief 仓库根目录 / Repository root directory."""

_SOURCE_ROOT = _PROJECT_ROOT / "src"
"""@brief src-layout Python 根目录 / Python src-layout root directory."""

if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from fogmoe_dbctl.migrations import runner  # noqa: E402

_VERSION_PATH = (
    _PROJECT_ROOT
    / "src/fogmoe_dbctl/migrations/versions/0072_workspace_attachment_import_intents.py"
)
"""@brief 0072 Alembic revision 路径 / 0072 Alembic revision path."""

_SQL_PATH = (
    _PROJECT_ROOT
    / "src/fogmoe_dbctl/migrations/sql/postgresql/0072_workspace_attachment_import_intents.sql"
)
"""@brief 0072 PostgreSQL migration SQL 路径 / 0072 PostgreSQL migration SQL path."""

_SCHEMA_PATH = _PROJECT_ROOT / "src/fogmoe_dbctl/schema.sql"
"""@brief DDL-only schema snapshot 路径 / DDL-only schema snapshot path."""


class WorkspaceAttachmentIntentMigrationTests(unittest.TestCase):
    """@brief intent aggregate、receipt gate 与 unavailable fence 的静态回归契约 / Static regression contract for the intent aggregate, receipt gate, and unavailable fence."""

    def test_revision_is_an_irreversible_child_of_0071(self) -> None:
        """@brief 0072 只接 0071，回退明确拒绝 / 0072 follows only 0071 and explicitly refuses downgrade.

        @return None / None.
        """

        version = _VERSION_PATH.read_text(encoding="utf-8")
        self.assertIn('revision = "0072_workspace_attachment_import_intents"', version)
        self.assertIn(
            'down_revision = "0071_workspace_attachment_import_receipts"', version
        )
        self.assertIn('run_migration_sql(__file__, "up")', version)
        downgrade = runner._sections(_SQL_PATH.read_text(encoding="utf-8"), _SQL_PATH)[
            "down"
        ]
        self.assertIn("0072 is irreversible", downgrade)
        self.assertIn("native-recovery provenance", downgrade)
        self.assertIn("ERRCODE = '0A000'", downgrade)

    def test_existing_0071_receipts_are_backfilled_before_new_gates(self) -> None:
        """@brief 已部署 receipt 在安装 pending-only trigger 前按相同字段回填 intent / Deployed receipts backfill intents with identical fields before installing the pending-only trigger.

        @return None / None.
        """

        upgrade = runner._sections(_SQL_PATH.read_text(encoding="utf-8"), _SQL_PATH)[
            "up"
        ]
        table = upgrade.index("CREATE TABLE workspace.attachment_import_intents")
        backfill = upgrade.index("INSERT INTO workspace.attachment_import_intents")
        intent_trigger = upgrade.index(
            "CREATE TRIGGER workspace_attachment_import_intents_validate_tr"
        )
        self.assertLess(table, backfill)
        self.assertLess(backfill, intent_trigger)
        for fragment in (
            "FROM workspace.attachment_import_receipts AS receipt",
            "receipt.imported_at",
            "source_message_id UUID NOT NULL UNIQUE",
            "prepared_at TIMESTAMPTZ NOT NULL",
            "Stop old workers",
        ):
            self.assertIn(fragment, upgrade)

    def test_receipt_requires_exact_intent_and_prepared_intent_blocks_unavailable(
        self,
    ) -> None:
        """@brief 新 receipt 精确匹配 intent；prepared intent 阻止 final unavailable / New receipts exactly match intents; a prepared intent blocks final unavailable.

        @return None / None.
        """

        upgrade = runner._sections(_SQL_PATH.read_text(encoding="utf-8"), _SQL_PATH)[
            "up"
        ]
        for fragment in (
            "CREATE OR REPLACE FUNCTION workspace.validate_attachment_import_receipt",
            "workspace.validate_attachment_import_binding",
            "receipt requires its exact previously prepared import intent",
            "intent.request_hash = NEW.request_hash",
            "intent.runtime_path = NEW.runtime_path",
            "intent.byte_size = NEW.byte_size",
            "intent.sha256 = NEW.sha256",
            "FROM workspace.attachment_import_intents AS intent",
            "without a receipt or prepared intent",
        ):
            self.assertIn(fragment, upgrade)
        self.assertLess(
            upgrade.index(
                "CREATE FUNCTION workspace.validate_attachment_import_intent"
            ),
            upgrade.index(
                "CREATE OR REPLACE FUNCTION workspace.validate_attachment_import_receipt"
            ),
        )

    def test_snapshot_advances_to_0072_with_intent_aggregate_and_fences(self) -> None:
        """@brief snapshot 前移 0072 并保留 aggregate、receipt gate 与 unavailable fence / Snapshot advances to 0072 and retains aggregate, receipt gate, and unavailable fence.

        @return None / None.
        """

        snapshot = _SCHEMA_PATH.read_text(encoding="utf-8")
        for fragment in (
            "through 0075_user_profile_dream_state",
            "Alembic head: 0075_user_profile_dream_state",
            "CREATE TABLE workspace.attachment_import_intents",
            "workspace_attachment_import_intents_validate_tr",
            "workspace_attachment_import_intents_immutable_tr",
            "workspace.validate_attachment_import_binding",
            "exact previously prepared import intent",
            "without a receipt or prepared intent",
        ):
            self.assertIn(fragment, snapshot)


if __name__ == "__main__":
    unittest.main()
