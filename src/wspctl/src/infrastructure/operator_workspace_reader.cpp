#include "wspctl/infrastructure/operator_workspace_reader.hpp"

#include <algorithm>
#include <cerrno>
#include <cstring>
#include <dirent.h>
#include <fcntl.h>
#include <linux/openat2.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <utility>

namespace wspctl {
namespace {

/** @brief persistent OverlayFS upper directory 名称 / Persistent OverlayFS upper-directory name. */
constexpr std::string_view kWorkspaceUpperDirectoryName{"upper"};

/** @brief 拥有一个临时文件描述符 / Own one temporary file descriptor. */
class OwnedFileDescriptor final {
public:
    /**
     * @brief 以 FD 构造 owner / Construct an owner from an FD.
     * @param descriptor 被拥有 FD / Owned FD.
     */
    explicit OwnedFileDescriptor(const int descriptor) noexcept : descriptor_(descriptor) {}

    /** @brief 析构时关闭 FD / Close the FD on destruction. */
    ~OwnedFileDescriptor() {
        if (descriptor_ >= 0) {
            static_cast<void>(close(descriptor_));
        }
    }

    /** @brief 禁止复制 FD ownership / Copying FD ownership is forbidden. */
    OwnedFileDescriptor(const OwnedFileDescriptor&) = delete;
    /** @brief 禁止复制赋值 FD ownership / Copy-assigning FD ownership is forbidden. */
    OwnedFileDescriptor& operator=(const OwnedFileDescriptor&) = delete;

    /**
     * @brief 移交 FD ownership / Transfer FD ownership.
     * @param other 被移交 owner / Owner being transferred.
     */
    OwnedFileDescriptor(OwnedFileDescriptor&& other) noexcept : descriptor_(std::exchange(other.descriptor_, -1)) {}

    /**
     * @brief 移动赋值 FD ownership / Move-assign FD ownership.
     * @param other 被移交 owner / Owner being transferred.
     * @return 当前 owner / This owner.
     */
    OwnedFileDescriptor& operator=(OwnedFileDescriptor&& other) noexcept {
        if (this != &other) {
            if (descriptor_ >= 0) {
                static_cast<void>(close(descriptor_));
            }
            descriptor_ = std::exchange(other.descriptor_, -1);
        }
        return *this;
    }

    /** @brief 借用受控 FD / Borrow the controlled FD. */
    [[nodiscard]] int get() const noexcept { return descriptor_; }

private:
    /** @brief 被拥有的 FD / Owned FD. */
    int descriptor_{-1};
};

/**
 * @brief 以 openat2 打开一个不跟随符号链接的子目录 / Open a child directory without following symbolic links via openat2.
 * @param parent_fd 已验证父目录 FD / Verified parent-directory FD.
 * @param component 单一路径分量 / One path component.
 * @return 子目录 FD 或 fail-closed 错误 / Child-directory FD or a fail-closed error.
 */
[[nodiscard]] Result<OwnedFileDescriptor> open_directory_beneath(
    const int parent_fd,
    const std::string_view component) {
    if (parent_fd < 0 || component.empty() || component == "." || component == ".." ||
        component.find('/') != std::string_view::npos || component.find('\0') != std::string_view::npos) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "invalid operator workspace path component"));
    }
#ifdef SYS_openat2
    std::string material(component);
    open_how how{};
    // The operator path is a query surface: suppress atime metadata updates even when the
    // underlying XFS mount uses a policy more permissive than relatime/noatime.
    how.flags = static_cast<std::uint64_t>(O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW | O_NOATIME);
    how.resolve = static_cast<std::uint64_t>(
        RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS | RESOLVE_NO_XDEV);
    const int descriptor = static_cast<int>(syscall(SYS_openat2, parent_fd, material.c_str(), &how, sizeof(how)));
    if (descriptor < 0) {
        /** @brief 保留 openat2 errno，避免 domain not-found 被安全映射吞掉 / Saved openat2 errno so a domain not-found is not swallowed by the security mapping. */
        const int saved_errno = errno;
        if (saved_errno == ENOENT || saved_errno == ENOTDIR) {
            return std::unexpected(make_error(ErrorCode::not_found, "operator workspace directory was not found"));
        }
        errno = saved_errno;
        return std::unexpected(errno_error(ErrorCode::permission_denied, "openat2 operator workspace directory"));
    }
    struct stat metadata {};
    if (fstat(descriptor, &metadata) != 0 || !S_ISDIR(metadata.st_mode)) {
        /** @brief 保留 syscall errno / Saved syscall errno. */
        const int saved_errno = errno;
        static_cast<void>(close(descriptor));
        errno = saved_errno;
        return std::unexpected(make_error(ErrorCode::permission_denied, "operator workspace component is not a directory"));
    }
    return OwnedFileDescriptor(descriptor);
#else
    static_cast<void>(component);
    return std::unexpected(make_error(
        ErrorCode::sandbox_preflight_failed,
        "kernel headers do not expose openat2; operator workspace traversal refuses a weaker fallback"));
#endif
}

/**
 * @brief 判断 OverlayFS internal whiteout 名称 / Check an OverlayFS internal whiteout name.
 * @param name 原始 POSIX directory-entry 名称 / Raw POSIX directory-entry name.
 * @return 是否应从 operator view 隐藏 / Whether it should be hidden from the operator view.
 */
[[nodiscard]] bool is_overlay_internal_name(const std::string_view name) noexcept {
    return name.starts_with(".wh.");
}

/**
 * @brief 将不跟随链接的 stat mode 转为 allowlisted 节点类型 / Convert an lstat mode into an allowlisted node kind.
 * @param mode `fstatat(..., AT_SYMLINK_NOFOLLOW)` 返回的 mode / Mode returned by `fstatat(..., AT_SYMLINK_NOFOLLOW)`.
 * @return allowlisted 节点类型；特殊文件为空 / Allowlisted node kind; empty for special files.
 */
[[nodiscard]] std::optional<domain::WorkspaceEntryKind> entry_kind_from_mode(const mode_t mode) noexcept {
    if (S_ISREG(mode)) {
        return domain::WorkspaceEntryKind::regular_file;
    }
    if (S_ISDIR(mode)) {
        return domain::WorkspaceEntryKind::directory;
    }
    if (S_ISLNK(mode)) {
        return domain::WorkspaceEntryKind::symbolic_link;
    }
    return std::nullopt;
}

/**
 * @brief 判断某目录项是否是 POSIX dot 条目 / Check whether a directory entry is a POSIX dot entry.
 * @param name 原始 POSIX directory-entry 名称 / Raw POSIX directory-entry name.
 * @return 是否为 `.` 或 `..` / Whether it is `.` or `..`.
 */
[[nodiscard]] bool is_dot_entry(const std::string_view name) noexcept {
    return name == "." || name == "..";
}

}  // namespace

Result<domain::WorkspaceListing> OperatorWorkspaceReader::list(
    const RuntimeQuotaBinding& binding,
    const domain::OperatorWorkspacePath& path) const {
    /** @brief 已验证 upper root 的 host path；永不拼接用户 path / Verified upper-root host path; no user path is ever appended. */
    const std::filesystem::path upper_root = binding.workspace_dir / kWorkspaceUpperDirectoryName;
    OwnedFileDescriptor directory_fd(
        open(upper_root.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW | O_NOATIME));
    if (directory_fd.get() < 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "open verified operator workspace upper directory"));
    }
    struct stat root_metadata {};
    if (fstat(directory_fd.get(), &root_metadata) != 0 || !S_ISDIR(root_metadata.st_mode)) {
        return std::unexpected(make_error(ErrorCode::io_failure, "verified operator workspace upper root is not a directory"));
    }
    for (const std::string_view component : path.relative_components()) {
        auto child = open_directory_beneath(directory_fd.get(), component);
        if (!child) {
            return std::unexpected(child.error());
        }
        directory_fd = std::move(*child);
    }

    /** @brief `fdopendir` 专用 FD，避免接管主 directory FD / Dedicated FD for fdopendir, avoiding transfer of the primary directory FD. */
    const int scan_fd = dup(directory_fd.get());
    if (scan_fd < 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "duplicate operator workspace directory FD"));
    }
    DIR* const directory = fdopendir(scan_fd);
    if (directory == nullptr) {
        static_cast<void>(close(scan_fd));
        return std::unexpected(errno_error(ErrorCode::io_failure, "fdopendir operator workspace directory"));
    }

    std::vector<domain::WorkspaceEntry> entries;
    entries.reserve(domain::kOperatorWorkspaceListingLimit);
    bool truncated{false};
    for (;;) {
        // POSIX signals EOF through a null result with errno unchanged. Reset it before every
        // read so an expected concurrent ENOENT below cannot be mistaken for a readdir failure.
        errno = 0;
        dirent* const entry = readdir(directory);
        if (entry == nullptr) {
            break;
        }
        /** @brief 从内核 NUL-terminated dirent 取得的原始名称 / Raw name obtained from the kernel NUL-terminated dirent. */
        const std::string_view raw_name(entry->d_name);
        if (is_dot_entry(raw_name) || is_overlay_internal_name(raw_name)) {
            continue;
        }
        struct stat metadata {};
        if (fstatat(directory_fd.get(), entry->d_name, &metadata, AT_SYMLINK_NOFOLLOW) != 0) {
            if (errno == ENOENT) {
                // A concurrently removed untrusted entry is absent from this snapshot; it is
                // not a directory enumeration error and must not poison the next EOF check.
                errno = 0;
                continue;
            }
            const int saved_errno = errno;
            static_cast<void>(closedir(directory));
            errno = saved_errno;
            return std::unexpected(errno_error(ErrorCode::io_failure, "inspect operator workspace directory entry"));
        }
        const std::optional<domain::WorkspaceEntryKind> kind = entry_kind_from_mode(metadata.st_mode);
        if (!kind.has_value()) {
            continue;
        }
        if (entries.size() == domain::kOperatorWorkspaceListingLimit) {
            truncated = true;
            break;
        }
        const auto encoded_name = domain::encode_workspace_entry_name(raw_name);
        if (!encoded_name) {
            static_cast<void>(closedir(directory));
            return std::unexpected(make_error(ErrorCode::io_failure, "operator workspace directory entry name is invalid"));
        }
        /** @brief 仅普通文件可以报告逻辑长度 / Only regular files may report a logical length. */
        const std::uint64_t size_bytes = *kind == domain::WorkspaceEntryKind::regular_file && metadata.st_size >= 0
            ? static_cast<std::uint64_t>(metadata.st_size)
            : 0U;
        const auto workspace_entry = domain::WorkspaceEntry::create(*encoded_name, *kind, size_bytes);
        if (!workspace_entry) {
            static_cast<void>(closedir(directory));
            return std::unexpected(make_error(ErrorCode::io_failure, "operator workspace directory entry violates domain policy"));
        }
        entries.push_back(*workspace_entry);
    }
    if (errno != 0) {
        const int saved_errno = errno;
        static_cast<void>(closedir(directory));
        errno = saved_errno;
        return std::unexpected(errno_error(ErrorCode::io_failure, "enumerate operator workspace directory"));
    }
    if (closedir(directory) != 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "close operator workspace directory"));
    }
    std::ranges::sort(entries, [](const domain::WorkspaceEntry& left, const domain::WorkspaceEntry& right) {
        return left.encoded_name() < right.encoded_name();
    });
    return domain::WorkspaceListing{
        .path = path,
        .entries = std::move(entries),
        .truncated = truncated,
    };
}

}  // namespace wspctl
