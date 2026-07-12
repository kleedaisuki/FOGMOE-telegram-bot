"""@brief Admin 日志适配器的有界 offload 测试 / Bounded-offload tests for the Admin log adapter."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fogmoe_bot.infrastructure.admin.log_reader import AsyncBoundedLogSource


def test_tail_reads_only_bounded_suffix_and_preserves_latest_lines(
    tmp_path: Path,
) -> None:
    """@brief 大文件只读有界尾部并保留最新行 / A large file yields only a bounded suffix while preserving newest lines.

    @param tmp_path pytest 临时目录 / Pytest temporary directory.
    """

    path = tmp_path / "bot.log"
    path.write_text("x" * 10_000 + "\nnew-one\nnew-two\n", encoding="utf-8")
    source = AsyncBoundedLogSource(lambda: path, max_bytes=64, max_concurrency=1)

    tail = asyncio.run(source.tail(lines=5))

    assert tail is not None
    assert tail.lines[-2:] == ("new-one\n", "new-two\n")
    assert tail.truncated
    assert sum(len(line.encode("utf-8")) for line in tail.lines) <= 64


def test_missing_log_source_is_a_typed_absence(tmp_path: Path) -> None:
    """@brief 日志文件缺失返回 None 而非内部异常文本 / A missing log file returns None rather than internal exception text.

    @param tmp_path pytest 临时目录 / Pytest temporary directory.
    """

    source = AsyncBoundedLogSource(lambda: tmp_path / "missing.log")

    assert asyncio.run(source.tail(lines=50)) is None
