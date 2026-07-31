#!/usr/bin/env bash

# @brief 将一个 virtual environment 收敛到可验证的普通 wspctl wheel / Reconcile one virtual environment to a verifiable regular wspctl wheel.
#
# 此脚本是普通 wheel 的唯一所有者：它把源码身份、project version、Python SOABI、非 editable
# metadata、native import 和 RECORD 内容哈希收敛为一个 receipt。调用方先同步依赖，再调用本
# 脚本；receipt 命中时绝不重新构建 C++ 扩展。/
# This script is the sole owner of the regular wheel: it converges source identity, project
# version, Python SOABI, non-editable metadata, native import, and RECORD content hashes into one
# receipt. Callers synchronize dependencies first and then invoke this script; a receipt hit never
# rebuilds the C++ extension.

set -euo pipefail

# @brief 仓库根目录 / Repository root directory.
REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# @brief wspctl 构建输入身份计算器 / wspctl build-input identity calculator.
BUILD_IDENTITY_TOOL="$REPOSITORY_ROOT/tools/wspctl_build_identity.py"
# @brief 新建 virtual environment 时使用的 Python / Python used to create a new virtual environment.
BOOTSTRAP_PYTHON="${PYTHON:-python}"
# @brief 调用方请求的 virtual environment 路径 / Virtual-environment path requested by the caller.
REQUESTED_VENV_DIR=""
# @brief 规范化后的 virtual environment 路径 / Canonical virtual-environment path.
VENV_DIR=""
# @brief 受控 virtual environment Python / Controlled virtual-environment Python.
PYTHON_EXECUTABLE=""
# @brief 绑定该 virtual environment 的 client receipt / Client receipt bound to this virtual environment.
CLIENT_DEPLOYMENT_RECEIPT_FILE=""
# @brief 串行化本 checkout 的 client 校验与 wheel 安装的锁 / Lock serializing client verification and wheel installation for this checkout.
CLIENT_LOCK_FILE="$REPOSITORY_ROOT/.runtime/wspctl-client.lock"

# @brief 输出错误并终止 / Print an error and terminate.
# @param $* 错误文本 / Error text.
# @return 不返回 / Does not return.
die() {
    printf 'wspctl client 准备失败: %s\n' "$*" >&2
    exit 1
}

# @brief 输出普通进度 / Print normal progress.
# @param $* 进度文本 / Progress text.
# @return 成功时返回零 / Zero on success.
note() {
    printf 'wspctl client: %s\n' "$*"
}

# @brief 显示命令行用法 / Display command-line usage.
# @return 不返回 / Does not return.
show_help() {
    cat <<'EOF'
用法: scripts/ensure-wspctl-client.sh --venv /absolute/or/relative/path/to/.venv

将指定 virtual environment 收敛为当前 checkout 的普通 fogmoe-telegram-bot wheel。receipt
验证 source identity、project version、Python SOABI、非 editable metadata、wspctl._native
和所有带哈希的 RECORD 文件；全部命中时不运行 pip wheel 或编译 C++。
EOF
}

# @brief 解析唯一支持的 virtual environment 参数 / Parse the sole supported virtual-environment argument.
# @param $@ 命令行参数 / Command-line arguments.
# @return 成功时返回零 / Zero on success.
parse_arguments() {
    while (( $# > 0 )); do
        case "$1" in
            --venv)
                (( $# >= 2 )) || die "--venv 缺少路径"
                [[ -z "$REQUESTED_VENV_DIR" ]] || die "--venv 只能提供一次"
                REQUESTED_VENV_DIR="$2"
                shift 2
                ;;
            help|--help|-h)
                show_help
                exit 0
                ;;
            *)
                die "未知参数: $1"
                ;;
        esac
    done
    [[ -n "$REQUESTED_VENV_DIR" ]] || die "必须提供 --venv"
}

# @brief 验证一个解释器满足项目 Python 版本 / Verify that an interpreter satisfies the project Python version.
# @param $1 Python executable / Python executable.
# @return Python 3.14+ 时返回零 / Zero for Python 3.14+.
require_python_314() {
    local python_executable="$1"

    "$python_executable" -I -c 'import sys; raise SystemExit(sys.version_info < (3, 14))'
}

# @brief 创建或验证受控 virtual environment / Create or validate the controlled virtual environment.
# @return 成功时返回零 / Zero on success.
ensure_virtual_environment() {
    if [[ ! -x "$PYTHON_EXECUTABLE" ]]; then
        command -v "$BOOTSTRAP_PYTHON" >/dev/null 2>&1 \
            || die "找不到 Python，无法创建 $VENV_DIR: $BOOTSTRAP_PYTHON"
        require_python_314 "$BOOTSTRAP_PYTHON" \
            || die "创建 virtual environment 需要 Python 3.14 或更新版本"
        note "创建项目 virtual environment: $VENV_DIR"
        "$BOOTSTRAP_PYTHON" -m venv "$VENV_DIR"
    fi
    require_python_314 "$PYTHON_EXECUTABLE" \
        || die "virtual environment 必须使用 Python 3.14 或更新版本: $VENV_DIR"
    "$PYTHON_EXECUTABLE" -I -c 'import pip' \
        || die "virtual environment 缺少 pip: $VENV_DIR"
}

# @brief 读取 pyproject 中的发布版本 / Read the distribution version from pyproject.
# @return 规范 project version / Canonical project version.
project_version() {
    "$PYTHON_EXECUTABLE" -I - "$REPOSITORY_ROOT/pyproject.toml" <<'PY'
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

# @brief 读取当前 Python 的扩展 ABI 标识 / Read the current Python extension ABI identifier.
# @return 非空 SOABI / Nonempty SOABI.
python_abi() {
    "$PYTHON_EXECUTABLE" -I - <<'PY'
import sysconfig

soabi = sysconfig.get_config_var("SOABI")
if not isinstance(soabi, str) or not soabi:
    raise SystemExit("Python does not expose a usable SOABI")
print(soabi)
PY
}

# @brief 计算当前普通 Python wheel 的构建身份 / Compute the current regular Python wheel build identity.
# @param $1 当前 Python SOABI / Current Python SOABI.
# @return 小写十六进制 SHA-256 身份 / Lowercase hexadecimal SHA-256 identity.
client_build_identity() {
    local expected_abi="$1"

    [[ -f "$BUILD_IDENTITY_TOOL" ]] \
        || die "缺少 wspctl 构建身份工具: $BUILD_IDENTITY_TOOL"
    "$PYTHON_EXECUTABLE" -I "$BUILD_IDENTITY_TOOL" \
        --source-root "$REPOSITORY_ROOT" \
        --component client \
        --attribute "python_abi=$expected_abi" \
        --attribute "platform=$(uname -m)" \
        --attribute "wheel_format=regular-v2"
}

# @brief 验证安装的是完整、普通且归属当前 distribution 的 wspctl wheel / Verify that the installed wspctl wheel is complete, regular, and owned by its current distribution.
# @return 普通 wheel、native import 和 RECORD 完整性均通过时返回零 / Zero when the regular wheel, native import, and RECORD integrity all pass.
deployed_client_is_regular_install() {
    "$PYTHON_EXECUTABLE" -I - <<'PY'
import base64
import csv
import hashlib
import importlib.metadata
import json
import sys
from pathlib import Path


def fail() -> None:
    raise SystemExit(1)


try:
    distribution = importlib.metadata.distribution("fogmoe-telegram-bot")
    direct_url_text = distribution.read_text("direct_url.json")
    if direct_url_text is not None:
        direct_url = json.loads(direct_url_text)
        if direct_url.get("dir_info", {}).get("editable") is True:
            fail()
    import wspctl
    import wspctl._native as native
except (ImportError, OSError, json.JSONDecodeError, importlib.metadata.PackageNotFoundError):
    fail()

native_path = getattr(native, "__file__", None)
package_path = getattr(wspctl, "__file__", None)
if not isinstance(native_path, str) or not isinstance(package_path, str):
    fail()
native_file = Path(native_path).resolve()
package_file = Path(package_path).resolve()
if not native_file.is_file() or not package_file.is_file():
    fail()
distribution_root = Path(distribution.locate_file("")).resolve()
environment_root = Path(sys.prefix).resolve()
if not distribution_root.is_relative_to(environment_root):
    fail()
if not native_file.is_relative_to(distribution_root):
    fail()
if not package_file.is_relative_to(distribution_root):
    fail()
record_text = distribution.read_text("RECORD")
if record_text is None:
    fail()
verified_files: set[Path] = set()
for row in csv.reader(record_text.splitlines()):
    if len(row) != 3:
        fail()
    relative_name, expected_hash, expected_size = row
    if not relative_name or Path(relative_name).is_absolute():
        fail()
    target = Path(distribution.locate_file(relative_name)).resolve()
    if not target.is_relative_to(environment_root) or not target.is_file():
        fail()
    if expected_size:
        try:
            if target.stat().st_size != int(expected_size):
                fail()
        except ValueError:
            fail()
    if not expected_hash:
        continue
    algorithm, separator, encoded_digest = expected_hash.partition("=")
    if separator != "=" or not algorithm or not encoded_digest:
        fail()
    try:
        hasher = hashlib.new(algorithm)
    except ValueError:
        fail()
    with target.open("rb") as installed_file:
        for chunk in iter(lambda: installed_file.read(1024 * 1024), b""):
            hasher.update(chunk)
    actual_digest = base64.urlsafe_b64encode(hasher.digest()).rstrip(b"=").decode("ascii")
    if actual_digest != encoded_digest:
        fail()
    verified_files.add(target)
if native_file not in verified_files or package_file not in verified_files:
    fail()
PY
}

# @brief 验证普通 wheel 同时匹配版本、ABI 与源码身份收据 / Verify a regular wheel matches the version, ABI, and source-identity receipt.
# @param $1 期望源码身份 / Expected source identity.
# @param $2 期望 project version / Expected project version.
# @param $3 期望 Python SOABI / Expected Python SOABI.
# @return 收据和完整性验证均通过时返回零 / Zero when receipt and integrity verification both pass.
deployed_client_is_current() {
    local expected_identity="$1"
    local expected_version="$2"
    local expected_abi="$3"

    deployed_client_is_regular_install >/dev/null 2>&1 || return 1
    "$PYTHON_EXECUTABLE" -I - \
        "$CLIENT_DEPLOYMENT_RECEIPT_FILE" \
        "$expected_identity" \
        "$expected_version" \
        "$expected_abi" <<'PY'
import importlib.metadata
import json
import sys
import sysconfig
from pathlib import Path

receipt_path = Path(sys.argv[1])
expected = {
    "schema": 2,
    "source_identity": sys.argv[2],
    "project_version": sys.argv[3],
    "python_abi": sys.argv[4],
}
try:
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    distribution = importlib.metadata.distribution("fogmoe-telegram-bot")
except (OSError, json.JSONDecodeError, importlib.metadata.PackageNotFoundError):
    raise SystemExit(1)
if receipt != expected:
    raise SystemExit(1)
if sysconfig.get_config_var("SOABI") != expected["python_abi"]:
    raise SystemExit(1)
raise SystemExit(0 if distribution.version == expected["project_version"] else 1)
PY
}

# @brief 原子写入普通 wheel 的构建收据 / Atomically write the regular-wheel build receipt.
# @param $1 源码身份 / Source identity.
# @param $2 project version / Project version.
# @param $3 Python SOABI / Python SOABI.
# @return 成功时返回零 / Zero on success.
write_client_deployment_receipt() {
    local source_identity="$1"
    local expected_version="$2"
    local expected_abi="$3"

    "$PYTHON_EXECUTABLE" -I - \
        "$CLIENT_DEPLOYMENT_RECEIPT_FILE" \
        "$source_identity" \
        "$expected_version" \
        "$expected_abi" <<'PY'
import json
import os
import sys
from pathlib import Path

receipt_path = Path(sys.argv[1])
receipt = {
    "schema": 2,
    "source_identity": sys.argv[2],
    "project_version": sys.argv[3],
    "python_abi": sys.argv[4],
}
temporary_path = receipt_path.with_name(f"{receipt_path.name}.{os.getpid()}.tmp")
temporary_path.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
os.replace(temporary_path, receipt_path)
PY
}

# @brief 将受控 virtual environment 收敛到当前普通 wheel / Reconcile the controlled virtual environment to the current regular wheel.
# @return 成功时返回零 / Zero on success.
ensure_deployed_client() {
    local expected_version
    local expected_abi
    local expected_identity
    local wheel_directory
    local wheel_path
    local -a wheel_candidates

    expected_version="$(project_version)" \
        || die "无法读取 pyproject.toml 中的 project.version"
    expected_abi="$(python_abi)" \
        || die "无法读取 Python extension ABI"
    expected_identity="$(client_build_identity "$expected_abi")" \
        || die "无法计算普通 wheel 的构建身份"
    if deployed_client_is_current "$expected_identity" "$expected_version" "$expected_abi"; then
        note "普通 wheel client 已通过 version/ABI/source identity/RECORD/native import 验证；跳过 pip 构建"
        return 0
    fi

    wheel_directory="$(mktemp -d "$VENV_DIR/.wspctl-wheel-XXXXXX")" \
        || die "无法创建 wheel staging 目录"
    note "普通 wheel client 收据失效；构建并覆盖旧安装（含 editable）"
    if ! "$PYTHON_EXECUTABLE" -I -m pip wheel \
        --no-cache-dir \
        --no-deps \
        --wheel-dir "$wheel_directory" \
        "$REPOSITORY_ROOT"; then
        rm -rf -- "$wheel_directory"
        die "无法构建 wspctl 普通 wheel"
    fi
    mapfile -t wheel_candidates < <(
        find "$wheel_directory" -maxdepth 1 -type f -name 'fogmoe_telegram_bot-*.whl' -print | sort
    )
    if (( ${#wheel_candidates[@]} != 1 )); then
        rm -rf -- "$wheel_directory"
        die "wheel staging 没有产生唯一的 fogmoe-telegram-bot wheel"
    fi
    wheel_path="${wheel_candidates[0]}"
    if ! "$PYTHON_EXECUTABLE" -I -m pip install \
        --no-deps \
        --force-reinstall \
        "$wheel_path"; then
        rm -rf -- "$wheel_directory"
        die "无法安装 wspctl 普通 wheel"
    fi
    rm -rf -- "$wheel_directory"
    deployed_client_is_regular_install \
        || die "部署后的 Python client 不可用、被篡改或仍为 editable 安装"
    write_client_deployment_receipt "$expected_identity" "$expected_version" "$expected_abi" \
        || die "无法写入普通 wheel 构建收据"
    deployed_client_is_current "$expected_identity" "$expected_version" "$expected_abi" \
        || die "普通 wheel 安装后未通过 version/ABI/source identity/RECORD 验证"
}

# @brief 获取覆盖校验、receipt、wheel 构建与安装的 checkout-local 锁 / Acquire the checkout-local lock covering verification, receipt, wheel build, and installation.
# @return 成功时返回零 / Zero on success.
acquire_client_lock() {
    mkdir -p "$REPOSITORY_ROOT/.runtime"
    exec 9>"$CLIENT_LOCK_FILE"
    flock 9
}

# @brief 执行命令行入口 / Execute the command-line entrypoint.
# @param $@ 命令行参数 / Command-line arguments.
# @return 成功时返回零 / Zero on success.
main() {
    parse_arguments "$@"
    command -v realpath >/dev/null 2>&1 || die "缺少 realpath"
    command -v mktemp >/dev/null 2>&1 || die "缺少 mktemp"
    command -v find >/dev/null 2>&1 || die "缺少 find"
    command -v sort >/dev/null 2>&1 || die "缺少 sort"
    command -v flock >/dev/null 2>&1 || die "缺少 flock"
    VENV_DIR="$(realpath --canonicalize-missing -- "$REQUESTED_VENV_DIR")"
    PYTHON_EXECUTABLE="$VENV_DIR/bin/python"
    CLIENT_DEPLOYMENT_RECEIPT_FILE="$VENV_DIR/.fogmoe-wspctl-client-receipt"
    acquire_client_lock
    ensure_virtual_environment
    ensure_deployed_client
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
