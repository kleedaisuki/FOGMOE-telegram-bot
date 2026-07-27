#!/usr/bin/env bash

# @brief 卸载本 checkout 的 wspctl 开发控制平面 / Uninstall this checkout's wspctl development control plane.
#
# 默认保留 loopback image 和全部 persistent workspace，方便重新安装后恢复。只有明确的
# ``--purge`` 才会删除它们。/ By default this preserves the loopback image and all persistent
# workspaces so a later installation can recover them. Only explicit ``--purge`` deletes them.

set -euo pipefail

# @brief 脚本所在仓库根 / Repository root containing this script.
REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# @brief checkout-local wspctl control-plane root / Checkout-local wspctl control-plane root.
WORK_ROOT="$REPOSITORY_ROOT/.wspctl"
# @brief persistent XFS state mountpoint / Persistent XFS state mountpoint.
STATE_ROOT="$WORK_ROOT/state"
# @brief loopback XFS image path / Loopback XFS image path.
LOOP_IMAGE="$WORK_ROOT/state.xfs.img"
# @brief immutable-generation publication root / Immutable-generation publication root.
IMAGES_ROOT="$WORK_ROOT/images"
# @brief systemd service managed by the development launcher / Systemd service managed by the development launcher.
SERVICE_NAME="wspctld.service"
# @brief active systemd unit path / Active systemd unit path.
UNIT_PATH="/etc/systemd/system/$SERVICE_NAME"
# @brief generated environment file / Generated environment file.
ENVIRONMENT_FILE="$WORK_ROOT/wspctld.env"
# @brief host-artifact ownership manifest / Host-artifact ownership manifest.
INSTALL_MANIFEST_FILE="$WORK_ROOT/install-manifest"
# @brief 是否清除可恢复状态 / Whether to purge recoverable state.
PURGE_STATE=false

# @brief 输出错误并终止 / Print an error and terminate.
# @param $* 错误文本 / Error text.
die() {
    printf 'wspctl 卸载失败: %s\n' "$*" >&2
    exit 1
}

# @brief 输出普通进度 / Print normal progress.
# @param $* 进度文本 / Progress text.
note() {
    printf 'wspctl uninstall: %s\n' "$*"
}

# @brief 验证卸载需要的基础命令 / Verify prerequisite commands for uninstall.
require_commands() {
    local command_name

    for command_name in sudo systemctl grep findmnt mountpoint umount losetup sha256sum awk sort cut cat rm rmdir; do
        command -v "$command_name" >/dev/null 2>&1 \
            || die "缺少必需命令: $command_name"
    done
}

# @brief 验证开发 unit 的 work-root 绑定 / Verify that the active unit is bound to this development work root.
# @return 0 表示可以由本脚本管理 / Zero when this script may manage the unit.
is_checkout_unit() {
    sudo test -f "$UNIT_PATH" \
        && sudo grep --fixed-strings --quiet "ReadWritePaths=$WORK_ROOT /sys/fs/cgroup" "$UNIT_PATH"
}

# @brief 停止并移除仅属于本 checkout 的 unit / Stop and remove only the unit belonging to this checkout.
remove_checkout_unit() {
    if ! sudo test -e "$UNIT_PATH"; then
        return 0
    fi
    is_checkout_unit \
        || die "$UNIT_PATH 不属于 $WORK_ROOT；拒绝停止或删除其他 wspctld 安装"
    note "停止 $SERVICE_NAME"
    sudo systemctl stop "$SERVICE_NAME" || true
    sudo rm -f -- "$UNIT_PATH"
    sudo systemctl daemon-reload
}

# @brief 卸载只读 generation bind mounts，先处理最深路径 / Unmount readonly generation bind mounts, deepest paths first.
unmount_published_generations() {
    local mount_target
    local mount_targets=()

    if ! sudo test -d "$IMAGES_ROOT"; then
        return 0
    fi
    while IFS= read -r mount_target; do
        [[ "$mount_target" == "$IMAGES_ROOT"/* ]] && mount_targets+=("$mount_target")
    done < <(sudo findmnt --list --raw --noheadings --output TARGET | awk '{print length($0), $0}' | sort --numeric-sort --reverse | cut --delimiter=' ' --fields=2-)
    for mount_target in "${mount_targets[@]}"; do
        if sudo mountpoint -q "$mount_target"; then
            note "卸载 readonly generation: $mount_target"
            sudo umount "$mount_target" || die "无法卸载 generation: $mount_target"
        fi
    done
}

# @brief 卸载 state 并 detach 与本 image 关联的 loop devices / Unmount state and detach loop devices associated with this image.
unmount_loopback_state() {
    local loop_device

    if sudo mountpoint -q "$STATE_ROOT"; then
        note "卸载 XFS state: $STATE_ROOT"
        sudo umount "$STATE_ROOT" || die "无法卸载 state；请先停止仍在使用 workspace 的进程"
    fi
    if ! sudo test -e "$LOOP_IMAGE"; then
        return 0
    fi
    while IFS=: read -r loop_device _rest; do
        [[ "$loop_device" = /dev/loop* ]] || continue
        note "detach loop device: $loop_device"
        sudo losetup --detach "$loop_device" || die "无法 detach loop device: $loop_device"
    done < <(sudo losetup --associated "$LOOP_IMAGE")
}

# @brief 仅删除 manifest 所有且 checksum 未变化的 host artifacts / Delete only manifest-owned host artifacts whose checksums are unchanged.
remove_manifest_owned_artifacts() {
    local record_type
    local artifact_path
    local expected_checksum
    local actual_checksum

    if ! sudo test -f "$INSTALL_MANIFEST_FILE"; then
        note "没有 install manifest；保留 /usr/local 下可能由其他安装管理的 artifact"
        return 0
    fi
    while read -r record_type artifact_path expected_checksum; do
        [[ "$record_type" == "artifact" && "$artifact_path" = /usr/local/* ]] \
            || die "install manifest 格式无效"
        if ! sudo test -f "$artifact_path"; then
            continue
        fi
        actual_checksum="$(sudo sha256sum "$artifact_path" | awk '{print $1}')"
        if [[ "$actual_checksum" != "$expected_checksum" ]]; then
            note "保留被其他安装修改过的 artifact: $artifact_path"
            continue
        fi
        note "删除本 checkout 安装的 artifact: $artifact_path"
        sudo rm -f -- "$artifact_path"
    done < <(sudo cat "$INSTALL_MANIFEST_FILE")
    sudo rmdir --ignore-fail-on-non-empty /usr/local/libexec/wspctl 2>/dev/null || true
    sudo rmdir --ignore-fail-on-non-empty /usr/local/share/fogmoe-wspctl/systemd 2>/dev/null || true
    sudo rmdir --ignore-fail-on-non-empty /usr/local/share/fogmoe-wspctl 2>/dev/null || true
}

# @brief 删除明确请求清除的本 checkout state / Delete explicitly requested state for this checkout.
purge_checkout_state() {
    [[ "$WORK_ROOT" == "$REPOSITORY_ROOT/.wspctl" && "$WORK_ROOT" != "/" ]] \
        || die "拒绝删除未验证的 work root: $WORK_ROOT"
    note "清除不可恢复的 workspace、journal、image 和 loopback state"
    sudo rm -rf --one-file-system -- "$WORK_ROOT"
}

# @brief 显示用法 / Display usage.
show_help() {
    cat <<'EOF'
用法: ./uninstallWspctl.sh [--purge]

默认：停止并删除本 checkout 的 wspctld systemd unit、卸载 readonly generation 与 loopback XFS、
detach loop device，并且仅删除 checksum 与 install manifest 匹配的 /usr/local host artifacts。
它保留 ./.wspctl，以便下次安装恢复 persistent workspace。

--purge：在完成上述步骤后不可恢复地删除 ./.wspctl（包括 32 GiB loop image、workspace upper layers、
journal 和 immutable generations）。
EOF
}

case "${1:-}" in
    "")
        ;;
    --purge)
        PURGE_STATE=true
        ;;
    help|--help|-h)
        show_help
        exit 0
        ;;
    *)
        die "未知选项: $1（可用 --purge）"
        ;;
esac

require_commands
remove_checkout_unit
unmount_published_generations
unmount_loopback_state
remove_manifest_owned_artifacts
sudo rm -f -- "$ENVIRONMENT_FILE" "$INSTALL_MANIFEST_FILE"
if [[ "$PURGE_STATE" == true ]]; then
    purge_checkout_state
else
    note "已保留 $WORK_ROOT；确认不再需要恢复 workspace 后，可运行 ./uninstallWspctl.sh --purge"
fi
