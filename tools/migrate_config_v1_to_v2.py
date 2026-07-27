#!/usr/bin/env python
"""@brief 将本地配置从 schema v1 安全迁移到 v2 / Safely migrate a local configuration from schema v1 to v2.

该工具只处理已由 OpenRouter 承担全部活动 AI 路由的 legacy 配置。它会保留根 JSONC
文档中 ``ai`` 之外的每一个字节（包括注释和格式），把 OpenRouter 变为完整的
OpenAI-style ``/chat/completions`` endpoint，并在原子替换前用公开的
``read_bot_settings`` 验证新文档。

密钥只在内存中读取、写入和验证；所有异常与标准输出都不得回显其值。/
This utility only handles legacy configurations where OpenRouter owns every active AI route. It
preserves every byte outside the root ``ai`` member (including comments and formatting), turns
OpenRouter into a full OpenAI-style ``/chat/completions`` endpoint, and validates the rendered
document through public ``read_bot_settings`` before atomically replacing it.

Secrets are read, written, and validated only in memory; neither errors nor standard output may
echo their values.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypeAlias
from urllib.parse import urlsplit

from fogmoe_bot.config import ConfigurationError, read_bot_settings
from fogmoe_config.jsonc import JSONValue, JsoncDecodeError, load_jsonc

#: @brief 该脚本接受的 legacy 根配置版本 / Legacy root configuration version accepted by this utility.
LEGACY_SCHEMA_VERSION: Final[int] = 1
#: @brief 输出的根配置版本 / Output root configuration version.
TARGET_SCHEMA_VERSION: Final[int] = 2
#: @brief 可自动迁移的活动 provider ID / Active provider ID that can be migrated automatically.
OPENROUTER_ID: Final[str] = "openrouter"
#: @brief OpenRouter 未显式给出根地址时的兼容 API 根 / Compatible API root when OpenRouter omits one.
DEFAULT_OPENROUTER_API_BASE: Final[str] = "https://openrouter.ai/api/v1"
#: @brief OpenAI Chat Completions 的完整路径 / Complete OpenAI Chat Completions path.
OPENAI_COMPLETIONS_PATH: Final[str] = "/chat/completions"
#: @brief JSON 对象的便捷别名 / Convenience alias for a JSON object.
JsonObject: TypeAlias = dict[str, JSONValue]


class ConfigMigrationError(ValueError):
    """@brief 配置迁移被拒绝或无法安全完成 / Configuration migration was rejected or cannot complete safely."""


@dataclass(frozen=True, slots=True)
class MigrationReport:
    """@brief 一次成功迁移的无敏感摘要 / Non-sensitive summary of one successful migration.

    @param path 已迁移配置路径 / Migrated configuration path.
    @param backup_path 本地回滚副本；禁用备份时为 None / Local rollback copy, or None when backups are disabled.
    @param routed_tasks 已写入 OpenRouter route 的任务 / Tasks that received an OpenRouter route.
    """

    path: Path
    backup_path: Path | None
    routed_tasks: tuple[str, ...]


def main(argv: Sequence[str] | None = None) -> int:
    """@brief 运行命令行迁移入口 / Run the command-line migration entry point.

    @param argv 可选命令行参数 / Optional command-line arguments.
    @return 进程退出码 / Process exit status.
    """

    parser = argparse.ArgumentParser(
        description="Safely migrate a legacy OpenRouter config.json from schema v1 to v2."
    )
    parser.add_argument(
        "config",
        nargs="?",
        type=Path,
        default=Path("config.json"),
        help="operator-owned JSONC configuration path (default: config.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate the migration without creating a backup or changing the file",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="do not create the local ignored schema-v1 rollback copy",
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

    task_summary = ", ".join(report.routed_tasks) or "none"
    if arguments.dry_run:
        print(f"迁移预检通过：{report.path}；OpenRouter routes: {task_summary}。")
        return 0
    if report.backup_path is None:
        print(f"已迁移 {report.path} 至 schema v2；OpenRouter routes: {task_summary}。")
    else:
        print(
            f"已迁移 {report.path} 至 schema v2；已创建本地回滚副本 "
            f"{report.backup_path.name}；OpenRouter routes: {task_summary}。"
        )
    return 0


def migrate_config_file(
    path: Path,
    *,
    dry_run: bool = False,
    create_backup: bool = True,
) -> MigrationReport:
    """@brief 迁移一个 operator-owned JSONC 文件 / Migrate one operator-owned JSONC file.

    @param path legacy config.json 路径 / Legacy config.json path.
    @param dry_run 是否只验证而不落盘 / Whether to validate without writing.
    @param create_backup 是否先创建可忽略的回滚副本 / Whether to first create an ignored rollback copy.
    @return 不包含密钥的迁移摘要 / Migration summary without secrets.
    @raise ConfigMigrationError 输入不属于受支持的 v1 形状或验证/写入失败时抛出 /
        Raised when the input is not a supported v1 shape or validation/writing fails.
    """

    _require_regular_config_file(path)
    source = _read_source(path)
    document = _load_document(path)
    _require_legacy_schema(document)
    ai, routed_tasks = _migrate_ai(document)
    rendered = _replace_root_members(
        source,
        replacements={"schema_version": TARGET_SCHEMA_VERSION, "ai": ai},
    )
    _validate_rendered_bot_settings(path, rendered)

    if dry_run:
        return MigrationReport(path=path, backup_path=None, routed_tasks=routed_tasks)
    backup_path = _backup_path(path) if create_backup else None
    if backup_path is not None:
        _write_backup(path, backup_path, source)
    _atomic_replace(path, rendered)
    return MigrationReport(path=path, backup_path=backup_path, routed_tasks=routed_tasks)


def _backup_path(path: Path) -> Path:
    """@brief 派生与目标同名的隐藏回滚副本路径 / Derive a hidden rollback-copy path named after the target.

    @param path 目标配置路径 / Target configuration path.
    @return 同目录的隐藏 schema-v1 回滚副本路径 / Hidden schema-v1 rollback-copy path in the same directory.
    """

    return path.with_name(f".{path.name}.schema-v1.bak")


def _read_source(path: Path) -> str:
    """@brief 读取原始 JSONC 文本且不回显内容 / Read raw JSONC text without echoing its contents.

    @param path 配置文件路径 / Configuration-file path.
    @return UTF-8 原文 / UTF-8 source text.
    @raise ConfigMigrationError 文件不可读时抛出 / Raised when the file cannot be read.
    """

    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ConfigMigrationError(f"cannot read configuration file {path}") from error


def _require_regular_config_file(path: Path) -> None:
    """@brief 拒绝符号链接、目录与硬链接配置 / Reject symlink, directory, and hard-linked configuration inputs.

    @param path 操作者指定的配置路径 / Operator-supplied configuration path.
    @return None / None.
    @raise ConfigMigrationError 输入不是单一普通文件时抛出 / Raised when the input is not one regular file.

    @note 迁移以同目录原子替换完成；跟随符号链接或替换硬链接的一端都会让操作者误判
        实际被修改的对象。/ Migration finishes with a same-directory atomic replacement;
        following a symlink or replacing one side of a hard link would make the operator
        misidentify the object that changed.
    """

    try:
        metadata = path.lstat()
    except OSError as error:
        raise ConfigMigrationError(f"cannot stat configuration file {path}") from error
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ConfigMigrationError(
            "configuration file must be one non-symlink, non-hard-linked regular file"
        )


def _load_document(path: Path) -> JsonObject:
    """@brief 解析 legacy JSONC 文档 / Parse the legacy JSONC document.

    @param path 配置文件路径 / Configuration-file path.
    @return 经过 JSONC 语法校验的顶层对象 / JSONC-syntax-validated top-level object.
    @raise ConfigMigrationError JSONC 无效时抛出 / Raised when JSONC is invalid.
    """

    try:
        return load_jsonc(path)
    except JsoncDecodeError as error:
        raise ConfigMigrationError(f"invalid JSONC configuration at {path}") from error


def _require_legacy_schema(document: Mapping[str, JSONValue]) -> None:
    """@brief 确认输入恰好是 schema v1 / Require that the input is exactly schema v1.

    @param document 已解析根文档 / Parsed root document.
    @return None / None.
    @raise ConfigMigrationError 根版本不是整数 v1 时抛出 / Raised when the root version is not integer v1.
    """

    version = document.get("schema_version")
    if isinstance(version, bool) or version != LEGACY_SCHEMA_VERSION:
        raise ConfigMigrationError(
            f"schema_version must be the legacy integer {LEGACY_SCHEMA_VERSION}"
        )


def _migrate_ai(document: Mapping[str, JSONValue]) -> tuple[JsonObject, tuple[str, ...]]:
    """@brief 从 legacy AI 树构造 v2 OpenRouter 配置 / Build v2 OpenRouter settings from the legacy AI tree.

    @param document 已解析根文档 / Parsed root document.
    @return v2 AI 对象与实际路由任务 / v2 AI object and tasks that actually route.
    @raise ConfigMigrationError legacy 路由不完全属于 OpenRouter 或字段不合法时抛出 /
        Raised when legacy routes are not entirely OpenRouter-owned or fields are invalid.
    """

    legacy_ai = _required_object(document, "ai")
    legacy_providers = _required_object(legacy_ai, "providers")
    legacy_openrouter = _required_object(legacy_providers, OPENROUTER_ID)
    legacy_models = _required_object(legacy_openrouter, "models")
    legacy_routing = _required_object(legacy_ai, "routing")

    task_providers = _legacy_task_providers(legacy_routing)
    for task, providers in task_providers.items():
        unsupported = tuple(
            provider for provider in providers if provider != OPENROUTER_ID
        )
        if unsupported:
            raise ConfigMigrationError(
                f"ai.routing.{task} uses a non-OpenRouter provider; migrate that route manually"
            )

    api_key = _optional_string(legacy_openrouter.get("api_key"), "openrouter.api_key")
    provider: JsonObject = {
        "id": OPENROUTER_ID,
        "label": "OpenRouter",
        "style": "openai",
        "endpoint": _openrouter_endpoint(legacy_openrouter.get("api_base")),
        "auth": {
            "api_key": api_key,
            "header": "Authorization",
            "prefix": "Bearer ",
        },
        "headers": {},
    }

    routing: JsonObject = {}
    routed_tasks: list[str] = []
    for task in ("chat", "summary", "dreaming", "translation"):
        providers = task_providers[task]
        routes: list[JSONValue] = []
        if OPENROUTER_ID in providers:
            routes.append(_route_for_task(task, legacy_models))
            routed_tasks.append(task)
        routing[task] = {"routes": routes}

    return {"providers": [provider], "routing": routing}, tuple(routed_tasks)


def _legacy_task_providers(
    routing: Mapping[str, JSONValue],
) -> dict[str, tuple[str, ...]]:
    """@brief 提取 legacy 四类任务的有效 provider 顺序 / Extract effective legacy provider order for four tasks.

    @param routing legacy ``ai.routing`` 对象 / Legacy ``ai.routing`` object.
    @return 每个任务的去重 provider 元组 / Deduplicated provider tuple for each task.
    @raise ConfigMigrationError 路由字段缺失或类型不正确时抛出 / Raised when route fields are missing or malformed.
    """

    chat = _required_object(routing, "chat")
    chat_order = _string_tuple(chat.get("provider_order"), "ai.routing.chat.provider_order")
    result = {"chat": _deduplicate(chat_order)}
    for task in ("summary", "dreaming", "translation"):
        task_routing = _required_object(routing, task)
        primary = _optional_string(
            task_routing.get("provider"), f"ai.routing.{task}.provider"
        )
        fallback = _optional_string(
            task_routing.get("fallback_provider"),
            f"ai.routing.{task}.fallback_provider",
        )
        result[task] = _deduplicate(
            tuple(provider for provider in (primary, fallback) if provider is not None)
        )
    return result


def _route_for_task(task: str, models: Mapping[str, JSONValue]) -> JsonObject:
    """@brief 生成一个 v2 OpenRouter task route / Produce one v2 OpenRouter task route.

    @param task chat、summary、dreaming 或 translation / Chat, summary, dreaming, or translation.
    @param models legacy OpenRouter 模型目录 / Legacy OpenRouter model catalog.
    @return 含空 metadata 的完整 v2 route / Complete v2 route with empty metadata.
    @raise ConfigMigrationError 任务需要的模型未配置时抛出 / Raised when a task-required model is absent.
    """

    model_entries: list[JSONValue]
    supports_tools = task == "chat"
    match task:
        case "chat":
            model_entries = _chat_models(models)
        case "summary":
            model_entries = _model_entries(
                _required_model(models, "summary"),
                _optional_model(models, "summary_fallback"),
            )
        case "dreaming":
            model_entries = _model_entries(
                _optional_model(models, "dreaming")
                or _required_model(models, "summary")
            )
        case "translation":
            model_entries = _model_entries(_required_model(models, "translation"))
        case _:
            raise ConfigMigrationError(f"unsupported task {task!r}")
    return {
        "provider": OPENROUTER_ID,
        "models": model_entries,
        "supports_tools": supports_tools,
        "strict_tools": False,
        "disabled_tools": [],
        "safety_block_is_terminal": False,
        "meta": {},
    }


def _chat_models(models: Mapping[str, JSONValue]) -> list[JSONValue]:
    """@brief 映射 chat 主、回退与视觉模型 / Map chat primary, fallback, and vision models.

    @param models legacy OpenRouter 模型目录 / Legacy OpenRouter model catalog.
    @return 保序去重的 v2 模型对象；视觉模型标记为可接收图片 /
        Ordered, deduplicated v2 model objects with the vision model marked image-capable.
    @raise ConfigMigrationError chat 与 vision 均未配置时抛出 / Raised when neither chat nor vision is configured.
    """

    primary = _optional_model(models, "chat")
    fallback = _optional_model(models, "chat_fallback")
    vision = _optional_model(models, "vision")
    entries = _model_entries(primary, fallback, vision, image_model=vision)
    if not entries:
        raise ConfigMigrationError(
            "ai.providers.openrouter.models requires chat or vision for an active chat route"
        )
    return entries


def _model_entries(
    *names: str | None,
    image_model: str | None = None,
) -> list[JSONValue]:
    """@brief 构建保序去重的 route 模型条目 / Build ordered, deduplicated route-model entries.

    @param names 原有优先顺序中的模型名 / Model names in their legacy priority order.
    @param image_model 可接收图片的显式视觉模型 / Explicit vision model that accepts images.
    @return v2 ``models`` 数组 / v2 ``models`` array.
    """

    entries: list[JSONValue] = []
    positions: dict[str, int] = {}
    for name in names:
        if name is None:
            continue
        existing = positions.get(name)
        if existing is None:
            positions[name] = len(entries)
            entries.append({"name": name, "accepts_images": name == image_model})
        elif name == image_model:
            existing_entry = entries[existing]
            assert isinstance(existing_entry, dict)
            existing_entry["accepts_images"] = True
    return entries


def _required_model(models: Mapping[str, JSONValue], field: str) -> str:
    """@brief 读取任务所需模型名 / Read a model name required by an active task.

    @param models legacy 模型目录 / Legacy model catalog.
    @param field 模型字段名 / Model field name.
    @return 非空模型名 / Non-blank model name.
    @raise ConfigMigrationError 模型为空或类型错误时抛出 / Raised when the model is blank or malformed.
    """

    value = _optional_model(models, field)
    if value is None:
        raise ConfigMigrationError(
            f"ai.providers.openrouter.models.{field} is required by an active route"
        )
    return value


def _optional_model(models: Mapping[str, JSONValue], field: str) -> str | None:
    """@brief 读取可选模型名并规范空白 / Read an optional model name and normalize blank space.

    @param models legacy 模型目录 / Legacy model catalog.
    @param field 模型字段名 / Model field name.
    @return 去除外侧空白后的模型名或 None / Trimmed model name, or None.
    @raise ConfigMigrationError 模型字段类型不正确时抛出 / Raised when the model field has an invalid type.
    """

    return _optional_string(
        models.get(field), f"ai.providers.openrouter.models.{field}", strip=True
    )


def _openrouter_endpoint(value: JSONValue | None) -> str:
    """@brief 将 legacy OpenRouter API 根变为完整 endpoint / Turn a legacy OpenRouter API root into a complete endpoint.

    @param value legacy ``api_base`` 值 / Legacy ``api_base`` value.
    @return 完整、无 query/fragment 的 chat-completions URL /
        Complete chat-completions URL without query or fragment.
    @raise ConfigMigrationError endpoint 不安全或不是 HTTP(S) URL 时抛出 /
        Raised when the endpoint is unsafe or not an HTTP(S) URL.
    """

    base = _optional_string(value, "ai.providers.openrouter.api_base", strip=True)
    normalized = (base or DEFAULT_OPENROUTER_API_BASE).rstrip("/")
    if normalized.casefold().endswith(OPENAI_COMPLETIONS_PATH):
        endpoint = normalized
    else:
        endpoint = f"{normalized}{OPENAI_COMPLETIONS_PATH}"
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.path in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigMigrationError(
            "ai.providers.openrouter.api_base must be an HTTP(S) URL without query or fragment"
        )
    return endpoint


def _required_object(
    values: Mapping[str, JSONValue],
    field: str,
) -> JsonObject:
    """@brief 读取必需 JSON 对象 / Read one required JSON object.

    @param values 父对象 / Parent object.
    @param field 字段名 / Field name.
    @return 字典值 / Dictionary value.
    @raise ConfigMigrationError 字段缺失或不是对象时抛出 / Raised when the field is missing or not an object.
    """

    value = values.get(field)
    if not isinstance(value, dict):
        raise ConfigMigrationError(f"{field} must be an object")
    return value


def _optional_string(
    value: JSONValue | None,
    field: str,
    *,
    strip: bool = False,
) -> str | None:
    """@brief 读取可选字符串，绝不在异常中回显值 / Read an optional string without ever echoing its value.

    @param value 原始 JSON 值 / Raw JSON value.
    @param field 面向操作者的非敏感路径 / Non-sensitive operator-facing path.
    @param strip 是否去除字符串外侧空白 / Whether to trim surrounding whitespace.
    @return 原样或去白字符串；null 时为 None / Original or trimmed string; None for null.
    @raise ConfigMigrationError 值既非字符串也非 null 时抛出 / Raised when the value is neither a string nor null.
    """

    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigMigrationError(f"{field} must be a string or null")
    normalized = value.strip() if strip else value
    if strip and not normalized:
        return None
    return normalized


def _string_tuple(value: JSONValue | None, field: str) -> tuple[str, ...]:
    """@brief 读取字符串数组 / Read a string array.

    @param value 原始 JSON 值 / Raw JSON value.
    @param field 非敏感配置路径 / Non-sensitive configuration path.
    @return 不可变字符串序列 / Immutable string sequence.
    @raise ConfigMigrationError 值不是字符串数组或含空白项时抛出 /
        Raised when the value is not a string array or contains blank items.
    """

    if not isinstance(value, list):
        raise ConfigMigrationError(f"{field} must be an array")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ConfigMigrationError(f"{field} must contain non-blank strings")
        result.append(item.strip())
    return tuple(result)


def _deduplicate(values: tuple[str, ...]) -> tuple[str, ...]:
    """@brief 保持顺序地去重 provider / Deduplicate providers while preserving their order.

    @param values 原 provider 序列 / Original provider sequence.
    @return 保序去重序列 / Ordered deduplicated sequence.
    """

    return tuple(dict.fromkeys(values))


def _replace_root_members(
    source: str,
    *,
    replacements: Mapping[str, JSONValue],
) -> str:
    """@brief 仅替换根对象的指定值，保留其余原文 / Replace only selected root values while preserving the rest of the source.

    @param source 原始 JSONC 文本 / Original JSONC text.
    @param replacements 根成员名到新 JSON 值的映射 / Mapping from root member names to new JSON values.
    @return 更新后的 JSONC 文本 / Updated JSONC source text.
    @raise ConfigMigrationError 根成员找不到或文本结构无法安全定位时抛出 /
        Raised when a root member is missing or source structure cannot be safely located.
    """

    spans = _root_member_value_spans(source)
    missing = tuple(key for key in replacements if key not in spans)
    if missing:
        raise ConfigMigrationError(
            "root JSONC members required for migration are missing: " + ", ".join(missing)
        )
    newline = "\r\n" if "\r\n" in source else "\n"
    rendered_replacements: list[tuple[int, int, str]] = []
    for key, value in replacements.items():
        start, end = spans[key]
        rendered_replacements.append(
            (start, end, _render_json_value(value, source, start, newline))
        )
    result = source
    for start, end, replacement in sorted(
        rendered_replacements, key=lambda item: item[0], reverse=True
    ):
        result = f"{result[:start]}{replacement}{result[end:]}"
    return result


def _render_json_value(
    value: JSONValue,
    source: str,
    start: int,
    newline: str,
) -> str:
    """@brief 根据原根成员缩进渲染 JSON 值 / Render a JSON value with the original root-member indentation.

    @param value 待编码 JSON 值 / JSON value to encode.
    @param source 原始 JSONC 文本 / Original JSONC text.
    @param start 被替换值的起始偏移 / Start offset of the replaced value.
    @param newline 原文件使用的换行符 / Newline sequence used by the original file.
    @return 与成员缩进对齐的 JSON 文本 / JSON text aligned to the member indentation.
    """

    rendered = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    lines = rendered.splitlines()
    if len(lines) == 1:
        return rendered
    line_start = max(source.rfind("\n", 0, start), source.rfind("\r", 0, start)) + 1
    prefix = source[line_start:start]
    indentation = prefix[: len(prefix) - len(prefix.lstrip(" \t"))]
    return lines[0] + "".join(f"{newline}{indentation}{line}" for line in lines[1:])


def _root_member_value_spans(source: str) -> dict[str, tuple[int, int]]:
    """@brief 定位 JSONC 根对象的每个成员值范围 / Locate every root-object member value span in JSONC.

    @param source 原始 JSONC 文本 / Original JSONC text.
    @return 根键到其值半开区间的映射 / Mapping from root keys to half-open value spans.
    @raise ConfigMigrationError JSONC 文本无法安全扫描时抛出 / Raised when JSONC source cannot be scanned safely.
    @note JSONC 已先经共享 parser 校验；此扫描器仅用于保留未迁移原文。/
        JSONC is first validated by the shared parser; this scanner exists only to preserve
        unmigrated source text.
    """

    position = _skip_trivia(source, 0)
    if position >= len(source) or source[position] != "{":
        raise ConfigMigrationError("root JSONC value must be an object")
    position += 1
    spans: dict[str, tuple[int, int]] = {}
    while True:
        position = _skip_trivia(source, position)
        if position >= len(source):
            raise ConfigMigrationError("root JSONC object is unterminated")
        if source[position] == "}":
            return spans
        if source[position] != '"':
            raise ConfigMigrationError("root JSONC member key must be a string")
        key_end = _string_end(source, position)
        try:
            key = json.loads(source[position:key_end])
        except json.JSONDecodeError as error:
            raise ConfigMigrationError("root JSONC member key is malformed") from error
        if not isinstance(key, str):
            raise ConfigMigrationError("root JSONC member key must be a string")
        position = _skip_trivia(source, key_end)
        if position >= len(source) or source[position] != ":":
            raise ConfigMigrationError("root JSONC member is missing a colon")
        value_start = _skip_trivia(source, position + 1)
        value_end = _value_end(source, value_start)
        spans[key] = (value_start, value_end)
        position = _skip_trivia(source, value_end)
        if position >= len(source):
            raise ConfigMigrationError("root JSONC object is unterminated")
        if source[position] == "}":
            return spans
        if source[position] != ",":
            raise ConfigMigrationError("root JSONC members must be separated by commas")
        position += 1


def _skip_trivia(source: str, position: int) -> int:
    """@brief 越过 JSONC 空白与注释 / Skip JSONC whitespace and comments.

    @param source 原始 JSONC 文本 / Original JSONC source.
    @param position 起始偏移 / Starting offset.
    @return 下一个语法 token 的偏移 / Offset of the next syntax token.
    @raise ConfigMigrationError 注释未闭合时抛出 / Raised when a comment is unterminated.
    """

    while position < len(source):
        if source[position].isspace():
            position += 1
            continue
        if source.startswith("//", position):
            line_end = source.find("\n", position + 2)
            return len(source) if line_end < 0 else _skip_trivia(source, line_end + 1)
        if source.startswith("/*", position):
            comment_end = source.find("*/", position + 2)
            if comment_end < 0:
                raise ConfigMigrationError("JSONC block comment is unterminated")
            position = comment_end + 2
            continue
        return position
    return position


def _string_end(source: str, position: int) -> int:
    """@brief 找到 JSON 字符串的后闭引号 / Find the closing quote of one JSON string.

    @param source 原始 JSONC 文本 / Original JSONC source.
    @param position 开始双引号的偏移 / Offset of the opening quote.
    @return 右侧双引号之后的偏移 / Offset immediately after the closing quote.
    @raise ConfigMigrationError 字符串未闭合时抛出 / Raised when the string is unterminated.
    """

    if position >= len(source) or source[position] != '"':
        raise ConfigMigrationError("expected a JSON string")
    position += 1
    while position < len(source):
        character = source[position]
        if character == "\\":
            position += 2
            continue
        if character == '"':
            return position + 1
        position += 1
    raise ConfigMigrationError("JSON string is unterminated")


def _value_end(source: str, position: int) -> int:
    """@brief 找到一个 JSONC 值的末尾 / Find the end of one JSONC value.

    @param source 原始 JSONC 文本 / Original JSONC source.
    @param position 值起始偏移 / Value starting offset.
    @return 值结束后的半开偏移 / Half-open offset immediately after the value.
    @raise ConfigMigrationError 值不存在或容器未闭合时抛出 / Raised when a value is absent or a container is unterminated.
    """

    if position >= len(source):
        raise ConfigMigrationError("root JSONC member is missing a value")
    first = source[position]
    if first == '"':
        return _string_end(source, position)
    if first in "{[":
        return _container_end(source, position)
    end = position
    while end < len(source):
        character = source[end]
        if character.isspace() or character in ",}]":
            break
        if character == "/" and (
            source.startswith("//", end) or source.startswith("/*", end)
        ):
            break
        end += 1
    if end == position:
        raise ConfigMigrationError("root JSONC member is missing a value")
    return end


def _container_end(source: str, position: int) -> int:
    """@brief 找到嵌套 JSONC 对象或数组的闭合位置 / Find the close of a nested JSONC object or array.

    @param source 原始 JSONC 文本 / Original JSONC source.
    @param position 容器起始 ``{`` 或 ``[`` 的偏移 / Offset of opening ``{`` or ``[``.
    @return 容器闭合字符之后的偏移 / Offset immediately after the closing character.
    @raise ConfigMigrationError 嵌套容器未闭合或结构不匹配时抛出 /
        Raised when a nested container is unterminated or mismatched.
    """

    expected = "}" if source[position] == "{" else "]"
    closers = [expected]
    position += 1
    while position < len(source):
        character = source[position]
        if character == '"':
            position = _string_end(source, position)
            continue
        if source.startswith("//", position):
            line_end = source.find("\n", position + 2)
            position = len(source) if line_end < 0 else line_end + 1
            continue
        if source.startswith("/*", position):
            comment_end = source.find("*/", position + 2)
            if comment_end < 0:
                raise ConfigMigrationError("JSONC block comment is unterminated")
            position = comment_end + 2
            continue
        if character == "{":
            closers.append("}")
        elif character == "[":
            closers.append("]")
        elif character in "}]":
            if character != closers[-1]:
                raise ConfigMigrationError("JSONC container delimiters are mismatched")
            closers.pop()
            if not closers:
                return position + 1
        position += 1
    raise ConfigMigrationError("JSONC container is unterminated")


def _validate_rendered_bot_settings(path: Path, source: str) -> None:
    """@brief 用公开 Bot reader 验证待写入配置 / Validate pending configuration through the public Bot reader.

    @param path 目标配置路径 / Target configuration path.
    @param source 待验证 JSONC 文本 / Pending JSONC source.
    @return None / None.
    @raise ConfigMigrationError 渲染结果未通过 reader 时抛出 / Raised when the rendered result fails the reader.
    @note 临时文件以默认 owner-only 权限创建并在验证后删除。/
        The temporary file is created with default owner-only permissions and deleted after validation.
    """

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".config-v2-validate-", suffix=".json", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as temporary_file:
            temporary_file.write(source)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        read_bot_settings(temporary_path)
    except (OSError, ConfigurationError) as error:
        raise ConfigMigrationError(
            "rendered schema-v2 configuration was rejected by read_bot_settings"
        ) from error
    finally:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass


def _write_backup(source_path: Path, backup_path: Path, source: str) -> None:
    """@brief 创建不覆盖的本地回滚副本 / Create a non-overwriting local rollback copy.

    @param source_path 原配置路径 / Original configuration path.
    @param backup_path 回滚副本路径 / Rollback-copy path.
    @param source 原始 JSONC 文本 / Original JSONC source text.
    @return None / None.
    @raise ConfigMigrationError 回滚副本已存在或无法安全写入时抛出 /
        Raised when the rollback copy exists or cannot be written safely.
    """

    if backup_path.exists() or backup_path.is_symlink():
        raise ConfigMigrationError(
            f"refusing to overwrite existing rollback copy {backup_path.name}"
        )
    _atomic_write(backup_path, source, _file_mode(source_path))


def _atomic_replace(path: Path, source: str) -> None:
    """@brief 原子替换目标配置 / Atomically replace the target configuration.

    @param path 目标配置路径 / Target configuration path.
    @param source 新 JSONC 文本 / New JSONC source text.
    @return None / None.
    @raise ConfigMigrationError 原子写入失败时抛出 / Raised when atomic writing fails.
    """

    _atomic_write(path, source, _file_mode(path))


def _file_mode(path: Path) -> int:
    """@brief 取得已有配置的权限位 / Obtain the existing configuration permission bits.

    @param path 配置路径 / Configuration path.
    @return POSIX 权限位 / POSIX permission bits.
    @raise ConfigMigrationError 无法读取元数据时抛出 / Raised when metadata cannot be read.
    """

    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError as error:
        raise ConfigMigrationError(f"cannot stat configuration file {path}") from error


def _atomic_write(path: Path, source: str, mode: int) -> None:
    """@brief 以同目录临时文件完成原子写入 / Atomically write through a same-directory temporary file.

    @param path 输出路径 / Output path.
    @param source 待写文本 / Text to write.
    @param mode 应继承的权限位 / Permission bits to inherit.
    @return None / None.
    @raise ConfigMigrationError 临时文件或替换操作失败时抛出 / Raised when temporary writing or replacement fails.
    """

    descriptor: int | None = None
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.tmp-", dir=path.parent
        )
        temporary_path = Path(temporary_name)
        os.chmod(temporary_path, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as temporary_file:
            descriptor = None
            temporary_file.write(source)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        _fsync_parent_directory(path)
    except OSError as error:
        raise ConfigMigrationError(f"cannot atomically write configuration file {path}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _fsync_parent_directory(path: Path) -> None:
    """@brief 将原子替换的目录项同步到磁盘 / Persist the directory entry of an atomic replacement.

    @param path 刚被替换的文件路径 / Path that was just replaced.
    @return None / None.
    @raise OSError 目录无法打开或同步时抛出 / Raised when the directory cannot be opened or synced.

    @note ``fsync`` 临时文件只能保证内容，不能保证 ``rename`` 目录项已持久化；二者都需要。
        / ``fsync`` on the temporary file persists its content but not the directory entry
        created by ``rename``; both are required.
    """

    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
