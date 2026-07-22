"""@brief schema v1 配置迁移工具测试 / Schema-v1 configuration migration utility tests."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from fogmoe_config.jsonc import load_jsonc

#: @brief 被测试的迁移脚本路径 / Migration script path under test.
MIGRATION_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "tools" / "migrate_config_v1_to_v2.py"
)
#: @brief 测试用非真实 OpenRouter 密钥 / Non-real OpenRouter key used only by tests.
TEST_SECRET = "not-a-real-openrouter-secret"


def _migration_module() -> ModuleType:
    """@brief 以稳定模块名加载迁移脚本 / Load the migration script under a stable module name.

    @return 已执行的迁移模块 / Executed migration module.
    """

    module_name = "fogmoe_test_config_v1_to_v2_migration"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
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


#: @brief 可复用的迁移模块 / Reusable loaded migration module.
MIGRATION = _migration_module()


def test_migration_preserves_non_ai_source_and_maps_openrouter(
    tmp_path: Path,
    capsys: object,
) -> None:
    """@brief 仅替换 schema/AI，映射模型且不回显密钥 / Replace only schema/AI, map models, and never echo the key.

    @param tmp_path pytest 隔离目录 / Pytest isolated directory.
    @param capsys pytest 输出捕获器 / Pytest output capture fixture.
    @return None / None.
    """

    config_path = tmp_path / "config.json"
    legacy_source = _legacy_config_source()
    config_path.write_text(legacy_source, encoding="utf-8")

    assert MIGRATION.main([str(config_path)]) == 0
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert TEST_SECRET not in captured.out
    assert TEST_SECRET not in captured.err

    migrated_source = config_path.read_text(encoding="utf-8")
    assert _outside_replaced_members(legacy_source) == _outside_replaced_members(
        migrated_source
    )
    assert "// 保留这一条非 AI 注释" in migrated_source
    assert "// 此 AI 前置注释必须保留" in migrated_source
    assert "// 此 AI 内部注释可被迁移替换" not in migrated_source
    assert (tmp_path / ".config.json.schema-v1.bak").read_text(
        encoding="utf-8"
    ) == legacy_source

    document = load_jsonc(config_path)
    assert document["schema_version"] == 2
    ai = document["ai"]
    assert isinstance(ai, dict)
    providers = ai["providers"]
    assert isinstance(providers, list)
    assert len(providers) == 1
    provider = providers[0]
    assert isinstance(provider, dict)
    assert provider["id"] == "openrouter"
    assert provider["style"] == "openai"
    assert provider["endpoint"] == "https://openrouter.ai/api/v1/chat/completions"
    auth = provider["auth"]
    assert isinstance(auth, dict)
    assert auth["api_key"] == TEST_SECRET

    routing = ai["routing"]
    assert isinstance(routing, dict)
    assert _route_models(routing, "chat") == [
        {"name": "chat-primary", "accepts_images": False},
        {"name": "chat-fallback", "accepts_images": False},
        {"name": "vision-primary", "accepts_images": True},
    ]
    assert _route_models(routing, "summary") == [
        {"name": "summary-primary", "accepts_images": False},
        {"name": "summary-fallback", "accepts_images": False},
    ]
    assert _route_models(routing, "dreaming") == [
        {"name": "dreaming-primary", "accepts_images": False}
    ]
    assert _route_models(routing, "translation") == [
        {"name": "translation-primary", "accepts_images": False}
    ]
    for task in ("chat", "summary", "dreaming", "translation"):
        task_config = routing[task]
        assert isinstance(task_config, dict)
        routes = task_config["routes"]
        assert isinstance(routes, list) and len(routes) == 1
        route = routes[0]
        assert isinstance(route, dict)
        assert route["meta"] == {}


def test_dry_run_validates_without_writing_or_creating_a_backup(
    tmp_path: Path,
    capsys: object,
) -> None:
    """@brief dry-run 不触碰配置或回滚副本 / Dry-run leaves both configuration and rollback copy untouched.

    @param tmp_path pytest 隔离目录 / Pytest isolated directory.
    @param capsys pytest 输出捕获器 / Pytest output capture fixture.
    @return None / None.
    """

    config_path = tmp_path / "config.json"
    legacy_source = _legacy_config_source()
    config_path.write_text(legacy_source, encoding="utf-8")

    assert MIGRATION.main([str(config_path), "--dry-run"]) == 0
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert TEST_SECRET not in captured.out
    assert TEST_SECRET not in captured.err
    assert config_path.read_text(encoding="utf-8") == legacy_source
    assert not (tmp_path / ".config.json.schema-v1.bak").exists()


def test_non_openrouter_active_route_fails_closed_without_writing(
    tmp_path: Path,
    capsys: object,
) -> None:
    """@brief 非 OpenRouter 的活动 route 被拒绝且不写盘 / Reject a non-OpenRouter active route without writing.

    @param tmp_path pytest 隔离目录 / Pytest isolated directory.
    @param capsys pytest 输出捕获器 / Pytest output capture fixture.
    @return None / None.
    """

    config_path = tmp_path / "config.json"
    legacy_source = _legacy_config_source(chat_provider="other-provider")
    config_path.write_text(legacy_source, encoding="utf-8")

    assert MIGRATION.main([str(config_path)]) == 2
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert "non-OpenRouter provider" in captured.err
    assert TEST_SECRET not in captured.out
    assert TEST_SECRET not in captured.err
    assert config_path.read_text(encoding="utf-8") == legacy_source
    assert not (tmp_path / ".config.json.schema-v1.bak").exists()


def test_existing_dangling_rollback_symlink_fails_closed_without_writing(
    tmp_path: Path,
    capsys: object,
) -> None:
    """@brief 悬空回滚符号链接也必须拒绝覆盖 / A dangling rollback symlink must also be refused rather than overwritten.

    @param tmp_path pytest 隔离目录 / Pytest isolated directory.
    @param capsys pytest 输出捕获器 / Pytest output capture fixture.
    @return None / None.
    """

    config_path = tmp_path / "config.json"
    legacy_source = _legacy_config_source()
    config_path.write_text(legacy_source, encoding="utf-8")
    backup_path = tmp_path / ".config.json.schema-v1.bak"
    backup_path.symlink_to(tmp_path / "missing-rollback-target")

    assert MIGRATION.main([str(config_path)]) == 2
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert "refusing to overwrite existing rollback copy" in captured.err
    assert TEST_SECRET not in captured.out
    assert TEST_SECRET not in captured.err
    assert config_path.read_text(encoding="utf-8") == legacy_source
    assert backup_path.is_symlink()


def _route_models(
    routing: dict[str, object],
    task: str,
) -> list[dict[str, object]]:
    """@brief 从迁移后路由取出模型条目 / Extract model entries from a migrated route.

    @param routing 已解析的 v2 routing 对象 / Parsed v2 routing object.
    @param task 任务名 / Task name.
    @return 模型对象列表 / Model-object list.
    """

    task_config = routing[task]
    assert isinstance(task_config, dict)
    routes = task_config["routes"]
    assert isinstance(routes, list) and len(routes) == 1
    route = routes[0]
    assert isinstance(route, dict)
    models = route["models"]
    assert isinstance(models, list)
    assert all(isinstance(model, dict) for model in models)
    return models


def _outside_replaced_members(source: str) -> str:
    """@brief 移除根 schema/AI 值后比较剩余原文 / Compare remaining source after removing root schema/AI values.

    @param source JSONC 原文 / JSONC source text.
    @return 除替换值之外的原文拼接 / Concatenated source outside replaced values.
    """

    spans = MIGRATION._root_member_value_spans(source)  # type: ignore[attr-defined]
    intervals = sorted(spans[key] for key in ("schema_version", "ai"))
    cursor = 0
    pieces: list[str] = []
    for start, end in intervals:
        pieces.append(source[cursor:start])
        cursor = end
    pieces.append(source[cursor:])
    return "".join(pieces)


def _legacy_config_source(*, chat_provider: str = "openrouter") -> str:
    """@brief 创建带注释的最小合法 legacy 配置 / Build a minimal valid commented legacy configuration.

    @param chat_provider chat route 使用的 legacy provider / Legacy provider used by the chat route.
    @return schema-v1 JSONC 文本 / Schema-v1 JSONC text.
    """

    return f'''// 根注释也必须保留
{{
  // 版本注释保留，但数值升级。
  "schema_version": 1,
  // 保留这一条非 AI 注释。
  "identity": {{}},
  "telegram": {{}},
  "runtime": {{}},
  // 此 AI 前置注释必须保留。
  "ai": {{
    // 此 AI 内部注释可被迁移替换。
    "providers": {{
      "openrouter": {{
        "api_key": "{TEST_SECRET}",
        "api_base": "https://openrouter.ai/api/v1",
        "models": {{
          "chat": "chat-primary",
          "chat_fallback": "chat-fallback",
          "vision": "vision-primary",
          "summary": "summary-primary",
          "summary_fallback": "summary-fallback",
          "dreaming": "dreaming-primary",
          "translation": "translation-primary"
        }}
      }}
    }},
    "routing": {{
      "chat": {{"provider_order": ["{chat_provider}"]}},
      "summary": {{"provider": "openrouter", "fallback_provider": null}},
      "dreaming": {{"provider": "openrouter", "fallback_provider": null}},
      "translation": {{"provider": "openrouter", "fallback_provider": null}}
    }}
  }},
  "assistant": {{}},
  "database": {{"endpoint": {{}}, "application": {{}}}},
  "network": {{}},
  "integrations": {{}},
  "economy": {{}},
  "logging": {{}},
  "observability": {{}}
}}
'''
