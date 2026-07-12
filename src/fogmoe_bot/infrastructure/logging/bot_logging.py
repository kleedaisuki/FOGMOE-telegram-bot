"""@brief 异步文件日志基础设施 / Asynchronous file logging infrastructure."""

import atexit
import logging
import os
import queue
from datetime import datetime
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path
from typing import cast

from fogmoe_bot.infrastructure import config


#: @brief 当前日志消费者 / Active log consumer.
_LOG_LISTENER: QueueListener | None = None
#: @brief 当前生产者处理器 / Active log producer handler.
_QUEUE_HANDLER: QueueHandler | None = None
#: @brief 当前进程日志文件路径 / Current process log file path.
_CURRENT_LOG_FILE_PATH: Path | None = None
#: @brief 是否已注册退出清理 / Whether process-exit cleanup is registered.
_ATEXIT_REGISTERED = False


class DroppingQueueHandler(QueueHandler):
    """@brief 非阻塞丢弃式日志生产者 / Non-blocking log producer with drop-on-full.

    @note 队列满时宁可丢弃日志，也不阻塞 Telegram 更新处理或 AI 推理路径。
    """

    def __init__(self, log_queue: queue.Queue[logging.LogRecord]) -> None:
        """@brief 初始化生产者 / Initialize the producer.

        @param log_queue 有界日志队列 / Bounded log queue.
        """
        super().__init__(log_queue)
        self.dropped_records = 0

    def enqueue(self, record: logging.LogRecord) -> None:
        """@brief 非阻塞写入日志队列 / Enqueue a log record without blocking.

        @param record 已准备的日志记录 / Prepared log record.
        @return None / None.
        """
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self.dropped_records += 1


class DrainingQueueListener(QueueListener):
    """Queue listener whose shutdown sentinel cannot be dropped on a full queue."""

    def enqueue_sentinel(self) -> None:
        """Wait for consumer capacity when scheduling the terminal sentinel."""

        log_queue = cast(queue.Queue[object], self.queue)
        log_queue.put(getattr(self, "_sentinel", None))


def _resolve_log_level(value: str, *, fallback: int = logging.INFO) -> int:
    """@brief 解析日志级别 / Resolve a logging level.

    @param value 配置中的日志级别名称 / Configured logging level name.
    @param fallback 无效配置时的回退级别 / Fallback level for invalid configuration.
    @return Python logging 数值级别 / Numeric Python logging level.
    """
    return getattr(logging, (value or "").upper(), fallback)


def _new_log_file_path() -> Path:
    """@brief 创建无空格时间戳日志路径 / Build a whitespace-free timestamped log path.

    @return 当前进程专属日志文件路径 / Process-specific log file path.
    """
    timestamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    return config.LOG_DIR / f"tgbot_{timestamp}_{os.getpid()}.log"


def current_log_file_path() -> Path:
    """@brief 获取当前日志文件路径 / Get the current log file path.

    @return 已配置的当前日志文件路径 / Configured current log file path.
    @raise RuntimeError 日志管道尚未配置 / Raised before the logging pipeline is configured.
    """
    if _CURRENT_LOG_FILE_PATH is None:
        raise RuntimeError("Logging has not been configured")
    return _CURRENT_LOG_FILE_PATH


def prepare_litellm_logging() -> None:
    """@brief 在导入 LiteLLM 前压低其私有 handler / Silence LiteLLM private handler before import.

    @note 导入完成后由 ``configure_litellm_logging`` 接入项目队列。
    """
    os.environ["LITELLM_LOG"] = "ERROR"


def configure_litellm_logging() -> None:
    """@brief 将 LiteLLM 日志接入根日志队列 / Route LiteLLM logs into the root queue.

    @note LiteLLM 默认自行附加 StreamHandler；此处移除它以避免绕过生产者消费者管道。
    """
    level = _resolve_log_level(config.LITELLM_LOG_LEVEL, fallback=logging.WARNING)
    for logger_name in ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy"):
        third_party_logger = logging.getLogger(logger_name)
        for handler in tuple(third_party_logger.handlers):
            third_party_logger.removeHandler(handler)
            handler.close()
        third_party_logger.setLevel(level)
        third_party_logger.propagate = True
        third_party_logger.disabled = False


def configure_logging() -> Path:
    """@brief 配置生产者消费者日志管道 / Configure producer-consumer logging pipeline.

    @return 当前进程的日志文件路径 / Current process log file path.
    """
    global _ATEXIT_REGISTERED, _CURRENT_LOG_FILE_PATH, _LOG_LISTENER, _QUEUE_HANDLER

    if _LOG_LISTENER is not None:
        return current_log_file_path()

    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file_path = _new_log_file_path()
    log_level = _resolve_log_level(config.LOG_LEVEL)
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    file_handler = RotatingFileHandler(
        log_file_path,
        maxBytes=max(1, int(config.LOG_FILE_MAX_BYTES)),
        backupCount=max(0, int(config.LOG_FILE_BACKUP_COUNT)),
        encoding="utf-8",
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)

    log_queue: queue.Queue[logging.LogRecord] = queue.Queue(
        maxsize=max(1, int(config.LOG_QUEUE_MAX_SIZE))
    )
    queue_handler = DroppingQueueHandler(log_queue)
    queue_handler.setLevel(log_level)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(log_level)
    root_logger.addHandler(queue_handler)

    listener = DrainingQueueListener(
        log_queue,
        file_handler,
        respect_handler_level=True,
    )
    listener.start()

    _CURRENT_LOG_FILE_PATH = log_file_path
    _QUEUE_HANDLER = queue_handler
    _LOG_LISTENER = listener
    if not _ATEXIT_REGISTERED:
        atexit.register(shutdown_logging)
        _ATEXIT_REGISTERED = True
    return log_file_path


def shutdown_logging() -> None:
    """@brief 排空队列并停止日志消费者 / Drain the queue and stop the log consumer.

    @return None / None.
    """
    global _CURRENT_LOG_FILE_PATH, _LOG_LISTENER, _QUEUE_HANDLER

    listener = _LOG_LISTENER
    if listener is None:
        return

    root_logger = logging.getLogger()
    if _QUEUE_HANDLER is not None:
        root_logger.removeHandler(_QUEUE_HANDLER)
        _QUEUE_HANDLER.close()
    listener.stop()
    for handler in listener.handlers:
        handler.close()
    _QUEUE_HANDLER = None
    _LOG_LISTENER = None
    _CURRENT_LOG_FILE_PATH = None
