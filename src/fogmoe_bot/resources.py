"""@brief 受版本控制的 Bot 静态资源 / Version-controlled static resources for the Bot."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


#: @brief 静态资源所在的项目根目录 / Project root containing static resources.
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class BotResources:
    """@brief Bot 组合根需要的只读资源 / Read-only resources required by the Bot composition root.

    @param project_root 项目根目录 / Project root directory.
    @param help_text Telegram help 文本 / Telegram help text.
    @param system_prompt Assistant system prompt / Assistant system prompt.
    @param sticker_catalog_path AI sticker catalog 路径 / AI sticker catalog path.
    @param moderation_wordlist_path 治理词表路径 / Moderation word-list path.
    @param log_directory 日志与运行期 artifact 根目录 / Root directory for logs and runtime artifacts.
    """

    project_root: Path
    help_text: str
    system_prompt: str
    sticker_catalog_path: Path
    moderation_wordlist_path: Path
    log_directory: Path

    @property
    def generated_artifact_directory(self) -> Path:
        """@brief 返回生成媒体 artifact 目录 / Return generated-media artifact directory.

        @return 生成媒体目录 / Generated-media directory.
        """

        return self.log_directory / "generated_artifacts"

    @property
    def media_rate_limit_directory(self) -> Path:
        """@brief 返回媒体限流状态目录 / Return media rate-limit state directory.

        @return 媒体限流目录 / Media rate-limit directory.
        """

        return self.log_directory / "media_rate_limits"


def load_resources(
    *, log_directory: Path, project_root: Path = PROJECT_ROOT
) -> BotResources:
    """@brief 加载受版本控制的 Bot 文本与路径 / Load version-controlled Bot text and paths.

    @param log_directory 已解析的日志目录 / Resolved log directory.
    @param project_root 可替换项目根目录，便于测试 / Replaceable project root for tests.
    @return 不可变资源集合 / Immutable resource collection.
    """

    resource_root = project_root / "resources"
    return BotResources(
        project_root=project_root,
        help_text=(resource_root / "telegram_help.md").read_text(encoding="utf-8"),
        system_prompt=(resource_root / "prompts" / "system_prompt.md").read_text(
            encoding="utf-8"
        ),
        sticker_catalog_path=resource_root / "ai_sticker_packs.json",
        moderation_wordlist_path=resource_root / "spam_words.txt",
        log_directory=log_directory,
    )


__all__ = ["BotResources", "PROJECT_ROOT", "load_resources"]
