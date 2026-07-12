"""dbctl 配置读取 / dbctl configuration loading."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from fogmoe_dbctl.postgres import sqlalchemy_url


# 数据库控制面的稳定项目路径 / Stable project path for the database control plane.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_DIR = PROJECT_ROOT / "var" / "psql"

# 由迁移拥有、由运行时访问的 schema / Schemas owned by migrations and accessed at runtime.
APPLICATION_SCHEMAS = (
    "identity",
    "conversation",
    "assistant",
    "economy",
    "moderation",
    "crypto",
    "game",
    "media",
    "admin",
)


def project_root() -> Path:
    """@brief 获取项目根目录 / Get project root directory.

    @return 项目根目录路径 / Project root path.
    """

    return PROJECT_ROOT


def load_project_env() -> None:
    """@brief 加载项目 .env / Load project .env.

    @return None / None.
    """

    load_dotenv(project_root() / ".env")


def sqlalchemy_database_uri() -> str:
    """@brief 构造 SQLAlchemy 数据库 URL / Build SQLAlchemy database URL.

    @return SQLAlchemy 数据库 URL / SQLAlchemy database URL.
    """

    load_project_env()
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        return database_url

    return sqlalchemy_url(
        user=os.environ.get("POSTGRES_USER") or "postgres",
        password=os.environ.get("POSTGRES_PASSWORD") or "",
        host=os.environ.get("POSTGRES_HOST") or "localhost",
        port=int(os.environ.get("POSTGRES_PORT") or "5432"),
        database=os.environ.get("POSTGRES_DATABASE") or "fogmoe",
    )


def migration_schema() -> str:
    """@brief 获取迁移元数据 schema / Get migration metadata schema.

    @return Alembic 版本表 schema / Alembic version table schema.
    """

    load_project_env()
    return os.environ.get("DB_MIGRATION_SCHEMA") or "infra"


def admin_user_id() -> int:
    """@brief 获取初始管理员用户 ID / Get initial admin user ID.

    @return 管理员 Telegram 用户 ID / Administrator Telegram user ID.
    """

    load_project_env()
    return int(os.environ.get("ADMIN_USER_ID") or "1002288404")
