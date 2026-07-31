"""@brief 根配置默认路径测试 / Tests for the root configuration default path."""

from collections.abc import Callable
from pathlib import Path

import pytest

from fogmoe_bot.config import default_config_path as bot_default_config_path
from fogmoe_dashboard.config import default_config_path as dashboard_default_config_path

#: @brief 源码仓库根目录 / Source repository root directory.
PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "default_path",
    (bot_default_config_path, dashboard_default_config_path),
)
def test_default_config_path_is_independent_of_current_working_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    default_path: Callable[[], Path],
) -> None:
    """@brief 默认路径始终指向项目根 / Default path always targets the project root.

    @param monkeypatch pytest 环境替换器 / pytest environment patcher.
    @param tmp_path 与项目无关的当前目录 / Current directory outside the project.
    @param default_path 各程序自己的默认路径函数 / Each program's local default-path function.
    @return None / None.
    """

    monkeypatch.chdir(tmp_path)

    assert default_path() == PROJECT_ROOT / "config.json"


def test_dbctl_default_config_path_is_relative_to_startup_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """@brief dbctl 默认配置属于启动目录 / dbctl default config belongs to startup directory.

    @param monkeypatch pytest 环境替换器 / pytest environment patcher.
    @param tmp_path 模拟 wheel 部署的工作目录 / Working directory simulating a wheel deployment.
    @return None / None.

    @note 此约束禁止从包文件 ``__file__`` 反推部署路径，避免普通 wheel 安装时
        意外读取 site-packages 上层目录。/
        This constraint forbids inferring deployment paths from package ``__file__``,
        preventing ordinary wheel installations from reading above site-packages.
    """

    from fogmoe_dbctl.config import default_config_path

    monkeypatch.chdir(tmp_path)

    assert default_config_path() == tmp_path / "config.json"
