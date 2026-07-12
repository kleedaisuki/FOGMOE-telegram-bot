"""@brief 异步日志基础设施测试 / Asynchronous logging infrastructure tests."""

import logging
import re

from fogmoe_bot.infrastructure import config
from fogmoe_bot.infrastructure.logging import bot_logging


def test_configure_logging_uses_timestamped_file_and_queue_consumer(
    monkeypatch, tmp_path
):
    """@brief 日志生产者异步写入带时间戳文件 / Producer writes asynchronously to timestamped file."""
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level
    monkeypatch.setattr(config, "LOG_DIR", tmp_path)
    monkeypatch.setattr(config, "LOG_LEVEL", "INFO")
    monkeypatch.setattr(config, "LOG_QUEUE_MAX_SIZE", 10)

    try:
        log_path = bot_logging.configure_logging()
        logging.getLogger("fogmoe.test.logging").info("queued log record")
        bot_logging.shutdown_logging()
    finally:
        root_logger.handlers.clear()
        for handler in original_handlers:
            root_logger.addHandler(handler)
        root_logger.setLevel(original_level)

    assert re.fullmatch(r"tgbot_\d{8}T\d{6}[+-]\d{4}_\d+\.log", log_path.name)
    assert "queued log record" in log_path.read_text(encoding="utf-8")


def test_configure_litellm_logging_removes_private_handlers(monkeypatch):
    """@brief LiteLLM 私有 handler 被移除并传播 / LiteLLM private handlers are removed and propagated."""
    litellm_logger = logging.getLogger("LiteLLM")
    original_handlers = list(litellm_logger.handlers)
    original_level = litellm_logger.level
    original_propagate = litellm_logger.propagate
    original_disabled = litellm_logger.disabled
    private_handler = logging.StreamHandler()
    litellm_logger.addHandler(private_handler)
    monkeypatch.setattr(config, "LITELLM_LOG_LEVEL", "WARNING")

    try:
        bot_logging.configure_litellm_logging()

        assert litellm_logger.handlers == []
        assert litellm_logger.level == logging.WARNING
        assert litellm_logger.propagate is True
        assert litellm_logger.disabled is False
    finally:
        litellm_logger.handlers.clear()
        for handler in original_handlers:
            litellm_logger.addHandler(handler)
        litellm_logger.setLevel(original_level)
        litellm_logger.propagate = original_propagate
        litellm_logger.disabled = original_disabled


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
