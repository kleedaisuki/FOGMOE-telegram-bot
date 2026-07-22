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
from types import TracebackType
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

type _ExceptionInfo = tuple[type[BaseException], BaseException, TracebackType | None]
"""@brief 已验证的 logging exception tuple / Validated logging exception tuple."""


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

        exception_type, exception_message, exception_stack = _exception_details(
            record.exc_info
        )
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
                raw_type, raw_message, raw_stack = _exception_details(record.exc_info)
                exception_type = raw_type
                exception_message = (
                    _redact(raw_message) if raw_message is not None else None
                )
                exception_stack = _redact(raw_stack) if raw_stack is not None else None
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


def _exception_details(
    exc_info: object,
) -> tuple[str | None, str | None, str | None]:
    """@brief 安全提取已解析的 logging 异常信息 / Safely extract a resolved logging exception.

    ``logging`` 允许调用方传入 ``exc_info=False``。该值会原样留在
    ``LogRecord``，不是 ``sys.exc_info()`` 返回的三元组；队列 handler 在格式化之前读取
    它时必须把它视为“无异常”，否则一次可预期的网络重试会反过来触发 logging error。/
    ``logging`` permits callers to pass ``exc_info=False``. The value remains on the
    ``LogRecord`` rather than becoming a ``sys.exc_info()`` tuple, so queue handlers
    must treat it as no exception before formatting.

    @param exc_info LogRecord 的未可信 exc_info 字段 / Untrusted LogRecord exc_info field.
    @return 异常类型、消息与栈；无有效异常时均为 None /
        Exception type, message, and stack; all None without a valid exception.
    """

    if not isinstance(exc_info, tuple) or len(exc_info) != 3:
        return None, None, None
    error_type, error_value, error_traceback = exc_info
    if (
        not isinstance(error_type, type)
        or not issubclass(error_type, BaseException)
        or not isinstance(error_value, BaseException)
        or (
            error_traceback is not None
            and not isinstance(error_traceback, TracebackType)
        )
    ):
        return None, None, None
    normalized: _ExceptionInfo = (error_type, error_value, error_traceback)
    return (
        error_type.__name__,
        str(error_value),
        logging.Formatter().formatException(normalized),
    )


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
    "configure_logging",
    "current_log_file_path",
    "shutdown_logging",
]
