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
# @brief immutable OCI image publication root / Immutable OCI image publication root.
IMAGES_ROOT="$WORK_ROOT/images"
# @brief sealed OCI artifact store / Sealed OCI artifact store.
ARTIFACT_STORE="$WORK_ROOT/artifacts"
# @brief systemd service managed by the development installer / Systemd service managed by the development installer.
SERVICE_NAME="wspctld.service"
# @brief wspctl 专用 LXCFS service / Dedicated wspctl LXCFS service.
LXCFS_SERVICE_NAME="wspctl-lxcfs.service"
# @brief active systemd unit path / Active systemd unit path.
UNIT_PATH="/etc/systemd/system/$SERVICE_NAME"
# @brief active dedicated LXCFS unit path / Active dedicated LXCFS unit path.
LXCFS_UNIT_PATH="/etc/systemd/system/$LXCFS_SERVICE_NAME"
# @brief installed immutable LXCFS unit copy / Installed immutable LXCFS unit copy.
LXCFS_UNIT_ARTIFACT="/usr/local/share/fogmoe-wspctl/systemd/wspctl-lxcfs.service"
# @brief dedicated LXCFS mountpoint / 专用 LXCFS 挂载点。
LXCFS_ROOT="/run/fogmoe-wspctl-lxcfs/root"
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

    for command_name in sudo systemctl systemd-escape grep findmnt mountpoint umount fusermount3 losetup sha256sum awk sort cut cat rm rmdir; do
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

# @brief 验证 active LXCFS unit 与本 checkout 安装的 immutable copy 完全一致 /
# Verify that the active LXCFS unit exactly matches the immutable copy installed by this checkout.
# @return 0 表示可安全管理 / Zero when this script may safely manage it.
is_checkout_lxcfs_unit() {
    local active_checksum
    local artifact_checksum

    sudo test -f "$LXCFS_UNIT_PATH" \
        && sudo test -f "$LXCFS_UNIT_ARTIFACT" \
        || return 1
    active_checksum="$(sudo sha256sum "$LXCFS_UNIT_PATH" | awk '{print $1}')"
    artifact_checksum="$(sudo sha256sum "$LXCFS_UNIT_ARTIFACT" | awk '{print $1}')"
    [[ "$active_checksum" == "$artifact_checksum" ]]
}

# @brief 停止并移除仅属于本 checkout 的 unit / Stop and remove only the unit belonging to this checkout.
remove_checkout_unit() {
    if ! sudo test -e "$UNIT_PATH" && ! sudo test -e "$LXCFS_UNIT_PATH"; then
        return 0
    fi
    if sudo test -e "$UNIT_PATH"; then
        is_checkout_unit \
            || die "$UNIT_PATH 不属于 $WORK_ROOT；拒绝停止或删除其他 wspctld 安装"
    fi
    if sudo test -e "$LXCFS_UNIT_PATH"; then
        is_checkout_lxcfs_unit \
            || die "$LXCFS_UNIT_PATH 与本 checkout 安装工件不一致；拒绝停止或删除"
    fi
    note "禁用并停止 $SERVICE_NAME 与 $LXCFS_SERVICE_NAME"
    if sudo test -e "$UNIT_PATH"; then
        sudo systemctl disable --now "$SERVICE_NAME" || true
    fi
    if sudo test -e "$LXCFS_UNIT_PATH"; then
        sudo systemctl disable --now "$LXCFS_SERVICE_NAME" || true
    fi
    if sudo mountpoint -q "$LXCFS_ROOT"; then
        sudo fusermount3 --unmount "$LXCFS_ROOT" \
            || die "无法卸载专用 LXCFS FUSE mount: $LXCFS_ROOT"
    fi
    sudo rm -f -- "$UNIT_PATH" "$LXCFS_UNIT_PATH"
    sudo systemctl daemon-reload
}

# @brief 卸载只读 OCI image bind mounts，先处理最深路径 / Unmount readonly OCI image bind mounts, deepest paths first.
unmount_published_images() {
    local mount_target
    local mount_listing
    local mount_unit
    local mount_unit_path
    local mount_targets=()

    if ! sudo test -d "$IMAGES_ROOT"; then
        return 0
    fi
    mount_listing="$(sudo findmnt --raw --noheadings --output TARGET)" \
        || die "findmnt 无法枚举 image mounts"
    while IFS= read -r mount_target; do
        [[ "$mount_target" == "$IMAGES_ROOT"/* ]] && mount_targets+=("$mount_target")
    done < <(printf '%s\n' "$mount_listing" | awk '{print length($0), $0}' | sort --numeric-sort --reverse | cut --delimiter=' ' --fields=2-)
    for mount_target in "${mount_targets[@]}"; do
        mount_unit="$(systemd-escape --path --suffix=mount "$mount_target")"
        mount_unit_path="/etc/systemd/system/$mount_unit"
        if sudo test -f "$mount_unit_path"; then
            sudo grep --fixed-strings --quiet "Where=$mount_target" "$mount_unit_path" \
                || die "拒绝删除 Where 不匹配的 mount unit: $mount_unit_path"
            sudo grep --extended-regexp --quiet \
                "^What=$ARTIFACT_STORE/sha256/[0-9a-f]{64}/rootfs$" \
                "$mount_unit_path" \
                || die "拒绝删除 What 不属于本 checkout artifact store 的 mount unit: $mount_unit_path"
            note "禁用持久 OCI image mount: $mount_unit"
            sudo systemctl disable --now "$mount_unit" \
                || die "无法禁用 OCI image mount unit: $mount_unit"
            sudo rm -f -- "$mount_unit_path"
        fi
        if sudo mountpoint -q "$mount_target"; then
            note "卸载 readonly OCI image: $mount_target"
            sudo umount "$mount_target" || die "无法卸载 OCI image: $mount_target"
        fi
    done
    sudo systemctl daemon-reload
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

默认：停止并删除本 checkout 的 wspctld systemd unit、卸载 readonly OCI image 与 loopback XFS、
detach loop device，并且仅删除 checksum 与 install manifest 匹配的 /usr/local host artifacts。
它保留 ./.wspctl，以便下次安装恢复 persistent workspace。

--purge：在完成上述步骤后不可恢复地删除 ./.wspctl（包括 32 GiB loop image、workspace upper layers、
journal 和 content-addressed OCI images）。
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
unmount_published_images
unmount_loopback_state
remove_manifest_owned_artifacts
sudo rm -f -- "$ENVIRONMENT_FILE" "$INSTALL_MANIFEST_FILE"
if [[ "$PURGE_STATE" == true ]]; then
    purge_checkout_state
else
    note "已保留 $WORK_ROOT；确认不再需要恢复 workspace 后，可运行 ./uninstallWspctl.sh --purge"
fi
