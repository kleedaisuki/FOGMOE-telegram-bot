"""@brief 不阻塞 event loop 的有界日志尾部读取 / Bounded log-tail reading without blocking the event loop."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from fogmoe_bot.application.admin.models import LogTail


type LogPathProvider = Callable[[], Path]
"""@brief 返回当前日志文件的端口 / Port returning the current log file."""


class AsyncBoundedLogSource:
    """@brief 以 loop-owned semaphore 准入并 offload 文件 IO / Admit file reads with a loop-owned semaphore and offload their blocking I/O."""

    def __init__(
        self,
        path_provider: LogPathProvider,
        *,
        max_bytes: int = 64 * 1024,
        max_concurrency: int = 1,
    ) -> None:
        """@brief 注入路径与严格资源上限 / Inject path and strict resource bounds.

        @param path_provider 当前日志路径端口 / Current-log-path port.
        @param max_bytes 单次最大读取字节 / Maximum bytes read per call.
        @param max_concurrency 并发文件读取上限 / Concurrent file-read bound.
        @raise ValueError 边界非正 / Bounds are not positive.
        """

        if max_bytes < 1 or max_concurrency < 1:
            raise ValueError("Log-reading bounds must be positive")
        self._path_provider = path_provider
        self._max_bytes = max_bytes
        self._admission = asyncio.Semaphore(max_concurrency)
        """@brief event-loop-owned 文件 IO 准入门 / Event-loop-owned file-I/O admission gate."""

    async def tail(self, *, lines: int) -> LogTail | None:
        """@brief 在线程池中读取有界文件尾部 / Read a bounded file tail in the thread pool.

        @param lines 行数上限 / Maximum line count.
        @return 快照；文件不存在时为 None / Snapshot, or None when the file is absent.
        @raise ValueError 行数非正 / Line count is not positive.
        """

        if lines < 1:
            raise ValueError("Log tail line count must be positive")
        path = self._path_provider()
        async with self._admission:
            try:
                return await asyncio.to_thread(
                    _read_tail,
                    path,
                    lines=lines,
                    max_bytes=self._max_bytes,
                )
            except FileNotFoundError:
                return None


def _read_tail(path: Path, *, lines: int, max_bytes: int) -> LogTail:
    """@brief 从文件末尾有界读取 / Read a bounded suffix from a file.

    @param path 日志文件 / Log file.
    @param lines 行数上限 / Maximum line count.
    @param max_bytes 字节读取上限 / Byte-read bound.
    @return 解码后快照 / Decoded snapshot.
    @note 不扫描整个大文件，也不创建 tempfile / Never scans the entire large file or creates a temporary file.
    """

    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        read_size = min(size, max_bytes)
        handle.seek(size - read_size)
        data = handle.read(read_size)
    decoded = data.decode("utf-8", errors="replace")
    decoded_lines = decoded.splitlines(keepends=True)
    selected = tuple(decoded_lines[-lines:])
    truncated = size > read_size and len(decoded_lines) < lines
    return LogTail(selected, truncated)


__all__ = ["AsyncBoundedLogSource", "LogPathProvider"]
