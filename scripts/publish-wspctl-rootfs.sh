#!/usr/bin/env bash

# @brief 将固定 digest 的 OCI image 发布为只读 workspace root / Publish a pinned OCI image as a readonly workspace root.
#
# 本脚本是显式 operator 操作；start-wspctld.sh 绝不会调用它。/
# This script is an explicit operator action; start-wspctld.sh never invokes it.

set -euo pipefail

# @brief 仓库根 / Repository root.
REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# @brief 候选 OCI layout / Candidate OCI layout.
SOURCE_LAYOUT="${1:-$REPOSITORY_ROOT/.runtime/wspctl-rootfs/oci-layout}"
# @brief layout 内固定 reference / Fixed reference in the layout.
SOURCE_REFERENCE="${WSPCTL_IMAGE_REFERENCE:-wspctl-runtime}"
# @brief root-owned image work root / Root-owned image work root.
WORK_ROOT="$REPOSITORY_ROOT/.wspctl"
# @brief root-owned materialized artifact store / Root-owned materialized artifact store.
ARTIFACT_STORE="$WORK_ROOT/artifacts"
# @brief readonly image publication root / Readonly image publication root.
IMAGES_ROOT="$WORK_ROOT/images"
# @brief 当前选择的 OCI digest 文件 / Currently selected OCI-digest file.
CURRENT_IMAGE_FILE="$WORK_ROOT/current-image-digest"
# @brief native host build 目录 / Native host build directory.
BUILD_DIRECTORY="$REPOSITORY_ROOT/build/wspctld-dev"
# @brief native sealer/verifier / Native sealer/verifier.
SEALER="$BUILD_DIRECTORY/src/wspctl/wspctl-image"
# @brief 项目 Python，仅运行 importer / Project Python used only to run the importer.
PYTHON_EXECUTABLE="$REPOSITORY_ROOT/.venv/bin/python"

# @brief 输出错误并退出 / Print an error and exit.
# @param $* 错误文本 / Error text.
die() {
    printf 'wspctl image 发布失败: %s\n' "$*" >&2
    exit 1
}

[[ "$SOURCE_LAYOUT" = /* ]] || SOURCE_LAYOUT="$PWD/$SOURCE_LAYOUT"
[[ -d "$SOURCE_LAYOUT" ]] || die "OCI layout 不存在: $SOURCE_LAYOUT"
[[ -x "$PYTHON_EXECUTABLE" ]] || die "项目 Python 不存在: $PYTHON_EXECUTABLE"
[[ -x "$SEALER" ]] \
    || die "native sealer 尚未构建；先运行 cmake --build '$BUILD_DIRECTORY' --target wspctl-image"
command -v skopeo >/dev/null 2>&1 \
    || die "缺少 skopeo；OCI ingest 不会 fallback 到 tar 或 Docker"
command -v umoci >/dev/null 2>&1 \
    || die "缺少 umoci；OCI layers/whiteouts 必须由标准 unpacker 处理"

if [[ -n "${WSPCTL_IMAGE_DIGEST:-}" ]]; then
    MANIFEST_DIGEST="$WSPCTL_IMAGE_DIGEST"
elif [[ -r "$SOURCE_LAYOUT/wspctl-manifest-digest" ]]; then
    MANIFEST_DIGEST="$(<"$SOURCE_LAYOUT/wspctl-manifest-digest")"
else
    die "缺少 manifest digest；设置 WSPCTL_IMAGE_DIGEST=sha256:<64hex>"
fi
[[ "$MANIFEST_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || die "manifest digest 必须是 sha256:<64 lowercase hex>"
DIGEST_HEX="${MANIFEST_DIGEST#sha256:}"

sudo install -d -o root -g root -m 0700 \
    "$WORK_ROOT" "$ARTIFACT_STORE" "$IMAGES_ROOT"

sudo "$PYTHON_EXECUTABLE" "$REPOSITORY_ROOT/tools/publish_wspctl_image.py" \
    --source-layout "$SOURCE_LAYOUT" \
    --source-reference "$SOURCE_REFERENCE" \
    --manifest-digest "$MANIFEST_DIGEST" \
    --platform linux/amd64 \
    --artifact-store "$ARTIFACT_STORE" \
    --sealer "$SEALER" \
    --skopeo "$(command -v skopeo)" \
    --umoci "$(command -v umoci)"

SOURCE_ROOT="$ARTIFACT_STORE/sha256/$DIGEST_HEX/rootfs"
PUBLISH_ROOT="$IMAGES_ROOT/sha256/$DIGEST_HEX/rootfs"
sudo test -d "$SOURCE_ROOT" || die "importer 未产生 rootfs: $SOURCE_ROOT"
sudo install -d -o root -g root -m 0700 "$PUBLISH_ROOT"
if ! sudo mountpoint -q "$PUBLISH_ROOT"; then
    sudo mount --bind "$SOURCE_ROOT" "$PUBLISH_ROOT"
    sudo mount -o remount,bind,ro,nosuid,nodev "$PUBLISH_ROOT"
fi
sudo "$SEALER" \
    --verify true \
    --base-root "$PUBLISH_ROOT" \
    --images-root "$IMAGES_ROOT"
printf '%s\n' "$MANIFEST_DIGEST" \
    | sudo tee "$CURRENT_IMAGE_FILE.tmp" >/dev/null
sudo chown root:root "$CURRENT_IMAGE_FILE.tmp"
sudo chmod 0644 "$CURRENT_IMAGE_FILE.tmp"
sudo mv "$CURRENT_IMAGE_FILE.tmp" "$CURRENT_IMAGE_FILE"

printf 'source_oci_manifest_digest=%s\nrootfs=%s\n' \
    "$MANIFEST_DIGEST" "$PUBLISH_ROOT"
