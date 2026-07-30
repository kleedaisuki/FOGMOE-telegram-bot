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
# @brief hash-pinned builder tool inputs / Hash-pinned builder-tool inputs.
BUILD_TOOLS_LOCK="$REPOSITORY_ROOT/deploy/wspctl/image/build-tools.lock"
# @brief content-addressed build artifact 根 / Content-addressed build-artifact root.
OUTPUT_ROOT="${1:-$REPOSITORY_ROOT/.runtime/wspctl-rootfs}"
# @brief OCI layout 内的固定 reference / Fixed reference inside the OCI layout.
IMAGE_REFERENCE="wspctl-runtime"
# @brief 当前唯一受支持的 runtime 平台 / Sole currently supported runtime platform.
IMAGE_PLATFORM="linux/amd64"
# @brief 可复现时间输入 / Reproducible timestamp input.
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git -C "$REPOSITORY_ROOT" log -1 --format=%ct)}"
# @brief content-addressed build serialization lock / Content-addressed build serialization lock.
BUILD_LOCK="$OUTPUT_ROOT/.build.lock"
# @brief 已校验、可恢复下载的 builder tool cache / Verified resumable builder-tool cache.
BUILD_TOOLS_ROOT="$OUTPUT_ROOT/build-tools"

# @brief 输出错误并退出 / Print an error and exit.
# @param $* 错误文本 / Error text.
die() {
    printf 'wspctl rootfs 构建失败: %s\n' "$*" >&2
    exit 1
}

command -v buildah >/dev/null 2>&1 \
    || die "缺少 Buildah；请先安装 buildah（不会 fallback 到 Docker daemon）"
command -v diff >/dev/null 2>&1 \
    || die "缺少 diff，无法验证同 digest 的既有 OCI artifact"
command -v flock >/dev/null 2>&1 \
    || die "缺少 flock，无法串行化 content-addressed publication"
command -v curl >/dev/null 2>&1 \
    || die "缺少 curl，无法获取 hash-pinned builder tools"
command -v sha256sum >/dev/null 2>&1 \
    || die "缺少 sha256sum，无法验证 builder tools"
[[ "$SOURCE_DATE_EPOCH" =~ ^[0-9]+$ ]] \
    || die "SOURCE_DATE_EPOCH 必须是十进制 Unix 时间"
[[ "$OUTPUT_ROOT" = /* ]] \
    || die "输出 artifact root 必须是绝对路径: $OUTPUT_ROOT"
[[ "$(uname -m)" == "x86_64" ]] \
    || die "当前镜像定义只固定了 linux/amd64 base manifest；拒绝在 $(uname -m) 上隐式交叉构建"

mkdir -p "$OUTPUT_ROOT/sha256"
exec 9>"$BUILD_LOCK"
flock 9

# @brief 下载并校验 immutable builder tools / Download and verify immutable builder tools.
prepare_build_tools() {
    local expected_digest
    local filename
    local url
    local extra
    local destination
    local partial
    local actual_digest
    local attempt
    local downloaded

    mkdir -p "$BUILD_TOOLS_ROOT"
    while read -r expected_digest filename url extra; do
        [[ -z "$expected_digest" || "$expected_digest" == \#* ]] && continue
        [[ "$expected_digest" =~ ^[0-9a-f]{64}$ && "$filename" =~ ^[A-Za-z0-9._-]+$ \
            && ( "$url" == https://files.pythonhosted.org/* \
                || "$url" == https://nodejs.org/dist/* \
                || "$url" == https://registry.npmjs.org/pnpm/-/* ) \
            && -z "$extra" ]] \
            || die "build-tools.lock 含非法记录"
        destination="$BUILD_TOOLS_ROOT/$filename"
        partial="$destination.partial"
        if [[ -f "$destination" ]]; then
            actual_digest="$(sha256sum "$destination" | awk '{print $1}')"
            [[ "$actual_digest" == "$expected_digest" ]] \
                || die "cached builder tool digest 不匹配: $destination"
            continue
        fi
        printf 'wspctl rootfs: 获取 hash-pinned builder tool %s\n' "$filename"
        downloaded=false
        for ((attempt = 1; attempt <= 50; ++attempt)); do
            if curl --fail --location --show-error \
                --connect-timeout 30 --max-time 300 \
                --continue-at - --output "$partial" "$url"; then
                downloaded=true
                break
            fi
            printf 'wspctl rootfs: builder tool 下载中断，保留 partial 并续传（%d/50）\n' \
                "$attempt" >&2
            sleep 2
        done
        [[ "$downloaded" == true ]] \
            || die "builder tool 在 50 次有界续传后仍未完成: $filename"
        actual_digest="$(sha256sum "$partial" | awk '{print $1}')"
        if [[ "$actual_digest" != "$expected_digest" ]]; then
            rm -f -- "$partial"
            die "downloaded builder tool digest 不匹配: $filename"
        fi
        mv "$partial" "$destination"
    done < "$BUILD_TOOLS_LOCK"
}

prepare_build_tools
STAGING_ROOT="$(mktemp -d "$OUTPUT_ROOT/.build-staging-XXXXXX")"
STAGING_LAYOUT="$STAGING_ROOT/oci-layout"
BUILD_NAME="localhost/fogmoe/wspctl-runtime:build-${SOURCE_DATE_EPOCH}-$$"
DIGEST_FILE="$STAGING_ROOT/.manifest-digest.tmp"

# @brief 清理未发布 staging / Remove unpublished staging.
cleanup() {
    buildah rmi "$BUILD_NAME" >/dev/null 2>&1 || true
    if [[ -d "$STAGING_ROOT" ]]; then
        rm -rf --one-file-system "$STAGING_ROOT"
    fi
}
trap cleanup EXIT

printf 'wspctl rootfs: 构建显式 linux/amd64 OCI image\n'
buildah build \
    --file "$CONTAINERFILE" \
    --format oci \
    --http-proxy=true \
    --layers \
    --build-context "wspctl-build-tools=$BUILD_TOOLS_ROOT" \
    --platform "$IMAGE_PLATFORM" \
    --timestamp "$SOURCE_DATE_EPOCH" \
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
DIGEST_HEX="${MANIFEST_DIGEST#sha256:}"
DESTINATION="$OUTPUT_ROOT/sha256/$DIGEST_HEX"
OUTPUT_LAYOUT="$DESTINATION/oci-layout"
mv "$DIGEST_FILE" "$STAGING_LAYOUT/wspctl-manifest-digest"
printf '%s\n' "$SOURCE_DATE_EPOCH" > "$STAGING_LAYOUT/wspctl-source-date-epoch"
if [[ -e "$DESTINATION" ]]; then
    diff --recursive --brief "$STAGING_ROOT" "$DESTINATION" >/dev/null \
        || die "同 manifest digest 的既有 build artifact 内容不一致: $DESTINATION"
else
    mv "$STAGING_ROOT" "$DESTINATION"
fi
cleanup
trap - EXIT

CURRENT_BUILD_FILE="$OUTPUT_ROOT/current-image-digest"
printf '%s\n' "$MANIFEST_DIGEST" > "$CURRENT_BUILD_FILE.$$.tmp"
mv "$CURRENT_BUILD_FILE.$$.tmp" "$CURRENT_BUILD_FILE"

printf 'layout=%s\nreference=%s\nsource_oci_manifest_digest=%s\n' \
    "$OUTPUT_LAYOUT" "$IMAGE_REFERENCE" "$MANIFEST_DIGEST"
