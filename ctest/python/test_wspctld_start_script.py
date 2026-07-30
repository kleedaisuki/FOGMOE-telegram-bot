"""@brief wspctld 本地启动脚本契约测试 / Contract tests for the wspctld local-start script."""

from __future__ import annotations

import os
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
#: @brief 仓库根目录的完整安装入口 / Repository-root complete installation entrypoint.
INSTALL_SCRIPT = REPOSITORY_ROOT / "installWspctl.sh"


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
    assert "WSPCTL_IO_WEIGHT" in script
    assert "resolve_io_weight" in script
    assert 'MEMORY_MAX_BYTES="${WSPCTL_MEMORY_MAX:-4294967296}"' in script
    assert 'MEMORY_SWAP_MAX_BYTES="${WSPCTL_MEMORY_SWAP_MAX:-2147483648}"' in script
    assert 'TMP_SIZE_BYTES="${WSPCTL_TMP_SIZE_BYTES:-1073741824}"' in script
    assert 'CPU_MAX_US="${WSPCTL_CPU_MAX_US:-200000}"' in script
    assert (
        'WORKSPACE_HARD_BYTES="${WSPCTL_RUNTIME_WORKSPACE_HARD_BYTES:-4294967296}"'
        in script
    )
    assert "upsert_root_environment_setting" in script
    assert 'CGROUP_PARENT="/sys/fs/cgroup/system.slice"' in script
    assert "host cgroup v2 不提供 io.weight" in script
    assert "WSPCTL_IO_WEIGHT=$IO_WEIGHT" in script
    assert 'LOOP_SIZE="${WSPCTL_LOOP_SIZE:-32G}"' in script
    assert "fallocate --length" in script
    assert "losetup --find --show --nooverlap" in script
    assert "udevadm settle" in script
    assert "mkfs.xfs" in script
    assert "--probe" in script
    assert "--cache-file /dev/null" in script
    assert "--match-tag TYPE" in script
    assert "无法探测已有 loopback image 的 filesystem；未做格式化" in script
    assert "不是 XFS；拒绝重新格式化" in script
    assert "detach_new_loop_after_failure" in script
    assert 'losetup --detach "$loop_device"' in script
    assert 'mountpoint -q "$STATE_ROOT"' in script
    assert "prjquota" in script
    assert "pqnoenforce" in script
    assert "WSPCTL_XFS_GLOBAL_ADMISSION_BYTES" in script
    assert "WSPCTL_XFS_SYSTEM_RESERVE_BYTES" in script
    assert "select_published_image" in script
    assert "WSPCTL_IMAGE_DIGEST" in script
    assert '"$IMAGES_ROOT/sha256/$IMAGE_DIGEST_HEX/rootfs"' in script
    assert "installWspctl.sh" in script
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
    assert 'systemctl enable "$LXCFS_SERVICE_NAME" "$SERVICE_NAME"' in script
    assert "probe_broker_execution" not in script
    assert "RuntimeProcess" not in script
    assert "InvocationID" in script
    assert "static-ready-v2-image-lxcfs-service-sockets" in script
    assert 'LXCFS_SERVICE_NAME="wspctl-lxcfs.service"' in script
    assert 'LXCFS_ROOT="/run/fogmoe-wspctl-lxcfs/root"' in script
    assert "lxcfs fusermount3" in script
    assert "/usr/local/share/fogmoe-wspctl/systemd/wspctl-lxcfs.service" in script
    assert 'systemctl is-active --quiet "$LXCFS_SERVICE_NAME"' in script
    assert '"fuse.lxcfs"' in script
    assert "WSPCTL_SANDBOX_UID=$AGENT_UID" in script
    assert "WSPCTL_SANDBOX_GID=$AGENT_GID" in script
    assert "BROKER_VALIDATED_INVOCATION_ID" in script
    assert "validate_current_broker_readiness" in script
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
    assert "remove_retired_host_artifacts" in script
    assert 'retired_supervisor="/usr/local/libexec/wspctl/wsp-systemd"' in script


def test_io_weight_auto_detection_supports_wsl_and_explicit_override(
    tmp_path: Path,
) -> None:
    """@brief io.weight capability 探测必须支持 WSL 缺失文件与显式覆盖 / I/O-weight detection must support WSL absence and explicit overrides.

    @param tmp_path pytest 临时目录 / Pytest temporary directory.
    @return None / None.
    """

    cgroup_parent = tmp_path / "system.slice"
    cgroup_parent.mkdir()
    script = f"""
source {START_SCRIPT!s}
CGROUP_PARENT={cgroup_parent!s}
REQUESTED_IO_WEIGHT=auto
resolve_io_weight
[[ "$IO_WEIGHT" == 0 ]]
touch "$CGROUP_PARENT/io.weight"
resolve_io_weight
[[ "$IO_WEIGHT" == 100 ]]
rm "$CGROUP_PARENT/io.weight"
REQUESTED_IO_WEIGHT=777
resolve_io_weight
[[ "$IO_WEIGHT" == 777 ]]
"""
    checked = subprocess.run(
        ["bash", "-c", script],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
    )
    assert checked.returncode == 0, checked.stderr
    assert "禁用相对 I/O 权重" in checked.stdout


def test_broker_health_wait_is_bounded_during_generation_rollover() -> None:
    """@brief 健康检查必须有界等待 service generation 滚代 / Health checks must wait boundedly during service-generation rollover.

    @return None / None.
    """

    script = f"""
set -euo pipefail
source {START_SCRIPT!s}
attempts=0
broker_is_healthy() {{
    ((attempts += 1))
    ((attempts >= 3))
}}
sleep() {{ :; }}
wait_for_broker_healthy
[[ "$attempts" == 3 ]]

attempts=0
broker_is_healthy() {{
    ((attempts += 1))
    return 1
}}
if wait_for_broker_healthy; then
    exit 1
fi
[[ "$attempts" == 100 ]]
"""
    checked = subprocess.run(
        ["bash", "-c", script],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
    )
    assert checked.returncode == 0, checked.stderr


def test_start_service_records_each_new_ready_systemd_invocation_once() -> None:
    """@brief 每个新 systemd invocation 必须记录一次静态 readiness，同一代不得重复 /
    Every new systemd invocation must record static readiness once without repeating it for the same generation.

    @return None / None.
    """

    script = f"""
set -euo pipefail
source {START_SCRIPT!s}
BROKER_RESTART_REQUIRED=false
BROKER_VALIDATED_INVOCATION_ID=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
CURRENT_INVOCATION=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
record_calls=0

sudo() {{ :; }}
broker_is_healthy() {{ return 0; }}
current_broker_invocation_id() {{ printf '%s\\n' "$CURRENT_INVOCATION"; }}
try_current_broker_invocation_id() {{ printf '%s\\n' "$CURRENT_INVOCATION"; }}
record_applied_fingerprint() {{
    [[ "$1" == "$CURRENT_INVOCATION" ]]
    ((record_calls += 1))
}}

start_service
[[ "$record_calls" == 0 ]]

CURRENT_INVOCATION=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
start_service
[[ "$record_calls" == 1 ]]
"""
    checked = subprocess.run(
        ["bash", "-c", script],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
    )
    assert checked.returncode == 0, checked.stderr


def test_static_readiness_follows_generation_change_and_stops_on_health_failure(
    tmp_path: Path,
) -> None:
    """@brief 静态 readiness 仅在 InvocationID 改变时重试，健康失败不得记录 /
    Retry static readiness only when InvocationID changes and never record failed health.

    @param tmp_path pytest 临时目录 / Pytest temporary directory.
    @return None / None.
    """

    invocation_reads = tmp_path / "invocation-reads"
    script = f"""
set -euo pipefail
source {START_SCRIPT!s}
INVOCATION_READS={invocation_reads!s}
printf '0\\n' > "$INVOCATION_READS"
record_calls=0

wait_for_broker_healthy() {{ return 0; }}
try_current_broker_invocation_id() {{
    local reads
    reads="$(<"$INVOCATION_READS")"
    ((reads += 1))
    printf '%s\\n' "$reads" > "$INVOCATION_READS"
    if [[ "$reads" == 1 ]]; then
        printf '%s\\n' aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    else
        printf '%s\\n' bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
    fi
}}
record_applied_fingerprint() {{
    [[ "$1" == bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb ]]
    ((record_calls += 1))
}}

validate_current_broker_readiness
[[ "$(<"$INVOCATION_READS")" == 4 ]]
[[ "$record_calls" == 1 ]]
[[ "$BROKER_VALIDATED_INVOCATION_ID" == bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb ]]

record_calls=0
wait_for_broker_healthy() {{ return 1; }}
if validate_current_broker_readiness; then
    exit 1
fi
[[ "$record_calls" == 0 ]]
"""
    checked = subprocess.run(
        ["bash", "-c", script],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
    )
    assert checked.returncode == 0, checked.stderr


def test_broker_fingerprint_record_rejects_legacy_unvalidated_evidence(
    tmp_path: Path,
) -> None:
    """@brief 旧单行 fingerprint 不得伪装成 readiness-validated generation /
    A legacy one-line fingerprint must not masquerade as a readiness-validated generation.

    @param tmp_path pytest 临时目录 / Pytest temporary directory.
    @return None / None.
    """

    fingerprint_file = tmp_path / "fingerprint"
    fingerprint_file.write_text("desired\n", encoding="utf-8")
    script = f"""
set -euo pipefail
source {START_SCRIPT!s}
FINGERPRINT_FILE={fingerprint_file!s}
BROKER_RESTART_REQUIRED=false
broker_fingerprint() {{ printf 'desired\\n'; }}
prepare_restart_decision
[[ "$BROKER_RESTART_REQUIRED" == false ]]
[[ -z "$BROKER_VALIDATED_INVOCATION_ID" ]]

printf 'fingerprint=desired\\ninvocation_id=cccccccccccccccccccccccccccccccc\\n' > "$FINGERPRINT_FILE"
BROKER_RESTART_REQUIRED=false
prepare_restart_decision
[[ "$BROKER_RESTART_REQUIRED" == false ]]
[[ "$BROKER_VALIDATED_INVOCATION_ID" == cccccccccccccccccccccccccccccccc ]]
"""
    checked = subprocess.run(
        ["bash", "-c", script],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
    )
    assert checked.returncode == 0, checked.stderr


def test_loopback_probe_waits_for_udev_and_mounts_xfs(tmp_path: Path) -> None:
    """@brief loop 重绑后必须等待 udev 并以无缓存 probe 验证 XFS / Reattached loops must wait for udev and use an uncached XFS probe.

    @param tmp_path pytest 临时目录 / Pytest temporary directory.
    @return None / None.
    """

    work_root = tmp_path / "work"
    loop_image = work_root / "state.xfs.img"
    state_root = work_root / "state"
    ready_marker = tmp_path / "udev-ready"
    mount_marker = tmp_path / "mounted"
    work_root.mkdir()
    loop_image.touch()
    script = f"""
set -euo pipefail
source {START_SCRIPT!s}
WORK_ROOT={work_root!s}
LOOP_IMAGE={loop_image!s}
STATE_ROOT={state_root!s}
READY_MARKER={ready_marker!s}
MOUNT_MARKER={mount_marker!s}
sudo() {{ "$@"; }}
mountpoint() {{ return 1; }}
losetup() {{
    if [[ "$1" == "--associated" ]]; then
        return 0
    fi
    if [[ "$1 ${{2:-}} ${{3:-}}" == "--find --show --nooverlap" ]]; then
        printf '/dev/loop-test\\n'
        return 0
    fi
    return 1
}}
udevadm() {{
    [[ "$1" == "settle" ]]
    : > "$READY_MARKER"
}}
blkid() {{
    [[ -f "$READY_MARKER" ]]
    [[ "$*" == *"--probe"* ]]
    [[ "$*" == *"--cache-file /dev/null"* ]]
    printf 'xfs\\n'
}}
install() {{ mkdir -p "${{@: -1}}"; }}
mount() {{ : > "$MOUNT_MARKER"; }}
ensure_loopback_state_mount
[[ -f "$MOUNT_MARKER" ]]
"""
    checked = subprocess.run(
        ["bash", "-c", script],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
    )
    assert checked.returncode == 0, checked.stderr


def test_failed_loopback_probe_detaches_new_association(tmp_path: Path) -> None:
    """@brief 探测失败必须保留镜像并释放本轮 loop association / Probe failure must preserve the image and release the new loop association.

    @param tmp_path pytest 临时目录 / Pytest temporary directory.
    @return None / None.
    """

    work_root = tmp_path / "work"
    loop_image = work_root / "state.xfs.img"
    state_root = work_root / "state"
    detach_marker = tmp_path / "detached"
    work_root.mkdir()
    loop_image.write_bytes(b"persistent-data")
    script = f"""
set -euo pipefail
source {START_SCRIPT!s}
WORK_ROOT={work_root!s}
LOOP_IMAGE={loop_image!s}
STATE_ROOT={state_root!s}
DETACH_MARKER={detach_marker!s}
sudo() {{ "$@"; }}
mountpoint() {{ return 1; }}
losetup() {{
    if [[ "$1" == "--associated" ]]; then
        return 0
    fi
    if [[ "$1 ${{2:-}} ${{3:-}}" == "--find --show --nooverlap" ]]; then
        printf '/dev/loop-test\\n'
        return 0
    fi
    if [[ "$1" == "--detach" && "$2" == "/dev/loop-test" ]]; then
        : > "$DETACH_MARKER"
        return 0
    fi
    return 1
}}
udevadm() {{ [[ "$1" == "settle" ]]; }}
blkid() {{ return 2; }}
ensure_loopback_state_mount
"""
    checked = subprocess.run(
        ["bash", "-c", script],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
    )
    assert checked.returncode != 0
    assert "无法探测已有 loopback image" in checked.stderr
    assert detach_marker.is_file()
    assert loop_image.read_bytes() == b"persistent-data"


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
    assert "--http-proxy=true" in build_script
    assert "--format oci" in build_script
    assert "buildah push" in build_script
    assert 'OUTPUT_ROOT="${1:-$REPOSITORY_ROOT/.runtime/wspctl-rootfs}"' in build_script
    assert 'DESTINATION="$OUTPUT_ROOT/sha256/$DIGEST_HEX"' in build_script
    assert 'CURRENT_BUILD_FILE="$OUTPUT_ROOT/current-image-digest"' in build_script
    assert "--timestamp" in build_script
    assert "--layers" in build_script
    assert "--build-context" in build_script
    assert "build-tools.lock" in build_script
    assert "sha256sum" in build_script
    assert "--continue-at" in build_script
    assert "--source-date-epoch" not in build_script
    assert "--rewrite-timestamp" not in build_script
    assert "skopeo" in publish_script
    assert "umoci" in publish_script
    assert "publish_wspctl_image.py" in publish_script
    assert "WSPCTL_BUILD_PYTHON_BINDINGS=OFF" in publish_script
    assert "cmake --build" in publish_script
    assert "wspctl-image --parallel" in publish_script
    assert (
        'BUILD_OUTPUT_ROOT="$REPOSITORY_ROOT/.runtime/wspctl-rootfs"' in publish_script
    )
    assert (
        'REQUESTED_WORK_ROOT="${WSPCTL_WORK_ROOT:-$REPOSITORY_ROOT/.wspctl}"'
        in publish_script
    )
    assert 'WORK_ROOT="$(realpath --canonicalize-missing' in publish_script
    assert "--images-root" in publish_script
    assert "--current-image-file" in publish_script
    assert "--systemd-escape" in publish_script
    assert "/usr/local/libexec/wspctl/publish_wspctl_image.py" in publish_script
    assert "/usr/bin/flock --exclusive" in publish_script
    assert '--preserve-env="$SUDO_PROXY_ENVIRONMENT"' in publish_script
    assert "HTTP_PROXY,HTTPS_PROXY,ALL_PROXY,NO_PROXY" in publish_script
    assert "sudo -E" not in publish_script
    assert "mount --bind" not in publish_script
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
    assert 'systemctl disable --now "$SERVICE_NAME"' in script
    assert 'LXCFS_SERVICE_NAME="wspctl-lxcfs.service"' in script
    assert "is_checkout_lxcfs_unit" in script
    assert 'systemctl disable --now "$LXCFS_SERVICE_NAME"' in script
    assert 'fusermount3 --unmount "$LXCFS_ROOT"' in script
    assert "findmnt --raw --noheadings --output TARGET" in script
    assert "findmnt --list --raw" not in script
    assert "systemd-escape --path --suffix=mount" in script
    assert "systemctl disable --now" in script
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
    assert "InvocationID" in script
    assert "static readiness validation" in script
    assert "passed image/service/socket readiness validation" in script
    assert "NRestarts" in script
    assert "RestartPreventExitStatus" in script
    assert "NotifyAccess" in script
    assert "TimeoutStartUSec" in script
    assert "xfs_quota -x -c 'state -p'" in script
    assert "WSPCTL_STATUS=healthy" in script
    assert "WSPCTL_STATUS=degraded" in script
    assert "persistent runtime aggregates" in script
    assert "cgroup-aware procfs" in script
    assert 'LXCFS_SERVICE_NAME="wspctl-lxcfs.service"' in script
    assert "fuse.lxcfs" in script
    assert "proc/pressure/memory" in script
    assert "workspace OCI image" in script
    assert "source_oci_manifest_digest" in script
    assert "current-image-digest" in script
    assert "--verify true" in script
    assert "report_install_log" in script
    assert "wspctl_install_*.log" in script
    assert "tail -n 100" in script
    assert '"${BASH_SOURCE[0]}" == "$0"' in script
    assert 'report_socket "operator control"' in script
    assert "OPERATOR_SOCKET_PATH" in script
    assert "run_bash" not in script
    assert " add_file" not in script
    assert "rm -" not in script


def test_status_reports_the_newest_install_log(tmp_path: Path) -> None:
    """@brief 只读状态入口必须发现最新安装日志 / The readonly status entrypoint must discover the newest installation log.

    @param tmp_path pytest 临时目录 / Pytest temporary directory.
    @return None / None.
    """

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    older_log = log_dir / "wspctl_install_older.log"
    newer_log = log_dir / "wspctl_install_newer.log"
    older_log.write_text("older", encoding="utf-8")
    newer_log.write_text("newer", encoding="utf-8")
    older_timestamp = older_log.stat().st_mtime - 10
    newer_timestamp = newer_log.stat().st_mtime + 10
    os.utime(older_log, (older_timestamp, older_timestamp))
    os.utime(newer_log, (newer_timestamp, newer_timestamp))
    script = f"""
source {STATUS_SCRIPT!s}
LOG_DIR={log_dir!s}
report_install_log
"""
    checked = subprocess.run(
        ["bash", "-c", script],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
    )
    assert checked.returncode == 0, checked.stderr
    assert f"latest={newer_log}" in checked.stdout
    assert str(older_log) not in checked.stdout
    assert "tail -n 100" in checked.stdout


def test_install_entrypoint_owns_complete_control_plane_deployment() -> None:
    """@brief installWspctl 必须独占完整 control-plane 部署 / installWspctl must own the complete control-plane deployment.

    @return None / None.
    """

    checked = subprocess.run(
        ["bash", "-n", str(INSTALL_SCRIPT)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
    )
    assert checked.returncode == 0, checked.stderr
    script = INSTALL_SCRIPT.read_text(encoding="utf-8")
    build_call = '"$BUILD_IMAGE_SCRIPT"'
    publish_call = '"$PUBLISH_IMAGE_SCRIPT"'
    broker_call = '"$INSTALL_BROKER_SCRIPT" start'
    assert "sudo -v" in script
    assert "sudo buildah skopeo umoci cmake" in script
    assert "缺少 host 安装工具" in script
    assert 'scripts/build-wspctl-rootfs.sh"' in script
    assert 'scripts/publish-wspctl-rootfs.sh"' in script
    assert 'scripts/start-wspctld.sh"' in script
    assert build_call in script
    assert publish_call in script
    assert broker_call in script
    assert script.index(build_call) < script.index(publish_call) < script.index(broker_call)
    assert "initialize_install_log" in script
    assert "run_logged_install" in script
    assert "wspctl_install_" in script
    assert '2>&1 | tee -a "$LOG_FILE"' in script
    assert 'pipeline_status=("${PIPESTATUS[@]}")' in script
    assert '完整日志: %s\\n' in script
    assert '"${BASH_SOURCE[0]}" == "$0"' in script
    assert "runBot.sh" in script


def test_install_log_captures_failure_and_preserves_exit_status(
    tmp_path: Path,
) -> None:
    """@brief 安装日志必须捕获完整失败输出并保留原始退出码 / Installation logging must capture failure output and preserve its status.

    @param tmp_path pytest 临时目录 / Pytest temporary directory.
    @return None / None.
    """

    log_dir = tmp_path / "logs"
    script = f"""
source {INSTALL_SCRIPT!s}
LOG_DIR={log_dir!s}
require_install_prerequisites() {{ printf 'synthetic-prerequisite-ok\\n'; }}
install_wspctl() {{
    printf 'synthetic-build-output\\n'
    printf 'synthetic-publish-error\\n' >&2
    return 23
}}
initialize_install_log
run_logged_install
"""
    checked = subprocess.run(
        ["bash", "-c", script],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
    )
    assert checked.returncode == 23
    logs = list(log_dir.glob("wspctl_install_*.log"))
    assert len(logs) == 1
    log_content = logs[0].read_text(encoding="utf-8")
    assert "synthetic-prerequisite-ok" in log_content
    assert "synthetic-build-output" in log_content
    assert "synthetic-publish-error" in log_content
    assert "安装失败（exit=23）" in log_content
    assert logs[0].stat().st_mode & 0o777 == 0o600
    assert str(logs[0]) in checked.stdout


def test_runbot_only_checks_installed_broker_readiness() -> None:
    """@brief runBot 只能只读检查已安装 broker / runBot may only check an installed broker read-only.

    @return None / None.
    """

    script = (REPOSITORY_ROOT / "runBot.sh").read_text(encoding="utf-8")
    assert 'WSPCTLD_SERVICE_NAME="wspctld.service"' in script
    assert "read_workspace_broker_socket" in script
    assert "require_wspctld_ready" in script
    assert "print_latest_wspctl_install_log" in script
    assert "wspctl_install_*.log" in script
    assert "最近一次 wspctl 安装日志" in script
    assert 'systemctl is-active --quiet "$WSPCTLD_SERVICE_NAME"' in script
    assert '[ ! -S "$socket_path" ]' in script
    assert "runBot.sh 不会安装或启动它" in script
    assert script.index('require_wspctld_ready "$wspctld_socket_path"') < script.index(
        'nohup "$VENV_DIR/bin/fogmoe-bot"'
    )
    for forbidden in (
        "WSPCTLD_START_SCRIPT",
        "start-wspctld.sh",
        "build-wspctl-rootfs.sh",
        "publish-wspctl-rootfs.sh",
        "wspctld_log_file",
    ):
        assert forbidden not in script
    assert all(
        not line.lstrip().startswith("sudo ") for line in script.splitlines()
    )
    assert "init|setup|install)" not in script
    assert "安装与 Bot 运行已分离" in script
    assert '1. $WSPCTL_INSTALL_SCRIPT' in script


if __name__ == "__main__":
    test_start_script_is_bash_syntax_valid_and_declares_critical_contracts()
    test_image_build_and_publication_are_explicit_separate_commands()
    test_root_uninstaller_is_syntax_valid_and_requires_explicit_purge()
    test_root_status_script_is_readonly_and_reports_operational_boundaries()
    test_install_entrypoint_owns_complete_control_plane_deployment()
    test_runbot_only_checks_installed_broker_readiness()
