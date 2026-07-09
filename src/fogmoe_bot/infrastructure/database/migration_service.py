"""数据库迁移启动服务 / Database migration startup service."""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config

from fogmoe_bot.infrastructure import config

logger = logging.getLogger(__name__)


def _project_root() -> Path:
    """@brief 获取项目根目录 / Get project root directory.

    @return 项目根目录路径 / Project root path.
    """

    return Path(__file__).resolve().parents[4]


def _alembic_config() -> Config:
    """@brief 构造 Alembic 配置 / Build Alembic configuration.

    @return Alembic 配置对象 / Alembic configuration object.
    """

    alembic_cfg = Config(str(_project_root() / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", config.SQLALCHEMY_DATABASE_URI)
    return alembic_cfg


def run_startup_migrations() -> None:
    """@brief 启动前自动执行数据库迁移 / Automatically run database migrations before startup.

    @return None / None.
    @note 可通过 DB_AUTO_MIGRATE=false 禁用 / Can be disabled with DB_AUTO_MIGRATE=false.
    """

    if not config.DB_AUTO_MIGRATE:
        logger.info("Database auto migration is disabled.")
        return

    logger.info("Running database migrations before bot startup.")
    command.upgrade(_alembic_config(), "head")
    logger.info("Database migrations are up to date.")
