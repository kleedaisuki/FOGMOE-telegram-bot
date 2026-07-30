"""@brief 0069 Workspace runtime 迁移的静态 CTest 合约 / Static CTest contract for the 0069 Workspace-runtime migration."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
"""@brief 仓库根目录 / Repository root directory."""

_SOURCE_ROOT = _PROJECT_ROOT / "src"
"""@brief Python src-layout 根目录 / Python src-layout root directory."""

if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from fogmoe_dbctl.migrations import runner  # noqa: E402

_VERSION_PATH = (
    _PROJECT_ROOT / "src/fogmoe_dbctl/migrations/versions/0069_workspace_runtimes.py"
)
"""@brief 0069 Alembic revision 文件 / 0069 Alembic revision file."""

_SQL_PATH = (
    _PROJECT_ROOT
    / "src/fogmoe_dbctl/migrations/sql/postgresql/0069_workspace_runtimes.sql"
)
"""@brief 0069 PostgreSQL 迁移 SQL 文件 / 0069 PostgreSQL migration SQL file."""

_SCHEMA_PATH = _PROJECT_ROOT / "src/fogmoe_dbctl/schema.sql"
"""@brief DDL-only schema snapshot / DDL-only schema snapshot."""


class WorkspaceMigrationTests(unittest.TestCase):
    """@brief 迁移 fail-closed 语义与 snapshot 同步检查 / Checks for migration fail-closed semantics and snapshot synchronization."""

    def test_revision_has_the_expected_single_parent(self) -> None:
        """@brief 0069 只衔接 0068 canonical-message head / 0069 attaches only to the 0068 canonical-message head.

        @return None / None.
        """

        version = _VERSION_PATH.read_text(encoding="utf-8")
        self.assertIn('revision = "0069_workspace_runtimes"', version)
        self.assertIn('down_revision = "0068_canonical_assistant_messages"', version)
        self.assertIn('run_migration_sql(__file__, "up")', version)
        self.assertIn('run_migration_sql(__file__, "down")', version)

    def test_preflight_locks_and_drains_every_replay_path(self) -> None:
        """@brief 迁移锁住状态机，并拒绝旧 checkpoint/receipt 的可重放路径 / Migration locks state machines and rejects replayable old checkpoint/receipt paths.

        @return None / None.
        """

        sections = runner._sections(_SQL_PATH.read_text(encoding="utf-8"), _SQL_PATH)
        upgrade = sections["up"]
        self.assertIn("LOCK TABLE conversation.inference_activities", upgrade)
        self.assertIn("assistant.tool_agent_steps", upgrade)
        self.assertIn("assistant.tool_effect_receipts", upgrade)
        self.assertIn("IN SHARE ROW EXCLUSIVE MODE", upgrade)
        self.assertIn("status NOT IN ('completed', 'failed', 'cancelled')", upgrade)
        self.assertIn("requires a drained inference queue", upgrade)
        self.assertIn("tool_name = 'execute_python_code'", upgrade)
        self.assertIn("status <> 'succeeded'", upgrade)
        self.assertIn("non-succeeded row(s)", upgrade)
        self.assertIn("failed_final", upgrade)
        self.assertNotIn("DELETE FROM assistant.tool_effect_receipts", upgrade)
        self.assertNotIn("UPDATE assistant.tool_agent_steps", upgrade)

    def test_legacy_history_is_retained_without_semantic_translation(self) -> None:
        """@brief 成功旧 receipt/checkpoint 只保留为审计，绝不翻译为 Bash / Successful old receipts/checkpoints remain audit only and are never translated to Bash.

        @return None / None.
        """

        upgrade = runner._sections(_SQL_PATH.read_text(encoding="utf-8"), _SQL_PATH)[
            "up"
        ]
        self.assertIn("Successful historical ``execute_python_code`` receipts", upgrade)
        self.assertIn("They are not translated to", upgrade)
        self.assertIn(
            "Judge0 and a persistent Workspace have different authority", upgrade
        )
        self.assertIn("no retained checkpoint can execute legacy Python again", upgrade)

    def test_workspace_identity_is_unique_and_immutable(self) -> None:
        """@brief runtime identity 仅按 personal/group scope 唯一，且禁止静默重绑 / Runtime identity is unique only by personal/group scope and forbids silent rebinding.

        @return None / None.
        """

        upgrade = runner._sections(_SQL_PATH.read_text(encoding="utf-8"), _SQL_PATH)[
            "up"
        ]
        self.assertIn("CREATE SCHEMA IF NOT EXISTS workspace", upgrade)
        self.assertIn("CREATE TABLE workspace.runtimes", upgrade)
        self.assertIn("runtime_key UUID PRIMARY KEY", upgrade)
        self.assertIn("scope_kind IN ('personal', 'group')", upgrade)
        self.assertIn(
            "CONSTRAINT workspace_runtimes_scope_uq UNIQUE (scope_kind, scope_id)",
            upgrade,
        )
        self.assertIn("scope_kind = 'personal' AND scope_id > 0", upgrade)
        self.assertIn("scope_kind = 'group' AND scope_id <> 0", upgrade)
        self.assertIn("CREATE FUNCTION workspace.forbid_runtime_mutation", upgrade)
        self.assertIn("CREATE TRIGGER workspace_runtimes_immutable_tr", upgrade)
        self.assertIn("BEFORE UPDATE OR DELETE ON workspace.runtimes", upgrade)
        self.assertNotIn("ConversationId", upgrade)
        self.assertNotIn("message_thread_id", upgrade)

    def test_migration_uses_controlled_access_policy_not_a_second_grant_surface(
        self,
    ) -> None:
        """@brief 新 schema 不在迁移中私自创建漂移的 app GRANT / New schema does not create a drifting app GRANT surface inside the migration.

        @return None / None.
        """

        upgrade = runner._sections(_SQL_PATH.read_text(encoding="utf-8"), _SQL_PATH)[
            "up"
        ]
        self.assertIn("No direct application-role GRANT appears here", upgrade)
        self.assertNotIn("GRANT SELECT", upgrade)
        self.assertNotIn("GRANT INSERT", upgrade)
        self.assertIn("REVOKE ALL PRIVILEGES ON SCHEMA workspace FROM PUBLIC", upgrade)

    def test_downgrade_is_explicitly_irreversible(self) -> None:
        """@brief 回退会孤儿化 host overlay，故必须拒绝 / Downgrade would orphan host overlays and must be rejected.

        @return None / None.
        """

        downgrade = runner._sections(_SQL_PATH.read_text(encoding="utf-8"), _SQL_PATH)[
            "down"
        ]
        self.assertIn("0069 is irreversible", downgrade)
        self.assertIn("orphan recoverable host overlays", downgrade)
        self.assertIn("ERRCODE = '0A000'", downgrade)

    def test_sql_splitter_preserves_lock_and_trigger_function_bodies(self) -> None:
        """@brief runner SQL splitter 不会拆坏 PL/pgSQL trigger body / Runner SQL splitter does not break the PL/pgSQL trigger body.

        @return None / None.
        """

        upgrade = runner._sections(_SQL_PATH.read_text(encoding="utf-8"), _SQL_PATH)[
            "up"
        ]
        statements = runner._split_sql_statements(upgrade)
        self.assertGreaterEqual(len(statements), 13)
        self.assertTrue(
            any(
                "LOCK TABLE conversation.inference_activities" in item
                for item in statements
            )
        )
        self.assertTrue(
            any(
                "CREATE FUNCTION workspace.forbid_runtime_mutation" in item
                for item in statements
            )
        )
        self.assertTrue(
            any(
                "CREATE TRIGGER workspace_runtimes_immutable_tr" in item
                for item in statements
            )
        )

    def test_snapshot_retains_the_0069_workspace_delta_at_the_current_head(
        self,
    ) -> None:
        """@brief schema snapshot 保留 0069 Workspace DDL 且同步当前 head / Schema snapshot retains 0069 Workspace DDL while synchronizing the current head.

        @return None / None.
        """

        snapshot = _SCHEMA_PATH.read_text(encoding="utf-8")
        self.assertIn("through 0073_streaming_turn_steering", snapshot)
        self.assertIn(
            "Alembic head: 0073_streaming_turn_steering", snapshot
        )
        self.assertIn("CREATE SCHEMA IF NOT EXISTS workspace", snapshot)
        self.assertIn("CREATE TABLE workspace.runtimes", snapshot)
        self.assertIn("CREATE FUNCTION workspace.forbid_runtime_mutation", snapshot)
        self.assertIn("CREATE TRIGGER workspace_runtimes_immutable_tr", snapshot)
        self.assertIn("'town', 'chance', 'personal_rpg', 'workspace'", snapshot)


if __name__ == "__main__":
    unittest.main()
