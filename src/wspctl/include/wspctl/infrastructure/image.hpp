#pragma once

#include "wspctl/infrastructure/common.hpp"

#include <filesystem>
#include <string>

namespace wspctl {

/**
 * @brief 只读基础镜像清单 / Immutable base-image manifest.
 *
 * manifest 是由镜像构建阶段写入的最小证明，不接受 Bot 的 .venv 或宿主 /usr 作为镜像。
 * The manifest is a minimal attestation written by image build; Bot .venv and host /usr are never images.
 */
struct ImageManifest final {
    /** @brief schema 版本 / Schema version. */
    unsigned int version{1};
    /** @brief 不可变 generation 名称 / Immutable generation name. */
    std::string generation;
    /** @brief 镜像构建器产出的根文件系统 SHA-256 / Rootfs SHA-256 emitted by image builder. */
    std::string rootfs_digest;
    /** @brief 规范 manifest 字段的 SHA-256 / SHA-256 over canonical manifest fields. */
    std::string digest;
};

/**
 * @brief 读取并严格校验 manifest / Read and strictly validate a manifest.
 * @param base_root 镜像 rootfs 根 / Image rootfs root.
 * @return 校验后的 manifest / Validated manifest.
 */
[[nodiscard]] Result<ImageManifest> load_image_manifest(const std::filesystem::path& base_root);

/**
 * @brief 校验 broker 允许挂载的镜像位置 / Validate an image location broker may mount.
 * @param base_root 请求的 rootfs 路径 / Requested rootfs path.
 * @param images_root 受运维控制的 images 根 / Operations-controlled images root.
 * @return 校验后的 manifest / Validated manifest.
 * @note 只接受 <images_root>/<generation>/rootfs 的规范路径。
 *       Only the canonical <images_root>/<generation>/rootfs layout is accepted.
 */
[[nodiscard]] Result<ImageManifest> validate_image_root(
    const std::filesystem::path& base_root,
    const std::filesystem::path& images_root);

/**
 * @brief 为受控暂存 rootfs 写入不可变镜像清单 / Seal a controlled staging rootfs with an immutable-image manifest.
 * @param base_root 尚未发布且可写的 rootfs 根 / Writable, not-yet-published rootfs root.
 * @param generation 受限的 generation 目录名 / Restricted generation directory name.
 * @return 已写入并 fsync 的 manifest / Written and fsynced manifest.
 * @note 此函数只供可信镜像构建器使用；它拒绝既有 manifest，绝不原地重签已发布 generation。
 *       This function is only for the trusted image builder; it rejects an existing manifest and
 *       never reseals an already published generation in place.
 */
[[nodiscard]] Result<ImageManifest> seal_image_root(
    const std::filesystem::path& base_root,
    const std::string& generation);

/**
 * @brief 生成 manifest 自校验摘要 / Generate manifest self-validation digest.
 * @param generation generation 名称 / Generation name.
 * @param rootfs_digest rootfs 摘要 / Rootfs digest.
 * @return 64 位小写 SHA-256 / Lowercase 64-character SHA-256.
 */
[[nodiscard]] std::string manifest_digest(
    const std::string& generation,
    const std::string& rootfs_digest);

/**
 * @brief 计算去除 manifest 本身后的确定性 rootfs 摘要 / Calculate deterministic rootfs digest excluding the manifest itself.
 * @param base_root 镜像 rootfs / Image rootfs.
 * @return 64 位小写 SHA-256 / Lowercase 64-character SHA-256.
 * @note 接受目录、regular file 与不逃逸 rootfs 的相对 symlink；拒绝设备和其他不稳定节点。
 *       Directories, regular files, and rootfs-contained relative symlinks are accepted; devices and other unstable nodes are rejected.
 */
[[nodiscard]] Result<std::string> calculate_rootfs_digest(const std::filesystem::path& base_root);

}  // namespace wspctl
