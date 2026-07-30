#pragma once

#include "wspctl/infrastructure/common.hpp"

#include <filesystem>
#include <string>
#include <string_view>

namespace wspctl {

/**
 * @brief 强类型 OCI image manifest digest / Strongly typed OCI image-manifest digest.
 *
 * 该类型只能通过严格 parser 构造，因此 broker 内部不存在 tag、短 hash 或任意 generation。/
 * This type can only be constructed by its strict parser, so tags, short hashes, and arbitrary
 * generations cannot exist inside the broker.
 */
class OciImageDigest final {
public:
    /**
     * @brief 解析规范 ``sha256:<64hex>`` / Parse canonical ``sha256:<64hex>``.
     * @param value 输入文本 / Input text.
     * @return 强类型 digest 或输入错误 / Strongly typed digest or input error.
     */
    [[nodiscard]] static Result<OciImageDigest> parse(std::string_view value);

    /** @brief 返回完整 OCI digest / Return the full OCI digest. */
    [[nodiscard]] const std::string& value() const noexcept;
    /** @brief 返回 path-safe hex / Return the path-safe hexadecimal component. */
    [[nodiscard]] std::string_view hex() const noexcept;

private:
    /** @brief 从已验证文本构造 / Construct from validated text. */
    explicit OciImageDigest(std::string value);
    /** @brief 规范 digest / Canonical digest. */
    std::string value_;
};

/**
 * @brief 只读基础镜像清单 / Immutable base-image manifest.
 *
 * manifest 是由镜像构建阶段写入的最小证明，不接受 Bot 的 .venv 或宿主 /usr 作为镜像。
 * The manifest is a minimal attestation written by image build; Bot .venv and host /usr are never
 * images.
 */
struct ImageManifest final {
    /** @brief schema 版本 / Schema version. */
    unsigned int version{2};
    /** @brief 权威 OCI image manifest 摘要 / Authoritative OCI image-manifest digest. */
    std::string source_oci_manifest_digest;
    /** @brief 构建目标 OCI 平台 / Build target OCI platform. */
    std::string platform;
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
 * @note 只接受 <images_root>/sha256/<manifest-hex>/rootfs 的规范路径。
 *       Only the canonical <images_root>/sha256/<manifest-hex>/rootfs layout is accepted.
 */
[[nodiscard]] Result<ImageManifest> validate_image_root(const std::filesystem::path& base_root,
                                                        const std::filesystem::path& images_root);

/**
 * @brief 为受控暂存 rootfs 写入不可变镜像清单 / Seal a controlled staging rootfs with an
 * immutable-image manifest.
 * @param base_root 尚未发布且可写的 rootfs 根 / Writable, not-yet-published rootfs root.
 * @param platform 标准 OCI 平台名 / Canonical OCI platform name.
 * @param source_oci_manifest_digest 权威 OCI manifest 的 ``sha256:...`` 摘要 /
 *        ``sha256:...`` digest of the authoritative OCI manifest.
 * @return 已写入并 fsync 的 manifest / Written and fsynced manifest.
 * @note 此函数只供可信镜像发布器使用；它拒绝既有 manifest，绝不原地重签已发布 image。
 *       This function is only for the trusted image builder; it rejects an existing manifest and
 *       never reseals an already published image in place.
 */
[[nodiscard]] Result<ImageManifest> seal_image_root(const std::filesystem::path& base_root,
                                                    const std::string& platform,
                                                    const std::string& source_oci_manifest_digest);

/**
 * @brief 生成 manifest 自校验摘要 / Generate manifest self-validation digest.
 * @param source_oci_manifest_digest 权威 OCI manifest 摘要 / Authoritative OCI manifest digest.
 * @param platform OCI 平台名 / OCI platform name.
 * @param rootfs_digest rootfs 摘要 / Rootfs digest.
 * @return 64 位小写 SHA-256 / Lowercase 64-character SHA-256.
 */
[[nodiscard]] std::string manifest_digest(const std::string& source_oci_manifest_digest,
                                          const std::string& platform,
                                          const std::string& rootfs_digest);

/**
 * @brief 计算去除 manifest 本身后的确定性 rootfs 摘要 / Calculate deterministic rootfs digest
 * excluding the manifest itself.
 * @param base_root 镜像 rootfs / Image rootfs.
 * @return 64 位小写 SHA-256 / Lowercase 64-character SHA-256.
 * @note 接受目录、regular file 与按容器根语义不逃逸 rootfs 的 symlink；拒绝设备和其他不稳定节点。
 *       Directories, regular files, and symlinks contained under container-root semantics are
 *       accepted; devices and other unstable nodes are rejected.
 */
[[nodiscard]] Result<std::string> calculate_rootfs_digest(const std::filesystem::path& base_root);

} // namespace wspctl
