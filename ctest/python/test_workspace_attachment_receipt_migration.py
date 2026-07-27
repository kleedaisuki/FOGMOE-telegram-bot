"""@brief 0071 Workspace 附件 durable receipt 的 CTest / CTest for 0071 Workspace-attachment durable receipts."""

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

from fogmoe_bot.domain.workspace.attachment import (  # noqa: E402
    workspace_attachment_blocks_compaction,
    workspace_attachment_is_model_visible,
)
from fogmoe_dbctl.migrations import runner  # noqa: E402

_VERSION_PATH = (
    _PROJECT_ROOT
    / "src/fogmoe_dbctl/migrations/versions/0071_workspace_attachment_import_receipts.py"
)
"""@brief 0071 Alembic revision 文件 / 0071 Alembic revision file."""

_SQL_PATH = (
    _PROJECT_ROOT
    / "src/fogmoe_dbctl/migrations/sql/postgresql/0071_workspace_attachment_import_receipts.sql"
)
"""@brief 0071 PostgreSQL 迁移 SQL 文件 / 0071 PostgreSQL migration SQL file."""

_SCHEMA_PATH = _PROJECT_ROOT / "src/fogmoe_dbctl/schema.sql"
"""@brief DDL-only schema snapshot / DDL-only schema snapshot."""

_RETRIEVAL_PATH = _PROJECT_ROOT / "src/fogmoe_bot/infrastructure/database/retrieval.py"
"""@brief Episodic retrieval source adapter / Episodic retrieval source adapter."""

_PROFILE_PATH = (
    _PROJECT_ROOT / "src/fogmoe_bot/infrastructure/database/user_profile/source.py"
)
"""@brief Profile evidence source adapter / Profile evidence source adapter."""

_INFERENCE_REPOSITORY_PATH = (
    _PROJECT_ROOT
    / "src/fogmoe_bot/infrastructure/database/conversation_workflow/inference.py"
)
"""@brief PostgreSQL inference workflow repository / PostgreSQL inference workflow repository."""


class WorkspaceAttachmentReceiptMigrationTests(unittest.TestCase):
    """@brief receipt、模型可见性与最终失败状态机的静态回归契约 / Static regression contracts for receipt, model-visibility, and final-failure state machines."""

    def test_revision_is_one_way_child_of_the_attachment_boundary(self) -> None:
        """@brief 0071 只接 0070，回退明确拒绝 / 0071 follows only 0070 and explicitly refuses downgrade.

        @return None / None.
        """

        version = _VERSION_PATH.read_text(encoding="utf-8")
        self.assertIn('revision = "0071_workspace_attachment_import_receipts"', version)
        self.assertIn(
            'down_revision = "0070_workspace_attachment_model_boundary"', version
        )
        self.assertIn('run_migration_sql(__file__, "up")', version)
        downgrade = runner._sections(_SQL_PATH.read_text(encoding="utf-8"), _SQL_PATH)[
            "down"
        ]
        self.assertIn("0071 is irreversible", downgrade)
        self.assertIn("fabricate native publication semantics", downgrade)
        self.assertIn("ERRCODE = '0A000'", downgrade)

    def test_migration_drains_derivatives_and_terminalizes_unreceipted_history(
        self,
    ) -> None:
        """@brief 迁移排空派生任务，并把没有 receipt 的历史行终结为 unavailable / Migration drains derivative jobs and terminalizes receipt-less history as unavailable.

        @return None / None.
        """

        upgrade = runner._sections(_SQL_PATH.read_text(encoding="utf-8"), _SQL_PATH)[
            "up"
        ]
        for relation in (
            "conversation.conversation_messages",
            "conversation.inference_activities",
            "context_window.compactions",
            "retrieval.passage_vectors",
            "user_profile.dreams",
        ):
            self.assertIn(relation, upgrade)
        self.assertIn("requires a drained inference queue", upgrade)
        self.assertIn("requires drained context compactions", upgrade)
        self.assertIn("wspctl_0071_unreceipted_attachment_turns", upgrade)
        self.assertIn("'voice', 'audio', 'video', 'animation', 'video_note'", upgrade)
        self.assertIn("'state', 'unavailable'", upgrade)
        self.assertIn("Never infer a native write from a textual placeholder", upgrade)
        legacy_section = upgrade[
            : upgrade.index("CREATE TABLE workspace.attachment_import_receipts")
        ]
        self.assertNotIn(
            "INSERT INTO workspace.attachment_import_receipts", legacy_section
        )
        self.assertIn("DELETE FROM context_window.compactions", upgrade)
        self.assertIn("DELETE FROM retrieval.passages", upgrade)
        self.assertIn("DELETE FROM user_profile.evidence_events", upgrade)

    def test_receipt_is_immutable_and_binds_source_turn_scope_and_fixed_path(
        self,
    ) -> None:
        """@brief receipt 表及 insert trigger 同时绑定 source、Turn、scope 与路径 / Receipt table and insert trigger bind source, Turn, scope, and path together.

        @return None / None.
        """

        upgrade = runner._sections(_SQL_PATH.read_text(encoding="utf-8"), _SQL_PATH)[
            "up"
        ]
        for fragment in (
            "CREATE TABLE workspace.attachment_import_receipts",
            "source_message_id UUID NOT NULL UNIQUE",
            "scope_kind IN ('personal', 'group')",
            "request_id = turn_id::TEXT || ':attachment-import'",
            "^/workspace/uploads/attachment-[0-9a-f]{64}/payload$",
            "CREATE FUNCTION workspace.validate_attachment_import_receipt",
            "workspace_attachment_import_receipts_validate_tr",
            "CREATE FUNCTION workspace.forbid_attachment_import_receipt_mutation",
            "workspace_attachment_import_receipts_immutable_tr",
            "BEFORE UPDATE OR DELETE ON workspace.attachment_import_receipts",
            "matching durable current_turn_upload request",
            "scope does not match its durable command",
            "exact pending source placeholder",
        ):
            self.assertIn(fragment, upgrade)
        self.assertIn("IS DISTINCT FROM", upgrade)
        self.assertIn("IS NOT TRUE", upgrade)
        self.assertNotIn(
            "source_row.content #>> '{workspace_attachment,state}' <>", upgrade
        )

    def test_receipt_then_marker_transition_is_atomic_and_reverse_order_is_rejected(
        self,
    ) -> None:
        """@brief receipt 必须先插入、随后 pending→imported，且提交时再次验证 / A receipt must insert before pending→imported and is verified again at commit.

        @return None / None.
        """

        upgrade = runner._sections(_SQL_PATH.read_text(encoding="utf-8"), _SQL_PATH)[
            "up"
        ]
        insert_guard = upgrade.index(
            "CREATE FUNCTION workspace.validate_attachment_import_receipt"
        )
        visibility_guard = upgrade.index(
            "CREATE FUNCTION workspace.guard_attachment_visibility_transition"
        )
        self.assertLess(insert_guard, visibility_guard)
        for fragment in (
            "CREATE CONSTRAINT TRIGGER workspace_attachment_import_receipts_commit_tr",
            "DEFERRABLE INITIALLY DEFERRED",
            "receipt must commit with its source marker imported",
            "pending-to-imported or pending-to-unavailable",
            "imported marker requires its matching durable receipt",
        ):
            self.assertIn(fragment, upgrade)
        self.assertNotIn("pending-to-imported or imported-to", upgrade)

    def test_unavailable_is_only_a_fenced_final_attachment_transition(self) -> None:
        """@brief pending→unavailable 只允许最终失败、无 receipt 的当前附件 / pending→unavailable is allowed only for a finally failed current attachment without a receipt.

        @return None / None.
        """

        upgrade = runner._sections(_SQL_PATH.read_text(encoding="utf-8"), _SQL_PATH)[
            "up"
        ]
        repository = _INFERENCE_REPOSITORY_PATH.read_text(encoding="utf-8")
        self.assertIn("activity.status = 'failed'", upgrade)
        for fragment in (
            "jsonb_typeof(activity.request -> 'current_turn_upload') IS NOT DISTINCT FROM 'object'",
            "jsonb_typeof(activity.request #> '{scope,is_group}') IS NOT DISTINCT FROM 'boolean'",
            "jsonb_typeof(activity.request #> '{scope,message_id}') IS NOT DISTINCT FROM 'number'",
            "jsonb_typeof(activity.request #> '{current_turn_upload,source_message_id}')",
            "(activity.request #>> '{scope,message_id}' ~ '^[1-9][0-9]*$') IS TRUE",
            "(activity.request #>> '{current_turn_upload,source_message_id}' ~ '^[1-9][0-9]*$') IS TRUE",
            "IS NOT DISTINCT FROM activity.request #>> '{current_turn_upload,source_message_id}'",
            "jsonb_typeof(activity.request #> '{scope,group_id}') = 'number'",
            "jsonb_typeof(activity.request #> '{user,user_id}') = 'number'",
        ):
            self.assertIn(fragment, upgrade)
        self.assertIn("without a receipt", upgrade)
        self.assertIn("_terminalize_pending_current_attachment", repository)
        self.assertLess(
            repository.index("_terminalize_pending_current_attachment("),
            repository.index(
                "failed = turn.transition",
                repository.index("async def fail_inference_activity"),
            ),
        )
        terminalizer_start = repository.index(
            "async def _terminalize_pending_current_attachment"
        )
        terminalizer = repository[terminalizer_start : terminalizer_start + 4000]
        self.assertIn("unavailable", terminalizer)
        self.assertIn("jsonb_set", terminalizer)
        self.assertIn("'{workspace_attachment,state}' = 'pending'", terminalizer)
        self.assertNotIn("'imported'::JSONB", terminalizer)

    def test_imported_receipt_requires_the_exact_canonical_user_placeholder(
        self,
    ) -> None:
        """@brief receipt 插入与提交都绑定单一 canonical V2 文本占位符 / Both receipt insertion and commit bind the sole canonical-V2 text placeholder.

        @return None / None.
        @note 只检查 envelope 的 ``text`` 会允许 raw caption 残留在 ``model_message``；
            因而两个 DB gate 都必须比较完整 JSONB 结构。/ Checking only envelope ``text``
            would allow a raw caption to remain in ``model_message``; both DB gates must therefore
            compare the complete JSONB structure.
        """

        upgrade = runner._sections(_SQL_PATH.read_text(encoding="utf-8"), _SQL_PATH)[
            "up"
        ]
        for source_name, state in (
            ("source_row.content", "pending"),
            ("source_content", "imported"),
        ):
            with self.subTest(source=source_name, state=state):
                self.assertIn(
                    f"{source_name} -> 'model_message' IS DISTINCT FROM jsonb_build_object(",
                    upgrade,
                )
                self.assertIn(
                    f"{source_name} #>> '{{workspace_attachment,state}}' IS DISTINCT FROM '{state}'",
                    upgrade,
                )
        for fragment in (
            "'schema_version', 2",
            "'role', 'user'",
            "'parts', jsonb_build_array(jsonb_build_object(",
            "'type', 'text'",
            "'policy', jsonb_build_object('include_in_context', TRUE)",
            "'meta', '{}'::JSONB",
            "canonical model message",
        ):
            self.assertGreaterEqual(upgrade.count(fragment), 2)

    def test_malformed_marker_variants_are_hidden_and_block_compaction(self) -> None:
        """@brief 字符串/小数/布尔版本、缺字段、null 和非字符串 state 全部 fail-closed / String/decimal/bool versions, missing fields, null, and non-string state all fail closed.

        @return None / None.
        """

        malformed = (
            {"workspace_attachment": {}},
            {"workspace_attachment": {"version": "1", "state": "imported"}},
            {"workspace_attachment": {"version": 1.0, "state": "imported"}},
            {"workspace_attachment": {"version": True, "state": "imported"}},
            {"workspace_attachment": {"version": 1, "state": None}},
            {"workspace_attachment": {"version": 1, "state": 7}},
            {"workspace_attachment": None},
            {"workspace_attachment": {"version": 2, "state": "imported"}},
        )
        for content in malformed:
            with self.subTest(content=content):
                self.assertFalse(workspace_attachment_is_model_visible(content))
                self.assertTrue(workspace_attachment_blocks_compaction(content))

    def test_retrieval_and_profile_queries_use_null_safe_strict_visibility_predicate_everywhere(
        self,
    ) -> None:
        """@brief 候选和两个 LATERAL 聚合都以 NULL-safe v1 imported 条件放行 / Candidate and both LATERAL aggregates allow only a NULL-safe v1 imported condition.

        @return None / None.
        """

        expected_fragments = (
            "jsonb_typeof(attachment_message.content -> 'workspace_attachment') = 'object'",
            "jsonb_typeof(attachment_message.content #> '{workspace_attachment,version}') = 'number'",
            "jsonb_typeof(attachment_message.content #> '{workspace_attachment,state}') = 'string'",
            "'{workspace_attachment,version}' = '1'",
            "'{workspace_attachment,state}' = 'imported'",
            "IS NOT TRUE",
        )
        for path in (_RETRIEVAL_PATH, _PROFILE_PATH):
            source = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                for fragment in expected_fragments:
                    self.assertGreaterEqual(source.count(fragment), 3)
                self.assertGreaterEqual(source.count("IS NOT TRUE"), 3)
                self.assertNotIn("IS DISTINCT FROM 'imported'", source)

    def test_snapshot_carries_the_same_receipt_ddl_at_the_0071_head(self) -> None:
        """@brief schema snapshot 前移 0071 且保留所有 receipt/transition DDL / Schema snapshot advances to 0071 and retains all receipt/transition DDL.

        @return None / None.
        """

        snapshot = _SCHEMA_PATH.read_text(encoding="utf-8")
        for fragment in (
            "through 0072_workspace_attachment_import_intents",
            "Alembic head: 0072_workspace_attachment_import_intents",
            "CREATE TABLE workspace.attachment_import_receipts",
            "workspace_attachment_import_receipts_commit_tr",
            "workspace.guard_attachment_visibility_transition",
            "conversation_messages_workspace_attachment_visibility_tr",
        ):
            self.assertIn(fragment, snapshot)


if __name__ == "__main__":
    unittest.main()
