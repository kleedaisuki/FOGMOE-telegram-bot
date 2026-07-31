"""@brief Alembic 与 psql 迁移执行边界 / Alembic and psql migration execution boundary."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import as_file, files

from alembic import command
from alembic.config import Config

from fogmoe_dbctl.config import DbctlSettings, reveal_secret
from fogmoe_dbctl.postgres import direct_psql_environment, sqlalchemy_url


@contextmanager
def configured_alembic(settings: DbctlSettings) -> Iterator[Config]:
    """@brief 创建包资源驱动的 Alembic 配置 / Create a package-resource-backed Alembic configuration.

    @param settings dbctl 配置投影 / dbctl configuration projection.
    @return 在上下文内有效的 Alembic 配置 / Alembic configuration valid within the context.
    @note 迁移脚本是 ``fogmoe_dbctl.migrations`` 的已安装包资源，不依赖仓库根目录或
        ``alembic.ini``。/ Migration scripts are installed package resources of
        ``fogmoe_dbctl.migrations`` and do not depend on a repository root or
        ``alembic.ini``.
    """

    migration_resources = files("fogmoe_dbctl.migrations")
    with as_file(migration_resources) as migration_directory:
        alembic_config = Config()
        alembic_config.set_main_option("script_location", str(migration_directory))
        alembic_config.attributes["database_url"] = maintenance_database_url(settings)
        alembic_config.attributes["migration_schema"] = (
            settings.maintenance.migration_schema
        )
        alembic_config.attributes["admin_user_id"] = settings.administrator.user_id
        alembic_config.attributes["application_role"] = settings.application.username
        yield alembic_config


def maintenance_database_url(settings: DbctlSettings) -> str:
    """@brief 构造维护角色的 SQLAlchemy URL / Build the maintenance-role SQLAlchemy URL.

    @param settings dbctl 配置投影 / dbctl configuration projection.
    @return asyncpg SQLAlchemy URL / asyncpg SQLAlchemy URL.
    """

    return sqlalchemy_url(
        host=settings.endpoint.host,
        port=settings.endpoint.port,
        database=settings.endpoint.name,
        user=settings.maintenance.username,
        password=reveal_secret(
            settings.maintenance.password,
            field_name="database.maintenance.password",
        ),
    )


def run_alembic(
    *,
    settings: DbctlSettings,
    revision: str,
    dry_run: bool,
) -> None:
    """@brief 通过程序化 API 执行 Alembic / Run Alembic through its programmatic API.

    @param settings dbctl 配置投影 / dbctl configuration projection.
    @param revision Alembic 目标 revision / Alembic target revision.
    @param dry_run 是否只打印 / Whether to print only.
    @return None / None.
    @note 所有迁移输入显式写入 Alembic attributes，迁移环境不读取环境变量。/
        All migration inputs are injected into Alembic attributes; the migration environment reads no environment variables.
    """

    if dry_run:
        print(f"alembic upgrade {revision}")
        return

    with configured_alembic(settings) as alembic_config:
        command.upgrade(alembic_config, revision)


def run_psql_grants(
    *,
    settings: DbctlSettings,
    sql: str,
    dry_run: bool,
) -> None:
    """@brief 用 psql 执行单事务授权收敛 / Converge grants in one psql transaction.

    @param settings dbctl 配置投影 / dbctl configuration projection.
    @param sql SQL 文本 / SQL text.
    @param dry_run 是否只打印 / Whether to print only.
    @return None / None.
    @note 密码只进入显式子进程环境，不进入 argv 或日志。/
        The password enters only the explicit child environment, never argv or logs.
    """

    command_line = [
        "psql",
        "--no-psqlrc",
        "--single-transaction",
        "--set",
        "ON_ERROR_STOP=1",
    ]
    if dry_run:
        print("psql --single-transaction --set ON_ERROR_STOP=1")
        print(sql)
        return
    environment = direct_psql_environment(
        host=settings.endpoint.host,
        port=settings.endpoint.port,
        database=settings.endpoint.name,
        user=settings.maintenance.username,
        password=reveal_secret(
            settings.maintenance.password,
            field_name="database.maintenance.password",
        ),
    )
    subprocess.run(command_line, input=sql, text=True, env=environment, check=True)
