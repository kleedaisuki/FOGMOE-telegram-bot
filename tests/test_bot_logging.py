"""@brief 异步日志基础设施测试 / Asynchronous logging infrastructure tests."""

import logging
import re

from observability_testkit import make_telemetry

from fogmoe_bot.config import LoggingSettings
from fogmoe_bot.infrastructure.observability import logging as bot_logging


def test_configure_logging_uses_timestamped_file_and_queue_consumer(tmp_path):
    """@brief 日志生产者异步写入带时间戳文件 / Producer writes asynchronously to timestamped file."""
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level
    settings = LoggingSettings(level="INFO", queue_capacity=10)

    try:
        log_path = bot_logging.configure_logging(
            settings,
            tmp_path,
            make_telemetry(),
        )
        logging.getLogger("fogmoe.test.logging").info("queued log record")
        bot_logging.shutdown_logging()
    finally:
        root_logger.handlers.clear()
        for handler in original_handlers:
            root_logger.addHandler(handler)
        root_logger.setLevel(original_level)

    assert re.fullmatch(r"tgbot_\d{8}T\d{6}[+-]\d{4}_\d+\.log", log_path.name)
    assert "queued log record" in log_path.read_text(encoding="utf-8")


def test_shutdown_sentinel_waits_for_queue_capacity() -> None:
    """关停哨兵使用阻塞 put，不会在有界队列已满时被丢弃。"""

    class _Queue:
        def __init__(self) -> None:
            self.values = []

        def put(self, value) -> None:
            self.values.append(value)

        def put_nowait(self, value) -> None:
            del value
            raise AssertionError("shutdown must not use put_nowait")

    log_queue = _Queue()
    listener = bot_logging.DrainingQueueListener(log_queue)

    listener.enqueue_sentinel()

    assert log_queue.values == [None]
