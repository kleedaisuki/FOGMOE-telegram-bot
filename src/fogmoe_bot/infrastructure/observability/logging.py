"""@brief 文件兜底与 PostgreSQL 结构日志入口 / File fallback and PostgreSQL structured-log ingress."""

from __future__ import annotations

import atexit
import logging
import os
import queue
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path
from typing import cast

from fogmoe_bot.application.observability.telemetry import Telemetry
from fogmoe_bot.config import LoggingSettings
from fogmoe_bot.domain.observability.signals import Severity
from fogmoe_bot.domain.observability.trace import TraceContext

_LOG_LISTENER: QueueListener | None = None
"""@brief 当前日志消费者 / Active log consumer."""
_QUEUE_HANDLER: ContextQueueHandler | None = None
"""@brief 当前生产者 handler / Active producer handler."""
_CURRENT_LOG_FILE_PATH: Path | None = None
"""@brief 当前进程日志路径 / Current process log path."""
_ATEXIT_REGISTERED = False
"""@brief 是否已注册退出清理 / Whether exit cleanup is registered."""

_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)((?:api[_-]?key|token|password|secret)\s*[:=]\s*)[^\s,;]+"),
)
"""@brief 日志输出前的凭据模式 / Credential patterns applied before log output."""


class ContextQueueHandler(QueueHandler):
    """@brief 捕获生产者 trace context 的非阻塞队列 handler / Non-blocking queue handler capturing producer trace context."""

    def __init__(
        self,
        log_queue: queue.Queue[logging.LogRecord],
        telemetry: Telemetry,
    ) -> None:
        """@brief 注入日志队列和遥测 / Inject the log queue and telemetry.

        @param log_queue 有界日志队列 / Bounded log queue.
        @param telemetry 丢弃计数与 context 来源 / Drop counter and context source.
        """

        super().__init__(log_queue)
        self._telemetry = telemetry

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        """@brief 在生产者线程冻结消息与 trace context / Freeze the message and trace context in the producer thread.

        @param record 原始日志记录 / Original log record.
        @return 可跨线程记录 / Cross-thread-safe record.
        """

        exception_type: str | None = None
        exception_message: str | None = None
        exception_stack: str | None = None
        if record.exc_info is not None:
            error_type = record.exc_info[0]
            exception_type = error_type.__name__ if error_type is not None else None
            exception_message = str(record.exc_info[1])
            exception_stack = logging.Formatter().formatException(record.exc_info)
        prepared = cast(logging.LogRecord, super().prepare(record))
        prepared.fogmoe_trace_context = self._telemetry.current_context
        prepared.fogmoe_telemetry_attributes = self._telemetry.current_attributes
        prepared.fogmoe_exception_type = exception_type
        prepared.fogmoe_exception_message = exception_message
        prepared.fogmoe_exception_stack = exception_stack
        return prepared

    def enqueue(self, record: logging.LogRecord) -> None:
        """@brief 非阻塞入队并显式计数丢弃 / Enqueue without blocking and explicitly count drops.

        @param record 已准备记录 / Prepared record.
        @return None / None.
        """

        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self._telemetry.counter(
                "fogmoe.telemetry.log_records.dropped",
                attributes={
                    "logger.name": record.name,
                    "log.severity": record.levelname,
                },
            )


class DrainingQueueListener(QueueListener):
    """@brief 关停哨兵不会被满队列丢弃的 listener / Listener whose shutdown sentinel cannot be dropped by a full queue."""

    def enqueue_sentinel(self) -> None:
        """@brief 等待容量后加入终止哨兵 / Wait for capacity before enqueuing the terminal sentinel.

        @return None / None.
        """

        log_queue = cast(queue.Queue[object], self.queue)
        log_queue.put(getattr(self, "_sentinel", None))


class TelemetryLogHandler(logging.Handler):
    """@brief 将标准库 LogRecord 映射为 typed 日志信号 / Map standard-library LogRecords to typed log signals."""

    def __init__(self, telemetry: Telemetry) -> None:
        """@brief 注入非阻塞遥测 / Inject non-blocking telemetry.

        @param telemetry typed signal recorder / Typed signal recorder.
        """

        super().__init__()
        self._telemetry = telemetry

    def emit(self, record: logging.LogRecord) -> None:
        """@brief 映射、脱敏并发出日志 / Map, redact, and emit a log.

        @param record 已在生产者线程准备的记录 / Record prepared in the producer thread.
        @return None / None.
        """

        try:
            context_value = getattr(record, "fogmoe_trace_context", None)
            context = context_value if isinstance(context_value, TraceContext) else None
            exception_type: str | None = None
            exception_message: str | None = None
            exception_stack: str | None = None
            captured_type = getattr(record, "fogmoe_exception_type", None)
            captured_message = getattr(record, "fogmoe_exception_message", None)
            captured_stack = getattr(record, "fogmoe_exception_stack", None)
            if isinstance(captured_type, str):
                exception_type = captured_type
            if isinstance(captured_message, str):
                exception_message = _redact(captured_message)
            if isinstance(captured_stack, str):
                exception_stack = _redact(captured_stack)
            elif record.exc_info is not None:
                error_type = record.exc_info[0]
                exception_type = error_type.__name__ if error_type is not None else None
                exception_message = _redact(str(record.exc_info[1]))
                exception_stack = _redact(
                    logging.Formatter().formatException(record.exc_info)
                )
            raw_attributes = getattr(record, "telemetry_attributes", {})
            correlation_value = getattr(record, "fogmoe_telemetry_attributes", {})
            correlation_attributes = (
                dict(correlation_value)
                if isinstance(correlation_value, Mapping)
                else {}
            )
            attributes = {
                **correlation_attributes,
                **(raw_attributes if isinstance(raw_attributes, dict) else {}),
            }
            event_value = getattr(record, "event_name", None)
            event_name = (
                event_value.strip()
                if isinstance(event_value, str) and event_value.strip()
                else f"log.{record.name}"[:255]
            )
            self._telemetry.log(
                occurred_at=datetime.fromtimestamp(record.created, tz=UTC),
                severity=_severity(record.levelno),
                severity_text=record.levelname,
                logger_name=record.name,
                event_name=event_name,
                body=_redact(record.getMessage()),
                exception_type=exception_type,
                exception_message=exception_message,
                exception_stack=exception_stack,
                attributes=attributes,
                context=context,
            )
        except Exception:
            self.handleError(record)


def _severity(level: int) -> Severity:
    """@brief 映射 Python level 到 OTel severity / Map a Python level to OTel severity."""

    if level >= logging.CRITICAL:
        return Severity.FATAL
    if level >= logging.ERROR:
        return Severity.ERROR
    if level >= logging.WARNING:
        return Severity.WARN
    if level >= logging.INFO:
        return Severity.INFO
    if level >= logging.DEBUG:
        return Severity.DEBUG
    return Severity.TRACE


def _redact(value: str) -> str:
    """@brief 删除常见凭据值并限制大小 / Remove common credential values and bound size."""

    redacted = value
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(r"\1[REDACTED]", redacted)
    return redacted[:16384]


def _resolve_log_level(value: str, *, fallback: int = logging.INFO) -> int:
    """@brief 解析日志级别 / Resolve a logging level."""

    return getattr(logging, (value or "").upper(), fallback)


def _new_log_file_path(log_directory: Path) -> Path:
    """@brief 创建当前进程日志路径 / Build the current process log path.

    @param log_directory 已由组合根解析的日志目录 / Log directory resolved by the composition root.
    @return 带时间戳的进程日志路径 / Timestamped process-log path.
    """

    timestamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    return log_directory / f"tgbot_{timestamp}_{os.getpid()}.log"


def current_log_file_path() -> Path:
    """@brief 返回当前日志路径 / Return the current log path.

    @raise RuntimeError 日志尚未配置 / Logging is not configured.
    """

    if _CURRENT_LOG_FILE_PATH is None:
        raise RuntimeError("Logging has not been configured")
    return _CURRENT_LOG_FILE_PATH


def prepare_litellm_logging() -> None:
    """@brief 在导入 LiteLLM 前禁止其私有 stdout handler / Prevent LiteLLM private stdout handlers before import.

    @return None / None.
    @note LiteLLM 在 import 时读取 ``LITELLM_LOG`` 并可能注册私有 stdout handler。
        import 完成后 ``configure_litellm_logging`` 会删除私有 handler、让运行期日志
        继承唯一的 ``LOG_LEVEL`` 并进入项目统一管道。/
        LiteLLM reads ``LITELLM_LOG`` during import and may register private stdout handlers.
        After import, ``configure_litellm_logging`` removes private handlers and routes runtime
        logs at the sole ``LOG_LEVEL`` through the project pipeline.
    """

    os.environ["LITELLM_LOG"] = "ERROR"


def configure_litellm_logging(settings: LoggingSettings) -> None:
    """@brief 让 LiteLLM 继承根日志级别并传播到统一管道 / Inherit the root log level and route LiteLLM through the unified pipeline.

    @param settings 已验证的日志设置 / Validated logging settings.
    @return None / None.
    """

    level = _resolve_log_level(settings.level)
    for logger_name in ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy"):
        third_party_logger = logging.getLogger(logger_name)
        for handler in tuple(third_party_logger.handlers):
            third_party_logger.removeHandler(handler)
            handler.close()
        third_party_logger.setLevel(level)
        third_party_logger.propagate = True
        third_party_logger.disabled = False


def configure_logging(
    settings: LoggingSettings,
    log_directory: Path,
    telemetry: Telemetry,
) -> Path:
    """@brief 配置单一异步日志入口与双 sink / Configure one asynchronous logging ingress and two sinks.

    @param settings 已验证的日志设置 / Validated logging settings.
    @param log_directory 已由组合根解析的日志目录 / Log directory resolved by the composition root.
    @param telemetry PostgreSQL 结构日志入口 / PostgreSQL structured-log ingress.
    @return 当前文件日志路径 / Current file-log path.
    """

    global _ATEXIT_REGISTERED, _CURRENT_LOG_FILE_PATH, _LOG_LISTENER, _QUEUE_HANDLER
    if _LOG_LISTENER is not None:
        return current_log_file_path()

    log_directory.mkdir(parents=True, exist_ok=True)
    log_file_path = _new_log_file_path(log_directory)
    log_level = _resolve_log_level(settings.level)
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    file_handler = RotatingFileHandler(
        log_file_path,
        maxBytes=settings.file_max_bytes,
        backupCount=settings.file_backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    telemetry_handler = TelemetryLogHandler(telemetry)
    telemetry_handler.setLevel(log_level)

    log_queue: queue.Queue[logging.LogRecord] = queue.Queue(
        maxsize=settings.queue_capacity
    )
    queue_handler = ContextQueueHandler(log_queue, telemetry)
    queue_handler.setLevel(log_level)
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(log_level)
    root_logger.addHandler(queue_handler)

    listener = DrainingQueueListener(
        log_queue,
        file_handler,
        telemetry_handler,
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
    """@brief 排空日志并关闭 sinks / Drain logging and close its sinks.

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


__all__ = [
    "ContextQueueHandler",
    "DrainingQueueListener",
    "TelemetryLogHandler",
    "configure_litellm_logging",
    "configure_logging",
    "current_log_file_path",
    "prepare_litellm_logging",
    "shutdown_logging",
]
