"""@brief schema-v2 Workspace 配置迁移测试 / Schema-v2 Workspace configuration migration tests."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from fogmoe_bot.config import read_bot_settings
from fogmoe_config.jsonc import load_jsonc

#: @brief 仓库根目录 / Repository root directory.
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
#: @brief 配置迁移脚本目录 / Configuration migration script directory.
TOOLS_DIRECTORY = REPOSITORY_ROOT / "tools"
#: @brief 被测 schema-v2 迁移脚本路径 / Schema-v2 migration script under test.
MIGRATION_SCRIPT_PATH = TOOLS_DIRECTORY / "migrate_config_v2_to_wspctl.py"
#: @brief 完整模板配置路径 / Complete configuration template path.
EXAMPLE_CONFIGURATION_PATH = REPOSITORY_ROOT / "example.config.json"
#: @brief 只用于泄漏断言的非真实旧密钥 / Non-real retired key used only for leakage assertions.
TEST_RETIRED_SECRET = "not-a-real-judge0-secret"


def _migration_module() -> ModuleType:
    """@brief 以稳定模块名加载迁移 CLI / Load the migration CLI under a stable module name.

    @return 已执行的迁移模块 / Executed migration module.
    """

    module_name = "fogmoe_test_config_v2_workspace_migration"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    sys.path.insert(0, str(TOOLS_DIRECTORY))
    specification = importlib.util.spec_from_file_location(
        module_name,
        MIGRATION_SCRIPT_PATH,
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[module_name] = module
    specification.loader.exec_module(module)
    return module


#: @brief 可复用的 schema-v2 迁移模块 / Reusable schema-v2 migration module.
MIGRATION = _migration_module()


def test_migration_removes_legacy_code_execution_after_validation(
    tmp_path: Path,
    capsys: object,
) -> None:
    """@brief 删除旧配置、保留回滚副本且不回显密钥 / Remove legacy settings, preserve rollback copy, and never echo secrets.

    @param tmp_path pytest 隔离目录 / Pytest isolated directory.
    @param capsys pytest 输出捕获器 / Pytest output capture fixture.
    @return None / None.
    """

    configuration_path = tmp_path / "config.json"
    source = _schema_v2_source_with_retired_integration()
    configuration_path.write_text(source, encoding="utf-8")

    assert MIGRATION.main([str(configuration_path)]) == 0
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert TEST_RETIRED_SECRET not in captured.out
    assert TEST_RETIRED_SECRET not in captured.err

    migrated_source = configuration_path.read_text(encoding="utf-8")
    document = load_jsonc(configuration_path)
    integrations = document["integrations"]
    assert isinstance(integrations, dict)
    assert "code_execution" not in integrations
    assert "code_execution" not in migrated_source
    assert "not-a-real-judge0-secret" not in migrated_source
    assert "\"identity\"" in migrated_source
    assert "\"observability\"" in migrated_source
    assert (
        tmp_path / ".config.json.schema-v2-before-wspctl.bak"
    ).read_text(encoding="utf-8") == source
    read_bot_settings(configuration_path)


def test_dry_run_validates_without_writing_or_creating_backup(
    tmp_path: Path,
    capsys: object,
) -> None:
    """@brief dry-run 不触碰配置或备份 / Dry-run leaves both configuration and backup untouched.

    @param tmp_path pytest 隔离目录 / Pytest isolated directory.
    @param capsys pytest 输出捕获器 / Pytest output capture fixture.
    @return None / None.
    """

    configuration_path = tmp_path / "config.json"
    source = _schema_v2_source_with_retired_integration()
    configuration_path.write_text(source, encoding="utf-8")

    assert MIGRATION.main([str(configuration_path), "--dry-run"]) == 0
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert TEST_RETIRED_SECRET not in captured.out
    assert TEST_RETIRED_SECRET not in captured.err
    assert configuration_path.read_text(encoding="utf-8") == source
    assert not (tmp_path / ".config.json.schema-v2-before-wspctl.bak").exists()


def test_invalid_result_fails_closed_before_backup_or_replace(
    tmp_path: Path,
    capsys: object,
) -> None:
    """@brief reader 拒绝结果时绝不创建备份或替换 / Never back up or replace when the reader rejects the result.

    @param tmp_path pytest 隔离目录 / Pytest isolated directory.
    @param capsys pytest 输出捕获器 / Pytest output capture fixture.
    @return None / None.
    """

    configuration_path = tmp_path / "config.json"
    source = _schema_v2_source_with_retired_integration().replace(
        '"timeout_seconds": 30',
        '"timeout_seconds": 0',
        1,
    )
    configuration_path.write_text(source, encoding="utf-8")

    assert MIGRATION.main([str(configuration_path)]) == 2
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert TEST_RETIRED_SECRET not in captured.out
    assert TEST_RETIRED_SECRET not in captured.err
    assert configuration_path.read_text(encoding="utf-8") == source
    assert not (tmp_path / ".config.json.schema-v2-before-wspctl.bak").exists()


def test_already_upgraded_configuration_is_validated_idempotent_noop(
    tmp_path: Path,
    capsys: object,
) -> None:
    """@brief 不含旧键的 schema-v2 配置不会产生空备份 / A schema-v2 file without the old key produces no empty backup.

    @param tmp_path pytest 隔离目录 / Pytest isolated directory.
    @param capsys pytest 输出捕获器 / Pytest output capture fixture.
    @return None / None.
    """

    configuration_path = tmp_path / "config.json"
    source = EXAMPLE_CONFIGURATION_PATH.read_text(encoding="utf-8")
    configuration_path.write_text(source, encoding="utf-8")

    assert MIGRATION.main([str(configuration_path)]) == 0
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert "无需迁移" in captured.out
    assert configuration_path.read_text(encoding="utf-8") == source
    assert not (tmp_path / ".config.json.schema-v2-before-wspctl.bak").exists()


def test_symlink_or_existing_backup_fails_closed_without_overwrite(
    tmp_path: Path,
    capsys: object,
) -> None:
    """@brief 符号链接输入及既存回滚副本均拒绝写入 / Reject symlink inputs and pre-existing rollback copies.

    @param tmp_path pytest 隔离目录 / Pytest isolated directory.
    @param capsys pytest 输出捕获器 / Pytest output capture fixture.
    @return None / None.
    """

    source = _schema_v2_source_with_retired_integration()
    real_configuration_path = tmp_path / "real-config.json"
    real_configuration_path.write_text(source, encoding="utf-8")
    symlink_path = tmp_path / "config.json"
    symlink_path.symlink_to(real_configuration_path)

    assert MIGRATION.main([str(symlink_path)]) == 2
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert TEST_RETIRED_SECRET not in captured.out
    assert TEST_RETIRED_SECRET not in captured.err
    assert real_configuration_path.read_text(encoding="utf-8") == source

    configuration_path = tmp_path / "config-with-backup.json"
    configuration_path.write_text(source, encoding="utf-8")
    backup_path = tmp_path / ".config-with-backup.json.schema-v2-before-wspctl.bak"
    backup_path.symlink_to(tmp_path / "missing-rollback-target")

    assert MIGRATION.main([str(configuration_path)]) == 2
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert "refusing to overwrite existing rollback copy" in captured.err
    assert TEST_RETIRED_SECRET not in captured.out
    assert TEST_RETIRED_SECRET not in captured.err
    assert configuration_path.read_text(encoding="utf-8") == source
    assert backup_path.is_symlink()


def _schema_v2_source_with_retired_integration() -> str:
    """@brief 基于完整模板构造含历史 Judge0 键的合法 schema-v2 输入 / Build valid schema-v2 input with a historical Judge0 key.

    @return 带退役配置的完整 JSONC 原文 / Complete JSONC source with retired configuration.
    """

    source = EXAMPLE_CONFIGURATION_PATH.read_text(encoding="utf-8")
    marker = "    // 图片生成服务。\n"
    retired_member = f'''    // 旧 Judge0 配置仅用于迁移测试。
    "code_execution": {{
      "judge0_api_url": "https://ce.example.invalid",
      "judge0_api_key": "{TEST_RETIRED_SECRET}"
    }},
'''
    assert marker in source
    return source.replace(marker, f"{retired_member}{marker}", 1)
