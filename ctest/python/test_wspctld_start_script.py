"""@brief wspctld 本地启动脚本契约测试 / Contract tests for the wspctld local-start script."""

from __future__ import annotations

import subprocess
from pathlib import Path


#: @brief 仓库根目录 / Repository root directory.
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
#: @brief 独立 broker 启动脚本 / Independent broker start script.
START_SCRIPT = REPOSITORY_ROOT / "scripts" / "start-wspctld.sh"
#: @brief 显式 OCI rootfs 构建脚本 / Explicit OCI-rootfs build script.
BUILD_IMAGE_SCRIPT = REPOSITORY_ROOT / "scripts" / "build-wspctl-rootfs.sh"
#: @brief 显式 OCI rootfs 发布脚本 / Explicit OCI-rootfs publication script.
PUBLISH_IMAGE_SCRIPT = REPOSITORY_ROOT / "scripts" / "publish-wspctl-rootfs.sh"
#: @brief 仓库根目录的卸载脚本 / Repository-root uninstaller script.
UNINSTALL_SCRIPT = REPOSITORY_ROOT / "uninstallWspctl.sh"
#: @brief 仓库根目录的只读观测脚本 / Repository-root readonly observability script.
STATUS_SCRIPT = REPOSITORY_ROOT / "statusWspctl.sh"


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
    assert "editable_input_fingerprint" in script
    assert "editable Python client 已就绪；跳过 pip" in script
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
    assert "select_published_image" in script
    assert "WSPCTL_IMAGE_DIGEST" in script
    assert '"$IMAGES_ROOT/sha256/$IMAGE_DIGEST_HEX/rootfs"' in script
    assert "scripts/build-wspctl-rootfs.sh" in script
    assert "scripts/publish-wspctl-rootfs.sh" in script
    for forbidden in (
        "default_generation_name",
        "build_wspctl_image.py",
        "workspace_venv",
        "uv venv",
        "--venv",
        "--stdlib-only",
        "--python-source",
        "readelf",
        "ldconfig",
    ):
        assert forbidden not in script
    assert "systemctl start" in script
    assert "WSPCTL_CLIENT_UID" in script
    assert 'OPERATOR_UID="${WSPCTL_OPERATOR_UID:-0}"' in script
    assert 'SOCKET_PATH="$WORK_ROOT/run/bot/wspctld.sock"' in script
    assert 'OPERATOR_SOCKET_PATH="$WORK_ROOT/run/operator/wspctld.sock"' in script
    assert '"$WORK_ROOT/run" "$WORK_ROOT/run/bot"' in script
    assert '"$WORK_ROOT/run/operator"' in script
    assert '"$CLIENT_UID" != "$OPERATOR_UID"' in script
    assert 'sudo test -S "$OPERATOR_SOCKET_PATH"' in script
    assert "sudo stat --format='%u:%a' \"$OPERATOR_SOCKET_PATH\"" in script
    assert "/usr/local/bin/wspctl" in script
    assert "source_oci_manifest_digest" in script
    assert "write_install_manifest" in script
    assert "install-manifest" in script
    assert "/usr/local/libexec/wspctl/wsp-systemd" not in script


def test_image_build_and_publication_are_explicit_separate_commands() -> None:
    """@brief image build/publish 必须与 daemon start 分离 / Image build/publication must be separate from daemon start.

    @return None / None.
    """

    build_script = BUILD_IMAGE_SCRIPT.read_text(encoding="utf-8")
    publish_script = PUBLISH_IMAGE_SCRIPT.read_text(encoding="utf-8")
    for path in (BUILD_IMAGE_SCRIPT, PUBLISH_IMAGE_SCRIPT):
        checked = subprocess.run(
            ["bash", "-n", str(path)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        assert checked.returncode == 0, checked.stderr
    assert "buildah build" in build_script
    assert "--format oci" in build_script
    assert "buildah push" in build_script
    assert "skopeo" in publish_script
    assert "umoci" in publish_script
    assert "publish_wspctl_image.py" in publish_script
    assert "mount -o remount,bind,ro,nosuid,nodev" in publish_script
    assert "systemctl start" not in build_script
    assert "systemctl start" not in publish_script


def test_root_uninstaller_is_syntax_valid_and_requires_explicit_purge() -> None:
    """@brief 根目录卸载器必须保守地保留 state，除非显式 purge / The root uninstaller must conservatively retain state absent explicit purge.

    @return None / None.
    """

    checked = subprocess.run(
        ["bash", "-n", str(UNINSTALL_SCRIPT)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
    )
    if checked.returncode != 0:
        raise AssertionError(f"invalid bash:\n{checked.stderr}")
    script = UNINSTALL_SCRIPT.read_text(encoding="utf-8")
    assert "--purge" in script
    assert "rm -rf --one-file-system" in script
    assert "is_checkout_unit" in script
    assert "install manifest" in script
    assert "losetup --detach" in script
    assert "保留 ./.wspctl" in script


def test_root_status_script_is_readonly_and_reports_operational_boundaries() -> None:
    """@brief 状态脚本应报告运行边界且不得触发任务或变更 state / The status script must report operational boundaries without task execution or state mutation.

    @return None / None.
    """

    checked = subprocess.run(
        ["bash", "-n", str(STATUS_SCRIPT)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
    )
    if checked.returncode != 0:
        raise AssertionError(f"invalid bash:\n{checked.stderr}")
    script = STATUS_SCRIPT.read_text(encoding="utf-8")
    assert "systemctl show" in script
    assert "xfs_quota -x -c 'state -p'" in script
    assert "WSPCTL_STATUS=healthy" in script
    assert "WSPCTL_STATUS=degraded" in script
    assert "persistent runtime aggregates" in script
    assert "workspace OCI image" in script
    assert "source_oci_manifest_digest" in script
    assert "current-image-digest" in script
    assert "--verify true" in script
    assert 'report_socket "operator control"' in script
    assert "OPERATOR_SOCKET_PATH" in script
    assert "run_bash" not in script
    assert " add_file" not in script
    assert "rm -" not in script


def test_runbot_delegates_broker_readiness_to_the_independent_script() -> None:
    """@brief runBot 启动前必须委托 broker readiness / runBot must delegate broker readiness before Bot launch.

    @return None / None.
    """

    script = (REPOSITORY_ROOT / "runBot.sh").read_text(encoding="utf-8")
    assert 'WSPCTLD_START_SCRIPT="$BOT_DIR/scripts/start-wspctld.sh"' in script
    assert '"$WSPCTLD_START_SCRIPT" start' in script
    assert 'if ! "$WSPCTLD_START_SCRIPT" start' in script
    assert "wspctld 控制面启动失败；Bot 未启动" in script
    assert 'wspctld_log_file="$LOG_DIR/wspctld_${start_timestamp}.log"' in script
    assert script.index('"$WSPCTLD_START_SCRIPT" start') < script.index(
        'nohup "$VENV_DIR/bin/fogmoe-bot"'
    )


if __name__ == "__main__":
    test_start_script_is_bash_syntax_valid_and_declares_critical_contracts()
    test_image_build_and_publication_are_explicit_separate_commands()
    test_root_uninstaller_is_syntax_valid_and_requires_explicit_purge()
    test_root_status_script_is_readonly_and_reports_operational_boundaries()
    test_runbot_delegates_broker_readiness_to_the_independent_script()
