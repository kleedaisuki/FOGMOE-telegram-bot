#!/usr/bin/env bash

# @brief 将固定 digest 的 OCI image 发布为只读 workspace root / Publish a pinned OCI image as a readonly workspace root.
#
# 本脚本是显式 operator 操作；start-wspctld.sh 绝不会调用它。/
# This script is an explicit operator action; start-wspctld.sh never invokes it.

set -euo pipefail

# @brief 仓库根 / Repository root.
REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# @brief content-addressed build artifact 根 / Content-addressed build-artifact root.
BUILD_OUTPUT_ROOT="$REPOSITORY_ROOT/.runtime/wspctl-rootfs"
# @brief 可选的显式候选 OCI layout / Optional explicit candidate OCI layout.
SOURCE_LAYOUT="${1:-}"
# @brief layout 内固定 reference / Fixed reference in the layout.
SOURCE_REFERENCE="${WSPCTL_IMAGE_REFERENCE:-wspctl-runtime}"
# @brief 未规范化的 operator work root / Unnormalized operator work root.
REQUESTED_WORK_ROOT="${WSPCTL_WORK_ROOT:-$REPOSITORY_ROOT/.wspctl}"
# @brief root-owned canonical image work root / Root-owned canonical image work root.
WORK_ROOT="$(realpath --canonicalize-missing -- "$REQUESTED_WORK_ROOT")"
# @brief root-owned materialized artifact store / Root-owned materialized artifact store.
ARTIFACT_STORE="$WORK_ROOT/artifacts"
# @brief readonly image publication root / Readonly image publication root.
IMAGES_ROOT="$WORK_ROOT/images"
# @brief 当前选择的 OCI digest 文件 / Currently selected OCI-digest file.
CURRENT_IMAGE_FILE="$WORK_ROOT/current-image-digest"
# @brief 独立 host publisher build 目录 / Independent host-publisher build directory.
BUILD_DIRECTORY="$REPOSITORY_ROOT/build/wspctl-image-publish"
# @brief root-owned installed native sealer/verifier / Root-owned installed native sealer/verifier.
SEALER="/usr/local/bin/wspctl-image"
# @brief root-owned installed typed publisher / Root-owned installed typed publisher.
PUBLISHER="/usr/local/libexec/wspctl/publish_wspctl_image.py"
# @brief distro-owned Python，仅运行无第三方依赖的 publisher / Distro-owned Python used only for the dependency-free publisher.
PYTHON_EXECUTABLE="/usr/bin/python3"
# @brief root-owned OCI copy tool / Root-owned OCI copy tool.
SKOPEO="/usr/bin/skopeo"
# @brief root-owned OCI unpacker / Root-owned OCI unpacker.
UMOCI="/usr/bin/umoci"
# @brief root-owned publication serialization lock / Root-owned publication serialization lock.
PUBLISH_LOCK="$WORK_ROOT/publish.lock"

# @brief 输出错误并退出 / Print an error and exit.
# @param $* 错误文本 / Error text.
die() {
    printf 'wspctl image 发布失败: %s\n' "$*" >&2
    exit 1
}

command -v cmake >/dev/null 2>&1 \
    || die "缺少 cmake，无法构建 root-owned host publisher"
command -v sudo >/dev/null 2>&1 \
    || die "缺少 sudo，显式 image publication 必须由 root 完成"
[[ "$WORK_ROOT" = /* ]] || die "WSPCTL_WORK_ROOT 必须是绝对路径"
[[ "$WORK_ROOT" =~ ^/[A-Za-z0-9._/-]+$ ]] \
    || die "WSPCTL_WORK_ROOT 只允许绝对路径中的字母、数字、点、下划线和连字符"
[[ "$(uname -m)" == "x86_64" ]] \
    || die "当前发布契约只支持 linux/amd64，host 为 $(uname -m)"

if [[ -n "${WSPCTL_IMAGE_DIGEST:-}" ]]; then
    MANIFEST_DIGEST="$WSPCTL_IMAGE_DIGEST"
elif [[ -n "$SOURCE_LAYOUT" && -r "$SOURCE_LAYOUT/wspctl-manifest-digest" ]]; then
    MANIFEST_DIGEST="$(<"$SOURCE_LAYOUT/wspctl-manifest-digest")"
elif [[ -r "$BUILD_OUTPUT_ROOT/current-image-digest" ]]; then
    MANIFEST_DIGEST="$(<"$BUILD_OUTPUT_ROOT/current-image-digest")"
else
    die "缺少 manifest digest；设置 WSPCTL_IMAGE_DIGEST=sha256:<64hex>"
fi
[[ "$MANIFEST_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || die "manifest digest 必须是 sha256:<64 lowercase hex>"
DIGEST_HEX="${MANIFEST_DIGEST#sha256:}"

if [[ -z "$SOURCE_LAYOUT" ]]; then
    SOURCE_LAYOUT="$BUILD_OUTPUT_ROOT/sha256/$DIGEST_HEX/oci-layout"
fi
[[ "$SOURCE_LAYOUT" = /* ]] || SOURCE_LAYOUT="$PWD/$SOURCE_LAYOUT"
[[ -d "$SOURCE_LAYOUT" ]] || die "OCI layout 不存在: $SOURCE_LAYOUT"

printf 'wspctl image: 构建并安装 root-owned publisher/verifier\n'
CMAKE_SECURITY_ARGUMENTS=("-DWSPCTL_ALLOW_INSECURE_DEVELOPMENT_ROOT=OFF")
if [[ "$WORK_ROOT" == "$REPOSITORY_ROOT/.wspctl" ]]; then
    CMAKE_SECURITY_ARGUMENTS=("-DWSPCTL_ALLOW_INSECURE_DEVELOPMENT_ROOT=ON")
fi
cmake -S "$REPOSITORY_ROOT" -B "$BUILD_DIRECTORY" \
    -DBUILD_TESTING=OFF \
    -DWSPCTL_BUILD_TESTING=OFF \
    -DWSPCTL_BUILD_PYTHON_BINDINGS=OFF \
    -DWSPCTL_INSTALL_HOST_TOOLS=ON \
    -DWSPCTL_HOST_WORKDIR="$WORK_ROOT" \
    -DCMAKE_INSTALL_PREFIX=/usr/local \
    -DCMAKE_BUILD_TYPE=Release \
    "${CMAKE_SECURITY_ARGUMENTS[@]}"
cmake --build "$BUILD_DIRECTORY" --target wspctl-image --parallel
sudo cmake --install "$BUILD_DIRECTORY" --component WspctlPublisher

for trusted_tool in \
    "$PYTHON_EXECUTABLE" "$PUBLISHER" "$SEALER" "$SKOPEO" "$UMOCI" \
    /usr/bin/flock /usr/bin/systemctl /usr/bin/systemd-escape \
    /usr/bin/findmnt /usr/bin/mountpoint; do
    sudo test -x "$trusted_tool" \
        || die "缺少 root-owned publication tool: $trusted_tool"
    trusted_metadata="$(sudo stat --format='%u:%g:%a' -L "$trusted_tool")"
    [[ "$trusted_metadata" =~ ^0:0:[0-7]*[0-5][0-5]$ ]] \
        || die "publication tool 必须 root:root 且 group/world 不可写: $trusted_tool ($trusted_metadata)"
done
"$PYTHON_EXECUTABLE" -c \
    'import sys; raise SystemExit(sys.version_info < (3, 11))' \
    || die "root-owned OCI publisher 需要 distro Python 3.11 或更新版本"

sudo install -d -o root -g root -m 0700 \
    "$WORK_ROOT" "$ARTIFACT_STORE" "$IMAGES_ROOT"
sudo touch "$PUBLISH_LOCK"
sudo chown root:root "$PUBLISH_LOCK"
sudo chmod 0600 "$PUBLISH_LOCK"

sudo /usr/bin/flock --exclusive "$PUBLISH_LOCK" \
    "$PYTHON_EXECUTABLE" "$PUBLISHER" \
    --source-layout "$SOURCE_LAYOUT" \
    --source-reference "$SOURCE_REFERENCE" \
    --manifest-digest "$MANIFEST_DIGEST" \
    --platform linux/amd64 \
    --artifact-store "$ARTIFACT_STORE" \
    --images-root "$IMAGES_ROOT" \
    --current-image-file "$CURRENT_IMAGE_FILE" \
    --sealer "$SEALER" \
    --skopeo "$SKOPEO" \
    --umoci "$UMOCI" \
    --systemctl /usr/bin/systemctl \
    --systemd-escape /usr/bin/systemd-escape \
    --findmnt /usr/bin/findmnt \
    --mountpoint /usr/bin/mountpoint
