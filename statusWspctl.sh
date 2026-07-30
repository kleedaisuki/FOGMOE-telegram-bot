#!/usr/bin/env bash

# @brief 汇总本 checkout wspctl 开发控制平面的只读状态 / Summarize readonly state of this checkout's wspctl development control plane.
#
# 不读取 payload、journal 内容或 Bot 配置，也不请求 broker 执行任何 task。它只显示 operator
# 需要的 service、socket、mount、quota、容量和 aggregate runtime 计数。/
# It neither reads payload/journal contents nor Bot configuration, and never asks the broker to
# execute a task. It displays only operator-relevant service, socket, mount, quota, capacity,
# and aggregate runtime counts.

set -uo pipefail

# @brief 脚本所在仓库根 / Repository root containing this script.
REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# @brief checkout-local wspctl control-plane root / Checkout-local wspctl control-plane root.
WORK_ROOT="$REPOSITORY_ROOT/.wspctl"
# @brief persistent XFS state mountpoint / Persistent XFS state mountpoint.
STATE_ROOT="$WORK_ROOT/state"
# @brief loopback XFS image / Loopback XFS image.
LOOP_IMAGE="$WORK_ROOT/state.xfs.img"
# @brief readonly OCI image publication root / Readonly OCI image publication root.
IMAGES_ROOT="$WORK_ROOT/images"
# @brief 当前发布的 OCI manifest digest 记录 / Record of the currently published OCI manifest digest.
CURRENT_IMAGE_FILE="$WORK_ROOT/current-image-digest"
# @brief Bot 专属 daemon socket 路径 / Bot-exclusive daemon socket path.
SOCKET_PATH="$WORK_ROOT/run/bot/wspctld.sock"
# @brief root/operator 专属 daemon socket 路径 / Root/operator-exclusive daemon socket path.
OPERATOR_SOCKET_PATH="$WORK_ROOT/run/operator/wspctld.sock"
# @brief root-owned broker environment 文件 / Root-owned broker environment file.
ENVIRONMENT_FILE="$WORK_ROOT/wspctld.env"
# @brief broker service name / Broker service name.
SERVICE_NAME="wspctld.service"
# @brief checkout-local 生命周期日志目录 / Checkout-local lifecycle log directory.
LOG_DIR="$REPOSITORY_ROOT/logs"
# @brief 最近一次通过真实 runtime 执行验收的部署记录 / Deployment record for the last real runtime execution probe.
FINGERPRINT_FILE="$REPOSITORY_ROOT/.runtime/wspctld-fingerprint"
# @brief 整体健康状态 / Aggregate health status.
HEALTHY=true

# @brief 输出小节标题 / Print a section heading.
# @param $* 标题 / Heading.
heading() {
    printf '\n== %s ==\n' "$*"
}

# @brief 输出成功项目 / Print a healthy item.
# @param $* 文本 / Text.
ok() {
    printf 'OK   %s\n' "$*"
}

# @brief 输出警告并标记非健康 / Print a warning and mark aggregate health unhealthy.
# @param $* 文本 / Text.
warning() {
    printf 'WARN %s\n' "$*"
    HEALTHY=false
}

# @brief 输出信息项目 / Print an informational item.
# @param $* 文本 / Text.
info() {
    printf 'INFO %s\n' "$*"
}

# @brief 返回最新一轮完整 wspctl 安装日志 / Return the newest complete wspctl installation log.
# @return 成功时输出日志绝对路径；没有日志时非零 / Prints the absolute log path on success; nonzero when absent.
latest_install_log() {
    find "$LOG_DIR" -maxdepth 1 -type f -name 'wspctl_install_*.log' \
        -printf '%T@ %p\n' 2>/dev/null \
        | sort -nr \
        | head -n 1 \
        | cut -d' ' -f2-
}

# @brief 报告最近一次 build→publish→broker 安装日志 / Report the latest build-to-publish-to-broker installation log.
# @return 总是成功；日志缺失只作为信息 / Always succeeds; a missing log is informational.
report_install_log() {
    local install_log
    local log_metadata

    heading "installation log"
    if ! install_log="$(latest_install_log)" || [[ -z "$install_log" ]]; then
        info "尚无 wspctl_install 日志；下一次 ./installWspctl.sh 会完整记录三个安装阶段"
        return 0
    fi
    log_metadata="$(stat --format='owner=%U mode=%a size=%s modified=%y' "$install_log" 2>/dev/null || true)"
    info "latest=$install_log"
    [[ -n "$log_metadata" ]] && info "$log_metadata"
    info "查看末尾: tail -n 100 '$install_log'"
}

# @brief 报告 systemd service 及其主进程资源 / Report the systemd service and main-process resources.
report_service() {
    local service_properties

    heading "broker service"
    if sudo systemctl is-active --quiet "$SERVICE_NAME"; then
        ok "$SERVICE_NAME active"
    else
        warning "$SERVICE_NAME inactive"
    fi
    service_properties="$(sudo systemctl show "$SERVICE_NAME" \
        --property=MainPID --property=ActiveState --property=SubState \
        --property=MemoryCurrent --property=TasksCurrent --property=ExecMainStatus \
        --property=NRestarts --property=RestartPreventExitStatus \
        --property=Type --property=NotifyAccess --property=TimeoutStartUSec 2>/dev/null || true)"
    if [[ -n "$service_properties" ]]; then
        printf '%s\n' "$service_properties"
    else
        warning "无法读取 systemd service properties"
    fi
}

# @brief 只读核对当前 service generation 是否通过真实 runtime 执行验收 /
# Read-only verification that the current service generation passed a real runtime execution probe.
# @return 总是零；缺失或过期 evidence 会标记 degraded / Always zero; missing or stale evidence marks degraded.
report_execution_validation() {
    local current_invocation_id
    local validated_invocation_id=""
    local record_key
    local record_value

    heading "runtime execution validation"
    current_invocation_id="$(
        sudo systemctl show "$SERVICE_NAME" \
            --property=InvocationID \
            --value 2>/dev/null || true
    )"
    if [[ ! "$current_invocation_id" =~ ^[0-9a-fA-F]{32}$ ]]; then
        warning "无法读取当前 $SERVICE_NAME InvocationID"
        return 0
    fi
    if [[ -r "$FINGERPRINT_FILE" ]]; then
        while IFS='=' read -r record_key record_value; do
            if [[ "$record_key" == invocation_id ]]; then
                validated_invocation_id="$record_value"
            fi
        done < "$FINGERPRINT_FILE"
    fi
    if [[ "$validated_invocation_id" != "$current_invocation_id" ]]; then
        warning "当前 invocation 尚无真实 runtime 执行验收 evidence；重新运行 ./installWspctl.sh"
        return 0
    fi
    ok "InvocationID=$current_invocation_id passed /bin/true runtime canary"
}

# @brief 报告一个 Unix socket 权限 / Report one Unix-socket permission boundary.
# @param $1 显示名称 / Display name.
# @param $2 socket 路径 / Socket path.
# @param $3 期望 owner UID；空值表示只报告 / Expected owner UID; empty means report only.
report_socket() {
    local label="$1"
    local socket_path="$2"
    local expected_uid="$3"
    local socket_metadata
    local actual_uid
    local actual_mode

    heading "$label socket"
    if ! sudo test -S "$socket_path"; then
        warning "socket missing: $socket_path"
        return 0
    fi
    socket_metadata="$(sudo stat --format='uid=%u gid=%g mode=%a size=%s' "$socket_path" 2>/dev/null || true)"
    [[ -n "$socket_metadata" ]] || {
        warning "无法读取 socket metadata"
        return 0
    }
    actual_uid="$(sudo stat --format='%u' "$socket_path" 2>/dev/null || true)"
    actual_mode="$(sudo stat --format='%a' "$socket_path" 2>/dev/null || true)"
    if [[ "$actual_mode" != 600 || ( -n "$expected_uid" && "$actual_uid" != "$expected_uid" ) ]]; then
        warning "$socket_path has unexpected ownership/mode ($socket_metadata)"
        return 0
    fi
    ok "$socket_path ($socket_metadata)"
}

# @brief 从 root-owned broker 配置读取精确 Bot UID / Read the exact Bot UID from root-owned broker configuration.
# @return 成功时输出十进制 UID；缺失或非法时非零 / Prints a decimal UID on success; nonzero when missing or invalid.
configured_client_uid() {
    local client_uid

    sudo test -f "$ENVIRONMENT_FILE" || return 1
    client_uid="$(sudo awk -F= '$1 == "WSPCTL_CLIENT_UID" { value = $2 } END { print value }' "$ENVIRONMENT_FILE" 2>/dev/null)"
    [[ "$client_uid" =~ ^[0-9]+$ ]] || return 1
    printf '%s\n' "$client_uid"
}

# @brief 从 root-owned 配置或 current 记录读取 OCI manifest digest / Read the OCI manifest digest from root-owned configuration or the current record.
# @return 成功时输出规范 sha256 digest；缺失或非法时非零 / Prints a canonical sha256 digest on success; nonzero when missing or invalid.
configured_image_digest() {
    local image_digest=""

    if sudo test -f "$ENVIRONMENT_FILE"; then
        image_digest="$(sudo awk -F= '$1 == "WSPCTL_IMAGE_DIGEST" { value = $2 } END { print value }' "$ENVIRONMENT_FILE" 2>/dev/null)"
    fi
    if [[ -z "$image_digest" ]] && sudo test -f "$CURRENT_IMAGE_FILE"; then
        image_digest="$(sudo cat "$CURRENT_IMAGE_FILE" 2>/dev/null)"
    fi
    [[ "$image_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || return 1
    printf '%s\n' "$image_digest"
}

# @brief 报告 broker 选择的显式 OCI image 及其 native contract / Report the explicit OCI image selected by the broker and its native contract.
report_image() {
    local image_digest
    local digest_hex
    local rootfs
    local mount_options
    local verifier="/usr/local/bin/wspctl-image"
    local verification

    heading "workspace OCI image"
    if ! image_digest="$(configured_image_digest)"; then
        warning "未配置有效 WSPCTL_IMAGE_DIGEST，也没有 current-image-digest"
        return 0
    fi
    digest_hex="${image_digest#sha256:}"
    rootfs="$IMAGES_ROOT/sha256/$digest_hex/rootfs"
    info "source_oci_manifest_digest=$image_digest"
    if ! sudo test -d "$rootfs"; then
        warning "published rootfs missing: $rootfs"
        return 0
    fi
    if ! sudo mountpoint -q "$rootfs"; then
        warning "rootfs 不是独立 mountpoint: $rootfs"
        return 0
    fi
    mount_options="$(sudo findmnt --noheadings --output OPTIONS --target "$rootfs" 2>/dev/null || true)"
    if [[ ",$mount_options," != *,ro,* ]]; then
        warning "rootfs mount 不是 readonly: $rootfs ($mount_options)"
        return 0
    fi
    if ! sudo test -x "$verifier"; then
        warning "native image verifier missing: $verifier"
        return 0
    fi
    verification="$(sudo "$verifier" --verify true --base-root "$rootfs" --images-root "$IMAGES_ROOT" 2>&1)" || {
        warning "native image contract verification failed: $verification"
        return 0
    }
    if [[ "$verification" != *"source_oci_manifest_digest=$image_digest"* ]]; then
        warning "native manifest identity 与配置 digest 不一致"
        return 0
    fi
    ok "$rootfs (readonly; native contract verified)"
    printf '%s\n' "$verification"
}

# @brief 同时报告 Bot 与 operator socket 的独立 ACL 边界 / Report the independent ACL boundaries of Bot and operator sockets.
report_sockets() {
    local client_uid

    if ! client_uid="$(configured_client_uid)"; then
        warning "无法从 $ENVIRONMENT_FILE 读取有效 WSPCTL_CLIENT_UID"
        return 0
    fi
    report_socket "Bot control" "$SOCKET_PATH" "$client_uid"
    report_socket "operator control" "$OPERATOR_SOCKET_PATH" "0"
}

# @brief 报告 loop image、关联 device 与 XFS mount / Report loop image, associated device, and XFS mount.
report_storage() {
    local loop_devices
    local mount_description

    heading "loopback XFS"
    if sudo test -f "$LOOP_IMAGE"; then
        ok "image $(sudo du --block-size=1 "$LOOP_IMAGE" | awk '{print $1 " bytes allocated"}')"
        loop_devices="$(sudo losetup --associated "$LOOP_IMAGE" 2>/dev/null || true)"
        if [[ -n "$loop_devices" ]]; then
            printf '%s\n' "$loop_devices"
        else
            warning "image has no attached loop device"
        fi
    else
        warning "loop image missing: $LOOP_IMAGE"
    fi
    if ! sudo mountpoint -q "$STATE_ROOT"; then
        warning "state is not mounted: $STATE_ROOT"
        return 0
    fi
    mount_description="$(sudo findmnt --noheadings --output SOURCE,FSTYPE,OPTIONS --target "$STATE_ROOT" 2>/dev/null || true)"
    if [[ -n "$mount_description" ]]; then
        ok "mount $mount_description"
    else
        warning "无法读取 state mount"
    fi
    if [[ "$mount_description" != *xfs* || ( "$mount_description" != *prjquota* && "$mount_description" != *pquota* ) || "$mount_description" == *pqnoenforce* ]]; then
        warning "state mount 不满足强制 XFS project-quota contract"
    fi
    sudo df --human-readable --output=size,used,avail,pcent,target "$STATE_ROOT" 2>/dev/null || warning "无法读取 state block capacity"
    sudo df --human-readable --inodes --output=itotal,iused,iavail,ipcent,target "$STATE_ROOT" 2>/dev/null || warning "无法读取 state inode capacity"
    sudo xfs_quota -x -c 'state -p' "$STATE_ROOT" 2>/dev/null || warning "无法读取 XFS project-quota accounting/enforcement"
}

# @brief 报告不泄露 runtime identity 的持久状态聚合 / Report persistent-state aggregates without leaking runtime identities.
report_runtime_aggregates() {
    local runtime_count
    local record_count
    local journal_count
    local aggregate_paths=()

    heading "persistent runtime aggregates"
    if ! sudo mountpoint -q "$STATE_ROOT"; then
        warning "state 未挂载，无法读取 aggregate runtime 状态"
        return 0
    fi
    runtime_count="$(sudo find "$STATE_ROOT/runtimes" -mindepth 1 -maxdepth 1 -type d -printf . 2>/dev/null | wc -c)"
    record_count="$(sudo find "$STATE_ROOT/registry/records" -mindepth 1 -maxdepth 1 -type f -printf . 2>/dev/null | wc -c)"
    journal_count="$(sudo find "$STATE_ROOT/runtimes" -type f -path '*/control/journal/*' -printf . 2>/dev/null | wc -c)"
    info "runtime directories=$runtime_count registry records=$record_count journal records=$journal_count"
    sudo test -d "$STATE_ROOT/runtimes" && aggregate_paths+=("$STATE_ROOT/runtimes")
    sudo test -d "$STATE_ROOT/registry" && aggregate_paths+=("$STATE_ROOT/registry")
    if (( ${#aggregate_paths[@]} > 0 )); then
        sudo du --summarize --human-readable "${aggregate_paths[@]}" 2>/dev/null \
            || warning "无法读取 runtime/registry aggregate disk usage"
    fi
}

# @brief 状态汇总主入口 / Status-summary main entrypoint.
# @return 全部边界健康时为零，否则非零 / Zero when every boundary is healthy; nonzero otherwise.
main() {
    report_install_log
    report_service
    report_execution_validation
    report_sockets
    report_image
    report_storage
    report_runtime_aggregates

    if [[ "$HEALTHY" == true ]]; then
        printf '\nWSPCTL_STATUS=healthy\n'
        return 0
    fi
    printf '\nWSPCTL_STATUS=degraded\n'
    return 1
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
