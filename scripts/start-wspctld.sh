#!/usr/bin/env bash

# @brief 启动本 checkout 的 wspctld 开发服务 / Start the checkout-local wspctld development service.
#
# 这是 install.sh 调用的 host-broker 内部阶段：只构建 host control-plane 程序并
# 管理 daemon。workspace OCI image 必须由前序安装阶段显式发布；Bot 从不直接执行本脚本。/
# This is the internal host-broker stage invoked by install.sh: it only builds host
# control-plane programs and manages the daemon. The preceding installation stage must explicitly
# publish the workspace OCI image; the Bot never invokes this script directly.

set -euo pipefail

# @brief 脚本所在仓库根 / Repository root containing this script.
REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# @brief 项目 virtual environment / Project virtual environment.
VENV_DIR="$REPOSITORY_ROOT/.venv"
# @brief 受控 Python 解释器 / Controlled Python interpreter.
PYTHON_EXECUTABLE="$VENV_DIR/bin/python"
# @brief 仅用于 host receipt/source identity 的标准库 Python / Standard-library Python used only for host receipt/source identity.
HOST_IDENTITY_PYTHON="${PYTHON:-python}"
# @brief 未规范化的 checkout-local wspctl control-plane 根 / Unnormalized checkout-local wspctl control-plane root.
REQUESTED_WORK_ROOT="${WSPCTL_WORK_ROOT:-$REPOSITORY_ROOT/.wspctl}"
# @brief 规范化后的 checkout-local wspctl control-plane 根 / Canonical checkout-local wspctl control-plane root.
WORK_ROOT="$(realpath --canonicalize-missing -- "$REQUESTED_WORK_ROOT")"
# @brief XFS persistent-state mountpoint / XFS persistent-state mountpoint.
STATE_ROOT="$WORK_ROOT/state"
# @brief checkout-local loopback XFS image / Checkout-local loopback XFS image.
LOOP_IMAGE="$WORK_ROOT/state.xfs.img"
# @brief 首次创建 loopback image 的容量 / Capacity used when initially creating the loopback image.
LOOP_SIZE="${WSPCTL_LOOP_SIZE:-32G}"
# @brief readonly OCI image publication root / Readonly OCI image publication root.
IMAGES_ROOT="$WORK_ROOT/images"
# @brief systemd service 所在 cgroup v2 parent / Cgroup v2 parent containing the systemd service.
CGROUP_PARENT="/sys/fs/cgroup/system.slice"
# @brief CMake build directory for host tools / CMake build directory for host tools.
BUILD_DIRECTORY="$REPOSITORY_ROOT/build/wspctld-dev"
# @brief wspctl 构建输入身份计算器 / wspctl build-input identity calculator.
BUILD_IDENTITY_TOOL="$REPOSITORY_ROOT/tools/wspctl_build_identity.py"
# @brief 普通 Python wheel 的唯一 reconciler / Sole reconciler for the regular Python wheel.
CLIENT_RECONCILER="$REPOSITORY_ROOT/scripts/ensure-wspctl-client.sh"
# @brief 已安装 broker executable / Installed broker executable.
HOST_WSPCTLD="/usr/local/bin/wspctld"
# @brief 已安装 operator CLI / Installed operator CLI.
HOST_OPERATOR_CLI="/usr/local/bin/wspctl"
# @brief 已安装 image verifier / Installed image verifier.
HOST_IMAGE_VERIFIER="/usr/local/bin/wspctl-image"
# @brief 已安装 root-owned publisher / Installed root-owned publisher.
HOST_PUBLISHER="/usr/local/libexec/wspctl/publish_wspctl_image.py"
# @brief 已安装 systemd 资产目录 / Installed systemd asset directory.
HOST_SYSTEMD_ASSET_DIRECTORY="/usr/local/share/fogmoe-wspctl/systemd"
# @brief 已安装 wspctld unit 资产 / Installed wspctld unit asset.
HOST_WSPCTLD_UNIT_SOURCE="$HOST_SYSTEMD_ASSET_DIRECTORY/wspctld.service"
# @brief 已安装 LXCFS unit 资产 / Installed LXCFS unit asset.
HOST_LXCFS_UNIT_SOURCE="$HOST_SYSTEMD_ASSET_DIRECTORY/wspctl-lxcfs.service"
# @brief 已安装 broker environment 模板 / Installed broker environment template.
HOST_ENVIRONMENT_TEMPLATE="$HOST_SYSTEMD_ASSET_DIRECTORY/wspctld.env.example"
# @brief host service unit name / Host service-unit name.
SERVICE_NAME="wspctld.service"
# @brief wspctl 专用 LXCFS service unit / Dedicated wspctl LXCFS service unit.
LXCFS_SERVICE_NAME="wspctl-lxcfs.service"
# @brief wspctl 专用 LXCFS FUSE mount / Dedicated wspctl LXCFS FUSE mount.
LXCFS_ROOT="/run/fogmoe-wspctl-lxcfs/root"
# @brief expected client UID; default is direct host runBot user / Expected client UID; default is the direct-host runBot user.
CLIENT_UID="${WSPCTL_CLIENT_UID:-$(id -u)}"
# @brief 镜像内具名 Agent 的固定 UID / Fixed UID of the named Agent inside the image.
AGENT_UID=65533
# @brief 镜像内具名 Agent 的固定 GID / Fixed GID of the named Agent inside the image.
AGENT_GID=65533
# @brief 独立 operator UID；默认 root，绝不能与 Bot UID 相同 / Independent operator UID; defaults to root and must never equal the Bot UID.
OPERATOR_UID="${WSPCTL_OPERATOR_UID:-0}"
# @brief 当前发布的 OCI image digest 记录 / Record of the currently published OCI image digest.
CURRENT_IMAGE_FILE="$WORK_ROOT/current-image-digest"
# @brief 可选的 operator 固定 OCI image digest / Optional operator-pinned OCI image digest.
IMAGE_DIGEST="${WSPCTL_IMAGE_DIGEST:-}"
# @brief 当前 image 的 path-safe digest / Path-safe digest of the current image.
IMAGE_DIGEST_HEX=""
# @brief 当前已发布 readonly rootfs / Currently published readonly rootfs.
BASE_ROOT=""
# @brief operator 请求的 I/O 权重或 host capability 自动探测 / Operator-requested I/O weight or host-capability auto detection.
REQUESTED_IO_WEIGHT="${WSPCTL_IO_WEIGHT:-auto}"
# @brief 最终写入 broker 环境的 I/O 权重；0 表示禁用 / Effective I/O weight written to the broker environment; zero disables it.
IO_WEIGHT=""
# @brief 每个 runtime 的 memory.max / Per-runtime memory.max.
MEMORY_MAX_BYTES="${WSPCTL_MEMORY_MAX:-4294967296}"
# @brief 每个 runtime 的 memory.high / Per-runtime memory.high.
MEMORY_HIGH_BYTES="${WSPCTL_MEMORY_HIGH:-4294967296}"
# @brief 每个 runtime 的 memory.swap.max / Per-runtime memory.swap.max.
MEMORY_SWAP_MAX_BYTES="${WSPCTL_MEMORY_SWAP_MAX:-2147483648}"
# @brief 每个 runtime 私有 /tmp 的 tmpfs 上限 / Per-runtime private /tmp tmpfs limit.
TMP_SIZE_BYTES="${WSPCTL_TMP_SIZE_BYTES:-1073741824}"
# @brief 每个 runtime 的 cpu.max quota / Per-runtime cpu.max quota.
CPU_MAX_US="${WSPCTL_CPU_MAX_US:-200000}"
# @brief 每个 runtime 的 cpu.max period / Per-runtime cpu.max period.
CPU_PERIOD_US="${WSPCTL_CPU_PERIOD_US:-100000}"
# @brief 每个 runtime 的持久 workspace hard limit / Per-runtime persistent workspace hard limit.
WORKSPACE_HARD_BYTES="${WSPCTL_RUNTIME_WORKSPACE_HARD_BYTES:-4294967296}"
# @brief checkout-local lifecycle lock / Checkout-local lifecycle lock.
LOCK_FILE="$REPOSITORY_ROOT/.runtime/wspctld-control.lock"
# @brief 跨 checkout 串行化 /usr/local host artifact 安装的 root-owned 锁 / Root-owned lock serializing /usr/local host-artifact installation across checkouts.
HOST_INSTALL_LOCK="/run/lock/fogmoe-wspctl-host-install.lock"
# @brief 最近一次已应用 broker 配置的 fingerprint / Fingerprint of the most recently applied broker configuration.
FINGERPRINT_FILE="$REPOSITORY_ROOT/.runtime/wspctld-fingerprint"
# @brief 由本 checkout 安装的 host artifacts 清单 / Manifest of host artifacts installed by this checkout.
INSTALL_MANIFEST_FILE="$WORK_ROOT/install-manifest"
# @brief 由本 checkout 安装的 publisher/verifier host artifacts 构建收据 / Build receipt for publisher/verifier host artifacts installed by this checkout.
PUBLISHER_DEPLOYMENT_RECEIPT_FILE="$WORK_ROOT/publisher-deployment-receipt"
# @brief 由本 checkout 安装的 broker/runtime host artifacts 构建收据 / Build receipt for broker/runtime host artifacts installed by this checkout.
RUNTIME_DEPLOYMENT_RECEIPT_FILE="$WORK_ROOT/deployment-receipt"
# @brief Bot 专属 daemon socket 路径 / Bot-exclusive daemon socket path.
SOCKET_PATH="$WORK_ROOT/run/bot/wspctld.sock"
# @brief root/operator 专属 daemon socket 路径 / Root/operator-exclusive daemon socket path.
OPERATOR_SOCKET_PATH="$WORK_ROOT/run/operator/wspctld.sock"
# @brief 本次启动是否必须重启 service / Whether this invocation must restart the service.
BROKER_RESTART_REQUIRED=false
# @brief 等待静态 readiness 验收后记录的配置 fingerprint / Configuration fingerprint recorded only after static readiness validation.
BROKER_FINGERPRINT=""
# @brief 最近一次完成静态 readiness 验收的 systemd invocation / Last systemd invocation that passed static readiness validation.
BROKER_VALIDATED_INVOCATION_ID=""

# @brief 输出错误并终止 / Print an error and terminate.
# @param $* 错误文本 / Error text.
die() {
    printf 'wspctld 启动失败: %s\n' "$*" >&2
    exit 1
}

# @brief 输出普通进度 / Print normal progress.
# @param $* 进度文本 / Progress text.
note() {
    printf 'wspctld: %s\n' "$*"
}

# @brief 检验一个只允许十进制 UID 的值 / Validate a decimal-only UID value.
# @param $1 UID 文本 / UID text.
# @param $2 环境变量名 / Environment-variable name.
require_uid() {
    [[ "$1" =~ ^[0-9]+$ ]] || die "$2 必须是十进制 UID"
}

# @brief 校验首次创建 loopback image 的容量拼写 / Validate the capacity spelling for initial loopback-image creation.
# @param $1 容量文本 / Capacity text.
require_loop_size() {
    [[ "$1" =~ ^[1-9][0-9]*[KMGTP]$ ]] \
        || die "WSPCTL_LOOP_SIZE 必须是类似 20G 的正整数 IEC 容量"
}

# @brief 校验一个正十进制资源参数 / Validate one positive decimal resource parameter.
# @param $1 参数值 / Parameter value.
# @param $2 环境变量名 / Environment-variable name.
require_positive_decimal() {
    [[ "$1" =~ ^[1-9][0-9]*$ ]] || die "$2 必须是正十进制整数"
}

# @brief 校验一个非负十进制资源参数 / Validate one nonnegative decimal resource parameter.
# @param $1 参数值 / Parameter value.
# @param $2 环境变量名 / Environment-variable name.
require_decimal() {
    [[ "$1" =~ ^[0-9]+$ ]] || die "$2 必须是非负十进制整数"
}

# @brief 校验唯一受控的 checkout-local control-plane 根路径 / Validate the sole managed checkout-local control-plane root.
# @return 路径安全时返回零 / Zero when the path is safe.
require_work_root() {
    local managed_work_root="$REPOSITORY_ROOT/.wspctl"

    # A privileged installer may create/chmod this directory.  Accepting arbitrary absolute
    # paths would turn spelling-only validation into attacker-controlled root mutation.  Comparing
    # requested and canonical forms also rejects a symlink substituted at .wspctl.
    [[ "$REQUESTED_WORK_ROOT" == "$managed_work_root" \
        && "$WORK_ROOT" == "$managed_work_root" ]] \
        || die "不支持自定义 WSPCTL_WORK_ROOT；特权 control plane 只能使用 $managed_work_root"
}

# @brief 幂等写入 root-owned broker 环境设置 / Idempotently write one root-owned broker environment setting.
# @param $1 环境文件 / Environment file.
# @param $2 设置名 / Setting name.
# @param $3 设置值 / Setting value.
upsert_root_environment_setting() {
    local environment_file="$1"
    local setting_name="$2"
    local setting_value="$3"
    if sudo grep --quiet "^${setting_name}=" "$environment_file"; then
        sudo sed --in-place --regexp-extended \
            "s|^${setting_name}=.*$|${setting_name}=${setting_value}|" \
            "$environment_file"
        return 0
    fi
    printf '%s=%s\n' "$setting_name" "$setting_value" \
        | sudo tee --append "$environment_file" >/dev/null
}

# @brief 解析 host 支持的相对 I/O 权重策略 / Resolve relative I/O weighting supported by the host.
# @return 成功时返回零 / Zero on success.
resolve_io_weight() {
    if [[ "$REQUESTED_IO_WEIGHT" != "auto" ]]; then
        [[ "$REQUESTED_IO_WEIGHT" =~ ^[0-9]+$ ]] \
            || die "WSPCTL_IO_WEIGHT 必须是 auto 或 0..10000"
        (( 10#$REQUESTED_IO_WEIGHT <= 10000 )) \
            || die "WSPCTL_IO_WEIGHT 必须是 auto 或 0..10000"
        IO_WEIGHT="$REQUESTED_IO_WEIGHT"
        return 0
    fi
    if [[ -e "$CGROUP_PARENT/io.weight" ]]; then
        IO_WEIGHT=100
        return 0
    fi
    IO_WEIGHT=0
    note "host cgroup v2 不提供 io.weight；禁用相对 I/O 权重（memory/CPU/PIDs/XFS quota 仍强制）"
}

# @brief 验证 receipt 快路径所需的最小 host 命令 / Verify the minimal host commands needed by the receipt fast path.
# @return 成功时返回零 / Zero on success.
require_receipt_commands() {
    local command_name

    command -v "$HOST_IDENTITY_PYTHON" >/dev/null 2>&1 \
        || die "缺少用于 host receipt 的 Python: $HOST_IDENTITY_PYTHON"
    for command_name in sudo sha256sum awk flock mkdir realpath stat; do
        command -v "$command_name" >/dev/null 2>&1 \
            || die "缺少 receipt 快路径必需命令: $command_name"
    done
}

# @brief 获取跨 checkout 的 root-owned host-install flock / Acquire the cross-checkout root-owned host-install flock.
# @return 成功时返回零 / Zero on success.
# @note 锁文件可由普通调用者打开以取得 advisory lock，但其父目录与 inode 所有权均由 root
#       控制；文件内容不承载状态，因而不存在可替换的业务数据。/
#       The caller may open the lock file for the advisory lock, while root controls its parent
#       directory and inode ownership. The file carries no state and therefore no replaceable
#       business data.
acquire_host_install_lock() {
    if ! sudo test -e "$HOST_INSTALL_LOCK"; then
        sudo install -o root -g root -m 0666 /dev/null "$HOST_INSTALL_LOCK"
    fi
    sudo chown root:root "$HOST_INSTALL_LOCK"
    sudo chmod 0666 "$HOST_INSTALL_LOCK"
    exec 8>"$HOST_INSTALL_LOCK"
    flock 8
}

# @brief 验证仅在 host receipt 失效时才需要的编译命令 / Verify compilation commands needed only after a host receipt miss.
# @return 成功时返回零 / Zero on success.
require_host_build_commands() {
    local command_name

    for command_name in cmake "${CXX:-c++}"; do
        command -v "$command_name" >/dev/null 2>&1 \
            || die "host artifact 收据失效后缺少编译命令: $command_name"
    done
}

# @brief 验证仅在实际 daemon 启动时才需要的运行时命令 / Verify runtime commands needed only when actually starting the daemon.
# @return 成功时返回零 / Zero on success.
require_runtime_commands() {
    local command_name

    for command_name in systemctl journalctl udevadm findmnt mountpoint mount install bash grep tr stat \
        fallocate losetup mkfs.xfs blkid find sort xargs lxcfs fusermount3 mktemp; do
        command -v "$command_name" >/dev/null 2>&1 \
            || die "启动 wspctld 所需命令缺失: $command_name"
    done
}

# @brief 读取 pyproject 中的发布版本 / Read the distribution version from pyproject.
# @return 规范 project version / Canonical project version.
project_version() {
    "$HOST_IDENTITY_PYTHON" -I - "$REPOSITORY_ROOT/pyproject.toml" <<'PY'
import sys
import tomllib
from pathlib import Path

project = tomllib.loads(Path(sys.argv[1]).read_text(encoding="utf-8")).get("project")
version = project.get("version") if isinstance(project, dict) else None
if not isinstance(version, str) or not version:
    raise SystemExit("pyproject.toml is missing project.version")
print(version)
PY
}

# @brief 计算一个声明组件的构建输入身份 / Compute the build-input identity of one declared component.
# @param $1 组件名 / Component name.
# @param $@ 附加 ``--attribute`` 参数 / Additional ``--attribute`` arguments.
# @return 小写十六进制 SHA-256 身份 / Lowercase hexadecimal SHA-256 identity.
build_identity() {
    local component="$1"

    shift
    [[ -f "$BUILD_IDENTITY_TOOL" ]] \
        || die "缺少 wspctl 构建身份工具: $BUILD_IDENTITY_TOOL"
    "$HOST_IDENTITY_PYTHON" -I "$BUILD_IDENTITY_TOOL" \
        --source-root "$REPOSITORY_ROOT" \
        --component "$component" \
        "$@"
}

# @brief 计算 host C++ toolchain 的可追溯身份 / Compute a traceable identity for the host C++ toolchain.
# @return C++ 编译器与 CMake 版本的 SHA-256 / SHA-256 of the C++ compiler and CMake versions.
host_toolchain_identity() {
    local cxx_command="${CXX:-c++}"

    {
        "$cxx_command" --version
        cmake --version
    } | sha256sum | awk '{print $1}'
}

# @brief 计算当前特权 host 工件的构建身份 / Compute the current privileged host-artifact build identity.
# @return 小写十六进制 SHA-256 身份 / Lowercase hexadecimal SHA-256 identity.
host_build_identity() {
    build_identity host \
        --attribute "build_type=Release" \
        --attribute "cxx_standard=23" \
        --attribute "development_root=ON" \
        --attribute "install_prefix=/usr/local" \
        --attribute "platform=$(uname -m)" \
        --attribute "work_root=$WORK_ROOT"
}

# @brief 输出 publisher/verifier 所拥有的已安装 host artifact 路径 / Print installed publisher/verifier artifact paths owned by this checkout.
# @return 每行一个绝对路径 / One absolute path per line.
publisher_artifact_paths() {
    printf '%s\n' "$HOST_IMAGE_VERIFIER" "$HOST_PUBLISHER"
}

# @brief 输出 broker/runtime 所拥有的已安装 host artifact 路径 / Print installed broker/runtime artifact paths owned by this checkout.
# @return 每行一个绝对路径 / One absolute path per line.
runtime_artifact_paths() {
    printf '%s\n' \
        "$HOST_WSPCTLD" \
        "$HOST_OPERATOR_CLI" \
        "$HOST_LXCFS_UNIT_SOURCE" \
        "$HOST_WSPCTLD_UNIT_SOURCE" \
        "$HOST_ENVIRONMENT_TEMPLATE"
}

# @brief 输出本 checkout 所拥有的全部已安装 host artifact 路径 / Print all installed host-artifact paths owned by this checkout.
# @return 每行一个绝对路径 / One absolute path per line.
host_artifact_paths() {
    publisher_artifact_paths
    runtime_artifact_paths
}

# @brief 验证一个特权路径的非跟随 inode 元数据 / Verify non-following inode metadata for one privileged path.
# @param $1 path 绝对路径 / Absolute path.
# @param $2 expected_kind ``regular`` 或 ``directory`` / ``regular`` or ``directory``.
# @return 路径为 root:root、目标 inode 类型正确且不允许 group/world 写入时返回零 /
#         Zero when the path is root:root, has the expected inode type, and is not group/world writable.
# @note ``stat`` 默认不跟随 symlink；因此一个 symlink 不能在此处伪装成可信 artifact。验证完
#       所有不可写父目录后，非 root 攻击者也不能在 hash 与 systemd exec 之间替换路径。/
#       ``stat`` does not follow symlinks by default, so a symlink cannot masquerade as a trusted
#       artifact here. Once all non-writable parent directories are verified, a non-root attacker
#       cannot replace the path between hashing and systemd execution.
host_path_is_trusted() {
    local path="$1"
    local expected_kind="$2"
    local metadata
    local owner_uid
    local owner_gid
    local raw_mode
    local normalized_raw_mode
    local mode_value
    local expected_type_bits

    metadata="$(sudo stat --format='%u:%g:%f' -- "$path")" || return 1
    IFS=':' read -r owner_uid owner_gid raw_mode <<< "$metadata"
    [[ "$owner_uid" == "0" && "$owner_gid" == "0" \
        && "$raw_mode" =~ ^[0-9A-Fa-f]+$ ]] || return 1
    normalized_raw_mode="${raw_mode,,}"
    mode_value=$((16#$normalized_raw_mode))
    case "$expected_kind" in
        regular) expected_type_bits=$((16#8000)) ;;
        directory) expected_type_bits=$((16#4000)) ;;
        *) return 1 ;;
    esac
    (( (mode_value & 16#f000) == expected_type_bits \
        && (mode_value & 8#022) == 0 ))
}

# @brief 输出承载已安装 host artifact 的固定受信父目录 / Print the fixed trusted parent directories for installed host artifacts.
# @return 每行一个绝对目录 / One absolute directory per line.
# @note 这份白名单刻意没有从 receipt 读取；receipt 只能证明已知安装目标，不能声明新的
#       root 执行路径。/
#       This allowlist is deliberately not read from the receipt: a receipt may prove only known
#       install targets, never declare a new root-executed path.
host_artifact_parent_directories() {
    printf '%s\n' \
        /usr \
        /usr/local \
        /usr/local/bin \
        /usr/local/libexec \
        /usr/local/libexec/wspctl \
        /usr/local/share \
        /usr/local/share/fogmoe-wspctl \
        /usr/local/share/fogmoe-wspctl/systemd
}

# @brief 验证所有 host artifact 父目录的特权信任边界 / Verify the privileged trust boundary of every host-artifact parent directory.
# @return 所有目录均为可信 root-owned directory 时返回零 / Zero when every directory is a trusted root-owned directory.
host_artifact_parent_directories_are_trusted() {
    local directory_path

    while IFS= read -r directory_path; do
        host_path_is_trusted "$directory_path" directory || return 1
    done < <(host_artifact_parent_directories)
}

# @brief 验证已安装 host artifact 的类型、权限与信任链 / Verify an installed host artifact's type, permissions, and trust chain.
# @param $1 artifact 绝对路径 / Absolute artifact path.
# @return 可执行程序可执行、所有 artifact/root parent 均可信时返回零 /
#         Zero when executables are executable and the artifact/root-parent trust chain is valid.
host_artifact_is_usable() {
    local artifact_path="$1"
    local requires_execute=false

    case "$artifact_path" in
        "$HOST_WSPCTLD"|"$HOST_OPERATOR_CLI"|"$HOST_IMAGE_VERIFIER"|"$HOST_PUBLISHER")
            requires_execute=true
            ;;
        "$HOST_LXCFS_UNIT_SOURCE"|"$HOST_WSPCTLD_UNIT_SOURCE"|"$HOST_ENVIRONMENT_TEMPLATE")
            ;;
        *)
            return 1
            ;;
    esac
    host_artifact_parent_directories_are_trusted || return 1
    host_path_is_trusted "$artifact_path" regular || return 1
    [[ "$requires_execute" == true ]] || return 0
    sudo test -x "$artifact_path"
}

# @brief 验证一组已安装 host artifact 的 receipt、来源身份与内容校验和 / Verify one installed host-artifact set's receipt, source identity, and content checksums.
# @param $1 root-owned receipt 文件 / Root-owned receipt file.
# @param $2 receipt 中的角色 / Receipt role.
# @param $3 期望源码身份 / Expected source identity.
# @param $4 期望 project version / Expected project version.
# @param $5 输出该角色 artifact 的函数名 / Function printing that role's artifacts.
# @param $6 该角色 artifact 数量 / Artifact count for that role.
# @return 全部 artifact 均可信时为零 / Zero when every artifact is trusted.
host_artifacts_are_current() {
    local receipt_file="$1"
    local expected_role="$2"
    local expected_identity="$3"
    local expected_version="$4"
    local artifact_provider="$5"
    local expected_artifact_count="$6"
    local receipt_identity=""
    local receipt_version=""
    local receipt_platform=""
    local receipt_schema=""
    local receipt_role=""
    local receipt_toolchain=""
    local record_key
    local record_value
    local artifact_path
    local expected_checksum
    local actual_checksum
    local verified_artifact_count=0

    host_path_is_trusted "$WORK_ROOT" directory || return 1
    host_path_is_trusted "$receipt_file" regular || return 1
    sudo test -r "$receipt_file" || return 1
    while IFS='=' read -r record_key record_value; do
        case "$record_key" in
            schema) receipt_schema="$record_value" ;;
            role) receipt_role="$record_value" ;;
            source_identity) receipt_identity="$record_value" ;;
            project_version) receipt_version="$record_value" ;;
            platform) receipt_platform="$record_value" ;;
            toolchain_identity) receipt_toolchain="$record_value" ;;
        esac
    done < <(sudo cat "$receipt_file")
    [[ "$receipt_schema" == "2" \
        && "$receipt_role" == "$expected_role" \
        && "$receipt_identity" == "$expected_identity" \
        && "$receipt_version" == "$expected_version" \
        && "$receipt_platform" == "$(uname -m)" \
        && "$receipt_toolchain" =~ ^[0-9a-f]{64}$ ]] || return 1

    while IFS= read -r artifact_path; do
        host_artifact_is_usable "$artifact_path" || return 1
        expected_checksum="$(
            sudo awk -v path="$artifact_path" \
                '$1 == "artifact" && $2 == path { print $3 }' "$receipt_file"
        )"
        [[ "$expected_checksum" =~ ^[0-9a-f]{64}$ ]] || return 1
        actual_checksum="$(sudo sha256sum "$artifact_path" | awk '{print $1}')"
        [[ "$actual_checksum" == "$expected_checksum" ]] || return 1
        ((verified_artifact_count += 1))
    done < <("$artifact_provider")
    [[ "$verified_artifact_count" == "$expected_artifact_count" ]]
}

# @brief 仅为 receipt 创建 root-owned metadata 根，不创建 runtime state / Create only the root-owned receipt metadata root, not runtime state.
# @return 成功时返回零 / Zero on success.
ensure_receipt_parent() {
    sudo install -d -o root -g root -m 0700 "$WORK_ROOT"
}

# @brief 原子写入一组已安装 host artifact 的构建收据 / Atomically write the build receipt for one installed host-artifact set.
# @param $1 root-owned receipt 文件 / Root-owned receipt file.
# @param $2 receipt 角色 / Receipt role.
# @param $3 源码身份 / Source identity.
# @param $4 project version / Project version.
# @param $5 toolchain identity / Toolchain identity.
# @param $6 输出该角色 artifact 的函数名 / Function printing that role's artifacts.
# @return 成功时返回零 / Zero on success.
write_host_deployment_receipt() {
    local receipt_file="$1"
    local receipt_role="$2"
    local source_identity="$3"
    local expected_version="$4"
    local toolchain_identity="$5"
    local artifact_provider="$6"
    local temporary_file="$REPOSITORY_ROOT/.runtime/wspctl-${receipt_role}-receipt.$$.tmp"
    local artifact_path
    local artifact_checksum

    ensure_receipt_parent
    {
        printf 'schema=2\nrole=%s\nsource_identity=%s\nproject_version=%s\nplatform=%s\ntoolchain_identity=%s\n' \
            "$receipt_role" "$source_identity" "$expected_version" "$(uname -m)" "$toolchain_identity"
        while IFS= read -r artifact_path; do
            artifact_checksum="$(sudo sha256sum "$artifact_path" | awk '{print $1}')"
            printf 'artifact %s %s\n' "$artifact_path" "$artifact_checksum"
        done < <("$artifact_provider")
    } > "$temporary_file"
    sudo install -o root -g root -m 0600 "$temporary_file" "$receipt_file"
    rm -f -- "$temporary_file"
}

# @brief 配置专用 host-tool build tree；仅在 receipt 失效后调用 / Configure the dedicated host-tool build tree; called only after a receipt miss.
# @param $1 publisher 或 runtime 的最小构建角色 / Minimal build role: publisher or runtime.
# @return 成功时返回零 / Zero on success.
configure_host_build() {
    local build_role="$1"
    local -a role_definitions

    case "$build_role" in
        publisher)
            role_definitions=(
                -DWSPCTL_BUILD_HOST_PUBLISHER=ON
                -DWSPCTL_BUILD_HOST_RUNTIME=OFF
                -DWSPCTL_BUILD_WORKSPACE_SUPERVISOR=OFF
            )
            ;;
        runtime)
            role_definitions=(
                -DWSPCTL_BUILD_HOST_PUBLISHER=OFF
                -DWSPCTL_BUILD_HOST_RUNTIME=ON
                -DWSPCTL_BUILD_WORKSPACE_SUPERVISOR=OFF
                -DWSPCTL_ALLOW_INSECURE_DEVELOPMENT_ROOT=ON
                "-DWSPCTL_HOST_WORKDIR=$WORK_ROOT"
            )
            ;;
        *)
            die "未知 host build role: $build_role"
            ;;
    esac
    cmake -S "$REPOSITORY_ROOT" -B "$BUILD_DIRECTORY" \
        -DBUILD_TESTING=OFF \
        -DPython_EXECUTABLE="$HOST_IDENTITY_PYTHON" \
        -DWSPCTL_BUILD_TESTING=OFF \
        -DWSPCTL_BUILD_PYTHON_BINDINGS=OFF \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX=/usr/local \
        "${role_definitions[@]}"
}

# @brief 验证或按需构建 publisher/verifier host tools；不创建 runtime state / Verify or build publisher/verifier host tools only; never create runtime state.
# @return 成功时返回零 / Zero on success.
ensure_publisher_host_artifacts() {
    local expected_version
    local expected_identity
    local toolchain_identity

    expected_version="$(project_version)" \
        || die "无法读取 publisher artifact 的 project.version"
    expected_identity="$(host_build_identity)" \
        || die "无法计算 publisher artifact 的构建身份"
    if host_artifacts_are_current "$PUBLISHER_DEPLOYMENT_RECEIPT_FILE" publisher \
        "$expected_identity" "$expected_version" publisher_artifact_paths 2; then
        note "host wspctl-image / publisher 已通过 version/source identity/checksum 验证；跳过 CMake 构建"
        return 0
    fi

    require_host_build_commands
    toolchain_identity="$(host_toolchain_identity)" \
        || die "无法记录 publisher C++ toolchain identity"
    note "publisher artifact 收据失效；仅配置并构建 wspctl-image"
    configure_host_build publisher
    cmake --build "$BUILD_DIRECTORY" --target wspctl-image --parallel
    sudo cmake --install "$BUILD_DIRECTORY" --component WspctlPublisher
    sudo test -x "$HOST_IMAGE_VERIFIER" \
        || die "CMake install 未产生 wspctl-image: $HOST_IMAGE_VERIFIER"
    sudo test -x "$HOST_PUBLISHER" \
        || die "CMake install 未产生 publisher: $HOST_PUBLISHER"
    write_host_deployment_receipt "$PUBLISHER_DEPLOYMENT_RECEIPT_FILE" publisher \
        "$expected_identity" "$expected_version" "$toolchain_identity" publisher_artifact_paths \
        || die "无法写入 publisher artifact 构建收据"
    host_artifacts_are_current "$PUBLISHER_DEPLOYMENT_RECEIPT_FILE" publisher \
        "$expected_identity" "$expected_version" publisher_artifact_paths 2 \
        || die "publisher artifact 安装后未通过构建收据验证"
}

# @brief 验证或按需构建 broker/runtime host tools；不负责 Python client / Verify or build broker/runtime host tools; does not own the Python client.
# @return 成功时返回零 / Zero on success.
ensure_runtime_host_artifacts() {
    local expected_version
    local expected_identity
    local toolchain_identity

    expected_version="$(project_version)" \
        || die "无法读取 runtime artifact 的 project.version"
    expected_identity="$(host_build_identity)" \
        || die "无法计算 runtime artifact 的构建身份"
    if host_artifacts_are_current "$RUNTIME_DEPLOYMENT_RECEIPT_FILE" runtime \
        "$expected_identity" "$expected_version" runtime_artifact_paths 5; then
        note "host wspctld / wspctl / systemd assets 已通过 version/source identity/checksum 验证；跳过 CMake 构建"
        return 0
    fi

    require_host_build_commands
    toolchain_identity="$(host_toolchain_identity)" \
        || die "无法记录 runtime C++ toolchain identity"
    note "runtime artifact 收据失效；仅配置并构建 wspctld / wspctl"
    remove_retired_host_artifacts
    configure_host_build runtime
    cmake --build "$BUILD_DIRECTORY" --target wspctld wspctl --parallel
    sudo cmake --install "$BUILD_DIRECTORY" --component WspctlHost
    sudo test -x "$HOST_WSPCTLD" \
        || die "CMake install 未产生 wspctld: $HOST_WSPCTLD"
    sudo test -x "$HOST_OPERATOR_CLI" \
        || die "CMake install 未产生 operator wspctl: $HOST_OPERATOR_CLI"
    write_install_manifest
    write_host_deployment_receipt "$RUNTIME_DEPLOYMENT_RECEIPT_FILE" runtime \
        "$expected_identity" "$expected_version" "$toolchain_identity" runtime_artifact_paths \
        || die "无法写入 runtime artifact 构建收据"
    host_artifacts_are_current "$RUNTIME_DEPLOYMENT_RECEIPT_FILE" runtime \
        "$expected_identity" "$expected_version" runtime_artifact_paths 5 \
        || die "runtime artifact 安装后未通过构建收据验证"
}

# @brief 按旧 install manifest 安全移除已退役的 host artifact / Safely remove retired host artifacts proven by the prior install manifest.
#
# 这是一条升级迁移，不是 runtime 兼容路径：仅当旧 manifest 的 checksum 仍与文件相同才删除。/
# This is an upgrade migration, not a runtime compatibility path: deletion occurs only when
# the previous install manifest still proves the file checksum.
remove_retired_host_artifacts() {
    local record_type
    local artifact_path
    local expected_checksum
    local actual_checksum
    local retired_supervisor="/usr/local/libexec/wspctl/wsp-systemd"

    sudo test -f "$INSTALL_MANIFEST_FILE" || return 0
    while read -r record_type artifact_path expected_checksum; do
        [[ "$record_type" == "artifact" && "$artifact_path" == "$retired_supervisor" ]] \
            || continue
        sudo test -f "$artifact_path" || continue
        actual_checksum="$(sudo sha256sum "$artifact_path" | awk '{print $1}')"
        if [[ "$actual_checksum" != "$expected_checksum" ]]; then
            note "保留已被外部修改的 retired artifact: $artifact_path"
            continue
        fi
        note "迁移并删除已退役 host supervisor: $artifact_path"
        sudo rm -f -- "$artifact_path"
    done < <(sudo cat "$INSTALL_MANIFEST_FILE")
}

# @brief 写入本 checkout 实际安装的 host artifact manifest / Write the manifest of host artifacts actually installed by this checkout.
#
# 卸载器仅删除 checksum 与本 manifest 匹配的路径，绝不根据全局文件名盲删。/
# The uninstaller deletes only paths whose checksum matches this manifest; it never blindly
# deletes based on a global filename.
write_install_manifest() {
    local temporary_file="$REPOSITORY_ROOT/.runtime/wspctl-install-manifest.$$.tmp"
    local artifact_path
    local artifact_checksum

    : > "$temporary_file"
    while IFS= read -r artifact_path; do
        sudo test -f "$artifact_path" \
            || die "CMake install 未产生预期 host artifact: $artifact_path"
        artifact_checksum="$(sudo sha256sum "$artifact_path" | awk '{print $1}')"
        printf 'artifact %s %s\n' "$artifact_path" "$artifact_checksum" >> "$temporary_file"
    done < <(host_artifact_paths)
    sudo install -o root -g root -m 0600 "$temporary_file" "$INSTALL_MANIFEST_FILE"
    rm -f -- "$temporary_file"
}

# @brief 仅释放本轮启动新建且尚未挂载的 loop association / Release only an unmounted loop association created by this start attempt.
# @param $1 loop device 路径 / Loop-device path.
# @param $2 true 表示 association 由本轮创建 / True when this attempt created the association.
# @return 总是返回成功，保留原始失败原因 / Always succeeds to preserve the original failure.
detach_new_loop_after_failure() {
    local loop_device="$1"
    local attached_loop="$2"

    [[ "$attached_loop" == true ]] || return 0
    if ! sudo losetup --detach "$loop_device"; then
        note "警告: 失败后无法释放本轮 loop association: $loop_device"
        return 0
    fi
    sudo udevadm settle \
        || note "警告: loop association 已释放，但等待 udev 完成失败: $loop_device"
}

# @brief 在首次开发启动时创建并挂载 loopback XFS / Create and mount the loopback XFS on the first development start.
#
# 镜像放在 state mountpoint 的同级目录，因而不会被自身挂载遮蔽。已存在的镜像绝不重新
# format：若其不是 XFS，直接失败而不是猜测使用者是否愿意丢失数据。/
# The image lives beside the state mountpoint and is therefore never hidden by its own mount.
# An existing image is never reformatted: if it is not XFS, fail rather than guessing whether
# the user agrees to lose data.
#
# @note 新建 loop association 后必须等待 udev，再以无缓存 low-level probe 检查超级块；
#       否则一次暂时的空探测会被误报成非 XFS。/
#       After a new loop association, wait for udev and inspect the superblock with an
#       uncached low-level probe; otherwise a transient empty probe can be misreported as non-XFS.
# @return 成功时返回零 / Zero on success.
ensure_loopback_state_mount() {
    local loop_device=""
    local filesystem_type=""
    local probe_output=""
    local created_image=false
    local attached_loop=false

    if sudo mountpoint -q "$STATE_ROOT"; then
        return 0
    fi
    if ! sudo test -e "$LOOP_IMAGE"; then
        note "创建预分配的 $LOOP_SIZE loopback XFS image: $LOOP_IMAGE"
        sudo fallocate --length "$LOOP_SIZE" "$LOOP_IMAGE"
        sudo chown root:root "$LOOP_IMAGE"
        sudo chmod 0600 "$LOOP_IMAGE"
        created_image=true
    fi

    loop_device="$(sudo losetup --associated "$LOOP_IMAGE" | awk -F: 'NR == 1 {print $1}')"
    if [[ -z "$loop_device" ]]; then
        loop_device="$(sudo losetup --find --show --nooverlap "$LOOP_IMAGE")"
        attached_loop=true
    fi
    [[ "$loop_device" = /dev/loop* ]] \
        || die "无法为 loopback image 获得 loop device: $LOOP_IMAGE"
    if ! sudo udevadm settle; then
        detach_new_loop_after_failure "$loop_device" "$attached_loop"
        die "等待 loop device 的 udev 事件完成失败: $loop_device"
    fi

    if [[ "$created_image" == true ]]; then
        if ! sudo mkfs.xfs "$loop_device"; then
            detach_new_loop_after_failure "$loop_device" "$attached_loop"
            die "无法格式化新建的 loopback XFS image: $LOOP_IMAGE"
        fi
    else
        if ! probe_output="$(
            sudo blkid \
                --probe \
                --cache-file /dev/null \
                --output value \
                --match-tag TYPE \
                "$loop_device" 2>&1
        )"; then
            detach_new_loop_after_failure "$loop_device" "$attached_loop"
            die "无法探测已有 loopback image 的 filesystem；未做格式化: $LOOP_IMAGE（blkid: ${probe_output:-无输出}）"
        fi
        filesystem_type="$probe_output"
        if [[ "$filesystem_type" != "xfs" ]]; then
            detach_new_loop_after_failure "$loop_device" "$attached_loop"
            die "已有 loopback image 的 filesystem 为 ${filesystem_type:-未知}，不是 XFS；拒绝重新格式化: $LOOP_IMAGE"
        fi
    fi

    sudo install -d -o root -g root -m 0700 "$STATE_ROOT"
    note "挂载 loopback XFS project-quota state: $loop_device -> $STATE_ROOT"
    if ! sudo mount -t xfs -o rw,prjquota "$loop_device" "$STATE_ROOT"; then
        detach_new_loop_after_failure "$loop_device" "$attached_loop"
        die "无法挂载 loopback XFS project-quota state: $loop_device -> $STATE_ROOT"
    fi
}

# @brief 确认 state root 是已经准备好的强制 XFS project-quota mount / Confirm state root is a provisioned enforcing XFS project-quota mount.
require_state_mount() {
    local filesystem_type
    local mount_options

    sudo test -d "$STATE_ROOT" \
        || die "缺少 state mount: $STATE_ROOT；请先挂载专用 XFS prjquota/pquota filesystem"
    sudo mountpoint -q "$STATE_ROOT" \
        || die "$STATE_ROOT 必须自身是专用 XFS mountpoint，拒绝落到普通 checkout filesystem"
    filesystem_type="$(sudo findmnt --noheadings --output FSTYPE --target "$STATE_ROOT" | tr -d '[:space:]')"
    [[ "$filesystem_type" == "xfs" ]] \
        || die "$STATE_ROOT 的 filesystem 必须是 XFS，实际为: $filesystem_type"
    mount_options="$(sudo findmnt --noheadings --output OPTIONS --target "$STATE_ROOT")"
    [[ ",$mount_options," == *,prjquota,* || ",$mount_options," == *,pquota,* ]] \
        || die "$STATE_ROOT 必须以 prjquota 或 pquota 挂载"
    [[ ",$mount_options," != *,pqnoenforce,* ]] \
        || die "$STATE_ROOT 不得使用 pqnoenforce"
}

# @brief 为开发机 root-owned control-plane 目录准备安全权限 / Prepare root-owned control-plane directories for development.
prepare_control_plane_directories() {
    sudo install -d -o root -g root -m 0711 "$WORK_ROOT"
    # Bot may traverse only this view; the operator endpoint stays below the root-only sibling.
    sudo install -d -o root -g root -m 0711 "$WORK_ROOT/run" "$WORK_ROOT/run/bot"
    sudo install -d -o root -g root -m 0700 "$WORK_ROOT/run/operator"
    sudo install -d -o root -g root -m 0700 "$IMAGES_ROOT"
}

# @brief 选择并验证已经显式发布的 OCI image / Select and verify an explicitly published OCI image.
#
# @return None / None.
# @note 此函数只读，不构建、不导入、不挂载 image。/
#       This function is read-only: it never builds, imports, or mounts an image.
select_published_image() {
    if [[ -z "$IMAGE_DIGEST" ]]; then
        sudo test -r "$CURRENT_IMAGE_FILE" \
            || die "尚未安装 workspace image；请运行 ./install.sh"
        IMAGE_DIGEST="$(sudo cat "$CURRENT_IMAGE_FILE")"
    fi
    [[ "$IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] \
        || die "WSPCTL_IMAGE_DIGEST 必须是 sha256:<64 lowercase hex>"
    IMAGE_DIGEST_HEX="${IMAGE_DIGEST#sha256:}"
    BASE_ROOT="$IMAGES_ROOT/sha256/$IMAGE_DIGEST_HEX/rootfs"
    sudo test -d "$BASE_ROOT" \
        || die "指定 OCI image 尚未发布: $IMAGE_DIGEST；运行 scripts/publish-wspctl-rootfs.sh"
    sudo mountpoint -q "$BASE_ROOT" \
        || die "指定 OCI image 不是独立 readonly mount: $BASE_ROOT"
    sudo findmnt --noheadings --output OPTIONS --target "$BASE_ROOT" \
        | grep --extended-regexp --quiet '(^|,)ro(,|$)' \
        || die "指定 OCI image 必须是 readonly mount: $BASE_ROOT"
    sudo "$HOST_IMAGE_VERIFIER" \
        --verify true \
        --base-root "$BASE_ROOT" \
        --images-root "$IMAGES_ROOT" \
        | grep --fixed-strings --quiet "source_oci_manifest_digest=$IMAGE_DIGEST" \
        || die "指定 OCI image 未通过 native contract/digest 验证: $IMAGE_DIGEST"
    note "使用已发布 OCI image: $IMAGE_DIGEST"
}

# @brief 安装生成的 unit 与受控 environment file / Install generated unit and the controlled environment file.
install_service_configuration() {
    local unit_source="$HOST_WSPCTLD_UNIT_SOURCE"
    local lxcfs_unit_source="$HOST_LXCFS_UNIT_SOURCE"
    local environment_template="$HOST_ENVIRONMENT_TEMPLATE"
    local environment_file="$WORK_ROOT/wspctld.env"
    local filesystem_bytes
    local filesystem_inodes
    local reserve_bytes
    local reserve_inodes
    local admission_bytes
    local admission_inodes

    sudo test -f "$unit_source" \
        && sudo test -f "$lxcfs_unit_source" \
        && sudo test -f "$environment_template" \
        || die "缺少已安装的 systemd 部署资产；请运行 ./install.sh"
    sudo install -o root -g root -m 0644 "$lxcfs_unit_source" "/etc/systemd/system/$LXCFS_SERVICE_NAME"
    sudo install -o root -g root -m 0644 "$unit_source" "/etc/systemd/system/$SERVICE_NAME"
    if [[ ! -f "$environment_file" ]]; then
        sudo install -o root -g root -m 0600 "$environment_template" "$environment_file"
    fi
    sudo sed --in-place --regexp-extended \
        "s|^WSPCTL_CLIENT_UID=.*$|WSPCTL_CLIENT_UID=$CLIENT_UID|" \
        "$environment_file"
    sudo sed --in-place --regexp-extended \
        "s|^WSPCTL_OPERATOR_SOCKET=.*$|WSPCTL_OPERATOR_SOCKET=$OPERATOR_SOCKET_PATH|" \
        "$environment_file"
    sudo sed --in-place --regexp-extended \
        "s|^WSPCTL_OPERATOR_UID=.*$|WSPCTL_OPERATOR_UID=$OPERATOR_UID|" \
        "$environment_file"
    sudo sed --in-place --regexp-extended \
        "s|^WSPCTL_SANDBOX_UID=.*$|WSPCTL_SANDBOX_UID=$AGENT_UID|" \
        "$environment_file"
    sudo sed --in-place --regexp-extended \
        "s|^WSPCTL_SANDBOX_GID=.*$|WSPCTL_SANDBOX_GID=$AGENT_GID|" \
        "$environment_file"
    upsert_root_environment_setting "$environment_file" WSPCTL_MEMORY_MAX "$MEMORY_MAX_BYTES"
    upsert_root_environment_setting "$environment_file" WSPCTL_MEMORY_HIGH "$MEMORY_HIGH_BYTES"
    upsert_root_environment_setting "$environment_file" WSPCTL_MEMORY_SWAP_MAX "$MEMORY_SWAP_MAX_BYTES"
    upsert_root_environment_setting "$environment_file" WSPCTL_TMP_SIZE_BYTES "$TMP_SIZE_BYTES"
    upsert_root_environment_setting "$environment_file" WSPCTL_CPU_MAX_US "$CPU_MAX_US"
    upsert_root_environment_setting "$environment_file" WSPCTL_CPU_PERIOD_US "$CPU_PERIOD_US"
    upsert_root_environment_setting \
        "$environment_file" \
        WSPCTL_RUNTIME_WORKSPACE_HARD_BYTES \
        "$WORKSPACE_HARD_BYTES"
    sudo sed --in-place --regexp-extended \
        '/^WSPCTL_BASE_ROOT=/d;/^WSPCTL_SUPERVISOR=/d' \
        "$environment_file"
    if sudo grep --quiet '^WSPCTL_IMAGE_DIGEST=' "$environment_file"; then
        sudo sed --in-place --regexp-extended \
            "s|^WSPCTL_IMAGE_DIGEST=.*$|WSPCTL_IMAGE_DIGEST=$IMAGE_DIGEST|" \
            "$environment_file"
    else
        printf 'WSPCTL_IMAGE_DIGEST=%s\n' "$IMAGE_DIGEST" \
            | sudo tee --append "$environment_file" >/dev/null
    fi
    # Loopback image 的容量是显式上限，不能沿用生产模板的 50 GiB admission budget。
    # The loopback image is an explicit capacity ceiling and must not inherit the production
    # template's 50 GiB admission budget.
    filesystem_bytes="$(sudo stat --file-system --format='%S * %b' "$STATE_ROOT")"
    filesystem_inodes="$(sudo stat --file-system --format='%c' "$STATE_ROOT")"
    filesystem_bytes=$((filesystem_bytes))
    reserve_bytes=$((filesystem_bytes / 5))
    admission_bytes=$((filesystem_bytes - reserve_bytes))
    reserve_inodes=$((filesystem_inodes / 5))
    admission_inodes=$((filesystem_inodes - reserve_inodes))
    (( admission_bytes >= WORKSPACE_HARD_BYTES + 16777216 && admission_inodes >= 139264 )) \
        || die "loopback XFS 太小，无法容纳一个配置的 workspace 配额与 control layer"
    sudo sed --in-place --regexp-extended \
        "s|^WSPCTL_XFS_GLOBAL_ADMISSION_BYTES=.*$|WSPCTL_XFS_GLOBAL_ADMISSION_BYTES=$admission_bytes|" \
        "$environment_file"
    sudo sed --in-place --regexp-extended \
        "s|^WSPCTL_XFS_SYSTEM_RESERVE_BYTES=.*$|WSPCTL_XFS_SYSTEM_RESERVE_BYTES=$reserve_bytes|" \
        "$environment_file"
    sudo sed --in-place --regexp-extended \
        "s|^WSPCTL_XFS_GLOBAL_ADMISSION_INODES=.*$|WSPCTL_XFS_GLOBAL_ADMISSION_INODES=$admission_inodes|" \
        "$environment_file"
    sudo sed --in-place --regexp-extended \
        "s|^WSPCTL_XFS_SYSTEM_RESERVE_INODES=.*$|WSPCTL_XFS_SYSTEM_RESERVE_INODES=$reserve_inodes|" \
        "$environment_file"
    sudo sed --in-place --regexp-extended \
        "s|^WSPCTL_IO_WEIGHT=.*$|WSPCTL_IO_WEIGHT=$IO_WEIGHT|" \
        "$environment_file"
    sudo chown root:root "$environment_file"
    sudo chmod 0600 "$environment_file"
    sudo systemctl daemon-reload
}

# @brief 计算会影响正在运行 broker 的配置 fingerprint / Compute a fingerprint of inputs that affect the running broker.
# @return SHA-256 fingerprint / SHA-256 fingerprint.
broker_fingerprint() {
    local unit_source="$HOST_WSPCTLD_UNIT_SOURCE"
    local lxcfs_unit_source="$HOST_LXCFS_UNIT_SOURCE"
    local environment_file="$WORK_ROOT/wspctld.env"

    {
        printf 'health_contract=static-ready-v2-image-lxcfs-service-sockets\n'
        printf 'source_oci_manifest_digest=%s\nclient_uid=%s\noperator_uid=%s\noperator_socket=%s\n' \
            "$IMAGE_DIGEST" "$CLIENT_UID" "$OPERATOR_UID" "$OPERATOR_SOCKET_PATH"
        sudo sha256sum "$HOST_WSPCTLD" "$unit_source" "$lxcfs_unit_source"
        sudo sha256sum "$environment_file"
    } | sha256sum | awk '{print $1}'
}

# @brief 标记实际配置变更，避免无变化时丢失 activation cache / Mark a real configuration change without discarding activation cache on no-op starts.
prepare_restart_decision() {
    local desired_fingerprint
    local previous_fingerprint=""
    local previous_invocation_id=""
    local record_key
    local record_value

    desired_fingerprint="$(broker_fingerprint)"
    if [[ -r "$FINGERPRINT_FILE" ]]; then
        while IFS='=' read -r record_key record_value; do
            case "$record_key" in
                fingerprint)
                    previous_fingerprint="$record_value"
                    ;;
                invocation_id)
                    previous_invocation_id="$record_value"
                    ;;
                *)
                    # Legacy one-line fingerprints intentionally force one new readiness-validated deployment.
                    if [[ -z "$record_value" && -z "$previous_fingerprint" ]]; then
                        previous_fingerprint="$record_key"
                    fi
                    ;;
            esac
        done < "$FINGERPRINT_FILE"
    fi
    if [[ "$desired_fingerprint" != "$previous_fingerprint" ]]; then
        BROKER_RESTART_REQUIRED=true
    fi
    BROKER_FINGERPRINT="$desired_fingerprint"
    BROKER_VALIDATED_INVOCATION_ID="$previous_invocation_id"
}

# @brief 读取当前 systemd invocation identity / Read the current systemd invocation identity.
# @return 非空 invocation ID / Nonempty invocation ID.
current_broker_invocation_id() {
    local invocation_id

    invocation_id="$(
        try_current_broker_invocation_id
    )" || die "systemd 未返回可信的 $SERVICE_NAME InvocationID"
    printf '%s\n' "$invocation_id"
}

# @brief 尝试读取当前 systemd invocation identity，不终止调用方 /
# Try to read the current systemd invocation identity without terminating the caller.
# @return 成功时输出规范 invocation ID；service 正在滚代或不可读时非零 /
# Prints a canonical invocation ID on success; nonzero while the service is rolling over or unreadable.
try_current_broker_invocation_id() {
    local invocation_id

    invocation_id="$(
        sudo systemctl show "$SERVICE_NAME" \
            --property=InvocationID \
            --value 2>/dev/null
    )" || return 1
    [[ "$invocation_id" =~ ^[0-9a-fA-F]{32}$ ]] \
        || return 1
    printf '%s\n' "$invocation_id"
}

# @brief 在 broker 已通过静态 readiness 验收后原子记录 fingerprint 与 invocation /
# Atomically record the fingerprint and invocation after static readiness validation succeeds.
# @param $1 已验证的 systemd invocation ID / Validated systemd invocation ID.
record_applied_fingerprint() {
    local invocation_id="$1"
    local temporary_file="$FINGERPRINT_FILE.$$.tmp"

    printf 'fingerprint=%s\ninvocation_id=%s\n' \
        "$BROKER_FINGERPRINT" "$invocation_id" > "$temporary_file"
    mv -f -- "$temporary_file" "$FINGERPRINT_FILE"
}

# @brief 判定 service 与 socket 是否均可用 / Determine whether both service and socket are usable.
# @return 0 表示健康 / Zero if healthy.
# @note operator parent is intentionally root-only (0700), so all socket metadata probes run
#       through sudo. Otherwise an unprivileged caller would restart a healthy daemon on every
#       no-op invocation merely because it cannot traverse the operator directory.
broker_is_healthy() {
    sudo systemctl is-active --quiet "$LXCFS_SERVICE_NAME" \
        && sudo findmnt --noheadings --output FSTYPE --target "$LXCFS_ROOT" \
            | grep --fixed-strings --line-regexp --quiet "fuse.lxcfs" \
        && sudo test -r "$LXCFS_ROOT/proc/cpuinfo" \
        && sudo test -r "$LXCFS_ROOT/proc/meminfo" \
        && sudo systemctl is-active --quiet "$SERVICE_NAME" \
        && sudo test -S "$SOCKET_PATH" \
        && [[ "$(sudo stat --format='%u:%a' "$SOCKET_PATH")" == "$CLIENT_UID:600" ]] \
        && sudo test -S "$OPERATOR_SOCKET_PATH" \
        && [[ "$(sudo stat --format='%u:%a' "$OPERATOR_SOCKET_PATH")" == "$OPERATOR_UID:600" ]]
}

# @brief 等待当前 systemd-ready broker 的 socket metadata 可见 / Wait until socket metadata for the current systemd-ready broker is visible.
# @return 十秒内健康为 0，否则非零 / Zero when healthy within ten seconds, nonzero otherwise.
# @note ``Type=notify`` 已防止 stale socket pathname 冒充 readiness；此轮询仍覆盖重启滚代、
#       VFS metadata 可见性与外部并发 service 操作，但不再承担 daemon readiness 协议。/
#       ``Type=notify`` prevents a stale socket pathname from masquerading as readiness. This loop
#       still covers restart rollover, VFS metadata visibility, and concurrent external service
#       operations, but no longer acts as the daemon readiness protocol.
wait_for_broker_healthy() {
    # @brief 100 次 100ms 探测提供十秒有界启动窗口 / One hundred 100ms probes provide a bounded ten-second startup window.
    local attempt

    for ((attempt = 0; attempt < 100; ++attempt)); do
        if broker_is_healthy; then
            return 0
        fi
        sleep 0.1
    done
    return 1
}

# @brief 验收一个在静态 readiness 检查前后保持不变的 systemd generation /
# Validate a systemd generation that remains unchanged across static readiness checks.
# @return 稳定 generation 通过并记录 evidence 时为零 / Zero after a stable generation passes and evidence is recorded.
# @note 镜像内容在 ``select_published_image`` 中由 native verifier 静态验收；这里仅复核
#       Type=notify readiness、socket metadata 与稳定 InvocationID，绝不创建 Runtime。/
#       Image contents are statically validated by the native verifier in ``select_published_image``;
#       this step only rechecks Type=notify readiness, socket metadata, and a stable InvocationID,
#       and never creates a Runtime.
validate_current_broker_readiness() {
    local attempt
    local before_invocation_id
    local after_invocation_id

    for ((attempt = 1; attempt <= 3; ++attempt)); do
        wait_for_broker_healthy || return 1
        before_invocation_id="$(try_current_broker_invocation_id)" || return 1
        wait_for_broker_healthy || return 1
        after_invocation_id="$(try_current_broker_invocation_id)" || return 1
        if [[ "$before_invocation_id" != "$after_invocation_id" ]]; then
            note "readiness 验收期间 service generation 已变化；验收新 InvocationID=$after_invocation_id"
            continue
        fi
        record_applied_fingerprint "$before_invocation_id"
        BROKER_VALIDATED_INVOCATION_ID="$before_invocation_id"
        return 0
    done
    return 1
}

# @brief 启动或恢复 systemd broker / Start or recover the systemd broker.
start_service() {
    local invocation_id

    sudo systemctl enable "$LXCFS_SERVICE_NAME" "$SERVICE_NAME"
    if broker_is_healthy; then
        if [[ "$BROKER_RESTART_REQUIRED" == false ]]; then
            invocation_id="$(current_broker_invocation_id)"
            if [[ "$invocation_id" == "$BROKER_VALIDATED_INVOCATION_ID" ]]; then
                note "已就绪且本轮 invocation 已通过静态 readiness 验收: $SERVICE_NAME ($SOCKET_PATH)"
                return 0
            fi
            note "当前 invocation 尚无静态 readiness evidence；开始验收"
            validate_current_broker_readiness \
                || die "broker generation 未能稳定通过静态 readiness 验收"
            return 0
        fi
        note "检测到 broker artifact/config/image digest 变更，重启 $SERVICE_NAME"
        sudo systemctl restart "$SERVICE_NAME"
    else
        note "启动或恢复 $SERVICE_NAME"
        sudo systemctl start "$SERVICE_NAME"
    fi
    wait_for_broker_healthy \
        || {
            sudo systemctl --no-pager --full status "$LXCFS_SERVICE_NAME" || true
            sudo systemctl --no-pager --full status "$SERVICE_NAME" || true
            sudo journalctl --unit "$LXCFS_SERVICE_NAME" --lines 100 --no-pager \
                --output short-precise || true
            sudo journalctl --unit "$SERVICE_NAME" --lines 100 --no-pager \
                --output short-precise || true
            die "broker 没有通过 service/socket 健康检查"
        }
    validate_current_broker_readiness \
        || {
            sudo journalctl --unit "$SERVICE_NAME" --lines 100 --no-pager \
                --output short-precise || true
            die "broker generation 未能稳定通过静态 readiness 验收"
        }
}

# @brief 显示 broker 状态 / Display broker status.
show_status() {
    sudo systemctl --no-pager --full status "$LXCFS_SERVICE_NAME" "$SERVICE_NAME"
}

# @brief 停止 broker；不会删除任何持久 workspace / Stop broker without deleting persistent workspaces.
stop_service() {
    sudo systemctl stop "$SERVICE_NAME"
    sudo systemctl stop "$LXCFS_SERVICE_NAME"
}

# @brief 获取 checkout-local control-plane 锁 / Acquire the checkout-local control-plane lock.
# @return 成功时返回零 / Zero on success.
acquire_control_lock() {
    mkdir -p "$REPOSITORY_ROOT/.runtime"
    exec 9>"$LOCK_FILE"
    flock 9
}

# @brief 仅准备 OCI publisher/verifier host tools，不创建 runtime state 或 Python wheel / Prepare only OCI publisher/verifier host tools, without runtime state or the Python wheel.
# @return 成功时返回零 / Zero on success.
# @note publish-wspctl-rootfs.sh 只能调用该窄阶段；它不接触 lxcfs、loopback、systemd unit、
#       control-plane directory 或 client wheel。/
#       publish-wspctl-rootfs.sh may call only this narrow phase; it never touches lxcfs,
#       loopback, systemd units, control-plane directories, or the client wheel.
prepare_host_tools() {
    require_work_root
    require_receipt_commands
    acquire_control_lock
    acquire_host_install_lock
    ensure_publisher_host_artifacts
}

# @brief 准备普通 Python wheel 与完整 host runtime tools，不创建 state 或启动 daemon / Prepare the regular Python wheel and complete host runtime tools without state or daemon startup.
# @return 成功时返回零 / Zero on success.
prepare() {
    prepare_host_tools
    [[ -x "$CLIENT_RECONCILER" ]] \
        || die "缺少普通 wheel reconciler: $CLIENT_RECONCILER"
    "$CLIENT_RECONCILER" --venv "$VENV_DIR"
    ensure_runtime_host_artifacts
}

# @brief 执行启动流程 / Execute the start flow.
# @return 成功时返回零 / Zero on success.
start() {
    require_work_root
    require_uid "$CLIENT_UID" "WSPCTL_CLIENT_UID"
    require_uid "$OPERATOR_UID" "WSPCTL_OPERATOR_UID"
    [[ "$CLIENT_UID" != "$OPERATOR_UID" ]] \
        || die "WSPCTL_OPERATOR_UID 必须与 WSPCTL_CLIENT_UID 不同；不要给 Bot operator 权限"
    require_loop_size "$LOOP_SIZE"
    require_positive_decimal "$MEMORY_MAX_BYTES" "WSPCTL_MEMORY_MAX"
    require_positive_decimal "$MEMORY_HIGH_BYTES" "WSPCTL_MEMORY_HIGH"
    require_decimal "$MEMORY_SWAP_MAX_BYTES" "WSPCTL_MEMORY_SWAP_MAX"
    require_positive_decimal "$TMP_SIZE_BYTES" "WSPCTL_TMP_SIZE_BYTES"
    require_positive_decimal "$CPU_MAX_US" "WSPCTL_CPU_MAX_US"
    require_positive_decimal "$CPU_PERIOD_US" "WSPCTL_CPU_PERIOD_US"
    require_positive_decimal "$WORKSPACE_HARD_BYTES" "WSPCTL_RUNTIME_WORKSPACE_HARD_BYTES"
    (( MEMORY_HIGH_BYTES <= MEMORY_MAX_BYTES )) \
        || die "WSPCTL_MEMORY_HIGH 不得高于 WSPCTL_MEMORY_MAX"
    resolve_io_weight
    prepare
    require_runtime_commands
    prepare_control_plane_directories
    ensure_loopback_state_mount
    require_state_mount
    select_published_image
    install_service_configuration
    prepare_restart_decision
    start_service
}

# @brief 显示脚本用法 / Display script usage.
show_help() {
    cat <<'EOF'
用法: scripts/start-wspctld.sh [start|prepare|prepare-host-tools|status|stop|help]

本脚本是 ./install.sh 的内部 host-broker 阶段。start（默认）会在
./.wspctl/state.xfs.img 首次创建预分配的 loopback XFS（32G），
以 prjquota 挂载到 ./.wspctl/state，验证已显式发布且
按 OCI manifest digest 固定的 workspace image，再 enable/start systemd service。
prepare 仅验证或按需构建普通 wheel 与完整 host tools，不创建 state、不发布 image、也不启动 service。
prepare-host-tools 是 OCI publisher 专用窄阶段，只验证或按需构建 wspctl-image 与 publisher；
它不创建 state、不安装 client wheel、也不检查 lxcfs/loopback/runtime 前提。
正常安装请运行 ./install.sh；日常 Bot 启动不会调用本脚本。

环境变量：
  WSPCTL_CLIENT_UID   broker 接受的 Bot UID；默认当前运行 runBot.sh 的 UID。
  WSPCTL_OPERATOR_UID 独立 operator UID；默认 root。使用 sudo wspctl 查询，且不得等于 Bot UID。
  WSPCTL_WORK_ROOT    不支持覆盖；特权 control plane 固定为 checkout 的 ./.wspctl。
  WSPCTL_IMAGE_DIGEST 可选 sha256:<64hex> OCI manifest digest；默认读取已发布 current-image-digest。
  WSPCTL_LOOP_SIZE    首次创建 image 的容量；默认 32G，已有 image 不会自动 resize。
  WSPCTL_IO_WEIGHT    auto（默认）按 host cgroup v2 capability 选择 100 或 0；也可显式设为 0..10000。
  WSPCTL_MEMORY_MAX   每个 Runtime memory.max；默认 4294967296（4 GiB）。
  WSPCTL_MEMORY_HIGH  每个 Runtime memory.high；默认 4294967296（4 GiB）。
  WSPCTL_MEMORY_SWAP_MAX 每个 Runtime memory.swap.max；默认 2147483648（2 GiB）。
  WSPCTL_TMP_SIZE_BYTES 私有 /tmp tmpfs 上限；默认 1073741824（1 GiB）。
  WSPCTL_CPU_MAX_US   cpu.max quota；默认 200000。
  WSPCTL_CPU_PERIOD_US cpu.max period；默认 100000，即最多 2 CPUs。
  WSPCTL_RUNTIME_WORKSPACE_HARD_BYTES 持久 Workspace hard limit；默认 4294967296（4 GiB）。
EOF
}

# @brief 分派命令行入口 / Dispatch the command-line entrypoint.
# @param $@ 命令行参数 / Command-line arguments.
# @return 命令退出状态 / Command exit status.
main() {
    case "${1:-start}" in
        start)
            start
            ;;
        prepare)
            prepare
            ;;
        prepare-host-tools)
            prepare_host_tools
            ;;
        status)
            show_status
            ;;
        stop)
            stop_service
            ;;
        help|--help|-h)
            show_help
            ;;
        *)
            die "未知命令: $1（可用 start、prepare、prepare-host-tools、status、stop、help）"
            ;;
    esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
