"""Utility helpers for Telegram messages and sending."""

import asyncio
import logging
from datetime import timedelta
from functools import partial
from typing import Any, Awaitable, Callable

import telegram.error
from telegram.constants import ParseMode

try:  # pragma: no cover - optional dependency
    import telegramify_markdown  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    telegramify_markdown = None

AsyncSendFunc = Callable[..., Awaitable[Any]]

TELEGRAM_MAX_MESSAGE_LENGTH = 4096
TELEGRAM_SEND_RETRY_ATTEMPTS = 3
TELEGRAM_SEND_RETRY_INITIAL_DELAY_SECONDS = 0.5
TELEGRAM_SEND_RETRY_MAX_DELAY_SECONDS = 8.0
TELEGRAM_RETRY_AFTER_PADDING_SECONDS = 0.1


class PartialTelegramSendError(Exception):
    def __init__(
        self,
        message: str,
        sent_messages: list[Any],
        sent_text: str = "",
    ) -> None:
        super().__init__(message)
        self.sent_messages = list(sent_messages)
        self.sent_text = sent_text


def is_retryable_telegram_error(exc: BaseException) -> bool:
    """Return whether a Telegram send failure is likely transient."""
    if isinstance(exc, telegram.error.RetryAfter):
        return True
    if isinstance(exc, (telegram.error.BadRequest, telegram.error.Forbidden)):
        return False
    return isinstance(exc, (telegram.error.TimedOut, telegram.error.NetworkError))


def _retry_after_delay_seconds(exc: telegram.error.RetryAfter) -> float:
    retry_after = getattr(exc, "_retry_after", None)
    if retry_after is None:
        retry_after = exc.retry_after
    if isinstance(retry_after, timedelta):
        delay = retry_after.total_seconds()
    else:
        delay = float(retry_after)
    return max(0.0, delay) + TELEGRAM_RETRY_AFTER_PADDING_SECONDS


def _telegram_retry_delay_seconds(
    exc: BaseException,
    *,
    attempt: int,
    initial_delay: float,
    max_delay: float,
) -> float:
    if isinstance(exc, telegram.error.RetryAfter):
        return _retry_after_delay_seconds(exc)
    delay = initial_delay * (2 ** max(0, attempt - 1))
    return float(min(max_delay, delay))


def telegram_error_summary(exc: object) -> str:
    if isinstance(exc, telegram.error.RetryAfter):
        retry_after = (
            _retry_after_delay_seconds(exc) - TELEGRAM_RETRY_AFTER_PADDING_SECONDS
        )
        return f"{exc.__class__.__name__}: retry after {retry_after:.1f}s"
    return f"{exc.__class__.__name__}: {exc}"


async def retry_telegram_send(
    operation: Callable[[], Awaitable[Any]],
    *,
    logger: logging.Logger | None,
    action: str,
    attempts: int = TELEGRAM_SEND_RETRY_ATTEMPTS,
    initial_delay: float = TELEGRAM_SEND_RETRY_INITIAL_DELAY_SECONDS,
    max_delay: float = TELEGRAM_SEND_RETRY_MAX_DELAY_SECONDS,
) -> Any:
    """Run a Telegram send operation with retry/backoff for transient errors."""
    attempts = max(1, attempts)
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except Exception as exc:
            if not is_retryable_telegram_error(exc) or attempt >= attempts:
                raise
            delay = _telegram_retry_delay_seconds(
                exc,
                attempt=attempt,
                initial_delay=initial_delay,
                max_delay=max_delay,
            )
            if logger:
                logger.warning(
                    "Telegram %s failed with transient error (attempt %s/%s); "
                    "retrying in %.1fs: %s",
                    action,
                    attempt,
                    attempts,
                    delay,
                    telegram_error_summary(exc),
                )
            await asyncio.sleep(delay)
    raise RuntimeError("unreachable Telegram retry state")


def _split_text_segments(
    text: str, limit: int = TELEGRAM_MAX_MESSAGE_LENGTH
) -> list[str]:
    """Split text into chunks that respect Telegram's message length limit."""
    if len(text) <= limit:
        return [text]

    segments: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= limit:
            segments.append(remaining)
            break

        split_idx = remaining.rfind("\n", 0, limit)
        if split_idx <= 0:
            split_idx = remaining.rfind(" ", 0, limit)
        if split_idx <= 0:
            split_idx = limit

        chunk = remaining[:split_idx]
        segments.append(chunk)
        remaining = remaining[split_idx:]
        if remaining.startswith("\n"):
            remaining = remaining[1:]

    return [segment for segment in segments if segment]


def _is_missing_reply_error(error: telegram.error.BadRequest) -> bool:
    return "message to be replied not found" in str(error).lower()


def _remove_reply_target(send_kwargs: dict[str, Any]) -> None:
    send_kwargs.pop("reply_to_message_id", None)
    send_kwargs.pop("reply_to_message", None)
    send_kwargs.pop("quote", None)


async def _attempt_send(
    target: AsyncSendFunc,
    payload: str,
    *,
    mode: str | None,
    send_kwargs: dict[str, Any],
    fallback_send: AsyncSendFunc | None,
) -> Any:
    current_func = target
    attempted_fallback = False
    while True:
        call_kwargs = dict(send_kwargs)
        if current_func is fallback_send:
            _remove_reply_target(call_kwargs)
        try:
            if mode is not None:
                return await current_func(payload, parse_mode=mode, **call_kwargs)
            call_kwargs.pop("parse_mode", None)
            return await current_func(payload, **call_kwargs)
        except telegram.error.BadRequest as error:
            if (
                attempted_fallback
                or fallback_send is None
                or not _is_missing_reply_error(error)
            ):
                raise
            current_func = fallback_send
            attempted_fallback = True


async def _send_markdown_chunk(
    send_func: AsyncSendFunc,
    chunk_text: str,
    *,
    parse_mode: str,
    logger: logging.Logger | None,
    fallback_send: AsyncSendFunc | None,
    send_kwargs: dict[str, Any],
) -> Any:
    try:
        return await retry_telegram_send(
            lambda: _attempt_send(
                send_func,
                chunk_text,
                mode=parse_mode,
                send_kwargs=send_kwargs,
                fallback_send=fallback_send,
            ),
            logger=logger,
            action="send text message",
        )
    except telegram.error.BadRequest as error:
        if logger:
            logger.warning("Markdown send failed (%s).", error)

    if parse_mode == ParseMode.MARKDOWN and telegramify_markdown is not None:
        try:
            converted = telegramify_markdown.markdownify(
                chunk_text,
                max_line_length=None,
                normalize_whitespace=False,
            )
            return await retry_telegram_send(
                lambda: _attempt_send(
                    send_func,
                    converted,
                    mode=ParseMode.MARKDOWN_V2,
                    send_kwargs=send_kwargs,
                    fallback_send=fallback_send,
                ),
                logger=logger,
                action="send MarkdownV2 text message",
            )
        except telegram.error.BadRequest as error:
            if logger:
                logger.warning(
                    "MarkdownV2 retry failed (%s). Falling back to plain text.",
                    error,
                )

    return await retry_telegram_send(
        lambda: _attempt_send(
            send_func,
            chunk_text,
            mode=None,
            send_kwargs=send_kwargs,
            fallback_send=fallback_send,
        ),
        logger=logger,
        action="send plain text message",
    )


async def safe_send_markdown(
    send_func: AsyncSendFunc,
    text: str,
    *,
    parse_mode: str = ParseMode.MARKDOWN,
    logger: logging.Logger = logging.getLogger(__name__),
    fallback_send: AsyncSendFunc | None = None,
    **kwargs: Any,
) -> list[Any]:
    """Send text using Telegram Markdown with graceful fallbacks.

    Args:
        send_func: Awaitable function that accepts ``text`` as first arg.
        text: Message content.
        parse_mode: Telegram parse mode to attempt first.
        logger: Logger for warning messages.
        **kwargs: Additional keyword arguments forwarded to ``send_func``.

    Returns:
        A list of Telegram API responses, one per sent chunk.
    """

    chunks = _split_text_segments(text)

    results: list[Any] = []
    sent_chunks: list[str] = []
    for index, chunk in enumerate(chunks):
        chunk_kwargs = dict(kwargs)
        if index > 0:
            _remove_reply_target(chunk_kwargs)
        try:
            result = await _send_markdown_chunk(
                send_func,
                chunk,
                parse_mode=parse_mode,
                logger=logger,
                fallback_send=fallback_send,
                send_kwargs=chunk_kwargs,
            )
        except Exception as exc:
            if results:
                raise PartialTelegramSendError(
                    str(exc),
                    results,
                    "\n".join(sent_chunks).strip(),
                ) from exc
            raise
        results.append(result)
        sent_chunks.append(chunk)

    return results


def partial_send(
    bot_method: AsyncSendFunc, /, *args: Any, **kwargs: Any
) -> AsyncSendFunc:
    """Utility to pre-bind positional/keyword args for bot send methods."""
    return partial(bot_method, *args, **kwargs)
