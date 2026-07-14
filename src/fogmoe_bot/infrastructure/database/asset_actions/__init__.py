"""@brief 账户资产确认的 PostgreSQL 适配器 / PostgreSQL adapters for account-asset confirmations."""

from .confirmations import PostgresAssetActionConfirmationStore

__all__ = ["PostgresAssetActionConfirmationStore"]
