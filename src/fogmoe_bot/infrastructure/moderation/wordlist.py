"""@brief 文件型全局审核规则源 / File-backed global moderation-rule source."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from fogmoe_bot.domain.moderation.models import ModerationRule, RuleKind, RuleScope

logger = logging.getLogger(__name__)


class FileModerationRuleProvider:
    """@brief 从文本文件加载全局审核规则 / Load global moderation rules from a text file.

    @param path 规则文件路径 / Rule-file path.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._mtime_ns: int | None = None
        self._rules: tuple[ModerationRule, ...] = ()

    def get_global_rules(self) -> tuple[ModerationRule, ...]:
        """@brief 返回最新全局规则 / Return current global rules.

        文件修改后在下一次读取时自动刷新。
        / Automatically refreshes on the first read after the file changes.

        @return 全局规则 / Global rules.
        """

        self.refresh()
        return self._rules

    def refresh(self, *, force: bool = False) -> tuple[ModerationRule, ...]:
        """@brief 从磁盘刷新规则 / Refresh rules from disk.

        @param force 是否忽略修改时间强制加载 / Whether to ignore modification time.
        @return 当前全局规则 / Current global rules.
        """

        if not self._path.exists():
            logger.warning("全局审核规则文件不存在: %s", self._path)
            self._rules = ()
            self._mtime_ns = None
            return self._rules

        mtime_ns = self._path.stat().st_mtime_ns
        if not force and self._mtime_ns == mtime_ns:
            return self._rules

        rules: list[ModerationRule] = []
        for raw_line in self._path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("//"):
                pattern = line[2:].strip()
                try:
                    re.compile(pattern)
                except re.error:
                    logger.error("忽略无效的全局审核正则: %s", pattern)
                    continue
                kind = RuleKind.REGEX
            else:
                pattern = line
                kind = RuleKind.LITERAL
            rules.append(
                ModerationRule(
                    pattern=pattern,
                    kind=kind,
                    scope=RuleScope.GLOBAL,
                )
            )

        self._rules = tuple(rules)
        self._mtime_ns = mtime_ns
        logger.info("已加载 %s 条全局审核规则", len(self._rules))
        return self._rules
