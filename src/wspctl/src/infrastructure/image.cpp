#include "wspctl/infrastructure/image.hpp"

#include <openssl/evp.h>
#include <openssl/sha.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstdint>
#include <fcntl.h>
#include <fstream>
#include <linux/openat2.h>
#include <sys/statvfs.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/xattr.h>
#include <unistd.h>
#include <utility>
#include <vector>

namespace wspctl {
namespace {

/** @brief manifest 文件名 / Manifest file name. */
constexpr std::string_view kManifestFileName{".wspctl-image-manifest"};

/** @brief 判断严格的小写 SHA-256 / Check strict lowercase SHA-256. */
[[nodiscard]] bool is_sha256(const std::string_view value) {
    return value.size() == SHA256_DIGEST_LENGTH * 2U && std::all_of(value.begin(), value.end(), [](const char character) {
        return (character >= '0' && character <= '9') || (character >= 'a' && character <= 'f');
    });
}

/** @brief 判断标准 artifact digest 是否合法 / Check whether a canonical artifact digest is valid. */
[[nodiscard]] bool is_artifact_digest(const std::string_view value) {
    constexpr std::string_view kPrefix{"sha256:"};
    return value.starts_with(kPrefix) && is_sha256(value.substr(kPrefix.size()));
}

/** @brief 返回当前 broker binary 的 OCI 平台 / Return the OCI platform of the current broker binary. */
[[nodiscard]] constexpr std::string_view native_platform() {
#if defined(__x86_64__)
    return "linux/amd64";
#elif defined(__aarch64__)
    return "linux/arm64";
#else
    return "linux/unsupported";
#endif
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
    if (root_metadata.st_uid != 0U || root_metadata.st_gid != 0U ||
        manifest_metadata.st_uid != 0U || manifest_metadata.st_gid != 0U ||
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
    if (root_metadata.st_uid != 0U || root_metadata.st_gid != 0U ||
        (root_metadata.st_mode & (S_IWGRP | S_IWOTH)) != 0) {
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

/** @brief 校验标准 runtime image 的固定入口与 mountpoint / Validate fixed entrypoints and mountpoints of a standard runtime image. */
[[nodiscard]] Result<void> validate_runtime_contract(
    const std::filesystem::path& base_root) {
    const auto require_directory = [&base_root](
                                       const std::string_view relative,
                                       const mode_t expected_mode) -> Result<void> {
        struct stat metadata {};
        const std::filesystem::path path = base_root / relative;
        if (lstat(path.c_str(), &metadata) != 0 || !S_ISDIR(metadata.st_mode) ||
            metadata.st_uid != 0U || metadata.st_gid != 0U ||
            (expected_mode != 0U &&
             (metadata.st_mode & 07777U) != expected_mode)) {
            return std::unexpected(make_error(
                ErrorCode::sandbox_preflight_failed,
                "runtime image directory contract failed: /" +
                    std::string{relative}));
        }
        return {};
    };
    const auto require_executable = [&base_root](
                                        const std::string_view relative) -> Result<void> {
        const int root_fd = open(
            base_root.c_str(),
            O_PATH | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
        if (root_fd < 0) {
            return std::unexpected(errno_error(
                ErrorCode::sandbox_preflight_failed,
                "open runtime image root"));
        }
        const struct open_how how {
            .flags = static_cast<__u64>(O_PATH | O_CLOEXEC),
            .mode = 0U,
            .resolve = RESOLVE_IN_ROOT | RESOLVE_NO_MAGICLINKS,
        };
        const std::string relative_path{relative};
        const int executable_fd = static_cast<int>(syscall(
            SYS_openat2,
            root_fd,
            relative_path.c_str(),
            &how,
            sizeof(how)));
        const int open_error = errno;
        static_cast<void>(close(root_fd));
        if (executable_fd < 0) {
            errno = open_error;
            return std::unexpected(errno_error(
                ErrorCode::sandbox_preflight_failed,
                "resolve runtime image executable inside image root"));
        }
        struct stat metadata {};
        const bool stat_succeeded = fstat(executable_fd, &metadata) == 0;
        const int stat_error = errno;
        static_cast<void>(close(executable_fd));
        if (!stat_succeeded) {
            errno = stat_error;
            return std::unexpected(errno_error(
                ErrorCode::sandbox_preflight_failed,
                "stat runtime image executable"));
        }
        if (!S_ISREG(metadata.st_mode) || metadata.st_uid != 0U ||
            metadata.st_gid != 0U ||
            (metadata.st_mode & (S_IXUSR | S_IXGRP | S_IXOTH)) == 0U) {
            return std::unexpected(make_error(
                ErrorCode::sandbox_preflight_failed,
                "runtime image executable contract failed: /" +
                    std::string{relative}));
        }
        return {};
    };
    for (const std::string_view directory :
         {"proc", "dev", "run"}) {
        if (const auto result = require_directory(directory, 0755U); !result) {
            return result;
        }
    }
    for (const auto& [directory, mode] :
         std::array<std::pair<std::string_view, mode_t>, 2U>{{
             {"tmp", 01777U},
             {"workspace", 01777U},
         }}) {
        if (const auto result = require_directory(directory, mode); !result) {
            return result;
        }
    }
    for (const std::string_view executable :
         {"bin/bash", "usr/local/bin/python",
          "usr/local/libexec/wspctl/wsp-systemd"}) {
        if (const auto result = require_executable(executable); !result) {
            return result;
        }
    }
    const std::filesystem::path site_packages =
        base_root / "usr/local/lib/python3.14/site-packages";
    std::error_code error;
    if (std::filesystem::exists(site_packages, error) &&
        (!std::filesystem::is_directory(site_packages, error) ||
         !std::filesystem::is_empty(site_packages, error))) {
        return std::unexpected(make_error(
            ErrorCode::sandbox_preflight_failed,
            "runtime image must not contain Python site-packages"));
    }
    if (error) {
        return std::unexpected(make_error(
            ErrorCode::sandbox_preflight_failed,
            "cannot inspect runtime image site-packages"));
    }
    return {};
}

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

OciImageDigest::OciImageDigest(std::string value)
    : value_(std::move(value)) {}

Result<OciImageDigest> OciImageDigest::parse(const std::string_view value) {
    if (!is_artifact_digest(value)) {
        return std::unexpected(make_error(
            ErrorCode::invalid_argument,
            "OCI image digest must be sha256:<64 lowercase hex>"));
    }
    return OciImageDigest{std::string{value}};
}

const std::string& OciImageDigest::value() const noexcept {
    return value_;
}

std::string_view OciImageDigest::hex() const noexcept {
    return std::string_view{value_}.substr(std::string_view{"sha256:"}.size());
}

std::string manifest_digest(
    const std::string& source_oci_manifest_digest,
    const std::string& platform,
    const std::string& rootfs_digest) {
    return sha256_hex(
        "wspctl-image-manifest-v2\n" + source_oci_manifest_digest + "\n" +
        platform + "\n" + rootfs_digest + "\n");
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
        if (metadata.st_uid != 0U || metadata.st_gid != 0U) {
            return std::unexpected(make_error(
                ErrorCode::sandbox_preflight_failed,
                "image tree contains non-root metadata: /" + relative));
        }
        const bool intentional_sticky_directory =
            (relative == "tmp" || relative == "workspace") &&
            S_ISDIR(metadata.st_mode) &&
            (metadata.st_mode & 07777U) == 01777U;
        const mode_t unsafe_mode =
            metadata.st_mode &
            (S_ISUID | S_ISGID | S_IWGRP | S_IWOTH);
        if (!S_ISLNK(metadata.st_mode) && unsafe_mode != 0U &&
            !intentional_sticky_directory) {
            return std::unexpected(make_error(
                ErrorCode::sandbox_preflight_failed,
                "image tree contains unsafe mode: /" + relative));
        }
        errno = 0;
        if (lgetxattr(
                entry.path().c_str(), "security.capability", nullptr, 0U) >= 0 ||
            (errno != ENODATA && errno != ENOTSUP)) {
            return std::unexpected(make_error(
                ErrorCode::sandbox_preflight_failed,
                "image tree contains a file capability or unreadable xattr: /" +
                    relative));
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
            const std::filesystem::path lexical_target =
                (target_path.is_absolute()
                     ? *canonical_root / target_path.relative_path()
                     : entry.path().parent_path() / target_path)
                    .lexically_normal();
            if (!is_below(lexical_target, *canonical_root) && lexical_target != *canonical_root) {
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
    const std::string& platform,
    const std::string& source_oci_manifest_digest) {
    const auto canonical_root = canonical_existing(base_root);
    if (!canonical_root) {
        return std::unexpected(canonical_root.error());
    }
    if (platform != native_platform() ||
        !is_artifact_digest(source_oci_manifest_digest)) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "image provenance does not match the native platform"));
    }
    if (const auto sealable = validate_sealable_root(*canonical_root); !sealable) {
        return std::unexpected(sealable.error());
    }
    if (const auto contract = validate_runtime_contract(*canonical_root); !contract) {
        return std::unexpected(contract.error());
    }
    const auto rootfs_digest = calculate_rootfs_digest(*canonical_root);
    if (!rootfs_digest) {
        return std::unexpected(rootfs_digest.error());
    }
    const ImageManifest manifest{
        .version = 2U,
        .source_oci_manifest_digest = source_oci_manifest_digest,
        .platform = platform,
        .rootfs_digest = *rootfs_digest,
        .digest = manifest_digest(
            source_oci_manifest_digest, platform, *rootfs_digest),
    };
    const std::string content =
        std::string{"version=2\n"} +
        "source_oci_manifest_digest=" + manifest.source_oci_manifest_digest + "\n" +
        "platform=" + manifest.platform + "\n" +
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
    if (const auto contract = validate_runtime_contract(*canonical_root); !contract) {
        return std::unexpected(contract.error());
    }
    const std::filesystem::path manifest_path = *canonical_root / kManifestFileName;
    std::ifstream input(manifest_path);
    if (!input.is_open()) {
        return std::unexpected(make_error(ErrorCode::not_found, "cannot open image manifest"));
    }
    ImageManifest manifest;
    bool seen_version = false;
    bool seen_source_oci_manifest_digest = false;
    bool seen_platform = false;
    bool seen_rootfs_digest = false;
    bool seen_digest = false;
    std::string line;
    unsigned int line_count = 0;
    while (std::getline(input, line)) {
        ++line_count;
        if (line.empty() || line_count > 5U) {
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
            if (value != "2") {
                return std::unexpected(make_error(ErrorCode::unsupported_version, "unsupported image manifest version"));
            }
            seen_version = true;
        } else if (key == "source_oci_manifest_digest" &&
                   !seen_source_oci_manifest_digest) {
            manifest.source_oci_manifest_digest = value;
            seen_source_oci_manifest_digest = true;
        } else if (key == "platform" && !seen_platform) {
            manifest.platform = value;
            seen_platform = true;
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
    if (!input.eof() || !seen_version || !seen_source_oci_manifest_digest ||
        !seen_platform || !seen_rootfs_digest || !seen_digest ||
        !is_artifact_digest(manifest.source_oci_manifest_digest) ||
        manifest.platform != native_platform() || !is_sha256(manifest.rootfs_digest) ||
        !is_sha256(manifest.digest) ||
        manifest.digest != manifest_digest(
            manifest.source_oci_manifest_digest,
            manifest.platform,
            manifest.rootfs_digest)) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "image manifest validation failed"));
    }
    manifest.version = 2U;
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
    const std::filesystem::path digest_directory = relative.parent_path();
    const std::filesystem::path algorithm_directory = digest_directory.parent_path();
    const std::string digest_hex = digest_directory.filename().string();
    if (relative.filename() != "rootfs" || relative.parent_path().empty() ||
        algorithm_directory != "sha256" || !algorithm_directory.parent_path().empty() ||
        !is_sha256(digest_hex)) {
        return std::unexpected(make_error(
            ErrorCode::sandbox_preflight_failed,
            "base_root must be <images_root>/sha256/<manifest-hex>/rootfs"));
    }
    const auto manifest = load_image_manifest(*canonical_root);
    if (!manifest) {
        return std::unexpected(manifest.error());
    }
    if (manifest->source_oci_manifest_digest != "sha256:" + digest_hex) {
        return std::unexpected(make_error(
            ErrorCode::sandbox_preflight_failed,
            "source OCI manifest digest does not match image path"));
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
