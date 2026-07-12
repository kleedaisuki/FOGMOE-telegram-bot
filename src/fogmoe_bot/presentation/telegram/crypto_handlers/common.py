"""Composition helpers shared by Crypto Telegram handlers."""

from telegram.ext import ContextTypes

from fogmoe_bot.application.crypto.workflow import (
    CRYPTO_SERVICE_DATA_KEY,
    CryptoService,
)


def crypto_service(context: ContextTypes.DEFAULT_TYPE) -> CryptoService:
    """Load the configured Crypto service from ``bot_data``."""

    value = context.application.bot_data.get(CRYPTO_SERVICE_DATA_KEY)
    if not isinstance(value, CryptoService):
        raise RuntimeError("Crypto service is not configured")
    return value
