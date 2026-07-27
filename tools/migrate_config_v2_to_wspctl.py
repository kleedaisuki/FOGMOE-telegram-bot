#!/usr/bin/env python
"""@brief 移除 schema-v2 中废弃 Judge0 配置 / Retire obsolete Judge0 configuration from schema-v2 JSONC.

该工具是一次性、显式执行的 deployment migration（部署迁移）。它只接受已经是
``schema_version: 2`` 的配置，删除 ``integrations.code_execution``，并通过当前 Bot
reader 验证结果后才以同目录原子替换落盘。原文件先写入不可覆盖的本地备份；命令行和异常
都不回显已删除的 Judge0 密钥。/ This is an explicit, one-time deployment migration. It
accepts only ``schema_version: 2`` configuration, removes ``integrations.code_execution``,
validates the result through the current Bot reader, and only then writes a same-directory
atomic replacement. The original is first written to a non-overwritable local backup; neither
the command line nor exceptions echo retired Judge0 credentials.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypeAlias

from fogmoe_config.jsonc import JSONValue, JsoncDecodeError, parse_jsonc

from migrate_config_v1_to_v2 import (
    ConfigMigrationError,
    _atomic_replace,
    _read_source,
    _replace_root_members,
    _require_regular_config_file,
    _validate_rendered_bot_settings,
    _write_backup,
)

#: @brief 本迁移接受的根配置版本 / Root configuration version accepted by this migration.
SCHEMA_VERSION: Final[int] = 2
#: @brief 即将删除的 integrations 成员名 / Integrations member to be removed.
RETIRED_CODE_EXECUTION_MEMBER: Final[str] = "code_execution"
#: @brief 避免与旧 v1 迁移混淆的回滚副本标记 / Rollback-copy marker distinct from the v1 migration.
BACKUP_VERSION_LABEL: Final[str] = "schema-v2-before-wspctl"
#: @brief JSON 对象的便捷别名 / Convenience alias for a JSON object.
JsonObject: TypeAlias = dict[str, JSONValue]


@dataclass(frozen=True, slots=True)
class WorkspaceConfigMigrationReport:
    """@brief schema-v2 Workspace 配置迁移的无敏感结果 / Non-sensitive schema-v2 Workspace migration result.

    @param path 已检查或已迁移的配置路径 / Checked or migrated configuration path.
    @param backup_path 原文件回滚副本；未修改时为 None / Rollback copy of the original, or None when unchanged.
    @param changed 是否删除了废弃成员 / Whether the retired member was removed.
    """

    path: Path
    backup_path: Path | None
    changed: bool


def main(argv: Sequence[str] | None = None) -> int:
    """@brief 运行 schema-v2 Workspace 配置迁移 CLI / Run the schema-v2 Workspace configuration migration CLI.

    @param argv 可选参数序列 / Optional argument sequence.
    @return POSIX 进程退出码 / POSIX process exit status.
    """

    parser = argparse.ArgumentParser(
        description=(
            "Safely remove integrations.code_execution from a schema-v2 JSONC configuration."
        )
    )
    parser.add_argument(
        "config",
        nargs="?",
        type=Path,
        default=Path("config.json"),
        help="operator-owned schema-v2 JSONC configuration path (default: config.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate without creating a backup or changing the file",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="do not create the local ignored rollback copy when a change is needed",
    )
    arguments = parser.parse_args(argv)

    try:
        report = migrate_config_file(
            arguments.config,
            dry_run=arguments.dry_run,
            create_backup=not arguments.no_backup,
        )
    except ConfigMigrationError as error:
        print(f"配置迁移失败：{error}", file=sys.stderr)
        return 2

    if not report.changed:
        print(f"无需迁移：{report.path} 不包含 integrations.code_execution。")
        return 0
    if arguments.dry_run:
        print(f"迁移预检通过：{report.path}；将删除 integrations.code_execution。")
        return 0
    if report.backup_path is None:
        print(f"已迁移 {report.path}；已删除 integrations.code_execution。")
    else:
        print(
            f"已迁移 {report.path}；已删除 integrations.code_execution；"
            f"已创建本地回滚副本 {report.backup_path.name}。"
        )
    return 0


def migrate_config_file(
    path: Path,
    *,
    dry_run: bool = False,
    create_backup: bool = True,
) -> WorkspaceConfigMigrationReport:
    """@brief 删除 schema-v2 中废弃的 code execution 配置 / Remove retired code-execution configuration from schema-v2.

    @param path operator-owned JSONC 配置路径 / Operator-owned JSONC configuration path.
    @param dry_run 是否只做验证而不写盘 / Whether to validate only without writing.
    @param create_backup 修改前是否创建本地回滚副本 / Whether to create a local rollback copy before changing.
    @return 不含密钥的迁移结果 / Migration result without secrets.
    @raise ConfigMigrationError 配置版本、结构、验证或安全写入不满足要求时抛出 /
        Raised when the version, shape, validation, or safe-write requirements are not met.

    @note 没有 ``code_execution`` 的已升级配置会被验证后作为幂等 no-op 返回，绝不创建空备份。
        / An already-upgraded configuration without ``code_execution`` is validated and returned
        as an idempotent no-op; no empty backup is created.
    """

    _require_regular_config_file(path)
    source = _read_source(path)
    document = _parse_source(path, source)
    _require_schema_v2(document)
    integrations = _required_object(document, "integrations")
    if RETIRED_CODE_EXECUTION_MEMBER not in integrations:
        _validate_rendered_bot_settings(path, source)
        return WorkspaceConfigMigrationReport(
            path=path,
            backup_path=None,
            changed=False,
        )

    migrated_integrations = dict(integrations)
    del migrated_integrations[RETIRED_CODE_EXECUTION_MEMBER]
    rendered = _replace_root_members(
        source,
        replacements={"integrations": migrated_integrations},
    )
    _validate_rendered_bot_settings(path, rendered)

    if dry_run:
        return WorkspaceConfigMigrationReport(
            path=path,
            backup_path=None,
            changed=True,
        )

    backup_path = _backup_path(path) if create_backup else None
    if backup_path is not None:
        _write_backup(path, backup_path, source)
    _atomic_replace(path, rendered)
    return WorkspaceConfigMigrationReport(
        path=path,
        backup_path=backup_path,
        changed=True,
    )


def _parse_source(path: Path, source: str) -> JsonObject:
    """@brief 从已读取的稳定原文解析 JSONC / Parse JSONC from an already-read stable source.

    @param path 配置路径，仅用于无敏感错误上下文 / Configuration path, used only for non-sensitive error context.
    @param source 已读取的 UTF-8 JSONC 原文 / Already-read UTF-8 JSONC source.
    @return 根 JSON 对象 / Root JSON object.
    @raise ConfigMigrationError JSONC 无效时抛出 / Raised when JSONC is invalid.

    @note 不再次调用按路径读取的 parser，避免验证内容与即将替换的原文发生不必要的读取竞态。
        / This deliberately does not call a path-reading parser again, avoiding an unnecessary
        read race between the source being validated and the source about to be replaced.
    """

    try:
        return parse_jsonc(source)
    except JsoncDecodeError as error:
        raise ConfigMigrationError(f"invalid JSONC configuration at {path}") from error


def _require_schema_v2(document: Mapping[str, JSONValue]) -> None:
    """@brief 要求根版本恰好为 schema-v2 / Require exactly schema-v2 at the root.

    @param document 已解析的根配置对象 / Parsed root configuration object.
    @return None / None.
    @raise ConfigMigrationError 根版本不受支持时抛出 / Raised when the root version is unsupported.
    """

    version = document.get("schema_version")
    if type(version) is not int or version != SCHEMA_VERSION:
        raise ConfigMigrationError(
            f"schema_version must be the supported integer {SCHEMA_VERSION}"
        )


def _required_object(
    values: Mapping[str, JSONValue],
    field: str,
) -> JsonObject:
    """@brief 读取必需 JSON 对象 / Read one required JSON object.

    @param values 父 JSON 对象 / Parent JSON object.
    @param field 所需字段名 / Required field name.
    @return 字典值 / Dictionary value.
    @raise ConfigMigrationError 字段缺失或不是对象时抛出 / Raised when the field is absent or not an object.
    """

    value = values.get(field)
    if not isinstance(value, dict):
        raise ConfigMigrationError(f"{field} must be an object")
    return value


def _backup_path(path: Path) -> Path:
    """@brief 生成 Workspace 迁移专用回滚副本路径 / Derive the Workspace-migration rollback-copy path.

    @param path 原配置路径 / Original configuration path.
    @return 同目录、不可与 v1 备份混淆的隐藏路径 / Hidden same-directory path distinct from the v1 backup.
    """

    return path.with_name(f".{path.name}.{BACKUP_VERSION_LABEL}.bak")


if __name__ == "__main__":
    raise SystemExit(main())
