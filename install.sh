#!/usr/bin/env bash

# @brief 安装并启动本 checkout 的完整 wspctl host control plane / Install and start this checkout's complete wspctl host control plane.
#
# 这是开发部署的唯一聚合入口：按顺序构建 OCI artifact、发布 root-owned readonly image，
# 再安装、启用并启动 wspctld。runBot.sh 永远不会调用本脚本。/
# This is the sole aggregate development-deployment entrypoint: it builds the OCI artifact,
# publishes the root-owned readonly image, then installs, enables, and starts wspctld.
# runBot.sh never invokes this script.

set -euo pipefail

# @brief 仓库根目录 / Repository root directory.
REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# @brief OCI rootfs 构建入口 / OCI-rootfs build entrypoint.
BUILD_IMAGE_SCRIPT="$REPOSITORY_ROOT/scripts/build-wspctl-rootfs.sh"
# @brief OCI rootfs 发布入口 / OCI-rootfs publication entrypoint.
PUBLISH_IMAGE_SCRIPT="$REPOSITORY_ROOT/scripts/publish-wspctl-rootfs.sh"
# @brief host broker 安装与激活入口 / Host-broker installation and activation entrypoint.
INSTALL_BROKER_SCRIPT="$REPOSITORY_ROOT/scripts/start-wspctld.sh"
# @brief checkout-local 安装日志目录 / Checkout-local installation log directory.
LOG_DIR="$REPOSITORY_ROOT/logs"
# @brief 本轮完整安装日志 / Complete installation log for this attempt.
LOG_FILE=""

# @brief 输出错误并终止 / Print an error and terminate.
# @param $* 错误文本 / Error text.
# @return 不返回 / Does not return.
die() {
    printf 'wspctl 安装失败: %s\n' "$*" >&2
    exit 1
}

# @brief 输出安装阶段 / Print an installation stage.
# @param $* 阶段文本 / Stage text.
# @return 成功时返回零 / Zero on success.
note() {
    printf '\nwspctl install: %s\n' "$*"
}

# @brief 验证唯一支持的无参数安装界面 / Validate the sole supported argument-free install interface.
# @param $@ 命令行参数 / Command-line arguments.
# @return 成功时返回零 / Zero on success.
require_arguments() {
    case "${1:-}" in
        "")
            ;;
        help|--help|-h)
            cat <<'EOF'
用法: ./installWspctl.sh

依次完成 workspace OCI image 构建、root-owned readonly 发布、host artifacts/systemd unit
安装，以及 wspctld.service 的 enable/start。该命令是显式部署操作，可能要求 sudo 密码。

日常启动 Bot 只运行 ./runBot.sh start；runBot.sh 不会执行任何 wspctl 安装或特权修复。
EOF
            exit 0
            ;;
        *)
            die "未知选项: $1（本安装器不接受位置参数）"
            ;;
    esac
}

# @brief 验证安装入口与 sudo 凭据 / Verify installation entrypoints and sudo credentials.
# @return 成功时返回零 / Zero on success.
require_install_prerequisites() {
    local command_name
    local missing_commands=()
    local script_path

    for command_name in sudo buildah skopeo umoci cmake tee date; do
        command -v "$command_name" >/dev/null 2>&1 \
            || missing_commands+=("$command_name")
    done
    (( ${#missing_commands[@]} == 0 )) \
        || die "缺少 host 安装工具: ${missing_commands[*]}（请先通过系统包管理器安装）"
    for script_path in \
        "$BUILD_IMAGE_SCRIPT" \
        "$PUBLISH_IMAGE_SCRIPT" \
        "$INSTALL_BROKER_SCRIPT"; do
        [[ -x "$script_path" ]] || die "安装入口不存在或不可执行: $script_path"
    done
    sudo -v || die "无法取得安装所需的 sudo 凭据"
}

# @brief 为本轮安装创建仅当前用户可读写的日志 / Create an owner-only log for this installation attempt.
# @return 成功时返回零 / Zero on success.
initialize_install_log() {
    local start_timestamp

    mkdir -p "$LOG_DIR" || die "无法创建安装日志目录: $LOG_DIR"
    start_timestamp="$(date '+%Y%m%dT%H%M%S%z')" \
        || die "无法生成安装日志时间戳"
    LOG_FILE="$LOG_DIR/wspctl_install_${start_timestamp}_$$.log"
    (umask 077; : > "$LOG_FILE") \
        || die "无法创建安装日志: $LOG_FILE"
}

# @brief 执行完整、显式且有序的 host control-plane 安装 / Run the complete explicit ordered host-control-plane installation.
# @return 成功时返回零 / Zero on success.
install_wspctl() {
    note "[1/3] 构建 content-addressed workspace OCI image"
    "$BUILD_IMAGE_SCRIPT"

    note "[2/3] 验证并发布 root-owned readonly workspace image"
    "$PUBLISH_IMAGE_SCRIPT"

    note "[3/3] 安装、配置、启用并启动 wspctld.service"
    "$INSTALL_BROKER_SCRIPT" start

    note "安装完成；日常启动 Bot 请运行 ./runBot.sh start"
}

# @brief 实时显示并完整记录安装输出，同时保留真实失败状态 / Stream and persist all installation output while preserving the real failure status.
# @return 安装阶段或日志写入阶段的非零状态 / Nonzero status from installation or log persistence.
run_logged_install() {
    local -a pipeline_status
    local install_status
    local tee_status

    printf 'wspctl install: 完整日志: %s\n' "$LOG_FILE"
    set +e
    (
        set -e
        require_install_prerequisites
        install_wspctl
    ) 2>&1 | tee -a "$LOG_FILE"
    pipeline_status=("${PIPESTATUS[@]}")
    set -e
    install_status="${pipeline_status[0]}"
    tee_status="${pipeline_status[1]}"

    if (( tee_status != 0 )); then
        printf 'wspctl 安装失败: 无法完整写入日志: %s\n' "$LOG_FILE" >&2
        return "$tee_status"
    fi
    if (( install_status != 0 )); then
        printf '\nwspctl install: 安装失败（exit=%d）；完整日志: %s\n' \
            "$install_status" "$LOG_FILE" | tee -a "$LOG_FILE"
        return "$install_status"
    fi
    return 0
}

# @brief 安装器主入口 / Installer main entrypoint.
# @param $@ CLI 参数 / CLI arguments.
# @return 成功为零，参数、部署或日志失败时非零 / Zero on success; nonzero on argument, deployment, or logging failure.
main() {
    require_arguments "$@"
    initialize_install_log
    run_logged_install
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
