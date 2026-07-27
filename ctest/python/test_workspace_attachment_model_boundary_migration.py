"""@brief 0070 历史附件模型边界迁移的 CTest / CTest for the 0070 historical-attachment model-boundary migration."""

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
    _PROJECT_ROOT
    / "src/fogmoe_dbctl/migrations/versions/0070_workspace_attachment_model_boundary.py"
)
"""@brief 0070 Alembic revision 文件 / 0070 Alembic revision file."""

_SQL_PATH = (
    _PROJECT_ROOT
    / "src/fogmoe_dbctl/migrations/sql/postgresql/0070_workspace_attachment_model_boundary.sql"
)
"""@brief 0070 PostgreSQL 数据迁移 SQL 文件 / 0070 PostgreSQL data-migration SQL file."""

_SCHEMA_PATH = _PROJECT_ROOT / "src/fogmoe_dbctl/schema.sql"
"""@brief DDL-only schema snapshot / DDL-only schema snapshot."""

_RETRIEVAL_SOURCE_PATH = (
    _PROJECT_ROOT / "src/fogmoe_bot/infrastructure/database/retrieval.py"
)
"""@brief Episodic source adapter 路径 / Episodic-source adapter path."""

_PROFILE_SOURCE_PATH = (
    _PROJECT_ROOT / "src/fogmoe_bot/infrastructure/database/user_profile/source.py"
)
"""@brief Profile evidence-source adapter 路径 / Profile-evidence-source adapter path."""

_GROUP_TOOL_CATALOG_PATH = (
    _PROJECT_ROOT / "src/fogmoe_bot/application/assistant/tools/catalog.py"
)
"""@brief 群上下文工具目录路径 / Group-context tool-catalog path."""


class WorkspaceAttachmentModelBoundaryMigrationTests(unittest.TestCase):
    """@brief 历史 raw 附件不得经任一模型派生面回流 / Historical raw attachments must not return through any model-derivation surface."""

    def test_revision_is_a_single_irreversible_child_of_workspace_runtime_identity(self) -> None:
        """@brief 0070 只接在 0069 后，并拒绝不安全回退 / 0070 follows only 0069 and rejects an unsafe downgrade.

        @return None / None.
        """

        version = _VERSION_PATH.read_text(encoding="utf-8")
        self.assertIn('revision = "0070_workspace_attachment_model_boundary"', version)
        self.assertIn('down_revision = "0069_workspace_runtimes"', version)
        self.assertIn('run_migration_sql(__file__, "up")', version)
        self.assertIn('run_migration_sql(__file__, "down")', version)
        downgrade = runner._sections(_SQL_PATH.read_text(encoding="utf-8"), _SQL_PATH)[
            "down"
        ]
        self.assertIn("0070 is irreversible", downgrade)
        self.assertIn("fabricate unsafe workspace semantics", downgrade)
        self.assertIn("ERRCODE = '0A000'", downgrade)

    def test_migration_drains_and_locks_every_replayable_derivative(self) -> None:
        """@brief inference、checkpoint、vector、Dream 与工具 checkpoint 均先排空 / Inference, checkpoints, vectors, Dreams, and tool checkpoints are all drained first.

        @return None / None.
        """

        upgrade = runner._sections(_SQL_PATH.read_text(encoding="utf-8"), _SQL_PATH)[
            "up"
        ]
        for relation in (
            "conversation.conversation_turns",
            "conversation.conversation_messages",
            "conversation.inference_activities",
            "assistant.tool_agent_steps",
            "assistant.tool_effect_receipts",
            "context_window.compactions",
            "retrieval.passage_vectors",
            "user_profile.dreams",
            "conversation.group_message_projection",
        ):
            self.assertIn(relation, upgrade)
        self.assertIn("IN SHARE ROW EXCLUSIVE MODE", upgrade)
        self.assertIn("requires a drained inference queue", upgrade)
        self.assertIn("requires drained context compactions", upgrade)
        self.assertIn("requires a drained retrieval vector queue", upgrade)
        self.assertIn("requires drained Profile Dreaming jobs", upgrade)
        self.assertIn("Operators must stop every", upgrade)

    def test_every_legacy_attachment_turn_is_excluded_instead_of_receiving_a_fake_file_path(self) -> None:
        """@brief 无 add_file receipt 的全部旧媒体 Turn 均排除，而非伪造 Workspace 文件 / Every old media Turn without an add_file receipt is excluded rather than fabricating a Workspace file.

        @return None / None.
        """

        upgrade = runner._sections(_SQL_PATH.read_text(encoding="utf-8"), _SQL_PATH)[
            "up"
        ]
        self.assertIn("wspctl_0070_legacy_attachment_turns", upgrade)
        self.assertIn("IN ('photo', 'sticker', 'document')", upgrade)
        self.assertIn("UPDATE conversation.conversation_messages AS message", upgrade)
        self.assertIn("'{exclude_from_assistant}'", upgrade)
        self.assertIn("full user/assistant/tool chain", upgrade)
        self.assertIn("cannot prove that bytes reached a RuntimeProcess", upgrade)
        self.assertNotIn("workspace_attachment_file_path", upgrade)
        self.assertNotIn("INSERT INTO workspace.", upgrade)

    def test_legacy_placeholder_with_a_second_raw_part_is_still_tainted(self) -> None:
        """@brief 旧 canonical 多段消息不得以首段占位符绕过隔离 / A legacy canonical multi-part message must not bypass isolation through a placeholder first part.

        @return None / None.
        @note Canonical V2 可包含多个 ``text`` 或 ``image`` part；没有 native receipt 时，首段
            的 placeholder 既不能授权第二段 raw caption，也不能授权 image payload。/ Canonical
            V2 permits multiple ``text`` or ``image`` parts. Without a native receipt, a first-part
            placeholder authorizes neither a raw caption in a second part nor an image payload.
        """

        upgrade = runner._sections(_SQL_PATH.read_text(encoding="utf-8"), _SQL_PATH)[
            "up"
        ]
        legacy_insert_start = upgrade.index(
            "INSERT INTO wspctl_0070_legacy_attachment_turns"
        )
        legacy_insert_end = upgrade.index(
            "-- @brief 旧群上下文没有每次读取的 provenance",
            legacy_insert_start,
        )
        legacy_insert = upgrade[legacy_insert_start:legacy_insert_end]
        self.assertEqual(
            legacy_insert,
            "INSERT INTO wspctl_0070_legacy_attachment_turns (turn_id, conversation_id)\n"
            "SELECT DISTINCT message.turn_id, message.conversation_id\n"
            "FROM conversation.conversation_messages AS message\n"
            "WHERE message.role = 'user'\n"
            "  AND message.turn_id IS NOT NULL\n"
            "  AND COALESCE(\n"
            "    message.content #>> '{media,kind}',\n"
            "    message.content ->> 'content_kind'\n"
            "  ) IN ('photo', 'sticker', 'document');\n\n",
        )
        self.assertIn("permit multiple text/image parts", upgrade)
        self.assertNotIn("model_message,parts,0,text", legacy_insert)
        self.assertNotIn("workspace_file path=", legacy_insert)

    def test_all_existing_model_derivatives_are_deleted_or_rebuilt_from_safe_history(self) -> None:
        """@brief 清空 compaction、retrieval、Profile/Dream 与群旁路派生物 / Clear compaction, retrieval, Profile/Dream, and group-side-channel derivatives.

        @return None / None.
        """

        upgrade = runner._sections(_SQL_PATH.read_text(encoding="utf-8"), _SQL_PATH)[
            "up"
        ]
        for required_fragment in (
            "SET predecessor_compaction_id = NULL",
            "DELETE FROM context_window.compactions",
            "DELETE FROM retrieval.source_projections",
            "DELETE FROM retrieval.passages",
            "DELETE FROM user_profile.dream_sources",
            "DELETE FROM user_profile.dreams",
            "UPDATE user_profile.profiles AS profile",
            "observed_through_event_id = 0",
            "DELETE FROM user_profile.profile_revisions",
            "DELETE FROM user_profile.evidence_events",
            "'<group_attachment />'",
            "'[service message]'",
        ):
            self.assertIn(required_fragment, upgrade)
        self.assertIn("forgotten_through boundary", upgrade)
        self.assertIn("restart the new Bot process", upgrade)

    def test_legacy_text_only_group_turn_is_conservatively_tainted(self) -> None:
        """@brief 无 receipt 的旧群旁路可污染纯文本 Turn，迁移必须按 scope 整体隔离 / A receipt-less legacy group side channel can taint a text-only Turn, so the migration must isolate it by scope.

        @return None / None.
        @note 旧 ``fetch_group_context`` 结果是 Agent-turn-only 且不缓存，因而数据库没有可用于
            反向归因的单次读取记录。此回归契约要求迁移从 durable request 的 group scope 选出所有
            assistant Turn，再把它们并入与 raw attachment 相同的 taint 集。/ Old
            ``fetch_group_context`` results are Agent-turn-only and uncached, so the database
            has no single-read record for reverse attribution. This regression contract requires
            the migration to select every assistant Turn from the durable group scope, then merge
            it into the same taint set as raw attachments.
        """

        upgrade = runner._sections(_SQL_PATH.read_text(encoding="utf-8"), _SQL_PATH)[
            "up"
        ]
        catalog = _GROUP_TOOL_CATALOG_PATH.read_text(encoding="utf-8")
        self.assertIn('name="fetch_group_context"', catalog)
        self.assertIn("result_residency=ToolResultResidency.AGENT_TURN", catalog)
        self.assertIn("result_cacheable=False", catalog)

        group_insert = (
            "INSERT INTO wspctl_0070_group_side_channel_turns (turn_id, conversation_id)\n"
            "SELECT activity.turn_id, activity.conversation_id\n"
            "FROM conversation.inference_activities AS activity\n"
            "WHERE COALESCE(activity.request ->> 'task_kind', 'assistant') = 'assistant'\n"
            "  AND COALESCE(activity.request #>> '{scope,is_group}', 'false') = 'true';"
        )
        self.assertIn(group_insert, upgrade)
        self.assertIn("CREATE TEMP TABLE wspctl_0070_tainted_turns", upgrade)
        self.assertIn("FROM wspctl_0070_group_side_channel_turns", upgrade)
        self.assertIn("FROM wspctl_0070_tainted_turns AS affected", upgrade)
        self.assertIn("SELECT DISTINCT tainted.conversation_id", upgrade)
        self.assertIn("SELECT tainted.turn_id", upgrade)

        self.assertLess(
            upgrade.index("wspctl_0070_group_side_channel_turns"),
            upgrade.index("wspctl_0070_tainted_turns"),
        )
        self.assertLess(
            upgrade.index("wspctl_0070_tainted_turns"),
            upgrade.index("UPDATE conversation.conversation_messages AS message"),
        )

    def test_private_attachment_taint_propagates_to_later_text_only_assistant_turns(self) -> None:
        """@brief 私聊附件可经后续纯文本回复传播，闭包必须覆盖 retrieval/Profile / A private attachment can propagate through later text-only replies, so its closure must cover retrieval/Profile.

        @return None / None.
        @note 回归 fixture 的语义是 ``T2(received early) -> T1(photo caption=secret) ->
            assistant echo -> T2(accepted late, text) -> assistant repeats secret``。T2 本身
            没有 media 字段，故只按 direct-media Turn 删除 passage/evidence 会重新把 secret
            送回模型；唯一正确顺序是 canonical message ``sequence``，不是 listener 时钟。/
            The regression fixture semantics are ``T2(received early) -> T1(photo
            caption=secret) -> assistant echo -> T2(accepted late, text) -> assistant repeats
            secret``. T2 itself has no media field, so deleting passages/evidence only for the
            direct-media Turn would send the secret back to the model; canonical message
            ``sequence``, not the listener clock, is the only correct ordering.
        """

        upgrade = runner._sections(_SQL_PATH.read_text(encoding="utf-8"), _SQL_PATH)[
            "up"
        ]
        closure_insert = (
            "INSERT INTO wspctl_0070_private_attachment_descendant_turns (turn_id, conversation_id)\n"
            "SELECT DISTINCT activity.turn_id, activity.conversation_id\n"
            "FROM conversation.inference_activities AS activity\n"
            "JOIN wspctl_0070_legacy_attachment_turns AS direct_media\n"
            "  ON direct_media.conversation_id = activity.conversation_id\n"
            "WHERE COALESCE(activity.request ->> 'task_kind', 'assistant') = 'assistant'\n"
            "  AND COALESCE(activity.request #>> '{scope,is_group}', 'false') = 'false'\n"
            "  AND EXISTS (\n"
            "    SELECT 1\n"
            "    FROM conversation.conversation_messages AS activity_message\n"
            "    JOIN conversation.conversation_messages AS direct_media_message\n"
            "      ON direct_media_message.turn_id = direct_media.turn_id\n"
            "    WHERE activity_message.turn_id = activity.turn_id\n"
            "      AND activity_message.role = 'user'\n"
            "      AND activity_message.conversation_id = activity.conversation_id\n"
            "      AND direct_media_message.role = 'user'\n"
            "      AND direct_media_message.conversation_id = direct_media.conversation_id\n"
            "      AND COALESCE(\n"
            "        direct_media_message.content #>> '{media,kind}',\n"
            "        direct_media_message.content ->> 'content_kind'\n"
            "      ) IN ('photo', 'sticker', 'document')\n"
            "      AND activity_message.sequence >= direct_media_message.sequence\n"
            "  );"
        )
        self.assertIn(closure_insert, upgrade)
        self.assertIn("FROM wspctl_0070_private_attachment_descendant_turns", upgrade)
        self.assertIn("direct media, its private propagation closure", upgrade)
        closure_start = upgrade.index(closure_insert)
        closure_end = upgrade.index("-- @brief 合并 direct-media", closure_start)
        self.assertNotIn("created_at", upgrade[closure_start:closure_end])

        profile_start = upgrade.index(
            "INSERT INTO wspctl_0070_affected_profile_users"
        )
        profile_end = upgrade.index(
            "-- dream_sources is deleted explicitly",
            profile_start,
        )
        profile_users = upgrade[profile_start:profile_end]
        self.assertIn("JOIN wspctl_0070_tainted_turns AS affected", profile_users)
        self.assertIn("USING wspctl_0070_tainted_turns AS affected", upgrade)

    def test_future_episodic_and_profile_discovery_reject_the_same_turn_marker(self) -> None:
        """@brief 迁移 marker 被两类 future source 查询和其 LATERAL 聚合共同识别 / The migration marker is recognized by both future source queries and their LATERAL aggregation.

        @return None / None.
        """

        predicate = (
            "excluded_message.content @> "
            "jsonb_build_object('exclude_from_assistant', TRUE)"
        )
        retrieval_source = _RETRIEVAL_SOURCE_PATH.read_text(encoding="utf-8")
        profile_source = _PROFILE_SOURCE_PATH.read_text(encoding="utf-8")
        self.assertGreaterEqual(retrieval_source.count(predicate), 3)
        self.assertGreaterEqual(profile_source.count(predicate), 3)
        self.assertIn("CROSS JOIN LATERAL", retrieval_source)
        self.assertIn("CROSS JOIN LATERAL", profile_source)

    def test_schema_snapshot_moves_to_the_new_data_migration_head_without_ddl_drift(self) -> None:
        """@brief 0070 是数据边界迁移，snapshot 只前移 head 注释 / 0070 is a data-boundary migration, so the snapshot only advances its head comment.

        @return None / None.
        """

        snapshot = _SCHEMA_PATH.read_text(encoding="utf-8")
        self.assertIn("through 0070_workspace_attachment_model_boundary", snapshot)
        self.assertIn("Alembic head: 0070_workspace_attachment_model_boundary", snapshot)
        self.assertIn("CREATE TABLE workspace.runtimes", snapshot)


if __name__ == "__main__":
    unittest.main()
