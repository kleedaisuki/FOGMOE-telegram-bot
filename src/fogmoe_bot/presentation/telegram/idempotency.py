"""@brief Telegram source Update 幂等键 / Telegram source-Update idempotency keys."""

from __future__ import annotations

from telegram import Update


def telegram_update_idempotency_key(update: Update, operation: str) -> str:
    """@brief 为一个 Update 的业务操作生成稳定键 / Build a stable key for one operation on an Update.

    @param update Telegram Update / Telegram Update.
    @param operation bounded-context 操作名 / Bounded-context operation name.
    @return 可持久化稳定键 / Persistable stable key.
    @raise ValueError Update ID 或操作名无效 / The Update ID or operation name is invalid.
    """

    update_id = update.update_id
    normalized_operation = operation.strip().lower()
    if isinstance(update_id, bool) or not isinstance(update_id, int) or update_id < 0:
        raise ValueError("Telegram Update requires a non-negative integer update_id")
    if not normalized_operation or len(normalized_operation) > 80:
        raise ValueError("Telegram idempotency operation must be non-empty")
    return f"telegram-update:{update_id}:{normalized_operation}"


__all__ = ["telegram_update_idempotency_key"]
