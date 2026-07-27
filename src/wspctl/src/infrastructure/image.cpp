#include "wspctl/infrastructure/image.hpp"

#include <openssl/evp.h>
#include <openssl/sha.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstdint>
#include <fcntl.h>
#include <fstream>
#include <sys/statvfs.h>
#include <sys/stat.h>
#include <unistd.h>
#include <vector>

namespace wspctl {
namespace {

/** @brief manifest 文件名 / Manifest file name. */
constexpr std::string_view kManifestFileName{".wspctl-image-manifest"};

/** @brief 判断 generation 名称是否可安全作为目录组件 / Check whether a generation is a safe directory component. */
[[nodiscard]] bool is_safe_generation(const std::string_view generation) {
    if (generation.empty() || generation == "." || generation == ".." || generation.size() > 128U) {
        return false;
    }
    return std::all_of(generation.begin(), generation.end(), [](const unsigned char character) {
        return (character >= static_cast<unsigned char>('a') && character <= static_cast<unsigned char>('z')) ||
               (character >= static_cast<unsigned char>('A') && character <= static_cast<unsigned char>('Z')) ||
               (character >= static_cast<unsigned char>('0') && character <= static_cast<unsigned char>('9')) ||
               character == static_cast<unsigned char>('_') || character == static_cast<unsigned char>('-') ||
               character == static_cast<unsigned char>('.');
    });
}

/** @brief 判断严格的小写 SHA-256 / Check strict lowercase SHA-256. */
[[nodiscard]] bool is_sha256(const std::string_view value) {
    return value.size() == SHA256_DIGEST_LENGTH * 2U && std::all_of(value.begin(), value.end(), [](const char character) {
        return (character >= '0' && character <= '9') || (character >= 'a' && character <= 'f');
    });
}

/** @brief 计算任意 string 的 SHA-256 / Calculate SHA-256 of arbitrary string. */
[[nodiscard]] std::string sha256_hex(const std::string_view source) {
    std::array<unsigned char, SHA256_DIGEST_LENGTH> digest{};
    SHA256(reinterpret_cast<const unsigned char*>(source.data()), source.size(), digest.data());
    constexpr std::string_view kDigits{"0123456789abcdef"};
    std::string result;
    result.reserve(digest.size() * 2U);
    for (const unsigned char value : digest) {
        result.push_back(kDigits[(value >> 4U) & 0x0fU]);
        result.push_back(kDigits[value & 0x0fU]);
    }
    return result;
}

/** @brief 将路径解析为存在的规范绝对路径 / Resolve a path to an existing canonical absolute path. */
[[nodiscard]] Result<std::filesystem::path> canonical_existing(const std::filesystem::path& path) {
    if (!path.is_absolute()) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "path must be absolute"));
    }
    std::error_code error;
    const std::filesystem::path canonical = std::filesystem::canonical(path, error);
    if (error) {
        return std::unexpected(make_error(ErrorCode::not_found, "canonical path: " + error.message()));
    }
    return canonical;
}

/** @brief 判断路径是否严格在父路径之下 / Check that a path is strictly below its parent path. */
[[nodiscard]] bool is_below(const std::filesystem::path& child, const std::filesystem::path& parent) {
    const std::filesystem::path relative = child.lexically_relative(parent);
    const std::string rendered = relative.generic_string();
    return !relative.empty() && relative != "." && rendered != ".." && !rendered.starts_with("../");
}

/** @brief 校验镜像 root 与 manifest 不是 group/other 可写 / Ensure image root and manifest are not group/other writable. */
[[nodiscard]] Result<void> validate_immutable_mode(const std::filesystem::path& base_root) {
    struct stat root_metadata {};
    struct stat manifest_metadata {};
    const std::filesystem::path manifest_path = base_root / kManifestFileName;
    if (lstat(base_root.c_str(), &root_metadata) != 0 || !S_ISDIR(root_metadata.st_mode) ||
        lstat(manifest_path.c_str(), &manifest_metadata) != 0 || !S_ISREG(manifest_metadata.st_mode)) {
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed, "stat image root or manifest"));
    }
    if (root_metadata.st_uid != 0U || manifest_metadata.st_uid != 0U ||
        (root_metadata.st_mode & (S_IWGRP | S_IWOTH)) != 0 ||
        (manifest_metadata.st_mode & (S_IWGRP | S_IWOTH)) != 0) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "image root or manifest is not root-owned immutable metadata"));
    }
    return {};
}

/** @brief 校验尚未 seal 的 rootfs 元数据 / Validate metadata for an as-yet-unsealed rootfs. */
[[nodiscard]] Result<void> validate_sealable_root(const std::filesystem::path& base_root) {
    struct stat root_metadata {};
    struct stat manifest_metadata {};
    const std::filesystem::path manifest_path = base_root / kManifestFileName;
    if (lstat(base_root.c_str(), &root_metadata) != 0 || !S_ISDIR(root_metadata.st_mode)) {
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed, "stat sealable image root"));
    }
    if (root_metadata.st_uid != 0U || (root_metadata.st_mode & (S_IWGRP | S_IWOTH)) != 0) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "sealable image root is not root-owned immutable metadata"));
    }
    if (lstat(manifest_path.c_str(), &manifest_metadata) == 0) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "image manifest already exists; refusing to reseal"));
    }
    if (errno != ENOENT) {
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed, "stat image manifest before seal"));
    }
    return {};
}

/** @brief 以 O_EXCL 和 fsync 写入 manifest / Write a manifest with O_EXCL and fsync. */
[[nodiscard]] Result<void> write_manifest_atomically(
    const std::filesystem::path& base_root,
    const std::string_view content) {
    const std::filesystem::path manifest_path = base_root / kManifestFileName;
    const int manifest_fd = open(
        manifest_path.c_str(),
        O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW,
        0444);
    if (manifest_fd < 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "create image manifest"));
    }
    if (fchmod(manifest_fd, 0444) != 0) {
        const Error error = errno_error(ErrorCode::io_failure, "chmod image manifest");
        static_cast<void>(close(manifest_fd));
        return std::unexpected(error);
    }
    std::size_t offset = 0U;
    while (offset < content.size()) {
        const ssize_t count = write(
            manifest_fd,
            content.data() + static_cast<std::ptrdiff_t>(offset),
            content.size() - offset);
        if (count > 0) {
            offset += static_cast<std::size_t>(count);
            continue;
        }
        if (count < 0 && errno == EINTR) {
            continue;
        }
        const Error error = errno_error(ErrorCode::io_failure, "write image manifest");
        static_cast<void>(close(manifest_fd));
        return std::unexpected(error);
    }
    if (fsync(manifest_fd) != 0) {
        const Error error = errno_error(ErrorCode::io_failure, "fsync image manifest");
        static_cast<void>(close(manifest_fd));
        return std::unexpected(error);
    }
    if (close(manifest_fd) != 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "close image manifest"));
    }
    const int root_fd = open(base_root.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
    if (root_fd < 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "open image root for fsync"));
    }
    if (fsync(root_fd) != 0) {
        const Error error = errno_error(ErrorCode::io_failure, "fsync image root after manifest");
        static_cast<void>(close(root_fd));
        return std::unexpected(error);
    }
    if (close(root_fd) != 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "close image root after manifest"));
    }
    return {};
}

/** @brief rootfs 中一个经 lstat 固定的条目 / One rootfs entry fixed by lstat. */
struct TreeEntry final {
    /** @brief 相对于 rootfs 的规范路径 / Canonical path relative to rootfs. */
    std::string relative;
    /** @brief 绝对路径 / Absolute path. */
    std::filesystem::path absolute;
    /** @brief lstat 元数据 / lstat metadata. */
    struct stat metadata {};
    /** @brief symlink 目标（仅 symlink 时） / Symlink target (only for symlink). */
    std::string link_target;
};

/** @brief 向 EVP hash 写入无歧义长度前缀字段 / Write an unambiguous length-prefixed field into an EVP hash. */
[[nodiscard]] Result<void> hash_field(EVP_MD_CTX* context, const std::string_view field) {
    const std::uint64_t length = field.size();
    std::array<unsigned char, sizeof(length)> encoded_length{};
    for (unsigned int index = 0; index < encoded_length.size(); ++index) {
        encoded_length[index] = static_cast<unsigned char>((length >> (index * 8U)) & 0xffU);
    }
    if (EVP_DigestUpdate(context, encoded_length.data(), encoded_length.size()) != 1 ||
        EVP_DigestUpdate(context, field.data(), field.size()) != 1) {
        return std::unexpected(make_error(ErrorCode::internal, "update image digest"));
    }
    return {};
}

/** @brief 向 EVP hash 写入 64-bit 元数据 / Write 64-bit metadata into an EVP hash. */
[[nodiscard]] Result<void> hash_u64(EVP_MD_CTX* context, const std::uint64_t value) {
    std::array<unsigned char, sizeof(value)> bytes{};
    for (unsigned int index = 0; index < bytes.size(); ++index) {
        bytes[index] = static_cast<unsigned char>((value >> (index * 8U)) & 0xffU);
    }
    if (EVP_DigestUpdate(context, bytes.data(), bytes.size()) != 1) {
        return std::unexpected(make_error(ErrorCode::internal, "update image metadata digest"));
    }
    return {};
}

/** @brief 为单个 regular file 将内容写入 hash 并检查 TOCTOU inode / Hash one regular file and check its inode against lstat. */
[[nodiscard]] Result<void> hash_regular_file(EVP_MD_CTX* context, const TreeEntry& entry) {
    const int fd = open(entry.absolute.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0) {
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed, "open image regular file"));
    }
    struct stat opened {};
    if (fstat(fd, &opened) != 0 || opened.st_dev != entry.metadata.st_dev || opened.st_ino != entry.metadata.st_ino ||
        opened.st_size != entry.metadata.st_size || !S_ISREG(opened.st_mode)) {
        close(fd);
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "image changed while hashing"));
    }
    std::array<unsigned char, 64U * 1024U> buffer{};
    for (;;) {
        const ssize_t count = read(fd, buffer.data(), buffer.size());
        if (count > 0) {
            if (EVP_DigestUpdate(context, buffer.data(), static_cast<std::size_t>(count)) != 1) {
                close(fd);
                return std::unexpected(make_error(ErrorCode::internal, "hash image file content"));
            }
            continue;
        }
        if (count == 0) {
            break;
        }
        if (errno == EINTR) {
            continue;
        }
        const Error error = errno_error(ErrorCode::sandbox_preflight_failed, "read image regular file");
        close(fd);
        return std::unexpected(error);
    }
    if (close(fd) != 0) {
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed, "close image regular file"));
    }
    return {};
}

/** @brief 将 digest bytes 渲染为小写 hex / Render digest bytes as lowercase hex. */
[[nodiscard]] std::string render_hex(const unsigned char* bytes, const std::size_t size) {
    constexpr std::string_view kDigits{"0123456789abcdef"};
    std::string output;
    output.reserve(size * 2U);
    for (std::size_t index = 0; index < size; ++index) {
        output.push_back(kDigits[(bytes[index] >> 4U) & 0x0fU]);
        output.push_back(kDigits[bytes[index] & 0x0fU]);
    }
    return output;
}

}  // namespace

std::string manifest_digest(const std::string& generation, const std::string& rootfs_digest) {
    return sha256_hex("wspctl-image-manifest-v1\n" + generation + "\n" + rootfs_digest + "\n");
}

Result<std::string> calculate_rootfs_digest(const std::filesystem::path& base_root) {
    const auto canonical_root = canonical_existing(base_root);
    if (!canonical_root) {
        return std::unexpected(canonical_root.error());
    }
    std::vector<TreeEntry> entries;
    std::error_code iteration_error;
    std::filesystem::recursive_directory_iterator iterator(
        *canonical_root,
        std::filesystem::directory_options::none,
        iteration_error);
    if (iteration_error) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "iterate image rootfs: " + iteration_error.message()));
    }
    const std::filesystem::recursive_directory_iterator end;
    while (iterator != end) {
        const std::filesystem::directory_entry entry = *iterator;
        const std::filesystem::path relative_path = entry.path().lexically_relative(*canonical_root);
        const std::string relative = relative_path.generic_string();
        if (relative.empty() || relative == kManifestFileName) {
            iterator.increment(iteration_error);
            if (iteration_error) {
                return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "advance image iterator: " + iteration_error.message()));
            }
            continue;
        }
        struct stat metadata {};
        if (lstat(entry.path().c_str(), &metadata) != 0) {
            return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed, "lstat image tree entry"));
        }
        if (!S_ISREG(metadata.st_mode) && !S_ISDIR(metadata.st_mode) && !S_ISLNK(metadata.st_mode)) {
            return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "image tree contains device or unsupported inode"));
        }
        if (metadata.st_uid != 0U) {
            return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "image tree contains a non-root-owned inode"));
        }
        std::string link_target;
        if (S_ISLNK(metadata.st_mode)) {
            std::array<char, 4096> target_buffer{};
            const ssize_t target_size = readlink(entry.path().c_str(), target_buffer.data(), target_buffer.size());
            if (target_size <= 0 || static_cast<std::size_t>(target_size) == target_buffer.size()) {
                return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "invalid image symlink target"));
            }
            link_target.assign(target_buffer.data(), static_cast<std::size_t>(target_size));
            const std::filesystem::path target_path{link_target};
            if (target_path.is_absolute()) {
                return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "absolute image symlink target is forbidden"));
            }
            const std::filesystem::path lexical_target = (entry.path().parent_path() / target_path).lexically_normal();
            const auto canonical_target = canonical_existing(lexical_target);
            if (!canonical_target || (!is_below(*canonical_target, *canonical_root) && *canonical_target != *canonical_root)) {
                return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "image symlink escapes rootfs"));
            }
        }
        entries.push_back(TreeEntry{
            .relative = relative,
            .absolute = entry.path(),
            .metadata = metadata,
            .link_target = std::move(link_target),
        });
        iterator.increment(iteration_error);
        if (iteration_error) {
            return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "advance image iterator: " + iteration_error.message()));
        }
    }
    std::sort(entries.begin(), entries.end(), [](const TreeEntry& left, const TreeEntry& right) {
        return left.relative < right.relative;
    });
    EVP_MD_CTX* context = EVP_MD_CTX_new();
    if (context == nullptr || EVP_DigestInit_ex(context, EVP_sha256(), nullptr) != 1) {
        if (context != nullptr) {
            EVP_MD_CTX_free(context);
        }
        return std::unexpected(make_error(ErrorCode::internal, "initialize rootfs digest"));
    }
    if (const auto root_tag = hash_field(context, "wspctl-rootfs-v1"); !root_tag) {
        EVP_MD_CTX_free(context);
        return std::unexpected(root_tag.error());
    }
    for (const TreeEntry& entry : entries) {
        const char type = S_ISDIR(entry.metadata.st_mode) ? 'd' : (S_ISLNK(entry.metadata.st_mode) ? 'l' : 'f');
        const auto type_hash = hash_field(context, std::string_view(&type, 1U));
        const auto path_hash = hash_field(context, entry.relative);
        const auto mode_hash = hash_u64(context, static_cast<std::uint64_t>(entry.metadata.st_mode & 07777));
        const auto uid_hash = hash_u64(context, static_cast<std::uint64_t>(entry.metadata.st_uid));
        const auto gid_hash = hash_u64(context, static_cast<std::uint64_t>(entry.metadata.st_gid));
        const auto size_hash = hash_u64(context, static_cast<std::uint64_t>(entry.metadata.st_size));
        if (!type_hash || !path_hash || !mode_hash || !uid_hash || !gid_hash || !size_hash) {
            EVP_MD_CTX_free(context);
            return std::unexpected(make_error(ErrorCode::internal, "serialize rootfs digest entry"));
        }
        if (S_ISREG(entry.metadata.st_mode)) {
            if (const auto file_hash = hash_regular_file(context, entry); !file_hash) {
                EVP_MD_CTX_free(context);
                return std::unexpected(file_hash.error());
            }
        } else if (S_ISLNK(entry.metadata.st_mode)) {
            if (const auto link_hash = hash_field(context, entry.link_target); !link_hash) {
                EVP_MD_CTX_free(context);
                return std::unexpected(link_hash.error());
            }
        }
    }
    std::array<unsigned char, EVP_MAX_MD_SIZE> digest{};
    unsigned int digest_size = 0;
    if (EVP_DigestFinal_ex(context, digest.data(), &digest_size) != 1 || digest_size != SHA256_DIGEST_LENGTH) {
        EVP_MD_CTX_free(context);
        return std::unexpected(make_error(ErrorCode::internal, "finalize rootfs digest"));
    }
    EVP_MD_CTX_free(context);
    return render_hex(digest.data(), digest_size);
}

/** @brief 对受控 rootfs 计算摘要并一次性写入 manifest / Hash a controlled rootfs and write its manifest once. */
Result<ImageManifest> seal_image_root(
    const std::filesystem::path& base_root,
    const std::string& generation) {
    const auto canonical_root = canonical_existing(base_root);
    if (!canonical_root) {
        return std::unexpected(canonical_root.error());
    }
    if (!is_safe_generation(generation)) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "unsafe image generation"));
    }
    if (const auto sealable = validate_sealable_root(*canonical_root); !sealable) {
        return std::unexpected(sealable.error());
    }
    const auto rootfs_digest = calculate_rootfs_digest(*canonical_root);
    if (!rootfs_digest) {
        return std::unexpected(rootfs_digest.error());
    }
    const ImageManifest manifest{
        .version = 1U,
        .generation = generation,
        .rootfs_digest = *rootfs_digest,
        .digest = manifest_digest(generation, *rootfs_digest),
    };
    const std::string content =
        std::string{"version=1\n"} +
        "generation=" + manifest.generation + "\n" +
        "rootfs_digest=" + manifest.rootfs_digest + "\n" +
        "digest=" + manifest.digest + "\n";
    if (const auto written = write_manifest_atomically(*canonical_root, content); !written) {
        return std::unexpected(written.error());
    }
    return manifest;
}

Result<ImageManifest> load_image_manifest(const std::filesystem::path& base_root) {
    const auto canonical_root = canonical_existing(base_root);
    if (!canonical_root) {
        return std::unexpected(canonical_root.error());
    }
    if (const auto mode = validate_immutable_mode(*canonical_root); !mode) {
        return std::unexpected(mode.error());
    }
    const std::filesystem::path manifest_path = *canonical_root / kManifestFileName;
    std::ifstream input(manifest_path);
    if (!input.is_open()) {
        return std::unexpected(make_error(ErrorCode::not_found, "cannot open image manifest"));
    }
    ImageManifest manifest;
    bool seen_version = false;
    bool seen_generation = false;
    bool seen_rootfs_digest = false;
    bool seen_digest = false;
    std::string line;
    unsigned int line_count = 0;
    while (std::getline(input, line)) {
        ++line_count;
        if (line.empty() || line_count > 4U) {
            return std::unexpected(make_error(ErrorCode::malformed_frame, "manifest has an empty or extra line"));
        }
        const std::size_t separator = line.find('=');
        if (separator == std::string::npos || separator == 0U || separator == line.size() - 1U ||
            line.find('=', separator + 1U) != std::string::npos) {
            return std::unexpected(make_error(ErrorCode::malformed_frame, "invalid manifest field"));
        }
        const std::string_view key{line.data(), separator};
        const std::string_view value{line.data() + static_cast<std::ptrdiff_t>(separator + 1U), line.size() - separator - 1U};
        if (key == "version" && !seen_version) {
            if (value != "1") {
                return std::unexpected(make_error(ErrorCode::unsupported_version, "unsupported image manifest version"));
            }
            seen_version = true;
        } else if (key == "generation" && !seen_generation) {
            manifest.generation = value;
            seen_generation = true;
        } else if (key == "rootfs_digest" && !seen_rootfs_digest) {
            manifest.rootfs_digest = value;
            seen_rootfs_digest = true;
        } else if (key == "digest" && !seen_digest) {
            manifest.digest = value;
            seen_digest = true;
        } else {
            return std::unexpected(make_error(ErrorCode::malformed_frame, "duplicate or unknown image manifest field"));
        }
    }
    if (!input.eof() || !seen_version || !seen_generation || !seen_rootfs_digest || !seen_digest ||
        !is_safe_generation(manifest.generation) || !is_sha256(manifest.rootfs_digest) || !is_sha256(manifest.digest) ||
        manifest.digest != manifest_digest(manifest.generation, manifest.rootfs_digest)) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "image manifest validation failed"));
    }
    manifest.version = 1U;
    return manifest;
}

Result<ImageManifest> validate_image_root(
    const std::filesystem::path& base_root,
    const std::filesystem::path& images_root) {
    const auto canonical_root = canonical_existing(base_root);
    const auto canonical_images = canonical_existing(images_root);
    if (!canonical_root) {
        return std::unexpected(canonical_root.error());
    }
    if (!canonical_images) {
        return std::unexpected(canonical_images.error());
    }
    if (!is_below(*canonical_root, *canonical_images)) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "base_root is outside trusted images_root"));
    }
    const std::filesystem::path relative = canonical_root->lexically_relative(*canonical_images);
    if (relative.filename() != "rootfs" || relative.parent_path().empty() ||
        !relative.parent_path().parent_path().empty() || !is_safe_generation(relative.parent_path().filename().string())) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "base_root must be <images_root>/<generation>/rootfs"));
    }
    const auto manifest = load_image_manifest(*canonical_root);
    if (!manifest) {
        return std::unexpected(manifest.error());
    }
    if (std::filesystem::path(manifest->generation) != relative.parent_path().filename()) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "manifest generation does not match image path"));
    }
    struct statvfs filesystem_metadata {};
    if (statvfs(canonical_root->c_str(), &filesystem_metadata) != 0 || (filesystem_metadata.f_flag & ST_RDONLY) == 0U) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "image rootfs must be on a read-only filesystem"));
    }
    const auto actual_digest = calculate_rootfs_digest(*canonical_root);
    if (!actual_digest || *actual_digest != manifest->rootfs_digest) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "image rootfs content digest does not match manifest"));
    }
    return manifest;
}

}  // namespace wspctl
