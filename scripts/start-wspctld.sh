#!/usr/bin/env bash

# @brief 启动本 checkout 的 wspctld 开发服务 / Start the checkout-local wspctld development service.
#
# 这个脚本是开发机 control plane 的唯一入口：它在缺少时构建 pybind11 editable
# client 与 host binaries，发布缺少的只读 generation，并只通过 systemd 管理 daemon。
# Bot 本身从不获得 sudo，也不直接执行此脚本。/ This is the sole development-machine
# control-plane entrypoint: it builds missing pybind11 editable-client and host binaries,
# publishes a missing readonly generation, and manages the daemon exclusively through
# systemd. The Bot never receives sudo and never invokes this script directly.

set -euo pipefail

# @brief 脚本所在仓库根 / Repository root containing this script.
REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# @brief 项目 virtual environment / Project virtual environment.
VENV_DIR="$REPOSITORY_ROOT/.venv"
# @brief 受控 Python 解释器 / Controlled Python interpreter.
PYTHON_EXECUTABLE="$VENV_DIR/bin/python"
# @brief checkout-local wspctl control-plane root / Checkout-local wspctl control-plane root.
WORK_ROOT="$REPOSITORY_ROOT/.wspctl"
# @brief XFS persistent-state mountpoint / XFS persistent-state mountpoint.
STATE_ROOT="$WORK_ROOT/state"
# @brief checkout-local loopback XFS image / Checkout-local loopback XFS image.
LOOP_IMAGE="$WORK_ROOT/state.xfs.img"
# @brief 首次创建 loopback image 的容量 / Capacity used when initially creating the loopback image.
LOOP_SIZE="${WSPCTL_LOOP_SIZE:-32G}"
# @brief readonly generation publication root / Readonly generation publication root.
IMAGES_ROOT="$WORK_ROOT/images"
# @brief root-owned source generation store / Root-owned source generation store.
ARTIFACT_ROOT="$WORK_ROOT/artifacts"
# @brief CMake build directory for host tools / CMake build directory for host tools.
BUILD_DIRECTORY="$REPOSITORY_ROOT/build/wspctld-dev"
# @brief host service unit name / Host service-unit name.
SERVICE_NAME="wspctld.service"
# @brief expected client UID; default is direct host runBot user / Expected client UID; default is the direct-host runBot user.
CLIENT_UID="${WSPCTL_CLIENT_UID:-$(id -u)}"
# @brief 可选的 operator 指定 generation 名 / Optional operator-specified generation name.
GENERATION="${WSPCTL_GENERATION:-}"
# @brief checkout-local lifecycle lock / Checkout-local lifecycle lock.
LOCK_FILE="$REPOSITORY_ROOT/.runtime/wspctld-control.lock"
# @brief 最近一次已应用 broker 配置的 fingerprint / Fingerprint of the most recently applied broker configuration.
FINGERPRINT_FILE="$REPOSITORY_ROOT/.runtime/wspctld-fingerprint"
# @brief 最近一次成功同步 editable native client 的输入 fingerprint / Input fingerprint of the most recently synchronized editable native client.
EDITABLE_FINGERPRINT_FILE="$REPOSITORY_ROOT/.runtime/wspctl-editable-fingerprint"
# @brief 由本 checkout 安装的 host artifacts 清单 / Manifest of host artifacts installed by this checkout.
INSTALL_MANIFEST_FILE="$WORK_ROOT/install-manifest"
# @brief daemon socket path / Daemon socket path.
SOCKET_PATH="$WORK_ROOT/run/wspctld.sock"
# @brief 本次启动是否必须重启 service / Whether this invocation must restart the service.
BROKER_RESTART_REQUIRED=false
# @brief 等待健康检查成功后记录的配置 fingerprint / Configuration fingerprint recorded only after a healthy check.
BROKER_FINGERPRINT=""

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
require_uid() {
    [[ "$1" =~ ^[0-9]+$ ]] || die "WSPCTL_CLIENT_UID 必须是十进制 UID"
}

# @brief 检验 generation 仅能作为单一路径成员 / Validate that generation can only be one path component.
# @param $1 generation 名称 / Generation name.
require_generation() {
    [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] \
        || die "WSPCTL_GENERATION 只能包含字母、数字、点、下划线和连字符，且不能以点开头"
}

# @brief 校验首次创建 loopback image 的容量拼写 / Validate the capacity spelling for initial loopback-image creation.
# @param $1 容量文本 / Capacity text.
require_loop_size() {
    [[ "$1" =~ ^[1-9][0-9]*[KMGTP]$ ]] \
        || die "WSPCTL_LOOP_SIZE 必须是类似 20G 的正整数 IEC 容量"
}

# @brief 验证开发机的基础命令 / Verify development-machine prerequisite commands.
require_commands() {
    local command_name
    for command_name in cmake sudo systemctl findmnt mountpoint mount install readelf ldconfig bash sha256sum flock grep tr stat awk fallocate losetup mkfs.xfs blkid find sort xargs; do
        command -v "$command_name" >/dev/null 2>&1 \
            || die "缺少必需命令: $command_name"
    done
}

# @brief 计算会影响 editable native module 的输入 fingerprint / Compute the inputs fingerprint for the editable native module.
# @return SHA-256 fingerprint / SHA-256 fingerprint.
editable_input_fingerprint() {
    {
        printf 'editable-native-v1\n'
        sha256sum "$REPOSITORY_ROOT/pyproject.toml" "$REPOSITORY_ROOT/CMakeLists.txt" "$REPOSITORY_ROOT/src/wspctl/CMakeLists.txt"
        find "$REPOSITORY_ROOT/src/wspctl" -type f -print0 | sort -z | xargs -0 sha256sum
    } | sha256sum | awk '{print $1}'
}

# @brief 判定当前 virtual environment 能否导入 native client / Determine whether the current virtual environment can import the native client.
# @return 0 表示可导入 / Zero when importable.
native_client_is_importable() {
    "$PYTHON_EXECUTABLE" -c 'import wspctl._native' >/dev/null 2>&1
}

# @brief 建立或修复项目 virtual environment，并按输入增量同步 editable mapping / Create or repair the project virtual environment and synchronize the editable mapping only when inputs change.
ensure_editable_client() {
    local desired_fingerprint
    local previous_fingerprint=""

    if [[ ! -x "$PYTHON_EXECUTABLE" ]]; then
        command -v python >/dev/null 2>&1 || die "找不到 python，无法创建 $VENV_DIR"
        note "创建项目 virtual environment: $VENV_DIR"
        python -m venv "$VENV_DIR"
    fi
    "$PYTHON_EXECUTABLE" -c 'import sys; raise SystemExit(sys.version_info < (3, 14))' \
        || die "项目 virtual environment 必须使用 Python 3.14 或更新版本"
    desired_fingerprint="$(editable_input_fingerprint)"
    if [[ -r "$EDITABLE_FINGERPRINT_FILE" ]]; then
        previous_fingerprint="$(<"$EDITABLE_FINGERPRINT_FILE")"
    fi
    if native_client_is_importable && [[ "$desired_fingerprint" == "$previous_fingerprint" ]]; then
        note "editable Python client 已就绪；跳过 pip"
        return 0
    fi
    note "同步发生变化或缺失的 editable Python client（含 wspctl._native）"
    "$PYTHON_EXECUTABLE" -m pip install --editable "$REPOSITORY_ROOT"
    native_client_is_importable \
        || die "editable 安装没有产生可导入的 wspctl._native"
    local temporary_fingerprint_file="$EDITABLE_FINGERPRINT_FILE.$$.tmp"
    printf '%s\n' "$desired_fingerprint" > "$temporary_fingerprint_file"
    mv -f -- "$temporary_fingerprint_file" "$EDITABLE_FINGERPRINT_FILE"
}

# @brief 配置、编译并安装 host broker 工件 / Configure, build, and install host-broker artifacts.
ensure_host_artifacts() {
    note "配置并构建 wspctld / wsp-systemd / wspctl-image"
    cmake -S "$REPOSITORY_ROOT" -B "$BUILD_DIRECTORY" \
        -DPython_EXECUTABLE="$PYTHON_EXECUTABLE" \
        -DWSPCTL_INSTALL_HOST_TOOLS=ON \
        -DWSPCTL_ALLOW_INSECURE_DEVELOPMENT_ROOT=ON \
        -DWSPCTL_HOST_WORKDIR="$WORK_ROOT" \
        -DCMAKE_INSTALL_PREFIX=/usr/local
    cmake --build "$BUILD_DIRECTORY" --parallel
    sudo cmake --install "$BUILD_DIRECTORY"

    [[ -x "$BUILD_DIRECTORY/src/wspctl/wspctld" ]] \
        || die "CMake 没有产生 wspctld"
    [[ -x "$BUILD_DIRECTORY/src/wspctl/wsp-systemd" ]] \
        || die "CMake 没有产生 wsp-systemd"
    [[ -x "$BUILD_DIRECTORY/src/wspctl/wspctl-image" ]] \
        || die "CMake 没有产生 wspctl-image"
    write_install_manifest
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
    local artifact_paths=(
        /usr/local/bin/wspctld
        /usr/local/bin/wspctl-image
        /usr/local/libexec/wspctl/wsp-systemd
        /usr/local/share/fogmoe-wspctl/systemd/wspctld.service
        /usr/local/share/fogmoe-wspctl/systemd/wspctld.env.example
    )

    : > "$temporary_file"
    for artifact_path in "${artifact_paths[@]}"; do
        [[ -f "$artifact_path" || -x "$artifact_path" ]] \
            || die "CMake install 未产生预期 host artifact: $artifact_path"
        artifact_checksum="$(sha256sum "$artifact_path" | awk '{print $1}')"
        printf 'artifact %s %s\n' "$artifact_path" "$artifact_checksum" >> "$temporary_file"
    done
    sudo install -o root -g root -m 0600 "$temporary_file" "$INSTALL_MANIFEST_FILE"
    rm -f -- "$temporary_file"
}

# @brief 计算默认 immutable generation 的 rootfs 输入 fingerprint / Compute the rootfs-input fingerprint for the default immutable generation.
#
# 不以整个 Git commit 命名：文档、脚本或无关业务提交不应触发昂贵的 rootfs rebuild。/
# This deliberately does not use the complete Git commit as its name: documentation, scripts,
# or unrelated application commits must not trigger an expensive rootfs rebuild.
# @return 短 generation 名 / Short generation name.
default_generation_name() {
    local rootfs_fingerprint

    rootfs_fingerprint="$(
        {
            printf 'rootfs-input-v1\n'
            sha256sum "$REPOSITORY_ROOT/pyproject.toml" "$REPOSITORY_ROOT/tools/build_wspctl_image.py"
            find "$REPOSITORY_ROOT/src" -type f ! -name '*.pyc' -print0 | sort -z | xargs -0 sha256sum
            "$PYTHON_EXECUTABLE" -m pip freeze --all | sort
            sha256sum "$BUILD_DIRECTORY/src/wspctl/wsp-systemd" "$BUILD_DIRECTORY/src/wspctl/wspctl-image"
        } | sha256sum | awk '{print $1}'
    )"
    printf 'dev-%s\n' "${rootfs_fingerprint:0:12}"
}

# @brief 在 build 完成后选择 operator generation 或输入寻址 generation / Select the operator generation or an input-addressed generation after the build completes.
select_generation() {
    if [[ -z "$GENERATION" ]]; then
        GENERATION="$(default_generation_name)"
    fi
    require_generation "$GENERATION"
}

# @brief 在首次开发启动时创建并挂载 loopback XFS / Create and mount the loopback XFS on the first development start.
#
# 镜像放在 state mountpoint 的同级目录，因而不会被自身挂载遮蔽。已存在的镜像绝不重新
# format：若其不是 XFS，直接失败而不是猜测使用者是否愿意丢失数据。/
# The image lives beside the state mountpoint and is therefore never hidden by its own mount.
# An existing image is never reformatted: if it is not XFS, fail rather than guessing whether
# the user agrees to lose data.
ensure_loopback_state_mount() {
    local loop_device=""
    local filesystem_type=""
    local created_image=false

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
        loop_device="$(sudo losetup --find --show "$LOOP_IMAGE")"
    fi
    [[ "$loop_device" = /dev/loop* ]] \
        || die "无法为 loopback image 获得 loop device: $LOOP_IMAGE"

    if [[ "$created_image" == true ]]; then
        sudo mkfs.xfs "$loop_device"
    else
        filesystem_type="$(sudo blkid --output value --tag TYPE "$loop_device" 2>/dev/null || true)"
        [[ "$filesystem_type" == "xfs" ]] \
            || die "已有 loopback image 不是 XFS；拒绝重新格式化: $LOOP_IMAGE"
    fi

    sudo install -d -o root -g root -m 0700 "$STATE_ROOT"
    note "挂载 loopback XFS project-quota state: $loop_device -> $STATE_ROOT"
    sudo mount -t xfs -o rw,prjquota "$loop_device" "$STATE_ROOT"
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
    sudo install -d -o root -g root -m 0700 "$IMAGES_ROOT" "$ARTIFACT_ROOT"
}

# @brief 解析必须显式许可的 GNU command 路径 / Resolve an explicitly allow-listed GNU-command path.
# @param $1 命令名 / Command name.
# @return 绝对命令路径 / Absolute command path.
gnu_command_path() {
    local command_path
    command_path="$(command -v "$1")" || die "rootfs 缺少 GNU command: $1"
    [[ "$command_path" = /* ]] || die "GNU command 不是绝对路径: $1"
    printf '%s\n' "$command_path"
}

# @brief 只在 generation 缺失时构建不可变 rootfs / Build an immutable rootfs only when its generation is absent.
ensure_generation() {
    local source_root="$ARTIFACT_ROOT/$GENERATION/rootfs"
    local publish_root="$IMAGES_ROOT/$GENERATION/rootfs"
    local command_name
    local gnu_arguments=()

    if ! sudo test -d "$source_root"; then
        note "构建缺失的 immutable generation: $GENERATION"
        for command_name in env cat chmod cp find grep ls mkdir rm sed tail tee touch wc; do
            gnu_arguments+=(--gnu-command "$(gnu_command_path "$command_name")")
        done
        sudo "$PYTHON_EXECUTABLE" "$REPOSITORY_ROOT/tools/build_wspctl_image.py" \
            --generation "$GENERATION" \
            --output-root "$ARTIFACT_ROOT" \
            --venv "$VENV_DIR" \
            --python-source "$REPOSITORY_ROOT/src" \
            --bash "$(command -v bash)" \
            "${gnu_arguments[@]}" \
            --wsp-systemd "$BUILD_DIRECTORY/src/wspctl/wsp-systemd" \
            --sealer "$BUILD_DIRECTORY/src/wspctl/wspctl-image" \
            --readelf "$(command -v readelf)" \
            --ldconfig "$(command -v ldconfig)" \
            --allow-insecure-development-output
    fi

    sudo test -d "$source_root" || die "rootfs builder 未产生: $source_root"
    sudo install -d -o root -g root -m 0700 "$publish_root"
    if ! sudo mountpoint -q "$publish_root"; then
        note "发布 readonly generation: $GENERATION"
        sudo mount --bind "$source_root" "$publish_root"
        sudo mount -o remount,bind,ro "$publish_root"
    fi
    sudo findmnt --noheadings --output OPTIONS --target "$publish_root" \
        | grep --extended-regexp --quiet '(^|,)ro(,|$)' \
        || die "发布 generation 必须是实际 readonly bind mount: $publish_root"
}

# @brief 安装生成的 unit 与受控 environment file / Install generated unit and the controlled environment file.
install_service_configuration() {
    local unit_source="$BUILD_DIRECTORY/deploy/wspctl/systemd/wspctld.service"
    local environment_template="$BUILD_DIRECTORY/deploy/wspctl/systemd/wspctld.env.example"
    local environment_file="$WORK_ROOT/wspctld.env"
    local base_root="$IMAGES_ROOT/$GENERATION/rootfs"
    local filesystem_bytes
    local filesystem_inodes
    local reserve_bytes
    local reserve_inodes
    local admission_bytes
    local admission_inodes

    [[ -f "$unit_source" && -f "$environment_template" ]] \
        || die "CMake 没有生成 systemd 部署资产"
    sudo install -o root -g root -m 0644 "$unit_source" "/etc/systemd/system/$SERVICE_NAME"
    if [[ ! -f "$environment_file" ]]; then
        sudo install -o root -g root -m 0600 "$environment_template" "$environment_file"
    fi
    sudo sed --in-place --regexp-extended \
        "s|^WSPCTL_CLIENT_UID=.*$|WSPCTL_CLIENT_UID=$CLIENT_UID|" \
        "$environment_file"
    sudo sed --in-place --regexp-extended \
        "s|^WSPCTL_BASE_ROOT=.*$|WSPCTL_BASE_ROOT=$base_root|" \
        "$environment_file"
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
    (( admission_bytes >= 1090519040 && admission_inodes >= 139264 )) \
        || die "loopback XFS 太小，至少需要容纳一个 1 GiB workspace 配额与 control layer"
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
    sudo chown root:root "$environment_file"
    sudo chmod 0600 "$environment_file"
    sudo systemctl daemon-reload
}

# @brief 计算会影响正在运行 broker 的配置 fingerprint / Compute a fingerprint of inputs that affect the running broker.
# @return SHA-256 fingerprint / SHA-256 fingerprint.
broker_fingerprint() {
    local unit_source="$BUILD_DIRECTORY/deploy/wspctl/systemd/wspctld.service"
    local environment_file="$WORK_ROOT/wspctld.env"

    {
        printf 'generation=%s\nclient_uid=%s\n' "$GENERATION" "$CLIENT_UID"
        sha256sum "$BUILD_DIRECTORY/src/wspctl/wspctld" "$unit_source"
        sudo sha256sum "$environment_file"
    } | sha256sum | awk '{print $1}'
}

# @brief 标记实际配置变更，避免无变化时丢失 activation cache / Mark a real configuration change without discarding activation cache on no-op starts.
prepare_restart_decision() {
    local desired_fingerprint
    local previous_fingerprint=""

    desired_fingerprint="$(broker_fingerprint)"
    if [[ -r "$FINGERPRINT_FILE" ]]; then
        previous_fingerprint="$(<"$FINGERPRINT_FILE")"
    fi
    if [[ "$desired_fingerprint" != "$previous_fingerprint" ]]; then
        BROKER_RESTART_REQUIRED=true
    fi
    BROKER_FINGERPRINT="$desired_fingerprint"
}

# @brief 在 broker 已健康后原子记录已应用 fingerprint / Atomically record the applied fingerprint after broker health succeeds.
record_applied_fingerprint() {
    local temporary_file="$FINGERPRINT_FILE.$$.tmp"

    printf '%s\n' "$BROKER_FINGERPRINT" > "$temporary_file"
    mv -f -- "$temporary_file" "$FINGERPRINT_FILE"
}

# @brief 判定 service 与 socket 是否均可用 / Determine whether both service and socket are usable.
# @return 0 表示健康 / Zero if healthy.
broker_is_healthy() {
    sudo systemctl is-active --quiet "$SERVICE_NAME" \
        && [[ -S "$SOCKET_PATH" ]] \
        && [[ "$(stat --format='%u:%a' "$SOCKET_PATH")" == "$CLIENT_UID:600" ]]
}

# @brief 启动或恢复 systemd broker / Start or recover the systemd broker.
start_service() {
    if broker_is_healthy; then
        if [[ "$BROKER_RESTART_REQUIRED" == false ]]; then
            note "已就绪: $SERVICE_NAME ($SOCKET_PATH)"
            record_applied_fingerprint
            return 0
        fi
        note "检测到 broker artifact/config/generation 变更，重启 $SERVICE_NAME"
        sudo systemctl restart "$SERVICE_NAME"
    else
        note "启动或恢复 $SERVICE_NAME"
        sudo systemctl start "$SERVICE_NAME"
    fi
    broker_is_healthy \
        || {
            sudo systemctl --no-pager --full status "$SERVICE_NAME" || true
            die "broker 没有通过 service/socket 健康检查"
        }
    record_applied_fingerprint
}

# @brief 显示 broker 状态 / Display broker status.
show_status() {
    sudo systemctl --no-pager --full status "$SERVICE_NAME"
}

# @brief 停止 broker；不会删除任何持久 workspace / Stop broker without deleting persistent workspaces.
stop_service() {
    sudo systemctl stop "$SERVICE_NAME"
}

# @brief 执行启动流程 / Execute the start flow.
start() {
    require_uid "$CLIENT_UID"
    require_loop_size "$LOOP_SIZE"
    require_commands
    mkdir -p "$REPOSITORY_ROOT/.runtime"
    exec 9>"$LOCK_FILE"
    flock 9
    prepare_control_plane_directories
    ensure_loopback_state_mount
    require_state_mount
    ensure_editable_client
    ensure_host_artifacts
    select_generation
    ensure_generation
    install_service_configuration
    prepare_restart_decision
    start_service
}

# @brief 显示脚本用法 / Display script usage.
show_help() {
    cat <<'EOF'
用法: scripts/start-wspctld.sh [start|status|stop|help]

start（默认）会在 ./.wspctl/state.xfs.img 首次创建预分配的 loopback XFS（32G），
以 prjquota 挂载到 ./.wspctl/state，构建缺失的 editable client、host binaries 与
immutable generation，再确保 systemd 的 wspctld.service 和 socket 可用。

环境变量：
  WSPCTL_CLIENT_UID   broker 接受的 Bot UID；默认当前运行 runBot.sh 的 UID。
  WSPCTL_GENERATION   可选 immutable generation 名；默认 rootfs 输入指纹的 dev-<hash>。
  WSPCTL_LOOP_SIZE    首次创建 image 的容量；默认 32G，已有 image 不会自动 resize。
EOF
}

case "${1:-start}" in
    start)
        start
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
        die "未知命令: $1（可用 start、status、stop、help）"
        ;;
esac
