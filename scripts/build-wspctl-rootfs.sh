#!/usr/bin/env bash

# @brief 用 Buildah 生成标准 OCI image layout / Build the standard OCI image layout with Buildah.
#
# 本脚本只构建 artifact，不发布、不挂载、也不启动 wspctld。/
# This script only builds an artifact; it never publishes, mounts, or starts wspctld.

set -euo pipefail

# @brief 仓库根 / Repository root.
REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# @brief wspctl 构建输入身份计算器 / wspctl build-input identity calculator.
BUILD_IDENTITY_TOOL="$REPOSITORY_ROOT/tools/wspctl_build_identity.py"
# @brief 仅使用标准库的构建身份 Python / Standard-library-only Python used for build identities.
IDENTITY_PYTHON="${PYTHON:-python}"
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
# @brief 可复现时间输入；仅在 OCI receipt 未命中时解析 / Reproducible timestamp input; resolved only after an OCI receipt miss.
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-}"
# @brief content-addressed build serialization lock / Content-addressed build serialization lock.
BUILD_LOCK="$OUTPUT_ROOT/.build.lock"
# @brief 已校验、可恢复下载的 builder tool cache / Verified resumable builder-tool cache.
BUILD_TOOLS_ROOT="$OUTPUT_ROOT/build-tools"
# @brief 当前 OCI artifact 的源码身份收据 / Source-identity receipt for the current OCI artifact.
BUILD_RECEIPT_FILE="$OUTPUT_ROOT/build-receipt"

# @brief 输出错误并退出 / Print an error and exit.
# @param $* 错误文本 / Error text.
die() {
    printf 'wspctl rootfs 构建失败: %s\n' "$*" >&2
    exit 1
}

command -v flock >/dev/null 2>&1 \
    || die "缺少 flock，无法串行化 content-addressed publication"
command -v "$IDENTITY_PYTHON" >/dev/null 2>&1 \
    || die "缺少 Python，无法计算 OCI 构建身份: $IDENTITY_PYTHON"
[[ -f "$BUILD_IDENTITY_TOOL" ]] \
    || die "缺少 wspctl 构建身份工具: $BUILD_IDENTITY_TOOL"
[[ "$OUTPUT_ROOT" = /* ]] \
    || die "输出 artifact root 必须是绝对路径: $OUTPUT_ROOT"
[[ "$(uname -m)" == "x86_64" ]] \
    || die "当前镜像定义只固定了 linux/amd64 base manifest；拒绝在 $(uname -m) 上隐式交叉构建"

mkdir -p "$OUTPUT_ROOT/sha256"
exec 9>"$BUILD_LOCK"
flock 9

# @brief 计算 OCI rootfs 的源码输入身份 / Compute the OCI rootfs source-input identity.
# @return 小写十六进制 SHA-256 身份 / Lowercase hexadecimal SHA-256 identity.
image_build_identity() {
    "$IDENTITY_PYTHON" -I "$BUILD_IDENTITY_TOOL" \
        --source-root "$REPOSITORY_ROOT" \
        --component image \
        --attribute "platform=$IMAGE_PLATFORM" \
        --attribute "rootfs_format=oci-v1"
}

# @brief 原子记录当前可用 OCI manifest digest / Atomically record the currently usable OCI manifest digest.
# @param $1 规范 OCI manifest digest / Canonical OCI manifest digest.
# @return 成功时返回零 / Zero on success.
record_current_build_digest() {
    local manifest_digest="$1"
    local current_build_file="$OUTPUT_ROOT/current-image-digest"

    printf '%s\n' "$manifest_digest" > "$current_build_file.$$.tmp"
    mv "$current_build_file.$$.tmp" "$current_build_file"
}

# @brief 验证 OCI index、manifest、config 与 layer 的完整 SHA-256 图 / Verify the complete SHA-256 graph of OCI index, manifest, config, and layers.
# @param $1 OCI layout 路径 / OCI layout path.
# @param $2 规范 OCI manifest digest / Canonical OCI manifest digest.
# @return OCI 图完整且内容寻址一致时返回零 / Zero when the OCI graph is complete and content-addressed consistently.
oci_layout_has_verified_graph() {
    local layout_path="$1"
    local manifest_digest="$2"

    [[ -f "$layout_path/oci-layout" && -f "$layout_path/index.json" ]] || return 1
    "$IDENTITY_PYTHON" -I - "$layout_path" "$manifest_digest" "$IMAGE_REFERENCE" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

OCI_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG_MEDIA_TYPE = "application/vnd.oci.image.config.v1+json"
OCI_LAYER_MEDIA_TYPES = {
    "application/vnd.oci.image.layer.v1.tar",
    "application/vnd.oci.image.layer.v1.tar+gzip",
    "application/vnd.oci.image.layer.v1.tar+zstd",
}


def fail() -> None:
    raise SystemExit(1)


def verify_descriptor(layout: Path, descriptor: object) -> bytes:
    if not isinstance(descriptor, dict):
        fail()
    digest = descriptor.get("digest")
    size = descriptor.get("size")
    if not isinstance(digest, str) or not isinstance(size, int) or size < 0:
        fail()
    algorithm, separator, encoded_digest = digest.partition(":")
    if algorithm != "sha256" or separator != ":" or len(encoded_digest) != 64:
        fail()
    try:
        int(encoded_digest, 16)
    except ValueError:
        fail()
    blob_path = layout / "blobs" / algorithm / encoded_digest
    if not blob_path.is_file() or blob_path.stat().st_size != size:
        fail()
    hasher = hashlib.sha256()
    with blob_path.open("rb") as blob_file:
        for chunk in iter(lambda: blob_file.read(1024 * 1024), b""):
            hasher.update(chunk)
    if hasher.hexdigest() != encoded_digest:
        fail()
    return blob_path.read_bytes()


try:
    layout_path = Path(sys.argv[1])
    expected_manifest_digest = sys.argv[2]
    expected_reference = sys.argv[3]
    layout_metadata = json.loads((layout_path / "oci-layout").read_text(encoding="utf-8"))
    index = json.loads((layout_path / "index.json").read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    fail()
if layout_metadata.get("imageLayoutVersion") != "1.0.0":
    fail()
manifests = index.get("manifests")
if index.get("schemaVersion") != 2 or not isinstance(manifests, list):
    fail()
matching_descriptors = [
    descriptor
    for descriptor in manifests
    if isinstance(descriptor, dict) and descriptor.get("digest") == expected_manifest_digest
]
if len(matching_descriptors) != 1:
    fail()
manifest_descriptor = matching_descriptors[0]
annotations = manifest_descriptor.get("annotations")
if not isinstance(annotations, dict) or annotations.get("org.opencontainers.image.ref.name") != expected_reference:
    fail()
if manifest_descriptor.get("mediaType") != OCI_MANIFEST_MEDIA_TYPE:
    fail()
try:
    manifest = json.loads(verify_descriptor(layout_path, manifest_descriptor).decode("utf-8"))
except (UnicodeDecodeError, json.JSONDecodeError):
    fail()
if manifest.get("schemaVersion") != 2:
    fail()
config = manifest.get("config")
if not isinstance(config, dict) or config.get("mediaType") != OCI_CONFIG_MEDIA_TYPE:
    fail()
verify_descriptor(layout_path, config)
layers = manifest.get("layers")
if not isinstance(layers, list):
    fail()
for layer in layers:
    if not isinstance(layer, dict) or layer.get("mediaType") not in OCI_LAYER_MEDIA_TYPES:
        fail()
    verify_descriptor(layout_path, layer)
PY
}

# @brief 判断已有 OCI artifact 是否精确匹配当前源码身份 / Determine whether an existing OCI artifact exactly matches the current source identity.
# @param $1 期望源码身份 / Expected source identity.
# @return receipt、digest 和 layout 均通过时返回零 / Zero when receipt, digest, and layout all pass.
rootfs_artifact_is_current() {
    local expected_identity="$1"
    local receipt_identity=""
    local manifest_digest=""
    local record_key
    local record_value
    local destination
    local output_layout

    [[ -r "$BUILD_RECEIPT_FILE" ]] || return 1
    while IFS='=' read -r record_key record_value; do
        case "$record_key" in
            schema)
                [[ "$record_value" == "1" ]] || return 1
                ;;
            image_source_identity)
                receipt_identity="$record_value"
                ;;
            manifest_digest)
                manifest_digest="$record_value"
                ;;
        esac
    done < "$BUILD_RECEIPT_FILE"
    [[ "$receipt_identity" == "$expected_identity" ]] || return 1
    [[ "$manifest_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || return 1
    destination="$OUTPUT_ROOT/sha256/${manifest_digest#sha256:}"
    output_layout="$destination/oci-layout"
    [[ -r "$output_layout/wspctl-manifest-digest" ]] || return 1
    [[ "$(<"$output_layout/wspctl-manifest-digest")" == "$manifest_digest" ]] || return 1
    oci_layout_has_verified_graph "$output_layout" "$manifest_digest" || return 1
    record_current_build_digest "$manifest_digest"
    printf 'wspctl rootfs: 已验证源码身份相同的 OCI artifact；跳过 Buildah 构建\n'
    printf 'layout=%s\nreference=%s\nsource_oci_manifest_digest=%s\n' \
        "$output_layout" "$IMAGE_REFERENCE" "$manifest_digest"
}

# @brief 原子写入 OCI artifact 的源码身份收据 / Atomically write the OCI artifact source-identity receipt.
# @param $1 源码身份 / Source identity.
# @param $2 规范 OCI manifest digest / Canonical OCI manifest digest.
# @return 成功时返回零 / Zero on success.
write_build_receipt() {
    local source_identity="$1"
    local manifest_digest="$2"
    local temporary_file="$BUILD_RECEIPT_FILE.$$.tmp"

    printf 'schema=1\nimage_source_identity=%s\nmanifest_digest=%s\nbuilt_source_date_epoch=%s\n' \
        "$source_identity" "$manifest_digest" "$SOURCE_DATE_EPOCH" > "$temporary_file"
    mv "$temporary_file" "$BUILD_RECEIPT_FILE"
}

IMAGE_BUILD_IDENTITY="$(image_build_identity)" \
    || die "无法计算 OCI rootfs 构建身份"
if rootfs_artifact_is_current "$IMAGE_BUILD_IDENTITY"; then
    exit 0
fi

# @brief 在 receipt 未命中后解析并校验可复现构建时间 / Resolve and validate the reproducible build timestamp after a receipt miss.
# @return 成功时返回零 / Zero on success.
resolve_source_date_epoch() {
    if [[ -z "$SOURCE_DATE_EPOCH" ]]; then
        command -v git >/dev/null 2>&1 \
            || die "OCI receipt 未命中且未设置 SOURCE_DATE_EPOCH；缺少 git 无法推导可复现时间"
        if ! SOURCE_DATE_EPOCH="$(git -C "$REPOSITORY_ROOT" log -1 --format=%ct)"; then
            die "无法从 Git history 读取 SOURCE_DATE_EPOCH；请显式设置十进制 Unix 时间"
        fi
    fi
    [[ "$SOURCE_DATE_EPOCH" =~ ^[0-9]+$ ]] \
        || die "SOURCE_DATE_EPOCH 必须是十进制 Unix 时间"
}

resolve_source_date_epoch

command -v buildah >/dev/null 2>&1 \
    || die "缺少 Buildah；请先安装 buildah（不会 fallback 到 Docker daemon）"
command -v diff >/dev/null 2>&1 \
    || die "缺少 diff，无法验证同 digest 的既有 OCI artifact"
command -v curl >/dev/null 2>&1 \
    || die "缺少 curl，无法获取 hash-pinned builder tools"
command -v sha256sum >/dev/null 2>&1 \
    || die "缺少 sha256sum，无法验证 builder tools"

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

write_build_receipt "$IMAGE_BUILD_IDENTITY" "$MANIFEST_DIGEST"
record_current_build_digest "$MANIFEST_DIGEST"

printf 'layout=%s\nreference=%s\nsource_oci_manifest_digest=%s\n' \
    "$OUTPUT_LAYOUT" "$IMAGE_REFERENCE" "$MANIFEST_DIGEST"
