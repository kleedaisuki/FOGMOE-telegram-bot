"""@brief dbctl 普通 wheel 部署回归测试 / dbctl regular-wheel deployment regression tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


_WHEEL_RESOURCE_PROBE = """
from __future__ import annotations

import sys
from importlib.resources import files
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from fogmoe_dbctl import __file__ as package_file
from fogmoe_dbctl.commands.migration_execution import configured_alembic
from fogmoe_dbctl.config import DbctlSettings
from fogmoe_dbctl.migrations import runner

environment_root = Path(sys.prefix).resolve()
installed_package = Path(package_file).resolve()
assert installed_package.is_relative_to(environment_root), installed_package

migration_resources = files("fogmoe_dbctl.migrations")
assert migration_resources.joinpath("env.py").is_file()
assert migration_resources.joinpath("versions").is_dir()
assert migration_resources.joinpath("sql").is_dir()

resource_config = Config()
resource_config.set_main_option("script_location", "fogmoe_dbctl:migrations")
resource_scripts = ScriptDirectory.from_config(resource_config)
resource_head = resource_scripts.get_current_head()
assert resource_head is not None
resource_revision = resource_scripts.get_revision(resource_head)
assert resource_revision is not None
assert Path(resource_revision.path).is_file()

settings = DbctlSettings.model_validate(
    {
        "maintenance": {"password": "maintenance-secret"},
    }
)
with configured_alembic(settings) as configured:
    configured_scripts = ScriptDirectory.from_config(configured)
    configured_head = configured_scripts.get_current_head()
    assert configured_head == resource_head
    configured_revision = configured_scripts.get_revision(configured_head)
    assert configured_revision is not None
    runner._current_dialect_name = lambda: "postgresql"
    sql_path = runner._sql_file_for_revision(configured_revision.path)
    assert sql_path.is_file()
    assert sql_path.is_relative_to(environment_root), sql_path
"""
#: @brief 隔离 wheel 资源探针 / Isolated regular-wheel resource probe.
#: 该脚本只在 ``-I`` 子解释器中运行，因而不能由 checkout 的 ``src`` 遮蔽 site-packages。/
#: This script runs only in a ``-I`` child interpreter, so the checkout's ``src`` cannot shadow
#: site-packages.


def _deployment_config() -> dict[str, object]:
    """@brief 构造无密钥的最小 dbctl 部署配置 / Build a minimal secret-free dbctl deployment config.

    @return 满足 dbctl 根投影的 JSON 文档 / JSON document satisfying the dbctl root projection.
    """

    return {
        "schema_version": 2,
        "identity": {"administrator": {}},
        "database": {
            "endpoint": {},
            "application": {},
            "maintenance": {},
            "reporting": {},
            "bootstrap": {},
        },
    }


def _isolated_environment() -> dict[str, str]:
    """@brief 构造不会注入 checkout 导入路径的子进程环境 / Build a child environment without checkout import injection.

    @return 适于普通 wheel 子进程的环境 / Environment suitable for a regular-wheel child process.
    """

    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def test_dbctl_installed_wheel_resolves_deployment_config_from_startup_directory(
    tmp_path: Path,
) -> None:
    """@brief 已安装 CLI 从部署启动目录读取 config.json / Installed CLI reads config.json from deployment startup directory.

    @param tmp_path 不含仓库文件的独立部署目录 / Isolated deployment directory without repository files.
    @return None / None.
    @note ``--dry-run`` 必须在未连接 PostgreSQL 时覆盖配置与命令组合根。/
        ``--dry-run`` must cover the configuration and command composition root without a PostgreSQL connection.
    """

    deployment_config = tmp_path / "config.json"
    deployment_config.write_text(json.dumps(_deployment_config()), encoding="utf-8")
    executable = Path(sys.executable).with_name("fogmoe-dbctl")
    assert executable.is_file(), executable

    completed = subprocess.run(
        [str(executable), "migrate", "--dry-run"],
        cwd=tmp_path,
        env=_isolated_environment(),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=15,
    )

    assert completed.returncode == 0, completed.stderr
    assert "alembic upgrade head" in completed.stdout
    assert "psql --single-transaction --set ON_ERROR_STOP=1" in completed.stdout


def test_dbctl_installed_wheel_exposes_alembic_and_sql_migration_resources(
    tmp_path: Path,
) -> None:
    """@brief 已安装 wheel 可解析 Alembic、版本与 SQL 迁移资源 / Installed wheel resolves Alembic, version, and SQL migration resources.

    @param tmp_path 不含 checkout 文件的子进程工作目录 / Child-process working directory without checkout files.
    @return None / None.
    @note 该测试显式覆盖普通 wheel 的资源定位；``schema.sql`` 是开发用 DDL snapshot，
        不属于 dbctl 的运行时迁移契约。/
        This test explicitly covers regular-wheel resource discovery; ``schema.sql`` is a development DDL snapshot,
        not part of dbctl's runtime migration contract.
    """

    completed = subprocess.run(
        [sys.executable, "-I", "-c", _WHEEL_RESOURCE_PROBE],
        cwd=tmp_path,
        env=_isolated_environment(),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=15,
    )

    assert completed.returncode == 0, completed.stderr
