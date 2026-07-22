#!/bin/bash

# 获取脚本所在目录的绝对路径
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
BOT_DIR="$SCRIPT_DIR"
SRC_DIR="$BOT_DIR/src"
LOG_DIR="$BOT_DIR/logs"
STATE_DIR="$BOT_DIR/.runtime"
PID_FILE="$STATE_DIR/fogmoe-bot.pid"
CONTROL_LOCK_FILE="$STATE_DIR/control.lock"
VENV_DIR="$BOT_DIR/.venv"
PYPROJECT_FILE="$BOT_DIR/pyproject.toml"
CONFIG_FILE="$BOT_DIR/config.json"
EXAMPLE_CONFIG_FILE="$BOT_DIR/example.config.json"
# 外层进程管理器必须晚于应用的排空截止时间才可升级为 SIGKILL。
STOP_TIMEOUT_SECONDS="${BOT_STOP_TIMEOUT_SECONDS:-200}"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 读取 Linux /proc 记录的进程启动时刻，防止 PID reuse。
get_process_start_time() {
    local process_pid="$1"
    local stat_line
    local stat_tail

    if [ ! -r "/proc/$process_pid/stat" ]; then
        return 1
    fi
    IFS= read -r stat_line < "/proc/$process_pid/stat" || return 1
    stat_tail="${stat_line##*) }"
    # 去掉 pid/comm 后，starttime（proc_pid_stat field 22）是第 20 项。
    set -- $stat_tail
    if [ "$#" -lt 20 ]; then
        return 1
    fi
    printf '%s\n' "${20}"
}

# 验证 PID、启动时刻、cwd 与精确入口，绝不按模糊进程文本杀进程。
process_is_managed_bot() {
    local process_pid="$1"
    local expected_start_time="$2"
    local current_start_time
    local process_cwd

    [[ "$process_pid" =~ ^[1-9][0-9]*$ ]] || return 1
    [[ "$expected_start_time" =~ ^[1-9][0-9]*$ ]] || return 1
    kill -0 "$process_pid" 2>/dev/null || return 1
    current_start_time="$(get_process_start_time "$process_pid")" || return 1
    [ "$current_start_time" = "$expected_start_time" ] || return 1
    process_cwd="$(readlink -f "/proc/$process_pid/cwd" 2>/dev/null)" || return 1
    [ "$process_cwd" = "$BOT_DIR" ] || return 1
    tr '\0' '\n' < "/proc/$process_pid/cmdline" 2>/dev/null \
        | grep -Fqx -- "$VENV_DIR/bin/fogmoe-bot"
}

# PID 文件是唯一进程发现来源；失效身份只清理本 checkout 的 stale 文件。
get_bot_identity() {
    local process_pid
    local process_start_time
    local shutdown_grace_seconds

    if [ ! -f "$PID_FILE" ]; then
        return 1
    fi
    if ! read -r process_pid process_start_time shutdown_grace_seconds < "$PID_FILE" \
        || ! [[ "$shutdown_grace_seconds" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
        rm -f -- "$PID_FILE"
        return 1
    fi
    if ! process_is_managed_bot "$process_pid" "$process_start_time"; then
        echo -e "${YELLOW}忽略失效 PID 文件: $PID_FILE${NC}" >&2
        rm -f -- "$PID_FILE"
        return 1
    fi
    printf '%s %s %s\n' \
        "$process_pid" "$process_start_time" "$shutdown_grace_seconds"
}

# 原子记录同一进程实例的 PID、/proc starttime 与启动时排空期限。
write_bot_identity() {
    local process_pid="$1"
    local process_start_time="$2"
    local shutdown_grace_seconds="$3"
    local temporary_file="$PID_FILE.$$.tmp"

    mkdir -p "$STATE_DIR"
    (umask 077; printf '%s %s %s\n' \
        "$process_pid" "$process_start_time" "$shutdown_grace_seconds" \
        > "$temporary_file") \
        || return 1
    mv -f -- "$temporary_file" "$PID_FILE"
}

# 只删除仍指向调用方所持进程实例的 PID 文件。
clear_bot_identity() {
    local expected_pid="$1"
    local expected_start_time="$2"
    local recorded_pid
    local recorded_start_time
    local recorded_shutdown_grace

    if read -r recorded_pid recorded_start_time recorded_shutdown_grace \
        < "$PID_FILE" 2>/dev/null \
        && [ "$recorded_pid" = "$expected_pid" ] \
        && [ "$recorded_start_time" = "$expected_start_time" ]; then
        rm -f -- "$PID_FILE"
    fi
}

# 修改生命周期前获取 checkout-local flock，避免并发 start/restart 竞态。
acquire_control_lock() {
    mkdir -p "$STATE_DIR"
    exec 9>"$CONTROL_LOCK_FILE"
    if ! flock -n 9; then
        echo -e "${RED}错误: 另一个 runBot.sh 生命周期操作仍在执行${NC}"
        exit 1
    fi
}

# 读取本次启动实际采用的运行时 grace period。
read_runtime_shutdown_grace() {
    if [ ! -x "$VENV_DIR/bin/python" ] || [ ! -f "$CONFIG_FILE" ]; then
        echo -e "${RED}错误: 无法读取运行时 shutdown grace 配置${NC}" >&2
        return 1
    fi
    PYTHONPATH="$SRC_DIR" "$VENV_DIR/bin/python" - "$CONFIG_FILE" <<'PY'
from pathlib import Path
import sys

from fogmoe_bot.config import read_bot_settings

print(read_bot_settings(Path(sys.argv[1])).runtime.mailbox.shutdown_grace_seconds)
PY
}

# 外层强杀期限必须是正整数，且严格晚于进程启动时的运行时 grace period。
validate_stop_timeout() {
    local runtime_grace_seconds="$1"

    if ! [[ "$STOP_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
        echo -e "${RED}错误: BOT_STOP_TIMEOUT_SECONDS 必须是正整数${NC}"
        return 1
    fi
    if ! awk -v outer="$STOP_TIMEOUT_SECONDS" -v inner="$runtime_grace_seconds" \
        'BEGIN { exit !(outer >= inner + 10) }'; then
        echo -e "${RED}错误: BOT_STOP_TIMEOUT_SECONDS 必须至少比 shutdown_grace_seconds 多 10 秒${NC}"
        return 1
    fi
}

# 获取当前或最近一次应用日志；应用进程会以时间戳文件名写入。
get_latest_log_file() {
    find "$LOG_DIR" -maxdepth 1 -type f -name 'tgbot_*.log' -printf '%T@ %p\n' 2>/dev/null \
        | sort -nr \
        | head -n 1 \
        | cut -d' ' -f2-
}

# 检查并创建虚拟环境
setup_venv() {
    if [ ! -d "$VENV_DIR" ]; then
        echo -e "${YELLOW}虚拟环境不存在，正在创建...${NC}"
        python3 -m venv "$VENV_DIR"

        if [ $? -ne 0 ]; then
            echo -e "${RED}✗ 创建虚拟环境失败${NC}"
            echo "请确保已安装 python3-venv:"
            echo "  Ubuntu/Debian: sudo apt install python3-venv"
            echo "  CentOS/RHEL: sudo yum install python3-venv"
            exit 1
        fi

        echo -e "${GREEN}✓ 虚拟环境创建成功${NC}"
    fi
}

# 安装依赖
install_dependencies() {
    echo -e "${YELLOW}正在检查并安装依赖...${NC}"

    # 激活虚拟环境
    source "$VENV_DIR/bin/activate"

    # 升级 pip
    echo "升级 pip..."
    pip install --upgrade pip -q

    # 安装项目和依赖
    if [ -f "$PYPROJECT_FILE" ]; then
        echo "按 pyproject.toml 安装项目依赖..."
        pip install -e "$BOT_DIR"

        if [ $? -eq 0 ]; then
            echo -e "${GREEN}✓ 依赖安装成功${NC}"
        else
            echo -e "${RED}✗ 依赖安装失败${NC}"
            exit 1
        fi
    else
        echo -e "${RED}错误: pyproject.toml 文件不存在${NC}"
        exit 1
    fi
}

# 初始化环境（首次设置）
init_environment() {
    echo "=== 初始化雾萌娘 Telegram Bot 环境 ==="
    echo ""

    # 创建虚拟环境
    setup_venv

    # 安装依赖
    install_dependencies

    # 首次创建操作者配置；example.config.json 是可提交的完整 JSONC 模板。
    if [ ! -f "$CONFIG_FILE" ]; then
        echo ""
        echo -e "${YELLOW}警告: config.json 文件不存在${NC}"

        if [ -f "$EXAMPLE_CONFIG_FILE" ]; then
            echo "是否要从 example.config.json 创建 config.json? (y/n)"
            read -r response
            if [[ "$response" =~ ^([yY][eE][sS]|[yY])$ ]]; then
                cp "$EXAMPLE_CONFIG_FILE" "$CONFIG_FILE"
                chmod 600 "$CONFIG_FILE"
                echo -e "${GREEN}✓ 已创建 config.json 文件${NC}"
                echo -e "${YELLOW}请编辑 config.json（JSONC）并配置必要参数${NC}"
                echo "  nano $CONFIG_FILE"
            fi
        else
            echo -e "${RED}错误: example.config.json 文件也不存在${NC}"
        fi
    fi

    echo ""
    echo -e "${GREEN}✓ 环境初始化完成！${NC}"
    echo ""
    echo "下一步:"
    echo "  1. 配置 config.json（JSONC）中的必要参数"
    echo "  2. 运行数据库迁移: $VENV_DIR/bin/fogmoe-dbctl migrate"
    echo "  3. 启动 bot: $0 start"
}

# 启动bot
start_bot() {
    local runtime_grace_seconds

    echo "=== 雾萌娘 Telegram Bot 启动脚本 ==="
    echo "Bot 目录: $BOT_DIR"

    # 检查是否已经在运行
    OLD_IDENTITY="$(get_bot_identity)"
    if [ -n "$OLD_IDENTITY" ]; then
        read -r OLD_PID _OLD_START_TIME <<< "$OLD_IDENTITY"
        echo -e "${YELLOW}Bot已在运行 (PID: $OLD_PID)${NC}"
        echo "如需重启，请使用: $0 restart"
        exit 1
    fi

    # 确保目录存在
    if [ ! -d "$BOT_DIR" ]; then
        echo -e "${RED}错误: Bot目录不存在: $BOT_DIR${NC}"
        exit 1
    fi

    # 确保 src 目录存在
    if [ ! -d "$SRC_DIR" ]; then
        echo -e "${RED}错误: src 目录不存在: $SRC_DIR${NC}"
        exit 1
    fi

    # 确保 bot 包存在
    if [ ! -f "$SRC_DIR/fogmoe_bot/main.py" ]; then
        echo -e "${RED}错误: bot 入口不存在: $SRC_DIR/fogmoe_bot/main.py${NC}"
        exit 1
    fi

    # 检查虚拟环境
    if [ ! -d "$VENV_DIR" ]; then
        echo -e "${YELLOW}虚拟环境不存在${NC}"
        echo "是否要初始化环境? (y/n)"
        read -r response
        if [[ "$response" =~ ^([yY][eE][sS]|[yY])$ ]]; then
            init_environment
            echo ""
        else
            echo -e "${RED}无法启动: 需要虚拟环境${NC}"
            echo "请运行: $0 init"
            exit 1
        fi
    fi

    # 激活虚拟环境
    echo "激活虚拟环境..."
    source "$VENV_DIR/bin/activate"

    # 配置只能来自根目录 JSONC 文件，避免启动时隐式读取遗留 .env。
    if [ ! -f "$CONFIG_FILE" ]; then
        echo -e "${RED}错误: config.json 文件不存在！${NC}"
        echo "请先创建 config.json 并配置必要参数"
        echo "可以参考 example.config.json 文件"
        exit 1
    fi

    runtime_grace_seconds="$(read_runtime_shutdown_grace)" || exit 1
    validate_stop_timeout "$runtime_grace_seconds" || exit 1

    # 切换到项目根目录，使用 src layout 启动入口
    cd "$BOT_DIR"

    # 启动bot并记录日志
    echo "正在启动bot..."
    mkdir -p "$LOG_DIR"
    START_TIMESTAMP=$(date '+%Y%m%dT%H%M%S')
    STDOUT_LOG_FILE="$LOG_DIR/stdout_${START_TIMESTAMP}.log"
    echo "标准输出日志: $STDOUT_LOG_FILE"
    PYTHONPATH="$SRC_DIR${PYTHONPATH:+:$PYTHONPATH}" \
        nohup "$VENV_DIR/bin/fogmoe-bot" \
        > "$STDOUT_LOG_FILE" 2>&1 9>&- &

    # 获取新进程PID
    NEW_PID=$!
    NEW_START_TIME="$(get_process_start_time "$NEW_PID")"
    if [ -z "$NEW_START_TIME" ] \
        || ! write_bot_identity \
            "$NEW_PID" "$NEW_START_TIME" "$runtime_grace_seconds"; then
        echo -e "${RED}✗ 错误: 无法记录 Bot 进程身份${NC}"
        kill "$NEW_PID" 2>/dev/null
        exit 1
    fi
    echo "Bot已启动 (PID: $NEW_PID)"

    # 检查进程是否成功启动
    sleep 2
    if process_is_managed_bot "$NEW_PID" "$NEW_START_TIME"; then
        LOG_FILE=$(get_latest_log_file)
        echo -e "${GREEN}✓ Bot运行正常${NC}"
        echo ""
        if [ -n "$LOG_FILE" ]; then
            echo "应用日志: $LOG_FILE"
            echo "查看日志: tail -f $LOG_FILE"
        fi
        echo "停止bot: $0 stop"
        echo "查看状态: $0 status"
    else
        clear_bot_identity "$NEW_PID" "$NEW_START_TIME"
        echo -e "${RED}✗ 错误: Bot启动失败${NC}"
        echo "请查看标准输出日志: $STDOUT_LOG_FILE"
        exit 1
    fi
}

# 停止bot
stop_bot() {
    echo "=== 雾萌娘 Telegram Bot 停止脚本 ==="

    BOT_IDENTITY="$(get_bot_identity)"

    if [ -z "$BOT_IDENTITY" ]; then
        echo "未发现运行中的bot进程"
        return 0
    fi
    read -r BOT_PID BOT_START_TIME BOT_SHUTDOWN_GRACE <<< "$BOT_IDENTITY"

    validate_stop_timeout "$BOT_SHUTDOWN_GRACE" || exit 1

    echo "发现bot进程 (PID: $BOT_PID)"
    echo "正在停止..."

    # 尝试优雅地停止
    kill "$BOT_PID"

    # 运行时在启动时记录的 grace period 内尽力按阶段排空；外层在其后升级。
    waited=0
    while process_is_managed_bot "$BOT_PID" "$BOT_START_TIME" \
        && [ "$waited" -lt "$STOP_TIMEOUT_SECONDS" ]; do
        sleep 1
        waited=$((waited + 1))
    done

    # 检查是否还在运行
    if process_is_managed_bot "$BOT_PID" "$BOT_START_TIME"; then
        echo "进程在 ${STOP_TIMEOUT_SECONDS} 秒内未完成排空，强制终止..."
        kill -9 "$BOT_PID"
        sleep 1
    fi

    # 最终检查
    if process_is_managed_bot "$BOT_PID" "$BOT_START_TIME"; then
        echo -e "${RED}✗ 错误: 无法停止进程 $BOT_PID${NC}"
        exit 1
    else
        clear_bot_identity "$BOT_PID" "$BOT_START_TIME"
        echo -e "${GREEN}✓ Bot已成功停止${NC}"

        # 显示最后几行日志
        LOG_FILE=$(get_latest_log_file)
        if [ -n "$LOG_FILE" ] && [ -f "$LOG_FILE" ]; then
            echo ""
            echo "=== 最后10行日志 ==="
            tail -n 10 "$LOG_FILE"
        fi
    fi
}

# 重启bot
restart_bot() {
    echo "=== 重启 Bot ==="
    stop_bot
    echo ""
    sleep 2
    start_bot
}

# 查看状态
status_bot() {
    echo "=== 雾萌娘 Telegram Bot 状态 ==="

    BOT_IDENTITY="$(get_bot_identity)"

    if [ -z "$BOT_IDENTITY" ]; then
        echo -e "状态: ${RED}✗ 未运行${NC}"
        exit 1
    else
        read -r BOT_PID _BOT_START_TIME <<< "$BOT_IDENTITY"
        echo -e "状态: ${GREEN}✓ 运行中${NC}"
        echo "PID: $BOT_PID"

        # 显示进程信息
        echo ""
        echo "进程详情:"
        ps -fp "$BOT_PID"

        # 检查虚拟环境
        if [ -d "$VENV_DIR" ]; then
            echo ""
            echo "虚拟环境: ✓ $VENV_DIR"
        fi

        # 显示最后几行日志
        LOG_FILE=$(get_latest_log_file)
        if [ -n "$LOG_FILE" ] && [ -f "$LOG_FILE" ]; then
            echo ""
            echo "=== 最后10行日志 ==="
            tail -n 10 "$LOG_FILE"
        fi
    fi
}

# 更新依赖
update_deps() {
    echo "=== 更新依赖 ==="

    if [ ! -d "$VENV_DIR" ]; then
        echo -e "${RED}错误: 虚拟环境不存在${NC}"
        echo "请先运行: $0 init"
        exit 1
    fi

    # 检查bot是否在运行
    BOT_IDENTITY="$(get_bot_identity)"
    if [ -n "$BOT_IDENTITY" ]; then
        echo -e "${YELLOW}警告: Bot正在运行，建议先停止${NC}"
        echo "是否继续更新? (y/n)"
        read -r response
        if [[ ! "$response" =~ ^([yY][eE][sS]|[yY])$ ]]; then
            exit 0
        fi
    fi

    install_dependencies

    echo ""
    echo -e "${GREEN}✓ 依赖更新完成${NC}"
    echo "如果bot正在运行，建议重启: $0 restart"
}

# 显示帮助
show_help() {
    echo "雾萌娘 Telegram Bot 管理脚本"
    echo ""
    echo "用法: $0 [命令]"
    echo ""
    echo "命令:"
    echo "  init      初始化环境（创建虚拟环境并安装依赖）"
    echo "  start     启动bot（默认）"
    echo "  stop      停止bot"
    echo "  restart   重启bot"
    echo "  status    查看bot状态"
    echo "  update    更新依赖包"
    echo "  help      显示此帮助信息"
    echo ""
    echo "示例:"
    echo "  $0 init      # 首次使用，初始化环境"
    echo "  $0           # 启动bot"
    echo "  $0 start     # 启动bot"
    echo "  $0 stop      # 停止bot"
    echo "  $0 restart   # 重启bot"
    echo "  $0 status    # 查看状态"
    echo "  $0 update    # 更新依赖"
    echo ""
    echo "首次使用流程:"
    echo "  1. $0 init                           # 初始化环境"
    echo "  2. 编辑 config.json 配置必要参数       # nano config.json"
    echo "  3. 运行数据库迁移                     # $VENV_DIR/bin/fogmoe-dbctl migrate"
    echo "  4. $0 start                          # 启动bot"
}

# 主逻辑
case "${1:-start}" in
    init|setup|install)
        acquire_control_lock
        init_environment
        ;;
    start)
        acquire_control_lock
        start_bot
        ;;
    stop)
        acquire_control_lock
        stop_bot
        ;;
    restart)
        acquire_control_lock
        restart_bot
        ;;
    status)
        status_bot
        ;;
    update|upgrade)
        acquire_control_lock
        update_deps
        ;;
    help|--help|-h)
        show_help
        ;;
    *)
        echo -e "${RED}错误: 未知命令 '$1'${NC}"
        echo ""
        show_help
        exit 1
        ;;
esac
