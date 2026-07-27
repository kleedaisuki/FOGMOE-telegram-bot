"""@brief wspctld 本地启动脚本契约测试 / Contract tests for the wspctld local-start script."""

from __future__ import annotations

import subprocess
from pathlib import Path


#: @brief 仓库根目录 / Repository root directory.
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
#: @brief 独立 broker 启动脚本 / Independent broker start script.
START_SCRIPT = REPOSITORY_ROOT / "scripts" / "start-wspctld.sh"


def test_start_script_is_bash_syntax_valid_and_declares_critical_contracts() -> None:
    """@brief 启动脚本必须可解析且保留关键的安全/构建契约 / The start script must parse and retain critical safety/build contracts.

    @return None / None.
    """

    checked = subprocess.run(
        ["bash", "-n", str(START_SCRIPT)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
    )
    if checked.returncode != 0:
        raise AssertionError(f"invalid bash:\n{checked.stderr}")
    script = START_SCRIPT.read_text(encoding="utf-8")
    assert "pip install --editable" in script
    assert "import wspctl._native" in script
    assert "-DWSPCTL_INSTALL_HOST_TOOLS=ON" in script
    assert "-DWSPCTL_ALLOW_INSECURE_DEVELOPMENT_ROOT=ON" in script
    assert "WSPCTL_LOOP_SIZE" in script
    assert 'LOOP_SIZE="${WSPCTL_LOOP_SIZE:-32G}"' in script
    assert "fallocate --length" in script
    assert "losetup --find --show" in script
    assert "mkfs.xfs" in script
    assert "已有 loopback image 不是 XFS；拒绝重新格式化" in script
    assert 'mountpoint -q "$STATE_ROOT"' in script
    assert "prjquota" in script
    assert "pqnoenforce" in script
    assert "WSPCTL_XFS_GLOBAL_ADMISSION_BYTES" in script
    assert "WSPCTL_XFS_SYSTEM_RESERVE_BYTES" in script
    assert "--allow-insecure-development-output" in script
    assert "mount -o remount,bind,ro" in script
    assert "systemctl start" in script
    assert "WSPCTL_CLIENT_UID" in script
    assert "require_generation" in script


def test_runbot_delegates_broker_readiness_to_the_independent_script() -> None:
    """@brief runBot 启动前必须委托 broker readiness / runBot must delegate broker readiness before Bot launch.

    @return None / None.
    """

    script = (REPOSITORY_ROOT / "runBot.sh").read_text(encoding="utf-8")
    assert 'WSPCTLD_START_SCRIPT="$BOT_DIR/scripts/start-wspctld.sh"' in script
    assert '"$WSPCTLD_START_SCRIPT" start' in script
    assert script.index('"$WSPCTLD_START_SCRIPT" start') < script.index(
        'nohup "$VENV_DIR/bin/fogmoe-bot"'
    )


if __name__ == "__main__":
    test_start_script_is_bash_syntax_valid_and_declares_critical_contracts()
    test_runbot_delegates_broker_readiness_to_the_independent_script()
