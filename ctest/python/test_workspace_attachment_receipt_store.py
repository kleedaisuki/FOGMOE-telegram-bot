"""@brief Workspace 附件 receipt PostgreSQL 错误分类的 CTest / CTest for Workspace-attachment receipt PostgreSQL error classification."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

from sqlalchemy.exc import DBAPIError, SQLAlchemyError

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
"""@brief 仓库根目录 / Repository root directory."""

_SOURCE_ROOT = _PROJECT_ROOT / "src"
"""@brief Python src-layout 根目录 / Python src-layout root directory."""

if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from fogmoe_bot.application.assistant.workspace_attachment_receipt import (  # noqa: E402
    WorkspaceAttachmentReceiptConflictError,
    WorkspaceAttachmentReceiptUnavailableError,
)
from fogmoe_bot.infrastructure.database.workspace_attachment_receipts import (  # noqa: E402
    _receipt_storage_error,
)


class _DriverError(Exception):
    """@brief 带可选 PostgreSQL SQLSTATE 的最小 driver 异常替身 / Minimal driver-error double with an optional PostgreSQL SQLSTATE."""

    def __init__(self, sqlstate: str | None, *, use_pgcode: bool = False) -> None:
        """@brief 保存一个 driver 暴露的 SQLSTATE / Store a SQLSTATE exposed by a driver.

        @param sqlstate 五字符 PostgreSQL SQLSTATE 或 ``None`` / Five-character PostgreSQL SQLSTATE, or ``None``.
        @param use_pgcode 是否使用 psycopg 风格的 ``pgcode`` 字段 / Whether to use psycopg-style ``pgcode`` instead.
        @return None / None.
        """

        if use_pgcode:
            self.pgcode = sqlstate
        else:
            self.sqlstate = sqlstate
        super().__init__(sqlstate or "driver error without SQLSTATE")


def _dbapi_error(sqlstate: str | None, *, use_pgcode: bool = False) -> DBAPIError:
    """@brief 构造携带 driver SQLSTATE 的 SQLAlchemy 异常 / Construct a SQLAlchemy exception carrying a driver SQLSTATE.

    @param sqlstate 目标 SQLSTATE 或 ``None`` / Target SQLSTATE, or ``None``.
    @param use_pgcode 是否覆盖 ``pgcode`` 兼容路径 / Whether to cover the ``pgcode`` compatibility path.
    @return 可交给 receipt adapter 分类的 DBAPIError / DBAPIError suitable for receipt-adapter classification.
    """

    return DBAPIError(
        "SELECT 1",
        {},
        _DriverError(sqlstate, use_pgcode=use_pgcode),
    )


class WorkspaceAttachmentReceiptStoreTests(unittest.TestCase):
    """@brief native publish 后，数据库错误绝不能把永久冲突伪装成网络重试 / A database error after native publish must never disguise a permanent conflict as a network retry."""

    def test_permanent_postgresql_semantics_become_receipt_conflicts(self) -> None:
        """@brief 约束、数据与 55000 状态错误均终结为 conflict / Constraint, data, and 55000 state errors terminate as conflicts.

        @return None / None.
        """

        for sqlstate in ("23514", "23505", "23503", "22003", "55000"):
            with self.subTest(sqlstate=sqlstate):
                self.assertIsInstance(
                    _receipt_storage_error(_dbapi_error(sqlstate)),
                    WorkspaceAttachmentReceiptConflictError,
                )
        self.assertIsInstance(
            _receipt_storage_error(_dbapi_error("23514", use_pgcode=True)),
            WorkspaceAttachmentReceiptConflictError,
        )

    def test_transient_or_unclassified_database_errors_remain_retryable(self) -> None:
        """@brief serialization/deadlock/connection 与无 SQLSTATE 故障仍归为暂时不可用 / Serialization, deadlock, connection, and no-SQLSTATE failures remain temporarily unavailable.

        @return None / None.
        """

        for sqlstate in ("40001", "40P01", "08006", None):
            with self.subTest(sqlstate=sqlstate):
                self.assertIsInstance(
                    _receipt_storage_error(_dbapi_error(sqlstate)),
                    WorkspaceAttachmentReceiptUnavailableError,
                )
        self.assertIsInstance(
            _receipt_storage_error(SQLAlchemyError("pool is unavailable")),
            WorkspaceAttachmentReceiptUnavailableError,
        )


if __name__ == "__main__":
    unittest.main()
