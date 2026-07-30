#include "wspctl/infrastructure/xfs_project_quota.hpp"

#include "wspctl/domain/runtime.hpp"

#include <openssl/sha.h>

#include <linux/dqblk_xfs.h>
#include <linux/fs.h>
#include <linux/magic.h>

#include <array>
#include <algorithm>
#include <atomic>
#include <charconv>
#include <cerrno>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <dirent.h>
#include <fcntl.h>
#include <limits>
#include <optional>
#include <string>
#include <string_view>
#include <sys/file.h>
#include <sys/ioctl.h>
#include <sys/quota.h>
#include <sys/stat.h>
#include <sys/statfs.h>
#include <sys/statvfs.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

namespace wspctl {
namespace {

/** @brief XFS quota basic-block 的字节数 / Byte count of an XFS quota basic block. */
constexpr std::uint64_t kQuotaBasicBlockBytes{512U};
/** @brief registry record 格式版本 / Registry-record format version. */
constexpr std::string_view kRegistryVersion{"1"};
/** @brief 单个 quota registry record 的最大字节数 / Maximum bytes of one quota-registry record. */
constexpr std::size_t kMaxRegistryRecordBytes{1024U};
/** @brief registry next-ID 文件的最大字节数 / Maximum bytes of the registry next-ID file. */
constexpr std::size_t kMaxNextIdBytes{64U};
/** @brief root-owned quota registry 目录名 / Root-owned quota-registry directory name. */
constexpr std::string_view kRegistryDirectoryName{"quota-registry"};
/** @brief runtime record 子目录名 / Runtime-record child-directory name. */
constexpr std::string_view kRegistryRecordsDirectoryName{"runtimes"};
/** @brief cross-process registry lock 文件名 / Cross-process registry-lock file name. */
constexpr std::string_view kRegistryLockName{"lock"};
/** @brief 每个 runtime 的跨 broker activation lock 文件名 / Per-runtime cross-broker activation-lock filename. */
constexpr std::string_view kActivationLockName{"activation.lock"};
/** @brief 单调 project-ID allocator 文件名 / Monotonic project-ID allocator file name. */
constexpr std::string_view kRegistryNextIdName{"next-id"};
/** @brief runtime state parent 目录名 / Runtime-state parent directory name. */
constexpr std::string_view kRuntimesDirectoryName{"runtimes"};
/** @brief runtime control tree 目录名 / Runtime control-tree directory name. */
constexpr std::string_view kControlDirectoryName{"control"};
/** @brief runtime workspace tree 目录名 / Runtime workspace-tree directory name. */
constexpr std::string_view kWorkspaceDirectoryName{"workspace"};
/** @brief durable invocation receipt 目录名 / Durable invocation-receipt directory name. */
constexpr std::string_view kJournalDirectoryName{"journal"};
/** @brief activation mount staging 目录名 / Activation mount-staging directory name. */
constexpr std::string_view kMountsDirectoryName{"mounts"};
/** @brief persistent OverlayFS upper directory name / Persistent OverlayFS upper-directory name. */
constexpr std::string_view kUpperDirectoryName{"upper"};
/** @brief activation OverlayFS work parent directory name / Activation OverlayFS work-parent directory name. */
constexpr std::string_view kWorkDirectoryName{"work"};
/** @brief mount namespace new-root directory name / Mount-namespace new-root directory name. */
constexpr std::string_view kRootDirectoryName{"root"};
/** @brief readonly lower workspace bind directory name / Readonly lower-workspace bind directory name. */
constexpr std::string_view kWorkspaceLowerDirectoryName{"workspace-lower"};
/** @brief 一次 crash recovery 最多扫描的 activation staging 子目录数 / Maximum activation-staging children scanned in one crash recovery. */
constexpr std::size_t kMaxActivationStagingEntries{256U};
/** @brief process-local suffix sequence for atomic registry temporary names / Process-local suffix sequence for atomic registry temporary names. */
std::atomic<std::uint64_t> g_temporary_sequence{0U};

/**
 * @brief quota pair 的持久化恢复状态 / Persisted recovery state of a quota pair.
 */
enum class RegistryState : unsigned char {
    /** @brief ID 已持久预留但 provisioning 未被证明完成 / IDs are durably reserved but provisioning is not proven complete. */
    allocating,
    /** @brief project roots、limits 与 layout 已读回验证 / Project roots, limits, and layout were read back and verified. */
    ready,
    /** @brief 恢复无法证明安全，ID 永不自动复用 / Recovery cannot prove safety; IDs are never automatically reused. */
    quarantined,
};

/**
 * @brief 一个 root-owned registry record / One root-owned registry record.
 */
struct RegistryRecord final {
    /** @brief canonical runtime UUID / Canonical runtime UUID. */
    std::string runtime_key;
    /** @brief control tree project ID / Control-tree project ID. */
    std::uint32_t control_project_id{};
    /** @brief workspace tree project ID / Workspace-tree project ID. */
    std::uint32_t workspace_project_id{};
    /** @brief 该 pair 的恢复状态 / Recovery state of this pair. */
    RegistryState state{RegistryState::allocating};
};

/**
 * @brief registry lock 下读取的一致快照 / Consistent snapshot read under the registry lock.
 */
struct RegistrySnapshot final {
    /** @brief raw runtime UUID 到 record 的映射 / Mapping from raw runtime UUID to record. */
    std::unordered_map<std::string, RegistryRecord> records;
    /** @brief 下一个永不回退的 control project ID / Next never-regressing control project ID. */
    std::uint32_t next_project_id{};
};

/**
 * @brief 受限 FD 的 RAII owner / RAII owner for a constrained file descriptor.
 */
class FileDescriptor final {
public:
    /**
     * @brief 接管一个 FD / Take ownership of one FD.
     * @param descriptor 要接管的 FD / FD to take ownership of.
     */
    explicit FileDescriptor(const int descriptor = -1) noexcept : descriptor_(descriptor) {}

    /** @brief 析构时关闭 FD / Close the FD on destruction. */
    ~FileDescriptor() { close(); }

    /** @brief FD 不能复制 / FDs cannot be copied. */
    FileDescriptor(const FileDescriptor&) = delete;
    /** @brief FD 不能复制赋值 / FDs cannot be copy-assigned. */
    FileDescriptor& operator=(const FileDescriptor&) = delete;

    /**
     * @brief 移动 FD ownership / Move FD ownership.
     * @param other 被移动的 owner / Owner being moved.
     */
    FileDescriptor(FileDescriptor&& other) noexcept : descriptor_(std::exchange(other.descriptor_, -1)) {}

    /**
     * @brief 移动赋值 FD ownership / Move-assign FD ownership.
     * @param other 被移动的 owner / Owner being moved.
     * @return 当前 owner / This owner.
     */
    FileDescriptor& operator=(FileDescriptor&& other) noexcept {
        if (this != &other) {
            close();
            descriptor_ = std::exchange(other.descriptor_, -1);
        }
        return *this;
    }

    /**
     * @brief 取得借用 FD / Get the borrowed FD.
     * @return 借用 FD / Borrowed FD.
     */
    [[nodiscard]] int get() const noexcept { return descriptor_; }

    /**
     * @brief 放弃 ownership 而不关闭 FD / Release ownership without closing the FD.
     * @return 原 FD / Original FD.
     */
    [[nodiscard]] int release() noexcept { return std::exchange(descriptor_, -1); }

    /** @brief 立即关闭当前 FD / Close the current FD immediately. */
    void close() noexcept {
        if (descriptor_ >= 0) {
            static_cast<void>(::close(descriptor_));
            descriptor_ = -1;
        }
    }

private:
    /** @brief 被拥有的 Linux file descriptor / Owned Linux file descriptor. */
    int descriptor_;
};

/**
 * @brief 一个 activation staging 目录的不可重用身份 / Non-reusable identity of one activation-staging directory.
 */
struct ActivationStagingEntryIdentity final {
    /** @brief 经过 SHA-256 语法校验的 direct-child basename / SHA-256-validated direct-child basename. */
    std::string name;
    /** @brief 扫描时的 filesystem device identity / Filesystem device identity at scan time. */
    dev_t device{};
    /** @brief 扫描时的 inode identity / Inode identity at scan time. */
    ino_t inode{};
};

/**
 * @brief 一个打开且已验证的 staging parent 及其快照 / One open verified staging parent and its snapshot.
 */
struct ActivationStagingParent final {
    /** @brief 防 TOCTOU 的 parent directory FD / Parent directory FD preventing TOCTOU path traversal. */
    FileDescriptor descriptor;
    /** @brief parent 的 filesystem device identity / Filesystem device identity of the parent. */
    dev_t device{};
    /** @brief parent 的 inode identity / Inode identity of the parent. */
    ino_t inode{};
    /** @brief 此 parent 的预期 XFS project ID / Expected XFS project ID for this parent. */
    std::uint32_t project_id{};
    /** @brief 已验证、待按 identity 删除的直接子目录 / Verified direct children to delete by identity. */
    std::vector<ActivationStagingEntryIdentity> entries;
};

/**
 * @brief 用 SHA-256 产生不含用户路径片段的组件 / Create a component without user path fragments using SHA-256.
 * @param source 要编码的稳定标识 / Stable identity to encode.
 * @return 64 个小写十六进制字符 / 64 lowercase hexadecimal characters.
 */
[[nodiscard]] std::string hash_component(const std::string_view source) {
    /** @brief SHA-256 output buffer / SHA-256 output buffer. */
    std::array<unsigned char, SHA256_DIGEST_LENGTH> digest{};
    SHA256(reinterpret_cast<const unsigned char*>(source.data()), source.size(), digest.data());
    /** @brief 十六进制数字表 / Hexadecimal digit table. */
    constexpr std::string_view kDigits{"0123456789abcdef"};
    /** @brief rendered digest / Rendered digest. */
    std::string output;
    output.reserve(digest.size() * 2U);
    for (const unsigned char value : digest) {
        output.push_back(kDigits[(value >> 4U) & 0x0fU]);
        output.push_back(kDigits[value & 0x0fU]);
    }
    return output;
}

/**
 * @brief 判断文件名是否为 SHA-256 小写 hex / Check whether a filename is lowercase SHA-256 hex.
 * @param value 要检查的文件名 / Filename to inspect.
 * @return 语法正确时为真 / True when the syntax is valid.
 */
[[nodiscard]] bool is_sha256_component(const std::string_view value) noexcept {
    return value.size() == SHA256_DIGEST_LENGTH * 2U &&
           value.find_first_not_of("0123456789abcdef") == std::string_view::npos;
}

/**
 * @brief 检查一个字符串是否为安全单路径组件 / Check one string is a safe single path component.
 * @param name 要检查的名称 / Name to inspect.
 * @return 安全时为真 / True when safe.
 */
[[nodiscard]] bool is_safe_component(const std::string_view name) noexcept {
    return !name.empty() && name != "." && name != ".." && name.find('/') == std::string_view::npos &&
           name.find('\0') == std::string_view::npos;
}

/**
 * @brief 用不跟随 symlink 的方式判断路径项是否存在 / Check whether a path entry exists without following symlinks.
 * @param path 要检查的路径 / Path to inspect.
 * @return 不存在、存在或 I/O 错误 / Missing, present, or an I/O error.
 */
[[nodiscard]] Result<bool> path_exists_no_follow(const std::filesystem::path& path) {
    /** @brief lstat metadata / lstat metadata. */
    struct stat metadata {};
    if (lstat(path.c_str(), &metadata) == 0) {
        return true;
    }
    if (errno == ENOENT) {
        return false;
    }
    return std::unexpected(errno_error(ErrorCode::io_failure, "lstat quota state path"));
}

/**
 * @brief 校验已存在目录为 root-owned private directory / Validate an existing directory is root-owned and private.
 * @param path 要校验的路径 / Path to validate.
 * @param purpose 诊断语义 / Diagnostic purpose.
 * @return 成功或错误 / Success or an error.
 */
[[nodiscard]] Result<void> validate_private_directory(
    const std::filesystem::path& path,
    const std::string_view purpose) {
    /** @brief directory metadata / Directory metadata. */
    struct stat metadata {};
    if (lstat(path.c_str(), &metadata) != 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "lstat " + std::string(purpose)));
    }
    if (!S_ISDIR(metadata.st_mode) || metadata.st_uid != 0U || (metadata.st_mode & (S_IWGRP | S_IWOTH)) != 0) {
        return std::unexpected(make_error(ErrorCode::io_failure, std::string(purpose) + " is not a private root-owned directory"));
    }
    return {};
}

/**
 * @brief fsync 一个目录 / fsync one directory.
 * @param path 已存在目录 / Existing directory.
 * @param purpose 诊断语义 / Diagnostic purpose.
 * @return 成功或 I/O 错误 / Success or an I/O error.
 */
[[nodiscard]] Result<void> sync_directory(const std::filesystem::path& path, const std::string_view purpose) {
    /** @brief opened directory descriptor / Opened directory descriptor. */
    FileDescriptor descriptor(open(path.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW));
    if (descriptor.get() < 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "open " + std::string(purpose)));
    }
    if (fsync(descriptor.get()) != 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "fsync " + std::string(purpose)));
    }
    return {};
}

/**
 * @brief 创建或收紧一个 root-owned private directory / Create or tighten one root-owned private directory.
 * @param path 直接父目录已存在的目标目录 / Target directory whose direct parent exists.
 * @param purpose 诊断语义 / Diagnostic purpose.
 * @return 成功或 I/O/ownership 错误 / Success or an I/O/ownership error.
 */
[[nodiscard]] Result<void> ensure_private_directory(
    const std::filesystem::path& path,
    const std::string_view purpose) {
    if (mkdir(path.c_str(), 0700) != 0 && errno != EEXIST) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "mkdir " + std::string(purpose)));
    }
    if (const auto valid = validate_private_directory(path, purpose); !valid) {
        return std::unexpected(valid.error());
    }
    if (chmod(path.c_str(), 0700) != 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "chmod " + std::string(purpose)));
    }
    if (const std::filesystem::path parent = path.parent_path(); !parent.empty()) {
        if (const auto synced = sync_directory(parent, "quota directory parent"); !synced) {
            return std::unexpected(synced.error());
        }
    }
    return sync_directory(path, purpose);
}

/**
 * @brief 读取一个 no-follow regular file 的有界内容 / Read bounded content from one no-follow regular file.
 * @param directory_fd 已验证 parent directory 的 FD / FD for the verified parent directory.
 * @param name 不含路径语义的文件名 / Filename without path semantics.
 * @param maximum_bytes 最大允许字节数 / Maximum permitted byte count.
 * @param purpose 诊断语义 / Diagnostic purpose.
 * @return 文件内容或 I/O/format 错误 / File contents or an I/O/format error.
 */
[[nodiscard]] Result<std::string> read_regular_file_at(
    const int directory_fd,
    const std::string_view name,
    const std::size_t maximum_bytes,
    const std::string_view purpose) {
    if (!is_safe_component(name)) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "unsafe quota registry file component"));
    }
    /** @brief opened regular-file descriptor / Opened regular-file descriptor. */
    FileDescriptor descriptor(openat(
        directory_fd,
        std::string(name).c_str(),
        O_RDONLY | O_CLOEXEC | O_NOFOLLOW));
    if (descriptor.get() < 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "open " + std::string(purpose)));
    }
    /** @brief opened file metadata / Opened file metadata. */
    struct stat metadata {};
    if (fstat(descriptor.get(), &metadata) != 0 || !S_ISREG(metadata.st_mode) || metadata.st_uid != 0U ||
        (metadata.st_mode & (S_IWGRP | S_IWOTH)) != 0 || metadata.st_size < 0 ||
        static_cast<std::uintmax_t>(metadata.st_size) > maximum_bytes) {
        return std::unexpected(make_error(ErrorCode::io_failure, std::string(purpose) + " is not a bounded private regular file"));
    }
    /** @brief loaded contents / Loaded contents. */
    std::string contents(static_cast<std::size_t>(metadata.st_size), '\0');
    /** @brief current read offset / Current read offset. */
    std::size_t offset{0U};
    while (offset < contents.size()) {
        const ssize_t count = read(
            descriptor.get(),
            contents.data() + static_cast<std::ptrdiff_t>(offset),
            contents.size() - offset);
        if (count < 0) {
            if (errno == EINTR) {
                continue;
            }
            return std::unexpected(errno_error(ErrorCode::io_failure, "read " + std::string(purpose)));
        }
        if (count == 0) {
            return std::unexpected(make_error(ErrorCode::io_failure, std::string(purpose) + " changed while reading"));
        }
        offset += static_cast<std::size_t>(count);
    }
    return contents;
}

/**
 * @brief 完整写入一个 FD / Fully write one FD.
 * @param descriptor 打开的可写 FD / Open writable FD.
 * @param contents 要写入的内容 / Contents to write.
 * @param purpose 诊断语义 / Diagnostic purpose.
 * @return 成功或 I/O 错误 / Success or an I/O error.
 */
[[nodiscard]] Result<void> write_all(
    const int descriptor,
    const std::string_view contents,
    const std::string_view purpose) {
    /** @brief current write offset / Current write offset. */
    std::size_t offset{0U};
    while (offset < contents.size()) {
        const ssize_t count = write(
            descriptor,
            contents.data() + static_cast<std::ptrdiff_t>(offset),
            contents.size() - offset);
        if (count < 0) {
            if (errno == EINTR) {
                continue;
            }
            return std::unexpected(errno_error(ErrorCode::io_failure, "write " + std::string(purpose)));
        }
        if (count == 0) {
            return std::unexpected(make_error(ErrorCode::io_failure, std::string(purpose) + " made no write progress"));
        }
        offset += static_cast<std::size_t>(count);
    }
    return {};
}

/**
 * @brief 原子替换 registry 内的一个小 regular file / Atomically replace one small regular file inside the registry.
 * @param directory 已验证目录 / Verified directory.
 * @param final_name 目标文件名 / Target filename.
 * @param contents 完整内容 / Complete contents.
 * @param purpose 诊断语义 / Diagnostic purpose.
 * @return 成功或 I/O 错误 / Success or an I/O error.
 * @note file 与 parent directory 都 fsync，崩溃后只会看到旧值或完整新值。
 *       Both file and parent directory are fsynced, so a crash sees only the old value or a complete new value.
 */
[[nodiscard]] Result<void> atomic_write_private_file(
    const std::filesystem::path& directory,
    const std::string_view final_name,
    const std::string_view contents,
    const std::string_view purpose) {
    if (!is_safe_component(final_name)) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "unsafe quota registry target component"));
    }
    /** @brief parent directory descriptor / Parent directory descriptor. */
    FileDescriptor directory_fd(open(directory.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW));
    if (directory_fd.get() < 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "open " + std::string(purpose) + " directory"));
    }
    /** @brief unique temporary filename / Unique temporary filename. */
    const std::string temporary_name = "." + std::string(final_name) + ".tmp." + std::to_string(getpid()) + "." +
                                       std::to_string(g_temporary_sequence.fetch_add(1U, std::memory_order_relaxed));
    /** @brief temporary output descriptor / Temporary output descriptor. */
    FileDescriptor output_fd(openat(
        directory_fd.get(),
        temporary_name.c_str(),
        O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW,
        0600));
    if (output_fd.get() < 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "create temporary " + std::string(purpose)));
    }
    const auto written = write_all(output_fd.get(), contents, purpose);
    if (!written || fchmod(output_fd.get(), 0600) != 0 || fchown(output_fd.get(), 0, 0) != 0 || fsync(output_fd.get()) != 0) {
        const Error error = written
            ? errno_error(ErrorCode::io_failure, "fsync temporary " + std::string(purpose))
            : written.error();
        output_fd.close();
        static_cast<void>(unlinkat(directory_fd.get(), temporary_name.c_str(), 0));
        return std::unexpected(error);
    }
    output_fd.close();
    if (renameat(directory_fd.get(), temporary_name.c_str(), directory_fd.get(), std::string(final_name).c_str()) != 0) {
        const Error error = errno_error(ErrorCode::io_failure, "rename " + std::string(purpose));
        static_cast<void>(unlinkat(directory_fd.get(), temporary_name.c_str(), 0));
        return std::unexpected(error);
    }
    if (fsync(directory_fd.get()) != 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "fsync " + std::string(purpose) + " directory"));
    }
    return {};
}

/**
 * @brief 按严格十进制解析 u32 / Parse a u32 using strict decimal syntax.
 * @param text 十进制文本 / Decimal text.
 * @param purpose 诊断语义 / Diagnostic purpose.
 * @return u32 或格式错误 / u32 or a format error.
 */
[[nodiscard]] Result<std::uint32_t> parse_u32(const std::string_view text, const std::string_view purpose) {
    if (text.empty() || text.find_first_not_of("0123456789") != std::string_view::npos) {
        return std::unexpected(make_error(ErrorCode::io_failure, std::string(purpose) + " is not decimal"));
    }
    /** @brief parsed unsigned value / Parsed unsigned value. */
    std::uint32_t value{};
    const auto [end, error] = std::from_chars(text.data(), text.data() + text.size(), value);
    if (error != std::errc{} || end != text.data() + text.size()) {
        return std::unexpected(make_error(ErrorCode::io_failure, std::string(purpose) + " is outside u32 range"));
    }
    return value;
}

/**
 * @brief 将 registry 状态渲染为 stable text / Render a registry state into stable text.
 * @param state 待渲染状态 / State to render.
 * @return stable text / Stable text.
 */
[[nodiscard]] std::string_view registry_state_name(const RegistryState state) noexcept {
    switch (state) {
        case RegistryState::allocating:
            return "allocating";
        case RegistryState::ready:
            return "ready";
        case RegistryState::quarantined:
            return "quarantined";
    }
    return "invalid";
}

/**
 * @brief 解析 registry 状态文本 / Parse stable registry-state text.
 * @param text 状态文本 / State text.
 * @return 状态或格式错误 / State or a format error.
 */
[[nodiscard]] Result<RegistryState> parse_registry_state(const std::string_view text) {
    if (text == "allocating") {
        return RegistryState::allocating;
    }
    if (text == "ready") {
        return RegistryState::ready;
    }
    if (text == "quarantined") {
        return RegistryState::quarantined;
    }
    return std::unexpected(make_error(ErrorCode::io_failure, "quota registry record has an unknown state"));
}

/**
 * @brief 将一条 record 序列化为严格五行文本 / Serialize one record as strict five-line text.
 * @param record 要写入的 record / Record to write.
 * @return 稳定可恢复文本 / Stable recoverable text.
 */
[[nodiscard]] std::string encode_record(const RegistryRecord& record) {
    return "version=" + std::string(kRegistryVersion) + "\n" +
           "runtime_key=" + record.runtime_key + "\n" +
           "control_project_id=" + std::to_string(record.control_project_id) + "\n" +
           "workspace_project_id=" + std::to_string(record.workspace_project_id) + "\n" +
           "state=" + std::string(registry_state_name(record.state)) + "\n";
}

/**
 * @brief 分割严格 ``key=value`` 行 / Split a strict ``key=value`` line.
 * @param line 一行文本 / One line of text.
 * @param expected_key 预期 key / Expected key.
 * @param purpose 诊断语义 / Diagnostic purpose.
 * @return value 或格式错误 / Value or a format error.
 */
[[nodiscard]] Result<std::string_view> record_value(
    const std::string_view line,
    const std::string_view expected_key,
    const std::string_view purpose) {
    /** @brief expected key prefix / Expected key prefix. */
    const std::string prefix = std::string(expected_key) + "=";
    if (!line.starts_with(prefix)) {
        return std::unexpected(make_error(ErrorCode::io_failure, std::string(purpose) + " has an unexpected field"));
    }
    const std::string_view value = line.substr(prefix.size());
    if (value.empty() || value.find('\0') != std::string_view::npos) {
        return std::unexpected(make_error(ErrorCode::io_failure, std::string(purpose) + " has an empty or NUL field"));
    }
    return value;
}

/**
 * @brief 将严格五行文本解析为 registry record / Parse strict five-line text into a registry record.
 * @param contents record 内容 / Record contents.
 * @return record 或格式错误 / Record or a format error.
 */
[[nodiscard]] Result<RegistryRecord> decode_record(const std::string_view contents) {
    /** @brief parsed lines / Parsed lines. */
    std::array<std::string_view, 5> lines{};
    /** @brief current source offset / Current source offset. */
    std::size_t offset{0U};
    for (std::size_t index = 0U; index < lines.size(); ++index) {
        const std::size_t newline = contents.find('\n', offset);
        if (newline == std::string_view::npos) {
            return std::unexpected(make_error(ErrorCode::io_failure, "quota registry record is truncated"));
        }
        lines[index] = contents.substr(offset, newline - offset);
        offset = newline + 1U;
    }
    if (offset != contents.size()) {
        return std::unexpected(make_error(ErrorCode::io_failure, "quota registry record has trailing data"));
    }
    const auto version = record_value(lines[0], "version", "quota registry record");
    const auto runtime_key = record_value(lines[1], "runtime_key", "quota registry record");
    const auto control_id = record_value(lines[2], "control_project_id", "quota registry record");
    const auto workspace_id = record_value(lines[3], "workspace_project_id", "quota registry record");
    const auto state = record_value(lines[4], "state", "quota registry record");
    if (!version || !runtime_key || !control_id || !workspace_id || !state || *version != kRegistryVersion) {
        return std::unexpected(make_error(ErrorCode::io_failure, "quota registry record version or fields are invalid"));
    }
    const auto parsed_runtime = domain::RuntimeId::parse(std::string(*runtime_key));
    const auto parsed_control = parse_u32(*control_id, "control project ID");
    const auto parsed_workspace = parse_u32(*workspace_id, "workspace project ID");
    const auto parsed_state = parse_registry_state(*state);
    if (!parsed_runtime || !parsed_control || !parsed_workspace || !parsed_state) {
        return std::unexpected(make_error(ErrorCode::io_failure, "quota registry record has invalid semantic fields"));
    }
    return RegistryRecord{
        .runtime_key = parsed_runtime->value(),
        .control_project_id = *parsed_control,
        .workspace_project_id = *parsed_workspace,
        .state = *parsed_state,
    };
}

/**
 * @brief 将两个 u64 相加且拒绝 overflow / Add two u64 values while rejecting overflow.
 * @param left 左操作数 / Left operand.
 * @param right 右操作数 / Right operand.
 * @param purpose 诊断语义 / Diagnostic purpose.
 * @return 和或 overflow 错误 / Sum or an overflow error.
 */
[[nodiscard]] Result<std::uint64_t> checked_add(
    const std::uint64_t left,
    const std::uint64_t right,
    const std::string_view purpose) {
    if (right > std::numeric_limits<std::uint64_t>::max() - left) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, std::string(purpose) + " overflows u64"));
    }
    return left + right;
}

/**
 * @brief 规范化并校验一个绝对目录 / Canonicalize and validate one absolute directory.
 * @param path 配置路径 / Configured path.
 * @param purpose 诊断语义 / Diagnostic purpose.
 * @return canonical directory 或错误 / Canonical directory or an error.
 */
[[nodiscard]] Result<std::filesystem::path> canonical_directory(
    const std::filesystem::path& path,
    const std::string_view purpose) {
    if (!path.is_absolute()) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, std::string(purpose) + " must be absolute"));
    }
    /** @brief canonicalization error / Canonicalization error. */
    std::error_code error;
    const std::filesystem::path canonical = std::filesystem::canonical(path, error);
    if (error) {
        return std::unexpected(make_error(ErrorCode::not_found, std::string(purpose) + ": " + error.message()));
    }
    /** @brief directory metadata / Directory metadata. */
    struct stat metadata {};
    if (lstat(canonical.c_str(), &metadata) != 0 || !S_ISDIR(metadata.st_mode)) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, std::string(purpose) + " must name a real directory"));
    }
    return canonical;
}

/**
 * @brief 判断 canonical child 是否位于 canonical parent 下 / Determine whether a canonical child is below a canonical parent.
 * @param child canonical child path / Canonical child path.
 * @param parent canonical parent path / Canonical parent path.
 * @return child 等于或位于 parent 下时为真 / True when child equals or is below parent.
 */
[[nodiscard]] bool is_below_or_equal(
    const std::filesystem::path& child,
    const std::filesystem::path& parent) noexcept {
    auto child_iterator = child.begin();
    auto parent_iterator = parent.begin();
    while (parent_iterator != parent.end()) {
        if (child_iterator == child.end() || *child_iterator != *parent_iterator) {
            return false;
        }
        ++child_iterator;
        ++parent_iterator;
    }
    return true;
}

/**
 * @brief 确认配置 mount 本身是一个非根挂载点 / Confirm the configured mount is itself a non-root mountpoint.
 * @param mount_path canonical configured mountpoint / Canonical configured mountpoint.
 * @return 成功或 fail-closed 错误 / Success or a fail-closed error.
 */
[[nodiscard]] Result<void> require_dedicated_mountpoint(const std::filesystem::path& mount_path) {
    if (mount_path == mount_path.root_path()) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "XFS quota mount must not be the host root filesystem"));
    }
    /** @brief mountpoint metadata / Mountpoint metadata. */
    struct stat mount_metadata {};
    /** @brief parent metadata / Parent metadata. */
    struct stat parent_metadata {};
    if (stat(mount_path.c_str(), &mount_metadata) != 0 || stat(mount_path.parent_path().c_str(), &parent_metadata) != 0) {
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed, "stat XFS quota mountpoint"));
    }
    if (mount_metadata.st_dev == parent_metadata.st_dev) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "XFS quota mount must be a dedicated mountpoint"));
    }
    return {};
}

/**
 * @brief 校验配置值的代数约束 / Validate algebraic constraints of the quota configuration.
 * @param config 配置 / Configuration.
 * @return 成功或 invalid-argument 错误 / Success or an invalid-argument error.
 */
[[nodiscard]] Result<void> validate_configuration_values(const XfsProjectQuotaConfig& config) {
    if (config.project_id_min == 0U || (config.project_id_min & 1U) != 0U ||
        (config.project_id_max & 1U) == 0U || config.project_id_max < config.project_id_min + 1U ||
        config.project_id_max == std::numeric_limits<std::uint32_t>::max()) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "XFS project-ID range must contain complete nonzero even/odd pairs"));
    }
    if (config.control_hard_bytes == 0U || config.control_hard_inodes == 0U || config.workspace_hard_bytes == 0U ||
        config.workspace_hard_inodes == 0U || config.global_admission_bytes == 0U ||
        config.global_admission_inodes == 0U || config.control_hard_bytes % kQuotaBasicBlockBytes != 0U ||
        config.workspace_hard_bytes % kQuotaBasicBlockBytes != 0U) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "XFS quota hard/admission limits must be nonzero and byte limits divisible by 512"));
    }
    const auto per_runtime_bytes = checked_add(config.control_hard_bytes, config.workspace_hard_bytes, "per-runtime byte reservation");
    const auto per_runtime_inodes = checked_add(config.control_hard_inodes, config.workspace_hard_inodes, "per-runtime inode reservation");
    if (!per_runtime_bytes || !per_runtime_inodes || *per_runtime_bytes > config.global_admission_bytes ||
        *per_runtime_inodes > config.global_admission_inodes) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "one runtime project pair does not fit in the XFS admission budget"));
    }
    return {};
}

/**
 * @brief 检查旧 global journal 与旧 runtime layout / Check for legacy global journal and legacy runtime layout.
 * @param state_root canonical state root / Canonical state root.
 * @return 没有 legacy tree 时成功 / Success when no legacy tree exists.
 * @note 这轮不迁移、不删除旧数据；operator 必须显式迁移后再启动。
 *       This round neither migrates nor deletes old data; an operator must explicitly migrate before startup.
 */
[[nodiscard]] Result<void> reject_legacy_state(const std::filesystem::path& state_root) {
    const auto global_journal = path_exists_no_follow(state_root / kJournalDirectoryName);
    if (!global_journal) {
        return std::unexpected(global_journal.error());
    }
    if (*global_journal) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "legacy state_root/journal exists; explicit XFS state migration is required"));
    }
    const std::filesystem::path runtimes_directory = state_root / kRuntimesDirectoryName;
    const auto runtimes_exists = path_exists_no_follow(runtimes_directory);
    if (!runtimes_exists) {
        return std::unexpected(runtimes_exists.error());
    }
    if (!*runtimes_exists) {
        return {};
    }
    if (const auto valid = validate_private_directory(runtimes_directory, "runtime state directory"); !valid) {
        return std::unexpected(valid.error());
    }
    /** @brief iterator construction error / Iterator construction error. */
    std::error_code error;
    std::filesystem::directory_iterator iterator(runtimes_directory, std::filesystem::directory_options::none, error);
    if (error) {
        return std::unexpected(make_error(ErrorCode::io_failure, "enumerate runtime state directory: " + error.message()));
    }
    for (const std::filesystem::directory_entry& entry : iterator) {
        /** @brief entry metadata query error / Entry metadata query error. */
        std::error_code status_error;
        const std::filesystem::file_status status = entry.symlink_status(status_error);
        if (status_error || !std::filesystem::is_directory(status)) {
            return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "runtime state directory contains an unsafe entry"));
        }
        const auto legacy_upper = path_exists_no_follow(entry.path() / "workspace-upper");
        const auto legacy_mounts = path_exists_no_follow(entry.path() / "mounts");
        if (!legacy_upper || !legacy_mounts) {
            return std::unexpected(!legacy_upper ? legacy_upper.error() : legacy_mounts.error());
        }
        if (*legacy_upper || *legacy_mounts) {
            return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "legacy runtime workspace-upper or mounts tree exists; explicit XFS state migration is required"));
        }
    }
    return {};
}

/**
 * @brief 读取 XFS project-quota accounting/enforcement 状态 / Read XFS project-quota accounting/enforcement state.
 * @param mount_path configured XFS mountpoint / Configured XFS mountpoint.
 * @return 成功或 kernel quota API 错误 / Success or a kernel quota-API error.
 */
[[nodiscard]] Result<void> require_project_quota_enforcement(const std::filesystem::path& mount_path) {
    /** @brief versioned XFS quota status / Versioned XFS quota status. */
    fs_quota_statv status{};
    status.qs_version = FS_QSTATV_VERSION1;
    /** @brief mounted XFS directory FD / Mounted XFS directory FD. */
    FileDescriptor mount_fd(open(mount_path.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW));
    if (mount_fd.get() < 0) {
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed, "open XFS quota mount FD"));
    }
#ifdef SYS_quotactl_fd
    if (syscall(
            SYS_quotactl_fd,
            mount_fd.get(),
            QCMD(Q_XGETQSTATV, PRJQUOTA),
            0,
            &status) != 0) {
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed, "Q_XGETQSTATV XFS project quota"));
    }
#else
    return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "kernel headers do not expose quotactl_fd; refusing a quota path fallback"));
#endif
    const std::uint16_t required_flags = static_cast<std::uint16_t>(FS_QUOTA_PDQ_ACCT | FS_QUOTA_PDQ_ENFD);
    if ((status.qs_flags & required_flags) != required_flags) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "XFS project quota accounting or enforcement is disabled"));
    }
    return {};
}

/**
 * @brief 按 512-byte block 设置 project hard limits / Set project hard limits in 512-byte blocks.
 * @param mount_path configured XFS mountpoint / Configured XFS mountpoint.
 * @param project_id target project ID / Target project ID.
 * @param hard_bytes requested byte hard limit / Requested byte hard limit.
 * @param hard_inodes requested inode hard limit / Requested inode hard limit.
 * @return 成功或 XFS quota API 错误 / Success or an XFS quota-API error.
 */
[[nodiscard]] Result<void> set_project_hard_limits(
    const std::filesystem::path& mount_path,
    const std::uint32_t project_id,
    const std::uint64_t hard_bytes,
    const std::uint64_t hard_inodes) {
    /** @brief XFS hard-limit request / XFS hard-limit request. */
    fs_disk_quota_t quota{};
    quota.d_version = FS_DQUOT_VERSION;
    quota.d_flags = FS_PROJ_QUOTA;
    quota.d_fieldmask = static_cast<__u16>(FS_DQ_BHARD | FS_DQ_IHARD);
    quota.d_id = project_id;
    quota.d_blk_hardlimit = hard_bytes / kQuotaBasicBlockBytes;
    quota.d_ino_hardlimit = hard_inodes;
    /** @brief mounted XFS directory FD / Mounted XFS directory FD. */
    FileDescriptor mount_fd(open(mount_path.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW));
    if (mount_fd.get() < 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "open XFS quota mount FD"));
    }
#ifdef SYS_quotactl_fd
    if (syscall(
            SYS_quotactl_fd,
            mount_fd.get(),
            QCMD(Q_XSETQLIM, PRJQUOTA),
            static_cast<int>(project_id),
            &quota) != 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "Q_XSETQLIM XFS project hard limits"));
    }
#else
    return std::unexpected(make_error(ErrorCode::io_failure, "kernel headers do not expose quotactl_fd; refusing a quota path fallback"));
#endif
    return {};
}

/**
 * @brief 读回并精确验证 project hard limits / Read back and exactly validate project hard limits.
 * @param mount_path configured XFS mountpoint / Configured XFS mountpoint.
 * @param project_id target project ID / Target project ID.
 * @param hard_bytes expected byte hard limit / Expected byte hard limit.
 * @param hard_inodes expected inode hard limit / Expected inode hard limit.
 * @return 成功或 XFS quota/readback 错误 / Success or an XFS quota/readback error.
 */
[[nodiscard]] Result<void> verify_project_hard_limits(
    const std::filesystem::path& mount_path,
    const std::uint32_t project_id,
    const std::uint64_t hard_bytes,
    const std::uint64_t hard_inodes) {
    /** @brief XFS quota readback / XFS quota readback. */
    fs_disk_quota_t quota{};
    quota.d_version = FS_DQUOT_VERSION;
    quota.d_flags = FS_PROJ_QUOTA;
    quota.d_id = project_id;
    /** @brief mounted XFS directory FD / Mounted XFS directory FD. */
    FileDescriptor mount_fd(open(mount_path.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW));
    if (mount_fd.get() < 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "open XFS quota mount FD"));
    }
#ifdef SYS_quotactl_fd
    if (syscall(
            SYS_quotactl_fd,
            mount_fd.get(),
            QCMD(Q_XGETQUOTA, PRJQUOTA),
            static_cast<int>(project_id),
            &quota) != 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "Q_XGETQUOTA XFS project hard limits"));
    }
#else
    return std::unexpected(make_error(ErrorCode::io_failure, "kernel headers do not expose quotactl_fd; refusing a quota path fallback"));
#endif
    if (quota.d_id != project_id || quota.d_blk_hardlimit != hard_bytes / kQuotaBasicBlockBytes ||
        quota.d_ino_hardlimit != hard_inodes) {
        return std::unexpected(make_error(ErrorCode::io_failure, "XFS project hard-limit readback differs from the required contract"));
    }
    return {};
}

/**
 * @brief 以 FD 读取一个 XFS project quota 的实际 accounting / Read actual accounting for one XFS project quota through an FD.
 * @param mount_path 已配置 XFS mountpoint / Configured XFS mountpoint.
 * @param project_id 待读取 project ID / Project ID to read.
 * @return 内核填充的 XFS quota 结构或错误 / Kernel-populated XFS quota structure or an error.
 */
[[nodiscard]] Result<fs_disk_quota_t> read_project_quota_accounting(
    const std::filesystem::path& mount_path,
    const std::uint32_t project_id) {
    /** @brief 将由内核填充的 XFS quota 结构 / XFS quota structure populated by the kernel. */
    fs_disk_quota_t quota{};
    quota.d_version = FS_DQUOT_VERSION;
    quota.d_flags = FS_PROJ_QUOTA;
    quota.d_id = project_id;
    /** @brief 已挂载 XFS 目录 FD / Mounted XFS directory FD. */
    FileDescriptor mount_fd(open(mount_path.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW));
    if (mount_fd.get() < 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "open XFS quota mount FD for operator usage"));
    }
#ifdef SYS_quotactl_fd
    if (syscall(
            SYS_quotactl_fd,
            mount_fd.get(),
            QCMD(Q_XGETQUOTA, PRJQUOTA),
            static_cast<int>(project_id),
            &quota) != 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "Q_XGETQUOTA XFS workspace usage"));
    }
#else
    return std::unexpected(make_error(
        ErrorCode::io_failure,
        "kernel headers do not expose quotactl_fd; refusing a quota path fallback"));
#endif
    if (quota.d_id != project_id) {
        return std::unexpected(make_error(ErrorCode::io_failure, "Q_XGETQUOTA returned a different project ID"));
    }
    return quota;
}

/**
 * @brief 设置一个目录的 project ID 和 PROJINHERIT 并读回 / Set and read back a directory's project ID and PROJINHERIT.
 * @param path root-owned directory / Root-owned directory.
 * @param project_id expected project ID / Expected project ID.
 * @return 成功或 ioctl/readback 错误 / Success or an ioctl/readback error.
 */
[[nodiscard]] Result<void> assign_project_directory(
    const std::filesystem::path& path,
    const std::uint32_t project_id) {
    /** @brief no-follow directory descriptor / No-follow directory descriptor. */
    FileDescriptor descriptor(open(path.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW));
    if (descriptor.get() < 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "open XFS project directory"));
    }
    /** @brief source directory metadata / Source directory metadata. */
    struct stat metadata {};
    if (fstat(descriptor.get(), &metadata) != 0 || !S_ISDIR(metadata.st_mode) || metadata.st_uid != 0U ||
        (metadata.st_mode & (S_IWGRP | S_IWOTH)) != 0) {
        return std::unexpected(make_error(ErrorCode::io_failure, "XFS project directory is not private root-owned"));
    }
    /** @brief existing XFS inode attributes / Existing XFS inode attributes. */
    fsxattr attributes{};
    if (ioctl(descriptor.get(), FS_IOC_FSGETXATTR, &attributes) != 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "FS_IOC_FSGETXATTR before project assignment"));
    }
    attributes.fsx_projid = project_id;
    attributes.fsx_xflags |= FS_XFLAG_PROJINHERIT;
    if (ioctl(descriptor.get(), FS_IOC_FSSETXATTR, &attributes) != 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "FS_IOC_FSSETXATTR project assignment"));
    }
    /** @brief assignment readback attributes / Assignment readback attributes. */
    fsxattr readback{};
    if (ioctl(descriptor.get(), FS_IOC_FSGETXATTR, &readback) != 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "FS_IOC_FSGETXATTR project assignment readback"));
    }
    if (readback.fsx_projid != project_id || (readback.fsx_xflags & FS_XFLAG_PROJINHERIT) == 0U) {
        return std::unexpected(make_error(ErrorCode::io_failure, "XFS project ID or PROJINHERIT readback differs from the required contract"));
    }
    return {};
}

/**
 * @brief 判断 metadata 是否属于 private root-owned directory / Check whether metadata belongs to a private root-owned directory.
 * @param metadata 待判断的 inode metadata / Inode metadata to inspect.
 * @return 是 private root-owned directory 时为真 / True when this is a private root-owned directory.
 */
[[nodiscard]] bool is_private_root_owned_directory(const struct stat& metadata) noexcept {
    return S_ISDIR(metadata.st_mode) && metadata.st_uid == 0U &&
           (metadata.st_mode & (S_IWGRP | S_IWOTH)) == 0;
}

/**
 * @brief 以已打开 FD 校验 project ID 与 PROJINHERIT / Validate project ID and PROJINHERIT through an already-open FD.
 * @param descriptor 已打开且不跟随 symlink 的目录 FD / Already-open no-follow directory FD.
 * @param project_id expected project ID / Expected project ID.
 * @param purpose 诊断语义 / Diagnostic purpose.
 * @return 成功或 ioctl/readback 错误 / Success or an ioctl/readback error.
 */
[[nodiscard]] Result<void> verify_project_attributes_fd(
    const int descriptor,
    const std::uint32_t project_id,
    const std::string_view purpose) {
    if (descriptor < 0) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "invalid XFS project-directory FD"));
    }
    /** @brief XFS inode attributes / XFS inode attributes. */
    fsxattr attributes{};
    if (ioctl(descriptor, FS_IOC_FSGETXATTR, &attributes) != 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "FS_IOC_FSGETXATTR " + std::string(purpose)));
    }
    if (attributes.fsx_projid != project_id || (attributes.fsx_xflags & FS_XFLAG_PROJINHERIT) == 0U) {
        return std::unexpected(make_error(ErrorCode::io_failure, std::string(purpose) + " assignment no longer matches registry"));
    }
    return {};
}

/**
 * @brief 以已打开 FD 校验 root-owned project 目录 / Validate a root-owned project directory through an open FD.
 * @param descriptor 已打开且不跟随 symlink 的目录 FD / Already-open no-follow directory FD.
 * @param project_id expected project ID / Expected project ID.
 * @param purpose 诊断语义 / Diagnostic purpose.
 * @return 成功或 metadata/ioctl 错误 / Success or a metadata/ioctl error.
 */
[[nodiscard]] Result<void> verify_project_directory_fd(
    const int descriptor,
    const std::uint32_t project_id,
    const std::string_view purpose) {
    /** @brief directory metadata / Directory metadata. */
    struct stat metadata {};
    if (fstat(descriptor, &metadata) != 0 || !is_private_root_owned_directory(metadata)) {
        return std::unexpected(make_error(
            ErrorCode::io_failure,
            std::string(purpose) + " is not a private root-owned directory during readback"));
    }
    return verify_project_attributes_fd(descriptor, project_id, purpose);
}

/**
 * @brief 校验 Agent-owned Overlay upper 根 / Validate the Agent-owned Overlay upper root.
 * @param path persistent Overlay upper root / Persistent Overlay upper root.
 * @param project_id expected workspace project ID / Expected workspace project ID.
 * @param config quota and Agent identity contract / Quota and Agent identity contract.
 * @return 成功或 fail-closed 错误 / Success or a fail-closed error.
 * @note 首次 activation 前允许 root，升级迁移期间允许旧 ``nobody``；除此之外只接受当前
 *       Agent。/ Root is accepted before first activation and legacy ``nobody`` during upgrade;
 *       otherwise only the current Agent is accepted.
 */
[[nodiscard]] Result<void> verify_workspace_upper_directory(
    const std::filesystem::path& path,
    const std::uint32_t project_id,
    const XfsProjectQuotaConfig& config) {
    /** @brief no-follow upper-root descriptor / No-follow upper-root descriptor. */
    FileDescriptor descriptor(open(path.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW));
    if (descriptor.get() < 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "open Overlay upper root for readback"));
    }
    /** @brief upper-root metadata / Upper-root metadata. */
    struct stat metadata {};
    if (fstat(descriptor.get(), &metadata) != 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "stat Overlay upper root"));
    }
    constexpr uid_t kLegacyNobodyUid{65534U};
    constexpr gid_t kLegacyNobodyGid{65534U};
    const bool initial_owner = metadata.st_uid == 0U && metadata.st_gid == 0U;
    const bool legacy_owner =
        metadata.st_uid == kLegacyNobodyUid && metadata.st_gid == kLegacyNobodyGid;
    const bool agent_owner =
        metadata.st_uid == config.workspace_uid && metadata.st_gid == config.workspace_gid;
    if (!S_ISDIR(metadata.st_mode) || (metadata.st_mode & 0777U) != 0700U ||
        (!initial_owner && !legacy_owner && !agent_owner)) {
        return std::unexpected(make_error(
            ErrorCode::io_failure,
            "Overlay upper root is not private or has an unexpected owner"));
    }
    return verify_project_attributes_fd(
        descriptor.get(),
        project_id,
        "Overlay upper root");
}

/**
 * @brief 校验目录持有预期 project ID 与 PROJINHERIT / Validate a directory has the expected project ID and PROJINHERIT.
 * @param path root-owned directory / Root-owned directory.
 * @param project_id expected project ID / Expected project ID.
 * @return 成功或 ioctl/readback 错误 / Success or an ioctl/readback error.
 */
[[nodiscard]] Result<void> verify_project_directory(
    const std::filesystem::path& path,
    const std::uint32_t project_id) {
    /** @brief no-follow directory descriptor / No-follow directory descriptor. */
    FileDescriptor descriptor(open(path.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW));
    if (descriptor.get() < 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "open XFS project directory for readback"));
    }
    return verify_project_directory_fd(descriptor.get(), project_id, "XFS project directory");
}

/**
 * @brief fd-relative 递归删除一个已验证 transient 目录内容 / Recursively delete verified transient-directory contents relative to an FD.
 * @param directory_fd verified transient-directory FD / Verified transient-directory FD.
 * @return 成功或 I/O 错误 / Success or an I/O error.
 */
[[nodiscard]] Result<void> remove_directory_contents_at(const int directory_fd) {
    /** @brief duplicate descriptor for directory iteration / Duplicate descriptor for directory iteration. */
    const int scan_fd = fcntl(directory_fd, F_DUPFD_CLOEXEC, 3);
    if (scan_fd < 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "duplicate transient directory FD"));
    }
    /** @brief DIR stream owning scan_fd / DIR stream owning scan_fd. */
    DIR* const directory = fdopendir(scan_fd);
    if (directory == nullptr) {
        const Error error = errno_error(ErrorCode::io_failure, "open transient directory stream");
        static_cast<void>(close(scan_fd));
        return std::unexpected(error);
    }
    for (;;) {
        errno = 0;
        /** @brief one directory entry / One directory entry. */
        dirent* const entry = readdir(directory);
        if (entry == nullptr) {
            const int read_error = errno;
            static_cast<void>(closedir(directory));
            if (read_error != 0) {
                errno = read_error;
                return std::unexpected(errno_error(ErrorCode::io_failure, "read transient directory stream"));
            }
            break;
        }
        const std::string_view name{entry->d_name};
        if (name == "." || name == "..") {
            continue;
        }
        /** @brief entry metadata / Entry metadata. */
        struct stat metadata {};
        if (fstatat(directory_fd, entry->d_name, &metadata, AT_SYMLINK_NOFOLLOW) != 0) {
            return std::unexpected(errno_error(ErrorCode::io_failure, "stat transient directory entry"));
        }
        if (S_ISDIR(metadata.st_mode)) {
            /** @brief child directory descriptor / Child directory descriptor. */
            FileDescriptor child(openat(directory_fd, entry->d_name, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW));
            if (child.get() < 0) {
                return std::unexpected(errno_error(ErrorCode::io_failure, "open transient child directory"));
            }
            if (const auto removed = remove_directory_contents_at(child.get()); !removed) {
                return std::unexpected(removed.error());
            }
            if (unlinkat(directory_fd, entry->d_name, AT_REMOVEDIR) != 0) {
                return std::unexpected(errno_error(ErrorCode::io_failure, "remove transient child directory"));
            }
            continue;
        }
        if (unlinkat(directory_fd, entry->d_name, 0) != 0) {
            return std::unexpected(errno_error(ErrorCode::io_failure, "remove transient directory entry"));
        }
    }
    if (fsync(directory_fd) != 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "fsync emptied transient directory"));
    }
    return {};
}

/**
 * @brief 原子地清空并重建一个已验证 private child directory / Atomically empty and recreate a verified private child directory.
 * @param parent 父目录 / Parent directory.
 * @param child_name child basename / Child basename.
 * @param purpose 诊断语义 / Diagnostic purpose.
 * @return 成功或 cleanup/I/O 错误 / Success or a cleanup/I/O error.
 */
[[nodiscard]] Result<void> recreate_private_child_directory(
    const std::filesystem::path& parent,
    const std::string_view child_name,
    const std::string_view purpose) {
    if (!is_safe_component(child_name)) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "unsafe transient quota directory component"));
    }
    /** @brief parent directory descriptor / Parent directory descriptor. */
    FileDescriptor parent_fd(open(parent.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW));
    if (parent_fd.get() < 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "open transient parent directory"));
    }
    /** @brief child metadata / Child metadata. */
    struct stat metadata {};
    if (fstatat(parent_fd.get(), std::string(child_name).c_str(), &metadata, AT_SYMLINK_NOFOLLOW) == 0) {
        if (!S_ISDIR(metadata.st_mode) || metadata.st_uid != 0U || (metadata.st_mode & (S_IWGRP | S_IWOTH)) != 0) {
            return std::unexpected(make_error(ErrorCode::io_failure, std::string(purpose) + " is not a private root-owned directory"));
        }
        /** @brief child directory descriptor / Child directory descriptor. */
        FileDescriptor child_fd(openat(
            parent_fd.get(),
            std::string(child_name).c_str(),
            O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW));
        if (child_fd.get() < 0) {
            return std::unexpected(errno_error(ErrorCode::io_failure, "open transient quota directory"));
        }
        if (const auto removed = remove_directory_contents_at(child_fd.get()); !removed) {
            return std::unexpected(removed.error());
        }
        child_fd.close();
        if (unlinkat(parent_fd.get(), std::string(child_name).c_str(), AT_REMOVEDIR) != 0) {
            return std::unexpected(errno_error(ErrorCode::io_failure, "remove transient quota directory"));
        }
        if (fsync(parent_fd.get()) != 0) {
            return std::unexpected(errno_error(ErrorCode::io_failure, "fsync transient quota parent after removal"));
        }
    } else if (errno != ENOENT) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "stat transient quota directory"));
    }
    if (mkdirat(parent_fd.get(), std::string(child_name).c_str(), 0700) != 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "create transient quota directory"));
    }
    if (fsync(parent_fd.get()) != 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "fsync transient quota parent after creation"));
    }
    return ensure_private_directory(parent / std::string(child_name), purpose);
}

/**
 * @brief 删除一个已验证 private child directory / Remove one verified private child directory.
 * @param parent 父目录 / Parent directory.
 * @param child_name child basename / Child basename.
 * @param purpose 诊断语义 / Diagnostic purpose.
 * @return 成功或 cleanup/I/O 错误 / Success or a cleanup/I/O error.
 */
[[nodiscard]] Result<void> remove_private_child_directory(
    const std::filesystem::path& parent,
    const std::string_view child_name,
    const std::string_view purpose) {
    if (!is_safe_component(child_name)) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "unsafe transient quota cleanup component"));
    }
    /** @brief parent directory descriptor / Parent directory descriptor. */
    FileDescriptor parent_fd(open(parent.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW));
    if (parent_fd.get() < 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "open transient quota cleanup parent"));
    }
    /** @brief child metadata / Child metadata. */
    struct stat metadata {};
    if (fstatat(parent_fd.get(), std::string(child_name).c_str(), &metadata, AT_SYMLINK_NOFOLLOW) != 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "stat transient quota cleanup directory"));
    }
    if (!S_ISDIR(metadata.st_mode) || metadata.st_uid != 0U || (metadata.st_mode & (S_IWGRP | S_IWOTH)) != 0) {
        return std::unexpected(make_error(ErrorCode::io_failure, std::string(purpose) + " is not a private root-owned directory"));
    }
    /** @brief child directory descriptor / Child directory descriptor. */
    FileDescriptor child_fd(openat(
        parent_fd.get(),
        std::string(child_name).c_str(),
        O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW));
    if (child_fd.get() < 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "open transient quota cleanup directory"));
    }
    if (const auto removed = remove_directory_contents_at(child_fd.get()); !removed) {
        return std::unexpected(removed.error());
    }
    child_fd.close();
    if (unlinkat(parent_fd.get(), std::string(child_name).c_str(), AT_REMOVEDIR) != 0 || fsync(parent_fd.get()) != 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "remove and fsync transient quota directory"));
    }
    return {};
}

/**
 * @brief 校验打开的 staging parent 仍是扫描时的 quota 目录 / Verify an open staging parent is still the quota directory scanned earlier.
 * @param parent 已打开的 staging parent 快照 / Open staging-parent snapshot.
 * @param purpose 诊断语义 / Diagnostic purpose.
 * @return 成功或 fail-closed 身份错误 / Success or a fail-closed identity error.
 */
[[nodiscard]] Result<void> verify_activation_staging_parent(
    const ActivationStagingParent& parent,
    const std::string_view purpose) {
    /** @brief 当前 parent metadata / Current parent metadata. */
    struct stat metadata {};
    if (fstat(parent.descriptor.get(), &metadata) != 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "stat " + std::string(purpose)));
    }
    if (!is_private_root_owned_directory(metadata) || metadata.st_dev != parent.device || metadata.st_ino != parent.inode) {
        return std::unexpected(make_error(ErrorCode::io_failure, std::string(purpose) + " changed identity or ownership after staging scan"));
    }
    return verify_project_directory_fd(parent.descriptor.get(), parent.project_id, purpose);
}

/**
 * @brief 扫描一个已验证的 activation staging parent，但不执行删除 / Snapshot one verified activation-staging parent without deleting it.
 * @param path staging parent 的绝对路径 / Absolute staging-parent path.
 * @param project_id 该 parent 的预期 XFS project ID / Expected XFS project ID of the parent.
 * @param purpose 诊断语义 / Diagnostic purpose.
 * @return 已打开、已验证且 bounded 的 direct-child 快照 / Open, verified, bounded direct-child snapshot.
 * @note 任何非 SHA-256 direct child、非目录、越设备或 project-ID 不匹配都会让整个恢复
 *       fail closed，避免只清理已扫描到的一部分。/ Any non-SHA-256 direct child,
 *       non-directory, cross-device entry, or project-ID mismatch fails the whole recovery
 *       closed, so a partially scanned set is never cleaned.
 */
[[nodiscard]] Result<ActivationStagingParent> scan_activation_staging_parent(
    const std::filesystem::path& path,
    const std::uint32_t project_id,
    const std::string_view purpose) {
    if (const auto verified = verify_project_directory(path, project_id); !verified) {
        return std::unexpected(verified.error());
    }
    /** @brief 防 TOCTOU 的 parent directory FD / Parent directory FD preventing TOCTOU. */
    FileDescriptor parent_fd(open(path.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW));
    if (parent_fd.get() < 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "open " + std::string(purpose)));
    }
    /** @brief parent metadata captured before directory iteration / Parent metadata captured before iteration. */
    struct stat parent_metadata {};
    if (fstat(parent_fd.get(), &parent_metadata) != 0 || !is_private_root_owned_directory(parent_metadata)) {
        return std::unexpected(make_error(ErrorCode::io_failure, std::string(purpose) + " is not a private root-owned directory"));
    }
    if (const auto verified = verify_project_directory_fd(parent_fd.get(), project_id, purpose); !verified) {
        return std::unexpected(verified.error());
    }
    ActivationStagingParent snapshot{
        .descriptor = std::move(parent_fd),
        .device = parent_metadata.st_dev,
        .inode = parent_metadata.st_ino,
        .project_id = project_id,
        .entries = {},
    };
    /** @brief duplicated FD owned by directory stream / Duplicated FD owned by the directory stream. */
    const int scan_fd = fcntl(snapshot.descriptor.get(), F_DUPFD_CLOEXEC, 3);
    if (scan_fd < 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "duplicate " + std::string(purpose) + " FD"));
    }
    /** @brief directory stream owning scan_fd / Directory stream owning scan_fd. */
    DIR* directory = fdopendir(scan_fd);
    if (directory == nullptr) {
        const Error error = errno_error(ErrorCode::io_failure, "open " + std::string(purpose) + " stream");
        static_cast<void>(close(scan_fd));
        return std::unexpected(error);
    }
    for (;;) {
        errno = 0;
        /** @brief one direct child entry / One direct-child entry. */
        dirent* const entry = readdir(directory);
        if (entry == nullptr) {
            const int read_error = errno;
            if (closedir(directory) != 0 && read_error == 0) {
                return std::unexpected(errno_error(ErrorCode::io_failure, "close " + std::string(purpose) + " stream"));
            }
            if (read_error != 0) {
                errno = read_error;
                return std::unexpected(errno_error(ErrorCode::io_failure, "read " + std::string(purpose) + " stream"));
            }
            break;
        }
        const std::string_view name{entry->d_name};
        if (name == "." || name == "..") {
            continue;
        }
        if (!is_sha256_component(name)) {
            static_cast<void>(closedir(directory));
            return std::unexpected(make_error(ErrorCode::io_failure, std::string(purpose) + " contains a non-activation direct child"));
        }
        if (snapshot.entries.size() >= kMaxActivationStagingEntries) {
            static_cast<void>(closedir(directory));
            return std::unexpected(make_error(ErrorCode::io_failure, std::string(purpose) + " exceeds the bounded crash-recovery staging limit"));
        }
        /** @brief direct child metadata / Direct-child metadata. */
        struct stat metadata {};
        if (fstatat(snapshot.descriptor.get(), entry->d_name, &metadata, AT_SYMLINK_NOFOLLOW) != 0) {
            const Error error = errno_error(ErrorCode::io_failure, "stat " + std::string(purpose) + " child");
            static_cast<void>(closedir(directory));
            return std::unexpected(error);
        }
        if (!is_private_root_owned_directory(metadata) || metadata.st_dev != snapshot.device) {
            static_cast<void>(closedir(directory));
            return std::unexpected(make_error(ErrorCode::io_failure, std::string(purpose) + " contains an unowned or cross-device direct child"));
        }
        /** @brief no-follow child descriptor / No-follow child descriptor. */
        FileDescriptor child_fd(openat(
            snapshot.descriptor.get(),
            entry->d_name,
            O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW));
        if (child_fd.get() < 0) {
            const Error error = errno_error(ErrorCode::io_failure, "open " + std::string(purpose) + " child");
            static_cast<void>(closedir(directory));
            return std::unexpected(error);
        }
        /** @brief child metadata re-read through opened FD / Child metadata re-read through opened FD. */
        struct stat opened_metadata {};
        if (fstat(child_fd.get(), &opened_metadata) != 0 || !is_private_root_owned_directory(opened_metadata) ||
            opened_metadata.st_dev != metadata.st_dev || opened_metadata.st_ino != metadata.st_ino) {
            static_cast<void>(closedir(directory));
            return std::unexpected(make_error(ErrorCode::io_failure, std::string(purpose) + " child changed identity during staging scan"));
        }
        if (const auto verified = verify_project_directory_fd(child_fd.get(), project_id, std::string(purpose) + " child"); !verified) {
            static_cast<void>(closedir(directory));
            return std::unexpected(verified.error());
        }
        snapshot.entries.push_back(ActivationStagingEntryIdentity{
            .name = std::string(name),
            .device = metadata.st_dev,
            .inode = metadata.st_ino,
        });
    }
    if (const auto verified = verify_activation_staging_parent(snapshot, purpose); !verified) {
        return std::unexpected(verified.error());
    }
    return snapshot;
}

/**
 * @brief 以扫描时 inode 身份删除一个 activation staging child / Delete one activation-staging child by its scanned inode identity.
 * @param parent 已打开的 staging parent 快照 / Open staging-parent snapshot.
 * @param entry 待删 child 的扫描身份 / Scanned identity of the child to delete.
 * @param purpose 诊断语义 / Diagnostic purpose.
 * @return 成功或 fail-closed 删除错误 / Success or a fail-closed deletion error.
 */
[[nodiscard]] Result<void> remove_scanned_activation_staging_entry(
    const ActivationStagingParent& parent,
    const ActivationStagingEntryIdentity& entry,
    const std::string_view purpose) {
    if (const auto verified_parent = verify_activation_staging_parent(parent, purpose); !verified_parent) {
        return std::unexpected(verified_parent.error());
    }
    /** @brief child metadata immediately before deletion / Child metadata immediately before deletion. */
    struct stat metadata {};
    if (fstatat(parent.descriptor.get(), entry.name.c_str(), &metadata, AT_SYMLINK_NOFOLLOW) != 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "stat " + std::string(purpose) + " child before deletion"));
    }
    if (!is_private_root_owned_directory(metadata) || metadata.st_dev != parent.device ||
        metadata.st_dev != entry.device || metadata.st_ino != entry.inode) {
        return std::unexpected(make_error(ErrorCode::io_failure, std::string(purpose) + " child identity or ownership changed before deletion"));
    }
    /** @brief no-follow child descriptor / No-follow child descriptor. */
    FileDescriptor child_fd(openat(
        parent.descriptor.get(),
        entry.name.c_str(),
        O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW));
    if (child_fd.get() < 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "open " + std::string(purpose) + " child before deletion"));
    }
    /** @brief child metadata read through opened FD / Child metadata read through opened FD. */
    struct stat opened_metadata {};
    if (fstat(child_fd.get(), &opened_metadata) != 0 || !is_private_root_owned_directory(opened_metadata) ||
        opened_metadata.st_dev != entry.device || opened_metadata.st_ino != entry.inode) {
        return std::unexpected(make_error(ErrorCode::io_failure, std::string(purpose) + " child changed identity while opening for deletion"));
    }
    if (const auto verified = verify_project_directory_fd(child_fd.get(), parent.project_id, std::string(purpose) + " child"); !verified) {
        return std::unexpected(verified.error());
    }
    if (const auto removed = remove_directory_contents_at(child_fd.get()); !removed) {
        return std::unexpected(removed.error());
    }
    child_fd.close();
    /** @brief child metadata rechecked after content deletion / Child metadata rechecked after content deletion. */
    struct stat after_emptying {};
    if (fstatat(parent.descriptor.get(), entry.name.c_str(), &after_emptying, AT_SYMLINK_NOFOLLOW) != 0 ||
        !is_private_root_owned_directory(after_emptying) || after_emptying.st_dev != entry.device ||
        after_emptying.st_ino != entry.inode) {
        return std::unexpected(make_error(ErrorCode::io_failure, std::string(purpose) + " child changed identity before unlink"));
    }
    if (unlinkat(parent.descriptor.get(), entry.name.c_str(), AT_REMOVEDIR) != 0 || fsync(parent.descriptor.get()) != 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "remove and fsync " + std::string(purpose) + " child"));
    }
    return {};
}

/**
 * @brief 从 binding 得到静态 runtime layout / Derive the static runtime layout from a binding.
 * @param state_root configured state root / Configured state root.
 * @param runtime_key canonical runtime UUID / Canonical runtime UUID.
 * @param record persisted project-pair record / Persisted project-pair record.
 * @return binding / Binding.
 */
[[nodiscard]] RuntimeQuotaBinding make_binding(
    const std::filesystem::path& state_root,
    const std::string_view runtime_key,
    const RegistryRecord& record) {
    /** @brief opaque runtime directory component / Opaque runtime directory component. */
    const std::string component = hash_component(runtime_key);
    /** @brief runtime root directory / Runtime root directory. */
    const std::filesystem::path runtime_dir = state_root / kRuntimesDirectoryName / component;
    return RuntimeQuotaBinding{
        .runtime_dir = runtime_dir,
        .control_dir = runtime_dir / kControlDirectoryName,
        .workspace_dir = runtime_dir / kWorkspaceDirectoryName,
        .control_project_id = record.control_project_id,
        .workspace_project_id = record.workspace_project_id,
    };
}

/**
 * @brief 校验 binding 的路径与 project pair 格式 / Validate binding paths and project-pair shape.
 * @param state_root configured state root / Configured state root.
 * @param config quota configuration / Quota configuration.
 * @param binding binding to validate / Binding to validate.
 * @return 成功或 invalid-argument 错误 / Success or an invalid-argument error.
 */
[[nodiscard]] Result<void> validate_binding_shape(
    const std::filesystem::path& state_root,
    const XfsProjectQuotaConfig& config,
    const RuntimeQuotaBinding& binding) {
    if (binding.runtime_dir.parent_path() != state_root / kRuntimesDirectoryName ||
        !is_sha256_component(binding.runtime_dir.filename().string()) ||
        binding.control_dir != binding.runtime_dir / kControlDirectoryName ||
        binding.workspace_dir != binding.runtime_dir / kWorkspaceDirectoryName ||
        binding.control_project_id < config.project_id_min || binding.workspace_project_id != binding.control_project_id + 1U ||
        binding.workspace_project_id > config.project_id_max ||
        ((binding.control_project_id - config.project_id_min) & 1U) != 0U) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "quota binding does not describe this XFS runtime layout"));
    }
    return {};
}

/**
 * @brief 校验 ready binding 的 root、limits 与基础 layout / Validate roots, limits, and base layout of a ready binding.
 * @param state_root configured state root / Configured state root.
 * @param config quota configuration / Quota configuration.
 * @param binding binding to validate / Binding to validate.
 * @return 成功或 fail-closed 错误 / Success or a fail-closed error.
 */
[[nodiscard]] Result<void> verify_ready_binding(
    const std::filesystem::path& state_root,
    const XfsProjectQuotaConfig& config,
    const RuntimeQuotaBinding& binding) {
    if (const auto shape = validate_binding_shape(state_root, config, binding); !shape) {
        return std::unexpected(shape.error());
    }
    if (const auto control = verify_project_directory(binding.control_dir, binding.control_project_id); !control) {
        return std::unexpected(control.error());
    }
    if (const auto workspace = verify_project_directory(binding.workspace_dir, binding.workspace_project_id); !workspace) {
        return std::unexpected(workspace.error());
    }
    if (const auto control_limits = verify_project_hard_limits(
            config.mount_path,
            binding.control_project_id,
            config.control_hard_bytes,
            config.control_hard_inodes); !control_limits) {
        return std::unexpected(control_limits.error());
    }
    if (const auto workspace_limits = verify_project_hard_limits(
            config.mount_path,
            binding.workspace_project_id,
            config.workspace_hard_bytes,
            config.workspace_hard_inodes); !workspace_limits) {
        return std::unexpected(workspace_limits.error());
    }
    for (const std::pair<std::filesystem::path, std::uint32_t>& directory : {
             std::pair{binding.control_dir / kJournalDirectoryName, binding.control_project_id},
             std::pair{binding.control_dir / kMountsDirectoryName, binding.control_project_id},
             std::pair{binding.workspace_dir / kWorkDirectoryName, binding.workspace_project_id},
         }) {
        if (const auto checked = verify_project_directory(directory.first, directory.second); !checked) {
            return std::unexpected(checked.error());
        }
    }
    if (const auto upper = verify_workspace_upper_directory(
            binding.workspace_dir / kUpperDirectoryName,
            binding.workspace_project_id,
            config);
        !upper) {
        return std::unexpected(upper.error());
    }
    return {};
}

/**
 * @brief 打开并取得一个 runtime 的非阻塞 activation ``flock`` / Open and acquire a runtime's nonblocking activation ``flock``.
 * @param binding 已验证的 runtime quota binding / Verified runtime quota binding.
 * @return 持有 ``LOCK_EX`` 的 private regular-file FD，或 ``busy`` / Private regular-file FD holding ``LOCK_EX``, or ``busy``.
 * @note 这个锁位于 control project 下；它不记录业务状态，只排他 activation staging 的创建、
 *       回收与删除。/ This lock lives under the control project; it records no business state and
 *       only serializes activation-staging creation, reclamation, and removal.
 */
[[nodiscard]] Result<FileDescriptor> lock_runtime_activation(const RuntimeQuotaBinding& binding) {
    /** @brief activation lock path / Activation-lock path. */
    const std::filesystem::path lock_path = binding.control_dir / kActivationLockName;
    /** @brief activation lock descriptor / Activation-lock descriptor. */
    FileDescriptor lock_fd(open(lock_path.c_str(), O_RDWR | O_CREAT | O_CLOEXEC | O_NOFOLLOW, 0600));
    if (lock_fd.get() < 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "open runtime activation lock"));
    }
    /** @brief activation lock metadata / Activation-lock metadata. */
    struct stat metadata {};
    if (fstat(lock_fd.get(), &metadata) != 0 || !S_ISREG(metadata.st_mode) || metadata.st_uid != 0U ||
        metadata.st_nlink != 1 || (metadata.st_mode & (S_IWGRP | S_IWOTH)) != 0) {
        return std::unexpected(make_error(ErrorCode::io_failure, "runtime activation lock is not a private root-owned regular file"));
    }
    if (fchmod(lock_fd.get(), 0600) != 0 || fchown(lock_fd.get(), 0, 0) != 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "protect runtime activation lock"));
    }
    if (flock(lock_fd.get(), LOCK_EX | LOCK_NB) != 0) {
        if (errno == EACCES || errno == EAGAIN) {
            return std::unexpected(make_error(ErrorCode::busy, "runtime activation is owned by another broker"));
        }
        return std::unexpected(errno_error(ErrorCode::io_failure, "lock runtime activation"));
    }
    return lock_fd;
}

/**
 * @brief 在已预留 pair 上创建并验证 runtime base layout / Create and validate the runtime base layout on an already-reserved pair.
 * @param state_root configured state root / Configured state root.
 * @param config quota configuration / Quota configuration.
 * @param binding allocated binding / Allocated binding.
 * @return 成功或 provisioning 错误 / Success or a provisioning error.
 */
[[nodiscard]] Result<void> provision_runtime_layout(
    const std::filesystem::path& state_root,
    const XfsProjectQuotaConfig& config,
    const RuntimeQuotaBinding& binding) {
    if (const auto runtimes = ensure_private_directory(state_root / kRuntimesDirectoryName, "runtime state root"); !runtimes) {
        return std::unexpected(runtimes.error());
    }
    if (const auto runtime = ensure_private_directory(binding.runtime_dir, "runtime state directory"); !runtime) {
        return std::unexpected(runtime.error());
    }
    if (const auto control = ensure_private_directory(binding.control_dir, "runtime control directory"); !control) {
        return std::unexpected(control.error());
    }
    if (const auto workspace = ensure_private_directory(binding.workspace_dir, "runtime workspace directory"); !workspace) {
        return std::unexpected(workspace.error());
    }
    if (const auto control_assignment = assign_project_directory(binding.control_dir, binding.control_project_id); !control_assignment) {
        return std::unexpected(control_assignment.error());
    }
    if (const auto workspace_assignment = assign_project_directory(binding.workspace_dir, binding.workspace_project_id); !workspace_assignment) {
        return std::unexpected(workspace_assignment.error());
    }
    if (const auto control_limits = set_project_hard_limits(
            config.mount_path,
            binding.control_project_id,
            config.control_hard_bytes,
            config.control_hard_inodes); !control_limits) {
        return std::unexpected(control_limits.error());
    }
    if (const auto workspace_limits = set_project_hard_limits(
            config.mount_path,
            binding.workspace_project_id,
            config.workspace_hard_bytes,
            config.workspace_hard_inodes); !workspace_limits) {
        return std::unexpected(workspace_limits.error());
    }
    for (const std::pair<std::filesystem::path, std::uint32_t>& directory : {
             std::pair{binding.control_dir / kJournalDirectoryName, binding.control_project_id},
             std::pair{binding.control_dir / kMountsDirectoryName, binding.control_project_id},
             std::pair{binding.workspace_dir / kUpperDirectoryName, binding.workspace_project_id},
             std::pair{binding.workspace_dir / kWorkDirectoryName, binding.workspace_project_id},
         }) {
        if (const auto created = ensure_private_directory(directory.first, "quota-owned runtime layout directory"); !created) {
            return std::unexpected(created.error());
        }
        if (const auto assigned = assign_project_directory(directory.first, directory.second); !assigned) {
            return std::unexpected(assigned.error());
        }
    }
    return verify_ready_binding(state_root, config, binding);
}

/**
 * @brief 打开并加排他锁的 registry lock file / Open and exclusively lock the registry-lock file.
 * @param registry_root root-owned registry root / Root-owned registry root.
 * @return lock-owning FD 或错误 / Lock-owning FD or an error.
 */
[[nodiscard]] Result<FileDescriptor> lock_registry(const std::filesystem::path& registry_root) {
    /** @brief lock file descriptor / Lock file descriptor. */
    FileDescriptor lock_fd(open(
        (registry_root / kRegistryLockName).c_str(),
        O_RDWR | O_CREAT | O_CLOEXEC | O_NOFOLLOW,
        0600));
    if (lock_fd.get() < 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "open quota registry lock"));
    }
    /** @brief lock metadata / Lock metadata. */
    struct stat metadata {};
    if (fstat(lock_fd.get(), &metadata) != 0 || !S_ISREG(metadata.st_mode) || metadata.st_uid != 0U ||
        (metadata.st_mode & (S_IWGRP | S_IWOTH)) != 0 || fchmod(lock_fd.get(), 0600) != 0 ||
        fchown(lock_fd.get(), 0, 0) != 0) {
        return std::unexpected(make_error(ErrorCode::io_failure, "quota registry lock is not a private root-owned regular file"));
    }
    if (flock(lock_fd.get(), LOCK_EX) != 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "lock quota registry"));
    }
    return lock_fd;
}

/**
 * @brief 以 shared flock 只读锁定既有 registry / Lock an existing registry read-only with a shared flock.
 * @param registry_root 已存在的 root-owned registry root / Existing root-owned registry root.
 * @return 持有 ``LOCK_SH`` 的 lock FD，或 not-found/fail-closed 错误 / Lock FD holding ``LOCK_SH``, or not-found/fail-closed error.
 * @note 与 allocation path 不同，此函数没有 ``O_CREAT``、``fchmod`` 或 ``fchown``，所以 replay
 *       lookup 不会把缺失状态变成持久状态。/ Unlike the allocation path, this has no ``O_CREAT``,
 *       ``fchmod``, or ``fchown``; a replay lookup cannot turn absent state into durable state.
 */
[[nodiscard]] Result<FileDescriptor> lock_existing_registry_shared(const std::filesystem::path& registry_root) {
    /** @brief existing lock file descriptor / Existing lock-file descriptor. */
    FileDescriptor lock_fd(open(
        (registry_root / kRegistryLockName).c_str(),
        O_RDONLY | O_CLOEXEC | O_NOFOLLOW));
    if (lock_fd.get() < 0) {
        if (errno == ENOENT) {
            return std::unexpected(make_error(ErrorCode::not_found, "quota registry lock does not exist"));
        }
        return std::unexpected(errno_error(ErrorCode::io_failure, "open existing quota registry lock"));
    }
    /** @brief existing lock metadata / Existing lock metadata. */
    struct stat metadata {};
    if (fstat(lock_fd.get(), &metadata) != 0 || !S_ISREG(metadata.st_mode) || metadata.st_uid != 0U ||
        metadata.st_nlink != 1 || (metadata.st_mode & (S_IWGRP | S_IWOTH)) != 0) {
        return std::unexpected(make_error(ErrorCode::io_failure, "existing quota registry lock is not a private root-owned regular file"));
    }
    if (flock(lock_fd.get(), LOCK_SH) != 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "shared-lock existing quota registry"));
    }
    return lock_fd;
}

/**
 * @brief 读取 registry 的 next project ID / Read the registry's next project ID.
 * @param registry_root root-owned registry root / Root-owned registry root.
 * @param initial_id configured initial ID / Configured initial ID.
 * @return persisted or initial next ID / Persisted or initial next ID.
 */
[[nodiscard]] Result<std::uint32_t> read_next_project_id(
    const std::filesystem::path& registry_root,
    const std::uint32_t initial_id) {
    const auto exists = path_exists_no_follow(registry_root / kRegistryNextIdName);
    if (!exists) {
        return std::unexpected(exists.error());
    }
    if (!*exists) {
        return initial_id;
    }
    /** @brief root directory descriptor / Root directory descriptor. */
    FileDescriptor root_fd(open(registry_root.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW));
    if (root_fd.get() < 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "open quota registry root"));
    }
    const auto contents = read_regular_file_at(root_fd.get(), kRegistryNextIdName, kMaxNextIdBytes, "quota registry next-ID");
    if (!contents || !contents->ends_with('\n')) {
        return std::unexpected(contents ? make_error(ErrorCode::io_failure, "quota registry next-ID lacks final newline") : contents.error());
    }
    return parse_u32(std::string_view(*contents).substr(0U, contents->size() - 1U), "quota registry next-ID");
}

/**
 * @brief 从 records directory 读取并验证完整 snapshot / Read and validate the full snapshot from the records directory.
 * @param registry_root root-owned registry root / Root-owned registry root.
 * @param config quota configuration / Quota configuration.
 * @return record 映射或错误 / Record mapping or an error.
 */
[[nodiscard]] Result<std::unordered_map<std::string, RegistryRecord>> read_registry_records(
    const std::filesystem::path& registry_root,
    const XfsProjectQuotaConfig& config) {
    /** @brief records directory path / Records directory path. */
    const std::filesystem::path records_directory = registry_root / kRegistryRecordsDirectoryName;
    if (const auto directory = validate_private_directory(records_directory, "quota registry records directory"); !directory) {
        return std::unexpected(directory.error());
    }
    /** @brief records directory descriptor / Records directory descriptor. */
    FileDescriptor records_fd(open(records_directory.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW));
    if (records_fd.get() < 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "open quota registry records directory"));
    }
    /** @brief duplicate descriptor for directory enumeration / Duplicate descriptor for directory enumeration. */
    const int scan_fd = fcntl(records_fd.get(), F_DUPFD_CLOEXEC, 3);
    if (scan_fd < 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "duplicate quota registry records descriptor"));
    }
    /** @brief record directory stream / Record directory stream. */
    DIR* const directory = fdopendir(scan_fd);
    if (directory == nullptr) {
        const Error error = errno_error(ErrorCode::io_failure, "open quota registry records stream");
        static_cast<void>(close(scan_fd));
        return std::unexpected(error);
    }
    /** @brief loaded records / Loaded records. */
    std::unordered_map<std::string, RegistryRecord> records;
    /** @brief every allocated project ID / Every allocated project ID. */
    std::unordered_set<std::uint32_t> ids;
    for (;;) {
        errno = 0;
        /** @brief directory entry / Directory entry. */
        dirent* const entry = readdir(directory);
        if (entry == nullptr) {
            const int read_error = errno;
            static_cast<void>(closedir(directory));
            if (read_error != 0) {
                errno = read_error;
                return std::unexpected(errno_error(ErrorCode::io_failure, "read quota registry records stream"));
            }
            break;
        }
        const std::string_view filename{entry->d_name};
        if (filename == "." || filename == "..") {
            continue;
        }
        if (!is_sha256_component(filename)) {
            return std::unexpected(make_error(ErrorCode::io_failure, "quota registry records directory contains an unknown entry"));
        }
        /** @brief record file metadata / Record file metadata. */
        struct stat metadata {};
        if (fstatat(records_fd.get(), entry->d_name, &metadata, AT_SYMLINK_NOFOLLOW) != 0 || !S_ISREG(metadata.st_mode)) {
            return std::unexpected(make_error(ErrorCode::io_failure, "quota registry record is not a regular file"));
        }
        const auto contents = read_regular_file_at(records_fd.get(), filename, kMaxRegistryRecordBytes, "quota registry record");
        if (!contents) {
            return std::unexpected(contents.error());
        }
        const auto record = decode_record(*contents);
        if (!record) {
            return std::unexpected(record.error());
        }
        if (hash_component(record->runtime_key) != filename || record->control_project_id < config.project_id_min ||
            record->workspace_project_id != record->control_project_id + 1U ||
            record->workspace_project_id > config.project_id_max ||
            ((record->control_project_id - config.project_id_min) & 1U) != 0U ||
            !ids.insert(record->control_project_id).second || !ids.insert(record->workspace_project_id).second ||
            !records.emplace(record->runtime_key, *record).second) {
            return std::unexpected(make_error(ErrorCode::io_failure, "quota registry contains an inconsistent runtime/project pair"));
        }
    }
    return records;
}

/**
 * @brief 读取、校验 registry snapshot 与 admission reservation / Read and validate a registry snapshot and admission reservations.
 * @param registry_root root-owned registry root / Root-owned registry root.
 * @param config quota configuration / Quota configuration.
 * @return snapshot 或一致性错误 / Snapshot or a consistency error.
 */
[[nodiscard]] Result<RegistrySnapshot> read_registry_snapshot(
    const std::filesystem::path& registry_root,
    const XfsProjectQuotaConfig& config) {
    const auto next_id = read_next_project_id(registry_root, config.project_id_min);
    const auto records = read_registry_records(registry_root, config);
    if (!next_id || !records) {
        return std::unexpected(!next_id ? next_id.error() : records.error());
    }
    if (*next_id < config.project_id_min || *next_id > config.project_id_max + 1U ||
        ((*next_id - config.project_id_min) & 1U) != 0U) {
        return std::unexpected(make_error(ErrorCode::io_failure, "quota registry next project ID is outside the configured pair range"));
    }
    /** @brief reserved bytes across all non-reusable states / Reserved bytes across all non-reusable states. */
    std::uint64_t reserved_bytes{0U};
    /** @brief reserved inodes across all non-reusable states / Reserved inodes across all non-reusable states. */
    std::uint64_t reserved_inodes{0U};
    /** @brief maximum allocated control ID / Maximum allocated control ID. */
    std::uint32_t maximum_control_id{0U};
    for (const auto& [runtime_key, record] : *records) {
        static_cast<void>(runtime_key);
        const auto bytes_after_control = checked_add(reserved_bytes, config.control_hard_bytes, "registry reserved bytes");
        const auto bytes_after_workspace = bytes_after_control
            ? checked_add(*bytes_after_control, config.workspace_hard_bytes, "registry reserved bytes")
            : Result<std::uint64_t>{std::unexpected(bytes_after_control.error())};
        const auto inodes_after_control = checked_add(reserved_inodes, config.control_hard_inodes, "registry reserved inodes");
        const auto inodes_after_workspace = inodes_after_control
            ? checked_add(*inodes_after_control, config.workspace_hard_inodes, "registry reserved inodes")
            : Result<std::uint64_t>{std::unexpected(inodes_after_control.error())};
        if (!bytes_after_workspace || !inodes_after_workspace) {
            return std::unexpected(!bytes_after_workspace ? bytes_after_workspace.error() : inodes_after_workspace.error());
        }
        reserved_bytes = *bytes_after_workspace;
        reserved_inodes = *inodes_after_workspace;
        maximum_control_id = std::max(maximum_control_id, record.control_project_id);
    }
    if (reserved_bytes > config.global_admission_bytes || reserved_inodes > config.global_admission_inodes ||
        (maximum_control_id != 0U && *next_id < maximum_control_id + 2U)) {
        return std::unexpected(make_error(ErrorCode::io_failure, "quota registry violates admission budget or monotonic project-ID allocation"));
    }
    return RegistrySnapshot{.records = std::move(*records), .next_project_id = *next_id};
}

/**
 * @brief 计算给定 snapshot 的已预留 admission / Compute admission reservations of a snapshot.
 * @param snapshot validated registry snapshot / Validated registry snapshot.
 * @param config quota configuration / Quota configuration.
 * @return ``(bytes,inodes)`` / ``(bytes,inodes)``.
 */
[[nodiscard]] Result<std::pair<std::uint64_t, std::uint64_t>> reservations_of(
    const RegistrySnapshot& snapshot,
    const XfsProjectQuotaConfig& config) {
    /** @brief reserved byte total / Reserved byte total. */
    std::uint64_t bytes{0U};
    /** @brief reserved inode total / Reserved inode total. */
    std::uint64_t inodes{0U};
    for (const auto& [runtime_key, record] : snapshot.records) {
        static_cast<void>(runtime_key);
        static_cast<void>(record);
        const auto after_control_bytes = checked_add(bytes, config.control_hard_bytes, "XFS admission bytes");
        const auto after_workspace_bytes = after_control_bytes
            ? checked_add(*after_control_bytes, config.workspace_hard_bytes, "XFS admission bytes")
            : Result<std::uint64_t>{std::unexpected(after_control_bytes.error())};
        const auto after_control_inodes = checked_add(inodes, config.control_hard_inodes, "XFS admission inodes");
        const auto after_workspace_inodes = after_control_inodes
            ? checked_add(*after_control_inodes, config.workspace_hard_inodes, "XFS admission inodes")
            : Result<std::uint64_t>{std::unexpected(after_control_inodes.error())};
        if (!after_workspace_bytes || !after_workspace_inodes) {
            return std::unexpected(!after_workspace_bytes ? after_workspace_bytes.error() : after_workspace_inodes.error());
        }
        bytes = *after_workspace_bytes;
        inodes = *after_workspace_inodes;
    }
    return std::pair{bytes, inodes};
}

/**
 * @brief 将新 runtime record 持久写入 records directory / Persist one new runtime record in the records directory.
 * @param registry_root root-owned registry root / Root-owned registry root.
 * @param record record to persist / Record to persist.
 * @return 成功或 I/O 错误 / Success or an I/O error.
 */
[[nodiscard]] Result<void> write_registry_record(
    const std::filesystem::path& registry_root,
    const RegistryRecord& record) {
    return atomic_write_private_file(
        registry_root / kRegistryRecordsDirectoryName,
        hash_component(record.runtime_key),
        encode_record(record),
        "quota registry runtime record");
}

/**
 * @brief 将 next project ID 持久写入 registry / Persist the next project ID in the registry.
 * @param registry_root root-owned registry root / Root-owned registry root.
 * @param next_project_id next never-reused control project ID / Next never-reused control project ID.
 * @return 成功或 I/O 错误 / Success or an I/O error.
 */
[[nodiscard]] Result<void> write_next_project_id(
    const std::filesystem::path& registry_root,
    const std::uint32_t next_project_id) {
    return atomic_write_private_file(
        registry_root,
        kRegistryNextIdName,
        std::to_string(next_project_id) + "\n",
        "quota registry next-ID");
}

/**
 * @brief 确保 registry 根与 records 子目录存在 / Ensure the registry root and records child directory exist.
 * @param state_root canonical state root / Canonical state root.
 * @return registry root 或错误 / Registry root or an error.
 */
[[nodiscard]] Result<std::filesystem::path> ensure_registry_directories(const std::filesystem::path& state_root) {
    /** @brief registry root / Registry root. */
    const std::filesystem::path registry_root = state_root / kRegistryDirectoryName;
    if (const auto root = ensure_private_directory(registry_root, "quota registry root"); !root) {
        return std::unexpected(root.error());
    }
    if (const auto records = ensure_private_directory(registry_root / kRegistryRecordsDirectoryName, "quota registry records root"); !records) {
        return std::unexpected(records.error());
    }
    return registry_root;
}

/**
 * @brief 按 binding 创建与读回 activation storage / Create and read back activation storage for a binding.
 * @param binding ready runtime binding / Ready runtime binding.
 * @param activation_component hashed activation component / Hashed activation component.
 * @return activation storage paths / Activation storage paths.
 */
[[nodiscard]] Result<RuntimeQuotaActivationStorage> create_activation_storage(
    const RuntimeQuotaBinding& binding,
    const std::string_view activation_component) {
    /** @brief control mount staging parent / Control mount-staging parent. */
    const std::filesystem::path mounts_parent = binding.control_dir / kMountsDirectoryName;
    /** @brief workspace work staging parent / Workspace work-staging parent. */
    const std::filesystem::path work_parent = binding.workspace_dir / kWorkDirectoryName;
    if (const auto control_activation = recreate_private_child_directory(mounts_parent, activation_component, "control activation directory"); !control_activation) {
        return std::unexpected(control_activation.error());
    }
    if (const auto workspace_work = recreate_private_child_directory(work_parent, activation_component, "workspace activation work directory"); !workspace_work) {
        return std::unexpected(workspace_work.error());
    }
    /** @brief activation control path / Activation control path. */
    const std::filesystem::path control_activation = mounts_parent / std::string(activation_component);
    /** @brief activation work path / Activation work path. */
    const std::filesystem::path workspace_work = work_parent / std::string(activation_component);
    if (const auto control_assigned = assign_project_directory(control_activation, binding.control_project_id); !control_assigned) {
        return std::unexpected(control_assigned.error());
    }
    if (const auto work_assigned = assign_project_directory(workspace_work, binding.workspace_project_id); !work_assigned) {
        return std::unexpected(work_assigned.error());
    }
    for (const std::filesystem::path& directory : {
             control_activation / kRootDirectoryName,
             control_activation / kWorkspaceLowerDirectoryName,
         }) {
        if (const auto created = ensure_private_directory(directory, "control activation mount directory"); !created) {
            return std::unexpected(created.error());
        }
        if (const auto assigned = assign_project_directory(directory, binding.control_project_id); !assigned) {
            return std::unexpected(assigned.error());
        }
    }
    return RuntimeQuotaActivationStorage{
        .control_activation_dir = control_activation,
        .workspace_work_dir = workspace_work,
    };
}

}  // namespace

RuntimeActivationLease::RuntimeActivationLease(RuntimeQuotaBinding binding, const int lock_fd) noexcept
    : binding_(std::move(binding)), lock_fd_(lock_fd) {}

RuntimeActivationLease::~RuntimeActivationLease() {
    if (lock_fd_ >= 0) {
        static_cast<void>(close(lock_fd_));
    }
}

RuntimeActivationLease::RuntimeActivationLease(RuntimeActivationLease&& other) noexcept
    : binding_(std::move(other.binding_)), lock_fd_(std::exchange(other.lock_fd_, -1)) {}

RuntimeActivationLease& RuntimeActivationLease::operator=(RuntimeActivationLease&& other) noexcept {
    if (this != &other) {
        if (lock_fd_ >= 0) {
            static_cast<void>(close(lock_fd_));
        }
        binding_ = std::move(other.binding_);
        lock_fd_ = std::exchange(other.lock_fd_, -1);
    }
    return *this;
}

const RuntimeQuotaBinding& RuntimeActivationLease::binding() const noexcept {
    return binding_;
}

Result<void> preflight_xfs_project_quota(
    const XfsProjectQuotaConfig& config,
    const std::filesystem::path& state_root) {
    if (const auto values = validate_configuration_values(config); !values) {
        return std::unexpected(values.error());
    }
    const auto mount_path = canonical_directory(config.mount_path, "XFS quota mount");
    const auto canonical_state_root = canonical_directory(state_root, "XFS quota state_root");
    if (!mount_path || !canonical_state_root) {
        return std::unexpected(!mount_path ? mount_path.error() : canonical_state_root.error());
    }
    if (const auto dedicated = require_dedicated_mountpoint(*mount_path); !dedicated) {
        return std::unexpected(dedicated.error());
    }
    /** @brief XFS filesystem metadata / XFS filesystem metadata. */
    struct statfs filesystem {};
    /** @brief XFS capacity metadata / XFS capacity metadata. */
    struct statvfs capacity {};
    /** @brief mountpoint device metadata / Mountpoint device metadata. */
    struct stat mount_metadata {};
    /** @brief state-root device metadata / State-root device metadata. */
    struct stat state_metadata {};
    if (statfs(mount_path->c_str(), &filesystem) != 0 || statvfs(mount_path->c_str(), &capacity) != 0 ||
        stat(mount_path->c_str(), &mount_metadata) != 0 || stat(canonical_state_root->c_str(), &state_metadata) != 0) {
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed, "inspect XFS quota mount"));
    }
    if (filesystem.f_type != XFS_SUPER_MAGIC || (capacity.f_flag & ST_RDONLY) != 0U ||
        mount_metadata.st_dev != state_metadata.st_dev || !is_below_or_equal(*canonical_state_root, *mount_path)) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "state_root must lie on the configured writable dedicated XFS quota mount"));
    }
    if (capacity.f_frsize == 0U || capacity.f_blocks > std::numeric_limits<std::uint64_t>::max() / capacity.f_frsize) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "XFS quota mount capacity cannot be represented safely"));
    }
    /** @brief usable filesystem bytes / Usable filesystem bytes. */
    const std::uint64_t filesystem_bytes = static_cast<std::uint64_t>(capacity.f_blocks) * static_cast<std::uint64_t>(capacity.f_frsize);
    /** @brief usable filesystem inode count / Usable filesystem inode count. */
    const std::uint64_t filesystem_inodes = static_cast<std::uint64_t>(capacity.f_files);
    const auto budget_bytes = checked_add(config.global_admission_bytes, config.system_reserve_bytes, "XFS byte budget");
    const auto budget_inodes = checked_add(config.global_admission_inodes, config.system_reserve_inodes, "XFS inode budget");
    if (!budget_bytes || !budget_inodes || *budget_bytes > filesystem_bytes || *budget_inodes > filesystem_inodes) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "XFS admission budget plus system reserve exceeds filesystem capacity"));
    }
    if (const auto state_directory = validate_private_directory(*canonical_state_root, "XFS quota state_root"); !state_directory) {
        return std::unexpected(state_directory.error());
    }
    if (const auto quota = require_project_quota_enforcement(*mount_path); !quota) {
        return std::unexpected(quota.error());
    }
    return reject_legacy_state(*canonical_state_root);
}

XfsProjectQuota::XfsProjectQuota(std::filesystem::path state_root, XfsProjectQuotaConfig config)
    : state_root_(std::move(state_root)), config_(std::move(config)) {}

Result<void> XfsProjectQuota::validate_activation_lease(const RuntimeActivationLease& lease) const {
    if (lease.lock_fd_ < 0) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "runtime activation lease no longer owns an activation lock"));
    }
    /** @brief activation-lock metadata / Activation-lock metadata. */
    struct stat lock_metadata {};
    if (fstat(lease.lock_fd_, &lock_metadata) != 0 || !S_ISREG(lock_metadata.st_mode) || lock_metadata.st_uid != 0U ||
        lock_metadata.st_nlink != 1 || (lock_metadata.st_mode & (S_IWGRP | S_IWOTH)) != 0) {
        return std::unexpected(make_error(ErrorCode::io_failure, "runtime activation lease lock is no longer a private root-owned regular file"));
    }
    if (const auto preflight = preflight_xfs_project_quota(config_, state_root_); !preflight) {
        return std::unexpected(preflight.error());
    }
    return verify_ready_binding(state_root_, config_, lease.binding_);
}

Result<RuntimeQuotaBinding> XfsProjectQuota::ensure_runtime(const std::string_view runtime_key) const {
    const auto parsed_runtime = domain::RuntimeId::parse(std::string(runtime_key));
    if (!parsed_runtime) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, parsed_runtime.error().message));
    }
    if (const auto preflight = preflight_xfs_project_quota(config_, state_root_); !preflight) {
        return std::unexpected(preflight.error());
    }
    const auto registry_root = ensure_registry_directories(state_root_);
    if (!registry_root) {
        return std::unexpected(registry_root.error());
    }
    const auto registry_lock = lock_registry(*registry_root);
    if (!registry_lock) {
        return std::unexpected(registry_lock.error());
    }
    const auto snapshot = read_registry_snapshot(*registry_root, config_);
    if (!snapshot) {
        return std::unexpected(snapshot.error());
    }
    const auto existing = snapshot->records.find(parsed_runtime->value());
    if (existing != snapshot->records.end()) {
        RegistryRecord record = existing->second;
        RuntimeQuotaBinding binding = make_binding(state_root_, parsed_runtime->value(), record);
        if (record.state == RegistryState::ready) {
            if (const auto verified = verify_ready_binding(state_root_, config_, binding); verified) {
                return binding;
            } else {
                record.state = RegistryState::quarantined;
                if (const auto persisted = write_registry_record(*registry_root, record); !persisted) {
                    return std::unexpected(persisted.error());
                }
                return std::unexpected(make_error(ErrorCode::invocation_in_doubt, "ready XFS quota binding failed readback and was quarantined"));
            }
        }
        if (record.state == RegistryState::allocating) {
            record.state = RegistryState::quarantined;
            if (const auto persisted = write_registry_record(*registry_root, record); !persisted) {
                return std::unexpected(persisted.error());
            }
            return std::unexpected(make_error(ErrorCode::invocation_in_doubt, "interrupted XFS quota provisioning was quarantined; operator recovery is required"));
        }
        return std::unexpected(make_error(ErrorCode::invocation_in_doubt, "runtime XFS quota binding is quarantined; operator recovery is required"));
    }
    RegistryRecord record{
        .runtime_key = parsed_runtime->value(),
        .control_project_id = snapshot->next_project_id,
        .workspace_project_id = snapshot->next_project_id + 1U,
        .state = RegistryState::allocating,
    };
    if (record.workspace_project_id > config_.project_id_max) {
        return std::unexpected(make_error(ErrorCode::busy, "XFS project-ID pair range is exhausted"));
    }
    const auto reservations = reservations_of(*snapshot, config_);
    const auto per_runtime_bytes = checked_add(config_.control_hard_bytes, config_.workspace_hard_bytes, "per-runtime byte reservation");
    const auto per_runtime_inodes = checked_add(config_.control_hard_inodes, config_.workspace_hard_inodes, "per-runtime inode reservation");
    const auto new_bytes = reservations && per_runtime_bytes
        ? checked_add(reservations->first, *per_runtime_bytes, "XFS admission bytes")
        : Result<std::uint64_t>{std::unexpected(reservations ? per_runtime_bytes.error() : reservations.error())};
    const auto new_inodes = reservations && per_runtime_inodes
        ? checked_add(reservations->second, *per_runtime_inodes, "XFS admission inodes")
        : Result<std::uint64_t>{std::unexpected(reservations ? per_runtime_inodes.error() : reservations.error())};
    if (!new_bytes || !new_inodes || *new_bytes > config_.global_admission_bytes || *new_inodes > config_.global_admission_inodes) {
        return std::unexpected(make_error(ErrorCode::busy, "XFS runtime admission budget is exhausted"));
    }
    RuntimeQuotaBinding binding = make_binding(state_root_, parsed_runtime->value(), record);
    const auto existing_runtime_dir = path_exists_no_follow(binding.runtime_dir);
    if (!existing_runtime_dir) {
        return std::unexpected(existing_runtime_dir.error());
    }
    if (*existing_runtime_dir) {
        return std::unexpected(make_error(ErrorCode::invocation_in_doubt, "runtime state exists without a quota registry record; explicit recovery is required"));
    }
    if (const auto next = write_next_project_id(*registry_root, snapshot->next_project_id + 2U); !next) {
        return std::unexpected(next.error());
    }
    if (const auto persisted = write_registry_record(*registry_root, record); !persisted) {
        return std::unexpected(persisted.error());
    }
    if (const auto provisioned = provision_runtime_layout(state_root_, config_, binding); !provisioned) {
        record.state = RegistryState::quarantined;
        if (const auto quarantined = write_registry_record(*registry_root, record); !quarantined) {
            return std::unexpected(quarantined.error());
        }
        return std::unexpected(make_error(ErrorCode::invocation_in_doubt, "XFS quota provisioning failed and the project pair was quarantined"));
    }
    record.state = RegistryState::ready;
    if (const auto persisted = write_registry_record(*registry_root, record); !persisted) {
        return std::unexpected(make_error(ErrorCode::invocation_in_doubt, "XFS layout is provisioned but ready registry persistence failed"));
    }
    return binding;
}

Result<RuntimeQuotaBinding> XfsProjectQuota::find_ready_runtime(const std::string_view runtime_key) const {
    const auto parsed_runtime = domain::RuntimeId::parse(std::string(runtime_key));
    if (!parsed_runtime) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, parsed_runtime.error().message));
    }
    if (const auto preflight = preflight_xfs_project_quota(config_, state_root_); !preflight) {
        return std::unexpected(preflight.error());
    }

    // This intentionally bypasses the allocation helper: replay must be observational.
    // In particular, a missing registry/records/lock tree must stay missing after this call.
    const std::filesystem::path registry_root = state_root_ / kRegistryDirectoryName;
    const auto registry_exists = path_exists_no_follow(registry_root);
    if (!registry_exists) {
        return std::unexpected(registry_exists.error());
    }
    if (!*registry_exists) {
        return std::unexpected(make_error(ErrorCode::not_found, "quota registry does not exist"));
    }
    if (const auto registry_directory = validate_private_directory(registry_root, "existing quota registry root"); !registry_directory) {
        return std::unexpected(registry_directory.error());
    }
    const std::filesystem::path records_directory = registry_root / kRegistryRecordsDirectoryName;
    const auto records_exists = path_exists_no_follow(records_directory);
    if (!records_exists) {
        return std::unexpected(records_exists.error());
    }
    if (!*records_exists) {
        return std::unexpected(make_error(ErrorCode::not_found, "quota registry records do not exist"));
    }

    const auto registry_lock = lock_existing_registry_shared(registry_root);
    if (!registry_lock) {
        return std::unexpected(registry_lock.error());
    }
    const auto snapshot = read_registry_snapshot(registry_root, config_);
    if (!snapshot) {
        return std::unexpected(snapshot.error());
    }
    const auto existing = snapshot->records.find(parsed_runtime->value());
    if (existing == snapshot->records.end()) {
        return std::unexpected(make_error(ErrorCode::not_found, "runtime has no persisted XFS quota binding"));
    }
    if (existing->second.state != RegistryState::ready) {
        return std::unexpected(make_error(
            ErrorCode::invocation_in_doubt,
            "runtime XFS quota binding is not ready; replay cannot provision or recover it"));
    }

    RuntimeQuotaBinding binding = make_binding(state_root_, parsed_runtime->value(), existing->second);
    if (const auto verified = verify_ready_binding(state_root_, config_, binding); !verified) {
        return std::unexpected(make_error(
            ErrorCode::invocation_in_doubt,
            "ready XFS quota binding failed read-only verification: " + verified.error().message));
    }
    return binding;
}

Result<domain::WorkspaceQuotaUsage> XfsProjectQuota::read_workspace_quota_usage(
    const RuntimeQuotaBinding& binding) const {
    if (const auto verified = verify_ready_binding(state_root_, config_, binding); !verified) {
        return std::unexpected(make_error(
            ErrorCode::invocation_in_doubt,
            "workspace quota binding failed read-only verification before usage query"));
    }
    const auto quota = read_project_quota_accounting(config_.mount_path, binding.workspace_project_id);
    if (!quota) {
        return std::unexpected(quota.error());
    }
    if (quota->d_bcount > std::numeric_limits<std::uint64_t>::max() / kQuotaBasicBlockBytes ||
        quota->d_blk_hardlimit > std::numeric_limits<std::uint64_t>::max() / kQuotaBasicBlockBytes) {
        return std::unexpected(make_error(ErrorCode::io_failure, "XFS workspace quota byte count overflows uint64"));
    }
    const auto usage = domain::WorkspaceQuotaUsage::create(
        static_cast<std::uint64_t>(quota->d_bcount) * kQuotaBasicBlockBytes,
        static_cast<std::uint64_t>(quota->d_blk_hardlimit) * kQuotaBasicBlockBytes,
        static_cast<std::uint64_t>(quota->d_icount),
        static_cast<std::uint64_t>(quota->d_ino_hardlimit));
    if (!usage) {
        return std::unexpected(make_error(
            ErrorCode::io_failure,
            "XFS workspace quota accounting violates the operator domain contract"));
    }
    return *usage;
}

Result<RuntimeActivationLease> XfsProjectQuota::acquire_activation_lease(const std::string_view runtime_key) const {
    auto binding = ensure_runtime(runtime_key);
    if (!binding) {
        return std::unexpected(binding.error());
    }
    auto lock = lock_runtime_activation(*binding);
    if (!lock) {
        return std::unexpected(lock.error());
    }
    if (const auto verified = verify_ready_binding(state_root_, config_, *binding); !verified) {
        return std::unexpected(verified.error());
    }
    return RuntimeActivationLease{std::move(*binding), lock->release()};
}

Result<RuntimeQuotaActivationStorage> XfsProjectQuota::prepare_activation_storage(
    const RuntimeActivationLease& lease,
    const std::string_view activation_id) const {
    const auto parsed_activation = domain::ActivationId::parse(std::string(activation_id));
    if (!parsed_activation) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, parsed_activation.error().message));
    }
    if (const auto lease_valid = validate_activation_lease(lease); !lease_valid) {
        return std::unexpected(lease_valid.error());
    }
    const std::string activation_component = hash_component(parsed_activation->value());
    return create_activation_storage(lease.binding_, activation_component);
}

Result<void> XfsProjectQuota::cleanup_activation_storage(
    const RuntimeActivationLease& lease,
    const std::string_view activation_id) const {
    const auto parsed_activation = domain::ActivationId::parse(std::string(activation_id));
    if (!parsed_activation) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, parsed_activation.error().message));
    }
    if (const auto lease_valid = validate_activation_lease(lease); !lease_valid) {
        return std::unexpected(lease_valid.error());
    }
    const std::string activation_component = hash_component(parsed_activation->value());
    if (const auto control = remove_private_child_directory(
            lease.binding_.control_dir / kMountsDirectoryName,
            activation_component,
            "control activation cleanup directory"); !control) {
        return std::unexpected(control.error());
    }
    if (const auto workspace = remove_private_child_directory(
            lease.binding_.workspace_dir / kWorkDirectoryName,
            activation_component,
            "workspace activation cleanup directory"); !workspace) {
        return std::unexpected(workspace.error());
    }
    return {};
}

Result<void> XfsProjectQuota::reclaim_dead_activation_storage(const RuntimeActivationLease& lease) const {
    if (const auto lease_valid = validate_activation_lease(lease); !lease_valid) {
        return std::unexpected(lease_valid.error());
    }
    // Snapshot both parents before the first unlink. A malformed entry in either tree therefore
    // leaves every staging entry intact for operator diagnosis instead of producing a misleading
    // partial recovery.
    const auto control = scan_activation_staging_parent(
        lease.binding_.control_dir / kMountsDirectoryName,
        lease.binding_.control_project_id,
        "control activation staging parent");
    if (!control) {
        return std::unexpected(control.error());
    }
    const auto workspace = scan_activation_staging_parent(
        lease.binding_.workspace_dir / kWorkDirectoryName,
        lease.binding_.workspace_project_id,
        "workspace activation staging parent");
    if (!workspace) {
        return std::unexpected(workspace.error());
    }
    for (const ActivationStagingEntryIdentity& entry : control->entries) {
        if (const auto removed = remove_scanned_activation_staging_entry(*control, entry, "control activation staging parent"); !removed) {
            return std::unexpected(removed.error());
        }
    }
    for (const ActivationStagingEntryIdentity& entry : workspace->entries) {
        if (const auto removed = remove_scanned_activation_staging_entry(*workspace, entry, "workspace activation staging parent"); !removed) {
            return std::unexpected(removed.error());
        }
    }
    return {};
}

}  // namespace wspctl
