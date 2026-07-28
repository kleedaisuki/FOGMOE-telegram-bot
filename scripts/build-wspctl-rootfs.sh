#!/usr/bin/env bash

# @brief 用 Buildah 生成标准 OCI image layout / Build the standard OCI image layout with Buildah.
#
# 本脚本只构建 artifact，不发布、不挂载、也不启动 wspctld。/
# This script only builds an artifact; it never publishes, mounts, or starts wspctld.

set -euo pipefail

# @brief 仓库根 / Repository root.
REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# @brief 唯一 rootfs 构建定义 / Sole rootfs build definition.
CONTAINERFILE="$REPOSITORY_ROOT/deploy/wspctl/image/Containerfile"
# @brief 输出 OCI layout / Output OCI layout.
OUTPUT_LAYOUT="${1:-$REPOSITORY_ROOT/.runtime/wspctl-rootfs/oci-layout}"
# @brief OCI layout 内的固定 reference / Fixed reference inside the OCI layout.
IMAGE_REFERENCE="wspctl-runtime"
# @brief 可复现时间输入 / Reproducible timestamp input.
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git -C "$REPOSITORY_ROOT" log -1 --format=%ct)}"

# @brief 输出错误并退出 / Print an error and exit.
# @param $* 错误文本 / Error text.
die() {
    printf 'wspctl rootfs 构建失败: %s\n' "$*" >&2
    exit 1
}

command -v buildah >/dev/null 2>&1 \
    || die "缺少 Buildah；请先安装 buildah（不会 fallback 到 Docker daemon）"
[[ "$SOURCE_DATE_EPOCH" =~ ^[0-9]+$ ]] \
    || die "SOURCE_DATE_EPOCH 必须是十进制 Unix 时间"
[[ "$OUTPUT_LAYOUT" = /* ]] \
    || die "输出 OCI layout 必须是绝对路径: $OUTPUT_LAYOUT"
[[ ! -e "$OUTPUT_LAYOUT" ]] \
    || die "输出已存在，拒绝覆盖: $OUTPUT_LAYOUT"

mkdir -p "$(dirname "$OUTPUT_LAYOUT")"
STAGING_LAYOUT="$(mktemp -d "$(dirname "$OUTPUT_LAYOUT")/.oci-layout.staging-XXXXXX")"
BUILD_NAME="localhost/fogmoe/wspctl-runtime:build-${SOURCE_DATE_EPOCH}-$$"
DIGEST_FILE="$STAGING_LAYOUT/.manifest-digest.tmp"

# @brief 清理未发布 staging / Remove unpublished staging.
cleanup() {
    buildah rmi "$BUILD_NAME" >/dev/null 2>&1 || true
    if [[ -d "$STAGING_LAYOUT" ]]; then
        rm -rf --one-file-system "$STAGING_LAYOUT"
    fi
}
trap cleanup EXIT

printf 'wspctl rootfs: 构建显式 linux/amd64 OCI image\n'
buildah build \
    --file "$CONTAINERFILE" \
    --format oci \
    --platform linux/amd64 \
    --source-date-epoch "$SOURCE_DATE_EPOCH" \
    --rewrite-timestamp \
    --tag "$BUILD_NAME" \
    "$REPOSITORY_ROOT"

printf 'wspctl rootfs: 导出 OCI image layout\n'
buildah push \
    --digestfile "$DIGEST_FILE" \
    --format oci \
    "$BUILD_NAME" \
    "oci:$STAGING_LAYOUT:$IMAGE_REFERENCE"

MANIFEST_DIGEST="$(<"$DIGEST_FILE")"
[[ "$MANIFEST_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || die "Buildah 没有返回规范 OCI manifest digest"
mv "$DIGEST_FILE" "$STAGING_LAYOUT/wspctl-manifest-digest"
printf '%s\n' "$SOURCE_DATE_EPOCH" > "$STAGING_LAYOUT/wspctl-source-date-epoch"
mv "$STAGING_LAYOUT" "$OUTPUT_LAYOUT"
cleanup
trap - EXIT

printf 'layout=%s\nreference=%s\nsource_oci_manifest_digest=%s\n' \
    "$OUTPUT_LAYOUT" "$IMAGE_REFERENCE" "$MANIFEST_DIGEST"
