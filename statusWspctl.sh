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
# @brief readonly generation publication root / Readonly generation publication root.
IMAGES_ROOT="$WORK_ROOT/images"
# @brief daemon socket path / Daemon socket path.
SOCKET_PATH="$WORK_ROOT/run/wspctld.sock"
# @brief broker service name / Broker service name.
SERVICE_NAME="wspctld.service"
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
        --property=MemoryCurrent --property=TasksCurrent --property=ExecMainStatus 2>/dev/null || true)"
    if [[ -n "$service_properties" ]]; then
        printf '%s\n' "$service_properties"
    else
        warning "无法读取 systemd service properties"
    fi
}

# @brief 报告 Unix socket 权限 / Report Unix-socket permissions.
report_socket() {
    local socket_metadata

    heading "control socket"
    if [[ ! -S "$SOCKET_PATH" ]]; then
        warning "socket missing: $SOCKET_PATH"
        return 0
    fi
    socket_metadata="$(stat --format='uid=%u gid=%g mode=%a size=%s' "$SOCKET_PATH" 2>/dev/null || true)"
    [[ -n "$socket_metadata" ]] || {
        warning "无法读取 socket metadata"
        return 0
    }
    ok "$SOCKET_PATH ($socket_metadata)"
}

# @brief 报告 loop image、关联 device 与 XFS mount / Report loop image, associated device, and XFS mount.
report_storage() {
    local loop_devices
    local mount_description

    heading "loopback XFS"
    if sudo test -f "$LOOP_IMAGE"; then
        ok "image $(sudo du --block-size=1 "$LOOP_IMAGE" | awk '{print $1 " bytes allocated"}')"
        loop_devices="$(sudo losetup --associated "$LOOP_IMAGE" 2>/dev/null || true)"
        [[ -n "$loop_devices" ]] && printf '%s\n' "$loop_devices" || warning "image has no attached loop device"
    else
        warning "loop image missing: $LOOP_IMAGE"
    fi
    if ! sudo mountpoint -q "$STATE_ROOT"; then
        warning "state is not mounted: $STATE_ROOT"
        return 0
    fi
    mount_description="$(sudo findmnt --noheadings --output SOURCE,FSTYPE,OPTIONS --target "$STATE_ROOT" 2>/dev/null || true)"
    [[ -n "$mount_description" ]] && ok "mount $mount_description" || warning "无法读取 state mount"
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

report_service
report_socket
report_storage
report_runtime_aggregates

if [[ "$HEALTHY" == true ]]; then
    printf '\nWSPCTL_STATUS=healthy\n'
    exit 0
fi
printf '\nWSPCTL_STATUS=degraded\n'
exit 1
