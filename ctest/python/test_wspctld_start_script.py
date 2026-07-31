"""@brief wspctld 本地启动脚本契约测试 / Contract tests for the wspctld local-start script."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
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
INSTALL_SCRIPT = REPOSITORY_ROOT / "install.sh"


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
    assert "wspctl_build_identity.py" in script
    assert "ensure-wspctl-client.sh" in script
    assert '"$CLIENT_RECONCILER" --venv "$VENV_DIR"' in script
    assert "deployed_client_is_current" not in script
    assert "write_client_deployment_receipt" not in script
    assert "pip wheel" not in script
    assert "HOST_IDENTITY_PYTHON" in script
    assert "project_version" in script
    assert "PUBLISHER_DEPLOYMENT_RECEIPT_FILE" in script
    assert "RUNTIME_DEPLOYMENT_RECEIPT_FILE" in script
    assert "host_artifacts_are_current" in script
    assert "write_host_deployment_receipt" in script
    assert "host_path_is_trusted" in script
    assert "host_artifact_parent_directories_are_trusted" in script
    assert "stat --format='%u:%g:%f'" in script
    assert "(mode_value & 8#022) == 0" in script
    assert "/usr/local/libexec/wspctl" in script
    assert "/usr/local/share/fogmoe-wspctl/systemd" in script
    assert "-DWSPCTL_BUILD_HOST_PUBLISHER=ON" in script
    assert "-DWSPCTL_BUILD_HOST_RUNTIME=ON" in script
    assert "-DBUILD_TESTING=OFF" in script
    assert "-DWSPCTL_BUILD_TESTING=OFF" in script
    assert "-DWSPCTL_BUILD_PYTHON_BINDINGS=OFF" in script
    assert "--target wspctl-image" in script
    assert "--target wspctld wspctl" in script
    assert "--component WspctlHost" in script
    assert "--component WspctlPublisher" in script
    assert "host_development_root_policy" not in script
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
    assert "installWspctl.sh" not in script
    assert "install.sh" in script
    for forbidden in (
        "default_generation_name",
        "build_wspctl_image.py",
        "workspace_venv",
        "uv venv",
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
    assert 'HOST_SYSTEMD_ASSET_DIRECTORY="/usr/local/share/fogmoe-wspctl/systemd"' in script
    assert 'HOST_LXCFS_UNIT_SOURCE="$HOST_SYSTEMD_ASSET_DIRECTORY/wspctl-lxcfs.service"' in script
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


def test_prepare_host_tools_is_narrow_and_does_not_touch_client_or_runtime_state(
    tmp_path: Path,
) -> None:
    """@brief publisher 窄准备不得触碰 client 或 runtime state / Narrow publisher preparation must not touch the client or runtime state.

    @param tmp_path pytest 临时目录 / Pytest temporary directory.
    @return None / None.
    """

    lock_file = tmp_path / "prepare.lock"
    script = f"""
set -euo pipefail
source {START_SCRIPT!s}
LOCK_FILE={lock_file!s}
require_work_root() {{ printf 'work-root\\n'; }}
require_receipt_commands() {{ printf 'receipt-commands\\n'; }}
acquire_control_lock() {{ printf 'control-lock\\n'; }}
acquire_host_install_lock() {{ printf 'host-install-lock\\n'; }}
ensure_publisher_host_artifacts() {{ printf 'publisher\\n'; }}
ensure_runtime_host_artifacts() {{ exit 97; }}
prepare_control_plane_directories() {{ exit 96; }}
CLIENT_RECONCILER=unexpected_client
unexpected_client() {{ exit 95; }}
ensure_loopback_state_mount() {{ exit 96; }}
require_state_mount() {{ exit 95; }}
select_published_image() {{ exit 94; }}
install_service_configuration() {{ exit 92; }}
start_service() {{ exit 93; }}
prepare_host_tools
"""
    completed = subprocess.run(
        ["bash", "-c", script],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == [
        "work-root",
        "receipt-commands",
        "control-lock",
        "host-install-lock",
        "publisher",
    ]


def test_work_root_rejects_custom_or_symlinked_privileged_paths(tmp_path: Path) -> None:
    """@brief 特权工作根只能是受控 checkout 路径 / Privileged work root must be the managed checkout path.

    @param tmp_path pytest 临时目录 / Pytest temporary directory.
    @return None / None.
    """

    foreign_root = tmp_path / "foreign-root"
    script = f"""
set -euo pipefail
source {START_SCRIPT!s}
REQUESTED_WORK_ROOT={foreign_root!s}
WORK_ROOT={foreign_root!s}
require_work_root
"""
    completed = subprocess.run(
        ["bash", "-c", script],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
    )

    assert completed.returncode != 0
    assert "不支持自定义 WSPCTL_WORK_ROOT" in completed.stderr


def test_verified_publisher_receipt_skips_cmake_and_cpp_tools(tmp_path: Path) -> None:
    """@brief publisher receipt 命中时不能探测 CMake/C++ / A publisher receipt hit must not probe CMake/C++.

    @param tmp_path pytest 临时目录 / Pytest temporary directory.
    @return None / None.
    """

    verifier = tmp_path / "wspctl-image"
    publisher = tmp_path / "publish_wspctl_image.py"
    receipt = tmp_path / "publisher-receipt"
    verifier.write_text("verified image tool\n", encoding="utf-8")
    publisher.write_text("verified publisher\n", encoding="utf-8")
    verifier.chmod(0o755)
    publisher.chmod(0o755)
    script = f"""
set -euo pipefail
source {START_SCRIPT!s}
sudo() {{ "$@"; }}
cmake() {{ exit 97; }}
c++() {{ exit 98; }}
host_path_is_trusted() {{ return 0; }}
host_artifact_parent_directories_are_trusted() {{ return 0; }}
HOST_IMAGE_VERIFIER={verifier!s}
HOST_PUBLISHER={publisher!s}
PUBLISHER_DEPLOYMENT_RECEIPT_FILE={receipt!s}
WORK_ROOT={tmp_path!s}
expected_version="$(project_version)"
expected_identity="$(host_build_identity)"
printf 'schema=2\\nrole=publisher\\nsource_identity=%s\\nproject_version=%s\\nplatform=%s\\ntoolchain_identity=%064d\\n' \\
    "$expected_identity" "$expected_version" "$(uname -m)" 0 > "$PUBLISHER_DEPLOYMENT_RECEIPT_FILE"
printf 'artifact %s %s\\n' "$HOST_IMAGE_VERIFIER" "$(sha256sum "$HOST_IMAGE_VERIFIER" | awk '{{print $1}}')" >> "$PUBLISHER_DEPLOYMENT_RECEIPT_FILE"
printf 'artifact %s %s\\n' "$HOST_PUBLISHER" "$(sha256sum "$HOST_PUBLISHER" | awk '{{print $1}}')" >> "$PUBLISHER_DEPLOYMENT_RECEIPT_FILE"
ensure_publisher_host_artifacts
"""
    completed = subprocess.run(
        ["bash", "-c", script],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 0, completed.stderr
    assert "跳过 CMake 构建" in completed.stdout


def test_host_receipt_rejects_untrusted_artifact_or_parent_directory(
    tmp_path: Path,
) -> None:
    """@brief host receipt 不得信任可写或非 root-owned 执行路径 / A host receipt must not trust writable or non-root-owned execution paths.

    @param tmp_path pytest 临时目录 / Pytest temporary directory.
    @return None / None.
    """

    artifact = tmp_path / "wspctld"
    receipt = tmp_path / "runtime-receipt"
    trusted_parent = tmp_path / "trusted-parent"
    artifact.write_text("trusted host binary\n", encoding="utf-8")
    artifact.chmod(0o755)
    trusted_parent.mkdir()
    checksum = hashlib.sha256(artifact.read_bytes()).hexdigest()
    script = f"""
set -euo pipefail
source {START_SCRIPT!s}
sudo() {{ "$@"; }}
WORK_ROOT={tmp_path!s}
HOST_WSPCTLD={artifact!s}
RECEIPT={receipt!s}
TRUSTED_PARENT={trusted_parent!s}
host_artifact_parent_directories() {{ printf '%s\\n' "$TRUSTED_PARENT"; }}
stat() {{
    local requested_path="${{@: -1}}"
    [[ "$1" == "--format=%u:%g:%f" ]] || command stat "$@"
    case "$requested_path" in
        "$WORK_ROOT") printf '0:0:41ed\\n' ;;
        "$RECEIPT") printf '0:0:81a4\\n' ;;
        "$TRUSTED_PARENT") printf '%s\\n' "$PARENT_METADATA" ;;
        "$HOST_WSPCTLD") printf '%s\\n' "$ARTIFACT_METADATA" ;;
        *) return 1 ;;
    esac
}}
provider() {{ printf '%s\\n' "$HOST_WSPCTLD"; }}
printf 'schema=2\\nrole=runtime\\nsource_identity=source\\nproject_version=version\\nplatform=%s\\ntoolchain_identity=%064d\\nartifact %s %s\\n' \\
    "$(uname -m)" 0 "$HOST_WSPCTLD" {checksum!s} > "$RECEIPT"

ARTIFACT_METADATA=0:0:81ed
PARENT_METADATA=0:0:41ed
host_artifacts_are_current "$RECEIPT" runtime source version provider 1

ARTIFACT_METADATA=1000:0:81ed
if host_artifacts_are_current "$RECEIPT" runtime source version provider 1; then
    exit 91
fi

ARTIFACT_METADATA=0:0:81fd
if host_artifacts_are_current "$RECEIPT" runtime source version provider 1; then
    exit 92
fi

ARTIFACT_METADATA=0:0:81ed
PARENT_METADATA=0:0:41fd
if host_artifacts_are_current "$RECEIPT" runtime source version provider 1; then
    exit 93
fi
"""
    completed = subprocess.run(
        ["bash", "-c", script],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr


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
    assert "wspctl_build_identity.py" in build_script
    assert "BUILD_RECEIPT_FILE" in build_script
    assert "rootfs_artifact_is_current" in build_script
    assert "已验证源码身份相同的 OCI artifact；跳过 Buildah 构建" in build_script
    assert 'OUTPUT_ROOT="${1:-$REPOSITORY_ROOT/.runtime/wspctl-rootfs}"' in build_script
    assert 'DESTINATION="$OUTPUT_ROOT/sha256/$DIGEST_HEX"' in build_script
    assert "record_current_build_digest" in build_script
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
    assert "PREPARE_HOST_TOOLS_SCRIPT" in publish_script
    assert '"$PREPARE_HOST_TOOLS_SCRIPT" prepare-host-tools' in publish_script
    assert "cmake --build" not in publish_script
    assert "WSPCTL_BUILD_PYTHON_BINDINGS=OFF" not in publish_script
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


def write_verified_synthetic_oci_receipt(
    output_root: Path,
    source_identity: str,
    reference: str,
) -> str:
    """@brief 写入完整 SHA-256 descriptor 图的合成 OCI receipt / Write a synthetic OCI receipt with a complete SHA-256 descriptor graph.

    @param output_root content-addressed artifact 根 / Content-addressed artifact root.
    @param source_identity receipt 中的源码身份 / Source identity recorded in the receipt.
    @param reference index descriptor 的 OCI reference / OCI reference in the index descriptor.
    @return 规范 OCI manifest digest / Canonical OCI manifest digest.
    """

    def descriptor_for(payload: bytes) -> tuple[str, int]:
        """@brief 为真实 OCI blob 计算 descriptor / Compute a descriptor for a real OCI blob.

        @param payload 将写入的 blob 字节 / Blob bytes that will be written.
        @return 规范 digest 与字节数 / Canonical digest and byte count.
        """

        digest = hashlib.sha256(payload).hexdigest()
        return f"sha256:{digest}", len(payload)

    config_payload = json.dumps(
        {"architecture": "amd64", "os": "linux"}, sort_keys=True
    ).encode("utf-8")
    layer_payload = b"verified synthetic OCI layer\n"
    config_digest, config_size = descriptor_for(config_payload)
    layer_digest, layer_size = descriptor_for(layer_payload)
    manifest_payload = json.dumps(
        {
            "schemaVersion": 2,
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": config_digest,
                "size": config_size,
            },
            "layers": [
                {
                    "mediaType": "application/vnd.oci.image.layer.v1.tar",
                    "digest": layer_digest,
                    "size": layer_size,
                }
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    manifest_digest, manifest_size = descriptor_for(manifest_payload)
    output_layout = (
        output_root / "sha256" / manifest_digest.removeprefix("sha256:") / "oci-layout"
    )
    blob_directory = output_layout / "blobs" / "sha256"
    blob_directory.mkdir(parents=True)
    for digest, payload in (
        (config_digest, config_payload),
        (layer_digest, layer_payload),
        (manifest_digest, manifest_payload),
    ):
        (blob_directory / digest.removeprefix("sha256:")).write_bytes(payload)
    (output_layout / "oci-layout").write_text(
        json.dumps({"imageLayoutVersion": "1.0.0"}), encoding="utf-8"
    )
    (output_layout / "index.json").write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "manifests": [
                    {
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "digest": manifest_digest,
                        "size": manifest_size,
                        "annotations": {
                            "org.opencontainers.image.ref.name": reference
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (output_layout / "wspctl-manifest-digest").write_text(
        f"{manifest_digest}\n", encoding="utf-8"
    )
    (output_root / "build-receipt").write_text(
        "\n".join(
            (
                "schema=1",
                f"image_source_identity={source_identity}",
                f"manifest_digest={manifest_digest}",
                "built_source_date_epoch=1",
                "",
            )
        ),
        encoding="utf-8",
    )
    return manifest_digest


def current_oci_image_source_identity() -> str:
    """@brief 计算当前 checkout 的 OCI image source identity / Compute the OCI-image source identity for the current checkout.

    @return 规范源码身份 / Canonical source identity.
    """

    identity_tool = REPOSITORY_ROOT / "tools" / "wspctl_build_identity.py"
    identity_result = subprocess.run(
        [
            sys.executable,
            str(identity_tool),
            "--source-root",
            str(REPOSITORY_ROOT),
            "--component",
            "image",
            "--attribute",
            "platform=linux/amd64",
            "--attribute",
            "rootfs_format=oci-v1",
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
    )
    if identity_result.returncode != 0:
        raise AssertionError(identity_result.stderr)
    return identity_result.stdout.strip()


def test_verified_oci_receipt_skips_buildah_when_the_tool_is_unavailable(
    tmp_path: Path,
) -> None:
    """@brief 已验证 OCI 收据必须在 Buildah 检查前短路 / A verified OCI receipt must short-circuit before Buildah is checked.

    @param tmp_path pytest 临时目录 / Pytest temporary directory.
    @return None / None.
    """

    source_identity = current_oci_image_source_identity()
    output_root = tmp_path / "rootfs"
    manifest_digest = write_verified_synthetic_oci_receipt(
        output_root,
        source_identity,
        "wspctl-runtime",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_buildah = fake_bin / "buildah"
    fake_buildah.write_text("#!/usr/bin/env bash\nexit 99\n", encoding="utf-8")
    fake_buildah.chmod(0o755)
    fake_git = fake_bin / "git"
    fake_git.write_text("#!/usr/bin/env bash\nexit 98\n", encoding="utf-8")
    fake_git.chmod(0o755)
    environment = os.environ | {
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    }

    completed = subprocess.run(
        ["bash", str(BUILD_IMAGE_SCRIPT), str(output_root)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
        timeout=20,
    )

    assert completed.returncode == 0, completed.stderr
    assert "跳过 Buildah 构建" in completed.stdout
    assert (output_root / "current-image-digest").read_text(encoding="utf-8") == (
        f"{manifest_digest}\n"
    )


def test_oci_receipt_with_wrong_reference_does_not_skip_the_build(
    tmp_path: Path,
) -> None:
    """@brief OCI index 的错误 reference annotation 必须使 receipt 失效 / An OCI index descriptor with a wrong reference annotation must invalidate the receipt.

    @param tmp_path pytest 临时目录 / Pytest temporary directory.
    @return None / None.
    """

    output_root = tmp_path / "rootfs"
    write_verified_synthetic_oci_receipt(
        output_root,
        current_oci_image_source_identity(),
        "different-reference",
    )
    environment = os.environ | {"SOURCE_DATE_EPOCH": "not-a-timestamp"}

    completed = subprocess.run(
        ["bash", str(BUILD_IMAGE_SCRIPT), str(output_root)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
        timeout=20,
    )

    assert completed.returncode != 0
    assert "跳过 Buildah 构建" not in completed.stdout
    assert "SOURCE_DATE_EPOCH 必须是十进制 Unix 时间" in completed.stderr


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
    """@brief install.sh 必须独占完整 control-plane 部署 / install.sh must own the complete control-plane deployment.

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
    assert "for command_name in sudo tee date" in script
    assert "Buildah/CMake are deliberately checked by their receipt-miss branches" in script
    assert "sudo buildah skopeo umoci cmake" not in script
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
    assert "installWspctl.sh" not in script


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
    assert 'WSPCTL_INSTALL_SCRIPT="$BOT_DIR/install.sh"' in script


if __name__ == "__main__":
    test_start_script_is_bash_syntax_valid_and_declares_critical_contracts()
    test_image_build_and_publication_are_explicit_separate_commands()
    test_root_uninstaller_is_syntax_valid_and_requires_explicit_purge()
    test_root_status_script_is_readonly_and_reports_operational_boundaries()
    test_install_entrypoint_owns_complete_control_plane_deployment()
    test_runbot_only_checks_installed_broker_readiness()
