from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

SRC_ROOT = Path(__file__).resolve().parents[4]

config = context.config

# No SQLAlchemy models in use yet.
target_metadata = None
VERSION_TABLE = "alembic_version"
VERSION_NUM_LENGTH = 255


def configure_import_path() -> None:
    """@brief 配置 src 导入路径 / Configure src import path.

    @return None / None.
    """

    src_root = str(SRC_ROOT)
    if src_root not in sys.path:
        sys.path.insert(0, src_root)


def configure_logging() -> None:
    """@brief 配置 Alembic 日志 / Configure Alembic logging.

    @return None / None.
    """

    if config.config_file_name is not None:
        fileConfig(config.config_file_name)


def get_url() -> str:
    """@brief 获取数据库连接 URL / Get database connection URL.

    @return 数据库连接 URL / Database connection URL.
    """

    try:
        from fogmoe_bot.infrastructure.config import SQLALCHEMY_DATABASE_URI

        return SQLALCHEMY_DATABASE_URI
    except Exception:
        return config.get_main_option("sqlalchemy.url")


def quote_identifier(identifier: str) -> str:
    """@brief 引用 PostgreSQL 标识符 / Quote a PostgreSQL identifier.

    @param identifier 标识符 / Identifier.
    @return 双引号引用后的标识符 / Double-quoted identifier.
    """

    return '"' + identifier.replace('"', '""') + '"'


def get_migration_schema() -> str:
    """@brief 获取迁移元数据 schema / Get migration metadata schema.

    @return Alembic 版本表 schema / Alembic version table schema.
    """

    try:
        from fogmoe_bot.infrastructure.config import DB_MIGRATION_SCHEMA

        return DB_MIGRATION_SCHEMA
    except Exception:
        return "infra"


def configure_context(connection: Connection | None = None) -> None:
    """@brief 配置 Alembic 上下文 / Configure Alembic context.

    @param connection 在线迁移连接 / Online migration connection.
    @return None / None.
    """

    if connection is None:
        context.configure(
            url=get_url(),
            target_metadata=target_metadata,
            literal_binds=True,
            dialect_opts={"paramstyle": "named"},
            version_table_schema=get_migration_schema(),
        )
        return

    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table_schema=get_migration_schema(),
    )


def ensure_version_table(connection: Connection, migration_schema: str) -> None:
    """@brief 确保 Alembic 版本表可容纳长 revision / Ensure Alembic version table fits long revisions.

    @param connection 同步数据库连接 / Synchronous database connection.
    @param migration_schema 迁移元数据 schema / Migration metadata schema.
    @return None / None.
    @note Alembic 默认 version_num 为 VARCHAR(32)，本项目 revision 名可能更长 / Alembic defaults version_num to VARCHAR(32), while this project can use longer revision names.
    """

    version_table = (
        f"{quote_identifier(migration_schema)}.{quote_identifier(VERSION_TABLE)}"
    )
    connection.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS {version_table} (
                version_num VARCHAR({VERSION_NUM_LENGTH}) NOT NULL,
                CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
            )
            """
        )
    )
    connection.execute(
        text(
            f"""
            ALTER TABLE {version_table}
            ALTER COLUMN version_num TYPE VARCHAR({VERSION_NUM_LENGTH})
            """
        )
    )


def run_migrations_offline() -> None:
    """@brief 离线执行迁移 / Run migrations offline.

    @return None / None.
    """

    configure_context()
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """@brief 在同步连接上执行迁移 / Run migrations on a sync connection.

    @param connection 同步数据库连接 / Sync database connection.
    @return None / None.
    """

    migration_schema = get_migration_schema()
    connection.execute(
        text(f"CREATE SCHEMA IF NOT EXISTS {quote_identifier(migration_schema)}")
    )
    ensure_version_table(connection, migration_schema)
    connection.commit()
    configure_context(connection)
    with context.begin_transaction():
        context.run_migrations()


def make_connectable():
    """@brief 创建异步迁移引擎 / Create async migration engine.

    @return SQLAlchemy 异步引擎 / SQLAlchemy async engine.
    """

    return async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        url=get_url(),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )


async def run_migrations_online() -> None:
    """@brief 在线执行迁移 / Run migrations online.

    @return None / None.
    """

    connectable = make_connectable()
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


configure_import_path()
configure_logging()

if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
