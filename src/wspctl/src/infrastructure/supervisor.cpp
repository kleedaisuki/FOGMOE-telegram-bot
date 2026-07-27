#include "wspctl/infrastructure/supervisor.hpp"

#include "wspctl/infrastructure/sandbox.hpp"

#include <openssl/crypto.h>
#include <openssl/evp.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <chrono>
#include <climits>
#include <csignal>
#include <cstdlib>
#include <fcntl.h>
#include <linux/close_range.h>
#include <linux/openat2.h>
#include <poll.h>
#include <sys/fsuid.h>
#include <sys/random.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <sys/resource.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <utility>
#include <vector>

namespace wspctl {
namespace {

/** @brief SIGCHLD 到达标记 / SIGCHLD arrival marker. */
volatile sig_atomic_t g_sigchld_pending = 0;

/**
 * @brief 最小 SIGCHLD 处理器 / Minimal SIGCHLD handler.
 * @param signal 收到的信号 / Received signal.
 */
void on_sigchld(const int signal) {
    if (signal == SIGCHLD) {
        g_sigchld_pending = 1;
    }
}

/** @brief 设置 SIGCHLD handler / Install the SIGCHLD handler. */
[[nodiscard]] Result<void> install_sigchld_handler() {
    struct sigaction action {};
    action.sa_handler = on_sigchld;
    sigemptyset(&action.sa_mask);
    action.sa_flags = SA_NOCLDSTOP;
    if (sigaction(SIGCHLD, &action, nullptr) != 0) {
        return std::unexpected(errno_error(ErrorCode::internal, "install SIGCHLD handler"));
    }
    struct sigaction pipe_action {};
    pipe_action.sa_handler = SIG_IGN;
    sigemptyset(&pipe_action.sa_mask);
    pipe_action.sa_flags = 0;
    if (sigaction(SIGPIPE, &pipe_action, nullptr) != 0) {
        return std::unexpected(errno_error(ErrorCode::internal, "ignore SIGPIPE in PID 1"));
    }
    return {};
}

/** @brief 关闭 FD 且置为 -1 / Close an FD and set it to -1. */
void close_fd(int& fd) noexcept {
    if (fd >= 0) {
        static_cast<void>(close(fd));
        fd = -1;
    }
}

/** @brief 文件写入时暂时采用 sandbox 文件系统身份 / Temporarily adopt sandbox filesystem credentials for file ingress. */
class ScopedFsCredentials final {
public:
    /**
     * @brief 尝试切换到 sandbox fsuid/fsgid / Attempt to switch to sandbox fsuid/fsgid.
     * @param uid 目标 filesystem UID / Target filesystem UID.
     * @param gid 目标 filesystem GID / Target filesystem GID.
     * @return 已切换凭据或失败 / Switched credentials or a failure.
     */
    [[nodiscard]] static Result<ScopedFsCredentials> enter(const uid_t uid, const gid_t gid) {
        ScopedFsCredentials credentials;
        credentials.previous_gid_ = setfsgid(gid);
        credentials.gid_changed_ = true;
        if (static_cast<gid_t>(setfsgid(static_cast<gid_t>(-1))) != gid) {
            credentials.restore();
            return std::unexpected(make_error(ErrorCode::permission_denied, "cannot assume sandbox filesystem GID"));
        }
        credentials.previous_uid_ = setfsuid(uid);
        credentials.uid_changed_ = true;
        if (static_cast<uid_t>(setfsuid(static_cast<uid_t>(-1))) != uid) {
            credentials.restore();
            return std::unexpected(make_error(ErrorCode::permission_denied, "cannot assume sandbox filesystem UID"));
        }
        return credentials;
    }

    /** @brief 支持移动，避免重复 restore / Support moves without restoring twice. */
    ScopedFsCredentials(ScopedFsCredentials&& other) noexcept
        : previous_uid_(other.previous_uid_),
          previous_gid_(other.previous_gid_),
          uid_changed_(std::exchange(other.uid_changed_, false)),
          gid_changed_(std::exchange(other.gid_changed_, false)) {}

    /** @brief 禁止复制，避免重复 restore / Copying is forbidden to avoid duplicate restore. */
    ScopedFsCredentials(const ScopedFsCredentials&) = delete;
    /** @brief 禁止复制赋值 / Copy assignment is forbidden. */
    ScopedFsCredentials& operator=(const ScopedFsCredentials&) = delete;

    /** @brief 析构时恢复 PID 1 原 fsuid/fsgid / Restore the original PID 1 fsuid/fsgid on destruction. */
    ~ScopedFsCredentials() { restore(); }

private:
    /** @brief 仅工厂建立实例 / Construct only through the factory. */
    ScopedFsCredentials() = default;

    /** @brief 恢复原文件系统凭据 / Restore original filesystem credentials. */
    void restore() noexcept {
        if (uid_changed_) {
            static_cast<void>(setfsuid(previous_uid_));
            uid_changed_ = false;
        }
        if (gid_changed_) {
            static_cast<void>(setfsgid(previous_gid_));
            gid_changed_ = false;
        }
    }

    /** @brief 原 filesystem UID / Original filesystem UID. */
    uid_t previous_uid_{};
    /** @brief 原 filesystem GID / Original filesystem GID. */
    gid_t previous_gid_{};
    /** @brief 是否已切换 fsuid / Whether fsuid was switched. */
    bool uid_changed_{false};
    /** @brief 是否已切换 fsgid / Whether fsgid was switched. */
    bool gid_changed_{false};
};

/**
 * @brief 用 openat2 打开一个不跟随链接的子目录 / Open a child directory without following links via openat2.
 * @param parent_fd 已验证父目录 FD / Verified parent-directory FD.
 * @param name 受限单路径分量 / Constrained single path component.
 * @return 子目录 FD 或错误 / Child-directory FD or an error.
 */
[[nodiscard]] Result<int> open_directory_beneath(const int parent_fd, const std::string_view name) {
    if (parent_fd < 0 || name.empty() || name == "." || name == ".." || name.find('/') != std::string_view::npos ||
        name.find('\0') != std::string_view::npos) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "invalid workspace directory component"));
    }
    std::string material(name);
    open_how how{};
    how.flags = static_cast<std::uint64_t>(O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
    how.resolve = static_cast<std::uint64_t>(
        RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS | RESOLVE_NO_XDEV);
    const int fd = static_cast<int>(syscall(SYS_openat2, parent_fd, material.c_str(), &how, sizeof(how)));
    if (fd < 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "openat2 workspace directory"));
    }
    struct stat metadata {};
    if (fstat(fd, &metadata) != 0 || !S_ISDIR(metadata.st_mode)) {
        const int saved_errno = errno;
        static_cast<void>(close(fd));
        errno = saved_errno;
        return std::unexpected(make_error(ErrorCode::io_failure, "workspace component is not a directory"));
    }
    return fd;
}

/**
 * @brief 在受控父目录下创建或安全打开私有目录 / Create or safely open a private directory below a controlled parent.
 * @param parent_fd 已验证父目录 FD / Verified parent-directory FD.
 * @param name 受限单路径分量 / Constrained single path component.
 * @param owner_uid 目录必须归属的 task UID / Task UID that must own the directory.
 * @param owner_gid 目录必须归属的 task GID / Task GID that must own the directory.
 * @return 已打开的 task-private 子目录 FD 或错误 / Opened task-private child-directory FD or an error.
 */
[[nodiscard]] Result<int> ensure_workspace_directory(
    const int parent_fd,
    const std::string_view name,
    const uid_t owner_uid,
    const gid_t owner_gid) {
    std::string material(name);
    if (mkdirat(parent_fd, material.c_str(), 0700) != 0 && errno != EEXIST) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "mkdirat workspace directory"));
    }
    const auto directory = open_directory_beneath(parent_fd, name);
    if (!directory) {
        return std::unexpected(directory.error());
    }
    struct stat metadata {};
    if (fstat(*directory, &metadata) != 0 || !S_ISDIR(metadata.st_mode) || metadata.st_uid != owner_uid ||
        metadata.st_gid != owner_gid) {
        static_cast<void>(close(*directory));
        return std::unexpected(make_error(
            ErrorCode::permission_denied,
            "workspace file-ingress directory is not task-owned private storage"));
    }
    if (fchmod(*directory, 0700) != 0) {
        const Error error = errno_error(ErrorCode::permission_denied, "protect workspace file-ingress directory");
        static_cast<void>(close(*directory));
        return std::unexpected(error);
    }
    return *directory;
}

/**
 * @brief 打开 PID 1 绑定的 workspace 根目录 / Open the workspace root bound to PID 1.
 * @param config supervisor 配置 / Supervisor configuration.
 * @return 可独立关闭的 workspace 目录 FD / Independently closable workspace-directory FD.
 */
[[nodiscard]] Result<int> open_workspace_root_for_payload(const SupervisorConfig& config) {
    int fd = -1;
    if (config.workspace_fd >= 0) {
        fd = fcntl(config.workspace_fd, F_DUPFD_CLOEXEC, 3);
        if (fd < 0) {
            return std::unexpected(errno_error(ErrorCode::io_failure, "duplicate workspace directory FD"));
        }
    } else {
        fd = open(config.test_workspace_root.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
        if (fd < 0) {
            return std::unexpected(errno_error(ErrorCode::io_failure, "open CTest workspace directory"));
        }
    }
    struct stat metadata {};
    if (fstat(fd, &metadata) != 0 || !S_ISDIR(metadata.st_mode)) {
        const int saved_errno = errno;
        static_cast<void>(close(fd));
        errno = saved_errno;
        return std::unexpected(make_error(ErrorCode::io_failure, "workspace FD is not a directory"));
    }
    return fd;
}

/**
 * @brief 得到不可预测的临时文件 basename / Produce an unpredictable temporary-file basename.
 * @return 安全 basename 或随机源错误 / Safe basename or random-source failure.
 */
[[nodiscard]] Result<std::string> make_payload_temporary_name() {
    std::array<unsigned char, 16U> random_bytes{};
    std::size_t offset = 0U;
    while (offset < random_bytes.size()) {
        const ssize_t count = getrandom(
            random_bytes.data() + static_cast<std::ptrdiff_t>(offset),
            random_bytes.size() - offset,
            0U);
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count <= 0) {
            return std::unexpected(errno_error(ErrorCode::io_failure, "getrandom file temporary name"));
        }
        offset += static_cast<std::size_t>(count);
    }
    constexpr std::string_view kDigits{"0123456789abcdef"};
    std::string name{".wsp-upload-"};
    name.reserve(name.size() + random_bytes.size() * 2U);
    for (const unsigned char byte : random_bytes) {
        name.push_back(kDigits[(byte >> 4U) & 0x0fU]);
        name.push_back(kDigits[byte & 0x0fU]);
    }
    return name;
}

/**
 * @brief 完整写入一个原始 bytes 分块 / Fully write one raw-byte chunk.
 * @param fd 目标 regular-file FD / Target regular-file FD.
 * @param bytes 待写入 bytes / Bytes to write.
 * @return 成功或 I/O 错误 / Success or I/O error.
 */
[[nodiscard]] Result<void> write_all_bytes(const int fd, const std::span<const std::byte> bytes) {
    std::size_t offset = 0U;
    while (offset < bytes.size()) {
        const ssize_t count = write(
            fd,
            bytes.data() + static_cast<std::ptrdiff_t>(offset),
            bytes.size() - offset);
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count <= 0) {
            return std::unexpected(errno_error(ErrorCode::io_failure, "write file chunk"));
        }
        offset += static_cast<std::size_t>(count);
    }
    return {};
}

/**
 * @brief 将 SHA-256 bytes 渲染为小写十六进制 / Render SHA-256 bytes as lowercase hexadecimal.
 * @param digest OpenSSL 返回的 digest bytes / Digest bytes returned by OpenSSL.
 * @param size digest byte count / Digest byte count.
 * @return 小写十六进制摘要 / Lowercase hexadecimal digest.
 */
[[nodiscard]] std::string render_sha256(const unsigned char* const digest, const unsigned int size) {
    constexpr std::string_view kDigits{"0123456789abcdef"};
    std::string rendered;
    rendered.reserve(static_cast<std::size_t>(size) * 2U);
    for (unsigned int index = 0U; index < size; ++index) {
        const unsigned char byte = digest[index];
        rendered.push_back(kDigits[(byte >> 4U) & 0x0fU]);
        rendered.push_back(kDigits[byte & 0x0fU]);
    }
    return rendered;
}

/**
 * @brief 以 renameat2 原子替换最终 payload 名称 / Atomically replace the final payload name with renameat2.
 * @param directory_fd 已验证 opaque uploads 目录 FD / Verified opaque uploads-directory FD.
 * @param temporary_name 已 seal 临时 basename / Sealed temporary basename.
 * @return 成功或 I/O 错误 / Success or I/O error.
 */
[[nodiscard]] Result<void> publish_payload_name(const int directory_fd, const std::string_view temporary_name) {
    std::string temporary(temporary_name);
    if (syscall(SYS_renameat2, directory_fd, temporary.c_str(), directory_fd, "payload", 0U) != 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "renameat2 publish file"));
    }
    return {};
}

/** @brief 设置 FD 为 nonblocking / Set an FD nonblocking. */
[[nodiscard]] Result<void> make_nonblocking(const int fd) {
    const int flags = fcntl(fd, F_GETFL);
    if (flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) != 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "make pipe nonblocking"));
    }
    return {};
}

/** @brief 单轮最多从一个输出 pipe 读取的字节数 / Maximum bytes read from one output pipe in one event-loop iteration. */
constexpr std::size_t kDrainBudgetBytes{64U * 1024U};

/** @brief stdout/stderr 共享的最终 UTF-8 输出预算 / Shared final UTF-8 output budget for stdout and stderr. */
struct OutputBudget final {
    /** @brief 尚可写入的规范化字节数 / Remaining normalized bytes writable. */
    std::size_t remaining{};
    /** @brief 一旦不能写入完整 code point，后续输出只 drain 不保留 / Once a full code point cannot fit, later output is drain-only. */
    bool exhausted{false};
};

/** @brief 流式 UTF-8 规范化器 / Streaming UTF-8 normalizer. */
class Utf8OutputNormalizer final {
public:
    /**
     * @brief 吸收 raw output 字节并追加规范 UTF-8 / Consume raw output bytes and append canonical UTF-8.
     * @param bytes 原始输出 / Raw output.
     * @param destination 对应 stdout/stderr 目的字符串 / Corresponding stdout/stderr destination.
     * @param budget 两路共享的最终输出预算 / Shared final output budget.
     * @param truncated 输出是否超过预算 / Whether output exceeded the budget.
     */
    void consume(
        const std::string_view bytes,
        std::string& destination,
        OutputBudget& budget,
        bool& truncated) {
        for (const unsigned char byte : bytes) {
            consume_byte(byte, destination, budget, truncated);
        }
    }

    /**
     * @brief 在 pipe EOF 时处理不完整 sequence / Process an incomplete sequence at pipe EOF.
     * @param destination 对应 stdout/stderr 目的字符串 / Corresponding stdout/stderr destination.
     * @param budget 两路共享的最终输出预算 / Shared final output budget.
     * @param truncated 输出是否超过预算 / Whether output exceeded the budget.
     */
    void finish(std::string& destination, OutputBudget& budget, bool& truncated) {
        if (pending_size_ != 0U) {
            append_questions(pending_size_, destination, budget, truncated);
            pending_size_ = 0U;
        }
    }

private:
    /** @brief 尚待验证的 UTF-8 sequence / UTF-8 sequence pending validation. */
    std::array<unsigned char, 4> pending_{};
    /** @brief pending sequence 当前长度 / Current pending-sequence length. */
    std::size_t pending_size_{};
    /** @brief pending sequence 所需长度 / Required pending-sequence length. */
    std::size_t expected_size_{};

    /** @brief 将一个完整 canonical sequence 写入预算 / Write one complete canonical sequence within budget. */
    static void append_sequence(
        const std::string_view sequence,
        std::string& destination,
        OutputBudget& budget,
        bool& truncated) {
        if (budget.exhausted || sequence.size() > budget.remaining) {
            budget.exhausted = true;
            truncated = true;
            return;
        }
        destination.append(sequence);
        budget.remaining -= sequence.size();
    }

    /** @brief 将指定数量的替换字符 '?' 写入预算 / Write a requested number of '?' replacement characters within budget. */
    static void append_questions(
        const std::size_t count,
        std::string& destination,
        OutputBudget& budget,
        bool& truncated) {
        append_sequence(std::string(count, '?'), destination, budget, truncated);
    }

    /** @brief 判定 pending sequence 的完整编码是否为标量值 / Check that a complete pending sequence encodes a scalar value. */
    [[nodiscard]] bool pending_is_valid() const noexcept {
        if (expected_size_ == 2U) {
            return true;
        }
        const unsigned char second = pending_[1];
        if (expected_size_ == 3U) {
            return !(pending_[0] == 0xe0U && second < 0xa0U) &&
                   !(pending_[0] == 0xedU && second > 0x9fU);
        }
        return !(pending_[0] == 0xf0U && second < 0x90U) &&
               !(pending_[0] == 0xf4U && second > 0x8fU);
    }

    /** @brief 将 pending sequence 作为有效或逐字节无效输出 / Emit pending sequence as valid or bytewise-invalid output. */
    void emit_pending(std::string& destination, OutputBudget& budget, bool& truncated) {
        if (pending_is_valid()) {
            std::string sequence;
            sequence.reserve(pending_size_);
            for (std::size_t index = 0; index < pending_size_; ++index) {
                sequence.push_back(static_cast<char>(pending_[index]));
            }
            append_sequence(sequence, destination, budget, truncated);
        } else {
            append_questions(pending_size_, destination, budget, truncated);
        }
        pending_size_ = 0U;
        expected_size_ = 0U;
    }

    /** @brief 吸收单个 raw byte / Consume one raw byte. */
    void consume_byte(
        const unsigned char byte,
        std::string& destination,
        OutputBudget& budget,
        bool& truncated) {
        if (budget.exhausted) {
            return;
        }
        if (pending_size_ == 0U) {
            if (byte == 0U) {
                append_questions(1U, destination, budget, truncated);
                return;
            }
            if (byte <= 0x7fU) {
                const char ascii = static_cast<char>(byte);
                append_sequence(std::string_view(&ascii, 1U), destination, budget, truncated);
                return;
            }
            if ((byte >= 0xc2U && byte <= 0xdfU) || (byte >= 0xe0U && byte <= 0xefU) ||
                (byte >= 0xf0U && byte <= 0xf4U)) {
                pending_[0] = byte;
                pending_size_ = 1U;
                expected_size_ = byte <= 0xdfU ? 2U : (byte <= 0xefU ? 3U : 4U);
                return;
            }
            append_questions(1U, destination, budget, truncated);
            return;
        }
        if (byte < 0x80U || byte > 0xbfU) {
            append_questions(pending_size_, destination, budget, truncated);
            pending_size_ = 0U;
            expected_size_ = 0U;
            consume_byte(byte, destination, budget, truncated);
            return;
        }
        pending_[pending_size_++] = byte;
        if (pending_size_ == expected_size_) {
            emit_pending(destination, budget, truncated);
        }
    }
};

/** @brief 用有界缓冲 drain 一个输出 FD / Drain one output FD into a bounded buffer. */
[[nodiscard]] Result<void> drain_output(
    int& fd,
    std::string& destination,
    Utf8OutputNormalizer& normalizer,
    OutputBudget& budget,
    bool& truncated) {
    std::array<char, 16U * 1024U> buffer{};
    std::size_t drain_budget = kDrainBudgetBytes;
    while (drain_budget > 0U) {
        const std::size_t chunk = std::min(buffer.size(), drain_budget);
        const ssize_t count = read(fd, buffer.data(), chunk);
        if (count > 0) {
            const std::size_t size = static_cast<std::size_t>(count);
            normalizer.consume(std::string_view(buffer.data(), size), destination, budget, truncated);
            drain_budget -= size;
            continue;
        }
        if (count == 0) {
            normalizer.finish(destination, budget, truncated);
            close_fd(fd);
            return {};
        }
        if (errno == EINTR) {
            continue;
        }
        if (errno == EAGAIN || errno == EWOULDBLOCK) {
            return {};
        }
        return std::unexpected(errno_error(ErrorCode::io_failure, "read task output"));
    }
    // A continuously writing child can keep a nonblocking pipe readable forever. Yielding after a
    // finite budget lets the outer loop observe deadline, cancellation, and cgroup-kill decisions.
    return {};
}

/** @brief 尝试写入剩余 stdin / Try to write remaining stdin. */
[[nodiscard]] Result<void> feed_stdin(int& fd, const std::string_view input, std::size_t& offset) {
    while (offset < input.size()) {
        const ssize_t count = write(fd, input.data() + static_cast<std::ptrdiff_t>(offset), input.size() - offset);
        if (count > 0) {
            offset += static_cast<std::size_t>(count);
            continue;
        }
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            return {};
        }
        if (count < 0 && errno == EPIPE) {
            close_fd(fd);
            return {};
        }
        return std::unexpected(errno_error(ErrorCode::io_failure, "write task stdin"));
    }
    close_fd(fd);
    return {};
}

/** @brief 向整个 task 进程组发送信号 / Send a signal to the whole task process group. */
void signal_process_group(const pid_t leader, const int signal) noexcept {
    if (leader > 0) {
        static_cast<void>(kill(-leader, signal));
    }
}

/** @brief 关闭除 stdin/stdout/stderr 外的所有 FD / Close every FD except stdin/stdout/stderr. */
void close_non_stdio_fds() noexcept {
#ifdef SYS_close_range
    if (syscall(SYS_close_range, 3U, UINT_MAX, 0U) == 0) {
        return;
    }
#endif
    struct rlimit limit {};
    if (getrlimit(RLIMIT_NOFILE, &limit) != 0) {
        limit.rlim_cur = 65'536U;
    }
    const rlim_t cap = std::min<rlim_t>(limit.rlim_cur, 1'048'576U);
    for (int fd = 3; static_cast<rlim_t>(fd) < cap; ++fd) {
        static_cast<void>(close(fd));
    }
}

/** @brief payload 可保留的最大文件描述符数 / Maximum number of file descriptors a payload may retain. */
constexpr rlim_t kTaskNofileLimit{256U};

/**
 * @brief 为 untrusted payload 收紧不可放宽的资源限制 / Tighten irreversible resource limits for an untrusted payload.
 * @return 两个限制均成功设置时为真 / True only when both limits were installed.
 * @note 不在此处设置 RLIMIT_FSIZE；workspace 的持久化存储预算必须与未来 XFS project quota 使用同一语义。
 *       RLIMIT_FSIZE is deliberately not set here: persistent-workspace storage must share semantics with the future XFS project quota.
 */
[[nodiscard]] bool install_task_rlimits() noexcept {
    /** @brief payload 的 soft/hard NOFILE 上限 / Payload soft/hard NOFILE limit. */
    const rlimit nofile{.rlim_cur = kTaskNofileLimit, .rlim_max = kTaskNofileLimit};
    /** @brief 禁止 core dump 的 soft/hard 限制 / Soft/hard limit disabling core dumps. */
    const rlimit core{.rlim_cur = 0U, .rlim_max = 0U};
    return setrlimit(RLIMIT_NOFILE, &nofile) == 0 && setrlimit(RLIMIT_CORE, &core) == 0;
}

/** @brief 向预打开的 cgroup 文件写一小段控制值 / Write a small control value to a preopened cgroup file. */
[[nodiscard]] Result<void> write_cgroup_control(const int fd, const std::string_view value) {
    if (fd < 0) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "missing mandatory task cgroup control FD"));
    }
    if (lseek(fd, 0, SEEK_SET) < 0) {
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed, "seek task cgroup control"));
    }
    std::size_t offset = 0;
    while (offset < value.size()) {
        const ssize_t count = write(fd, value.data() + static_cast<std::ptrdiff_t>(offset), value.size() - offset);
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count <= 0) {
            return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed, "write task cgroup control"));
        }
        offset += static_cast<std::size_t>(count);
    }
    return {};
}

/** @brief 判断 task cgroup.events 是否明确报告 populated 0 / Check whether task cgroup.events explicitly reports populated 0. */
[[nodiscard]] bool task_cgroup_is_empty(const std::string_view events) noexcept {
    constexpr std::string_view kPopulatedZero{"populated 0"};
    std::size_t offset = 0U;
    while (offset < events.size()) {
        const std::size_t end = events.find('\n', offset);
        const std::string_view line = events.substr(offset, end == std::string_view::npos ? events.size() - offset : end - offset);
        if (line == kPopulatedZero) {
            return true;
        }
        if (end == std::string_view::npos) {
            break;
        }
        offset = end + 1U;
    }
    return false;
}

/** @brief 等待 pre-opened task cgroup.events 证明空闲 / Wait for pre-opened task cgroup.events to prove emptiness. */
[[nodiscard]] Result<void> wait_task_cgroup_empty(const int events_fd, const std::chrono::milliseconds timeout) {
    if (events_fd < 0) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "missing mandatory task cgroup.events FD"));
    }
    const auto deadline = std::chrono::steady_clock::now() + timeout;
    std::array<char, 4096> buffer{};
    do {
        if (lseek(events_fd, 0, SEEK_SET) < 0) {
            return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed, "seek task cgroup.events"));
        }
        const ssize_t count = read(events_fd, buffer.data(), buffer.size());
        if (count < 0) {
            if (errno == EINTR) {
                continue;
            }
            return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed, "read task cgroup.events"));
        }
        if (count == 0) {
            return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "task cgroup.events returned no population state"));
        }
        if (task_cgroup_is_empty(std::string_view(buffer.data(), static_cast<std::size_t>(count)))) {
            return {};
        }
        const auto now = std::chrono::steady_clock::now();
        if (now >= deadline) {
            break;
        }
        const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(deadline - now);
        pollfd descriptor{.fd = events_fd, .events = POLLIN | POLLPRI | POLLHUP, .revents = 0};
        const int ready = poll(&descriptor, 1U, static_cast<int>(std::min<std::int64_t>(remaining.count(), 10)));
        if (ready < 0 && errno != EINTR) {
            return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed, "poll task cgroup.events"));
        }
    } while (std::chrono::steady_clock::now() < deadline);
    return std::unexpected(make_error(ErrorCode::child_failure, "task cgroup remained populated after cgroup.kill"));
}

/** @brief 杀死 task cgroup 并以 populated 0 建立 completion barrier / Kill task cgroup and establish a populated-0 completion barrier. */
[[nodiscard]] Result<void> kill_task_cgroup_and_wait(const SupervisorConfig& config) {
    if (const auto killed = write_cgroup_control(config.task_cgroup_kill_fd, "1"); !killed) {
        return std::unexpected(killed.error());
    }
    return wait_task_cgroup_empty(config.task_cgroup_events_fd, std::chrono::seconds(5));
}

/** @brief 在成功回复前持久化 workspace OverlayFS / Persist the workspace OverlayFS before replying success. */
[[nodiscard]] Result<void> sync_workspace(const int workspace_fd) {
    if (workspace_fd < 0) {
        // Direct unit construction has no runtime mount; wsp-systemd main always supplies this FD.
        return {};
    }
    if (syncfs(workspace_fd) != 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "syncfs workspace OverlayFS"));
    }
    return {};
}

/**
 * @brief 向 broker 发送文件阶段 ACK / Send a file phase acknowledgement to the broker.
 * @param fd supervisor control socket FD / Supervisor control-socket FD.
 * @param acknowledgement 已验证 ACK / Validated acknowledgement.
 * @return 成功或编码/传输错误 / Success or an encoding/transport error.
 */
[[nodiscard]] Result<void> send_payload_ack_frame(const int fd, const PayloadAck& acknowledgement) {
    const auto payload = encode_payload_ack(acknowledgement);
    if (!payload) {
        return std::unexpected(payload.error());
    }
    const auto frame = encode_frame(MessageKind::payload_ack, *payload);
    if (!frame) {
        return std::unexpected(frame.error());
    }
    return send_frame(fd, *frame);
}

/**
 * @brief 向 broker 发送文件收据 / Send a file receipt to the broker.
 * @param fd supervisor control socket FD / Supervisor control-socket FD.
 * @param result 已验证文件收据 / Validated file receipt.
 * @return 成功或编码/传输错误 / Success or an encoding/transport error.
 */
[[nodiscard]] Result<void> send_payload_result_frame(const int fd, const PayloadResult& result) {
    const auto payload = encode_payload_result(result);
    if (!payload) {
        return std::unexpected(payload.error());
    }
    const auto frame = encode_frame(MessageKind::payload_result, *payload);
    if (!frame) {
        return std::unexpected(frame.error());
    }
    return send_frame(fd, *frame);
}

/** @brief child 执行直接 argv / Child executes direct argv. */
[[noreturn]] void exec_task_child(
    const ExecuteRequest& request,
    const SupervisorConfig& config,
    const int stdin_fd,
    const int stdout_fd,
    const int stderr_fd,
    const int start_fd) {
    if (setpgid(0, 0) != 0) {
        _exit(126);
    }
    if (dup2(stdin_fd, STDIN_FILENO) < 0 || dup2(stdout_fd, STDOUT_FILENO) < 0 || dup2(stderr_fd, STDERR_FILENO) < 0) {
        _exit(126);
    }
    if (stdin_fd > STDERR_FILENO) {
        static_cast<void>(close(stdin_fd));
    }
    if (stdout_fd > STDERR_FILENO) {
        static_cast<void>(close(stdout_fd));
    }
    if (stderr_fd > STDERR_FILENO) {
        static_cast<void>(close(stderr_fd));
    }
    char start_byte = 0;
    ssize_t start_read = 0;
    do {
        start_read = read(start_fd, &start_byte, 1U);
    } while (start_read < 0 && errno == EINTR);
    static_cast<void>(close(start_fd));
    if (start_read != 1 || start_byte != 'R') {
        _exit(126);
    }
    // This closes broker control and cgroup control FDs before untrusted code executes.
    close_non_stdio_fds();
    if (!install_task_rlimits()) {
        _exit(126);
    }
    struct sigaction default_pipe_action {};
    default_pipe_action.sa_handler = SIG_DFL;
    sigemptyset(&default_pipe_action.sa_mask);
    default_pipe_action.sa_flags = 0;
    if (sigaction(SIGPIPE, &default_pipe_action, nullptr) != 0) {
        _exit(126);
    }
    const std::string_view effective_cwd =
        config.test_workspace_root != "/workspace" && request.cwd == "/workspace" ? std::string_view(config.test_workspace_root) : std::string_view(request.cwd);
    if (chdir(std::string(effective_cwd).c_str()) != 0) {
        dprintf(STDERR_FILENO, "wsp-systemd: chdir failed\n");
        _exit(126);
    }
    const auto hardened = harden_task(config.sandbox_uid, config.sandbox_gid);
    if (!hardened) {
        dprintf(STDERR_FILENO, "wsp-systemd: sandbox hardening failed\n");
        _exit(126);
    }
    clearenv();
    static_cast<void>(setenv("PATH", "/usr/bin:/bin", 1));
    std::vector<char*> arguments;
    arguments.reserve(request.argv.size() + 1U);
    for (const std::string& argument : request.argv) {
        arguments.push_back(const_cast<char*>(argument.c_str()));
    }
    arguments.push_back(nullptr);
    if (request.argv.front().find('/') != std::string::npos) {
        execv(arguments.front(), arguments.data());
    } else {
        execvp(arguments.front(), arguments.data());
    }
    dprintf(STDERR_FILENO, "wsp-systemd: exec failed\n");
    _exit(127);
}

}  // namespace

/** @brief PID 1 保留的单次文件写入暂存状态 / One file-ingress staging state retained by PID 1. */
struct Supervisor::ActivePayload final {
    /** @brief 当前 workspace 根目录的独立 FD / Independently owned FD for the current workspace root. */
    int workspace_fd{-1};
    /** @brief opaque uploads 目录的独立 FD / Independently owned FD for the opaque uploads directory. */
    int directory_fd{-1};
    /** @brief 尚未发布的临时 regular-file FD / Unpublished temporary regular-file FD. */
    int file_fd{-1};
    /** @brief 稳定文件写入调用 ID / Stable file-ingress invocation ID. */
    std::string request_id;
    /** @brief 文件 opaque directory capability / Opaque directory capability for the file. */
    std::string opaque_id;
    /** @brief 随机临时文件 basename / Random temporary-file basename. */
    std::string temporary_name;
    /** @brief 最终 runtime 内路径 / Final runtime-internal path. */
    std::string runtime_path;
    /** @brief 调用方声明的完整 bytes / Complete bytes declared by the caller. */
    std::size_t expected_bytes{};
    /** @brief 目前实际写入的 bytes / Bytes actually written so far. */
    std::size_t received_bytes{};
    /** @brief 调用方声明的内容 SHA-256 / Content SHA-256 declared by the caller. */
    std::string expected_sha256;
    /** @brief streaming SHA-256 上下文 / Streaming SHA-256 context. */
    EVP_MD_CTX* digest{nullptr};
    /** @brief 是否已经检查 bytes/hash 并 fdatasync / Whether bytes/hash were checked and fdatasync completed. */
    bool sealed{false};

    /** @brief 释放文件、目录与 OpenSSL 上下文 / Release file, directory, and OpenSSL context. */
    ~ActivePayload() {
        if (digest != nullptr) {
            EVP_MD_CTX_free(digest);
        }
        close_fd(file_fd);
        close_fd(directory_fd);
        close_fd(workspace_fd);
    }
};

Supervisor::Supervisor(SupervisorConfig config) : config_(config) {}

Supervisor::~Supervisor() {
    static_cast<void>(discard_active_payload());
}

Result<void> Supervisor::discard_active_payload() {
    if (!active_payload_) {
        return {};
    }
    std::unique_ptr<ActivePayload> payload = std::move(active_payload_);
    Result<void> cleanup{};
    if (!payload->temporary_name.empty() && payload->directory_fd >= 0) {
        const auto credentials = ScopedFsCredentials::enter(config_.sandbox_uid, config_.sandbox_gid);
        if (!credentials) {
            cleanup = std::unexpected(credentials.error());
        } else if (unlinkat(payload->directory_fd, payload->temporary_name.c_str(), 0) != 0 && errno != ENOENT) {
            cleanup = std::unexpected(errno_error(ErrorCode::io_failure, "unlink unpublished file temporary"));
        } else if (fsync(payload->directory_fd) != 0) {
            cleanup = std::unexpected(errno_error(ErrorCode::io_failure, "fsync file directory after abort"));
        }
    }
    return cleanup;
}

unsigned int Supervisor::reap_children() noexcept {
    unsigned int reaped = 0;
    int status = 0;
    for (;;) {
        const pid_t child = waitpid(-1, &status, WNOHANG);
        if (child > 0) {
            ++reaped;
            continue;
        }
        if (child < 0 && errno == EINTR) {
            continue;
        }
        break;
    }
    g_sigchld_pending = 0;
    return reaped;
}

Result<ExecutionResult> Supervisor::execute_once(const ExecuteRequest& request) {
    if (const auto valid = validate_execute_request(request); !valid) {
        return std::unexpected(valid.error());
    }
    if (const auto signals = install_sigchld_handler(); !signals) {
        return std::unexpected(signals.error());
    }
    const bool any_task_cgroup_fd = config_.task_cgroup_procs_fd >= 0 || config_.task_cgroup_kill_fd >= 0 ||
        config_.task_cgroup_events_fd >= 0;
    const bool all_task_cgroup_fds = config_.task_cgroup_procs_fd >= 0 && config_.task_cgroup_kill_fd >= 0 &&
        config_.task_cgroup_events_fd >= 0;
    if (any_task_cgroup_fd != all_task_cgroup_fds) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "incomplete task cgroup control configuration"));
    }
    const bool use_task_cgroup = all_task_cgroup_fds;
    if (use_task_cgroup) {
        if (const auto empty = wait_task_cgroup_empty(config_.task_cgroup_events_fd, std::chrono::seconds(5)); !empty) {
            return std::unexpected(empty.error());
        }
    }
    std::array<int, 2> stdin_pipe{-1, -1};
    std::array<int, 2> stdout_pipe{-1, -1};
    std::array<int, 2> stderr_pipe{-1, -1};
    std::array<int, 2> start_pipe{-1, -1};
    if (pipe2(stdin_pipe.data(), O_CLOEXEC) != 0 || pipe2(stdout_pipe.data(), O_CLOEXEC) != 0 ||
        pipe2(stderr_pipe.data(), O_CLOEXEC) != 0 || pipe2(start_pipe.data(), O_CLOEXEC) != 0) {
        for (int fd : stdin_pipe) {
            close_fd(fd);
        }
        for (int fd : stdout_pipe) {
            close_fd(fd);
        }
        for (int fd : stderr_pipe) {
            close_fd(fd);
        }
        for (int fd : start_pipe) {
            close_fd(fd);
        }
        return std::unexpected(errno_error(ErrorCode::io_failure, "create task pipes"));
    }
    const pid_t child = fork();
    if (child < 0) {
        for (int fd : stdin_pipe) {
            close_fd(fd);
        }
        for (int fd : stdout_pipe) {
            close_fd(fd);
        }
        for (int fd : stderr_pipe) {
            close_fd(fd);
        }
        for (int fd : start_pipe) {
            close_fd(fd);
        }
        return std::unexpected(errno_error(ErrorCode::child_failure, "fork task"));
    }
    if (child == 0) {
        close_fd(stdin_pipe[1]);
        close_fd(stdout_pipe[0]);
        close_fd(stderr_pipe[0]);
        close_fd(start_pipe[1]);
        exec_task_child(request, config_, stdin_pipe[0], stdout_pipe[1], stderr_pipe[1], start_pipe[0]);
    }
    close_fd(start_pipe[0]);
    if (setpgid(child, child) != 0 && getpgid(child) != child) {
        signal_process_group(child, SIGKILL);
        static_cast<void>(close(start_pipe[1]));
        static_cast<void>(waitpid(child, nullptr, 0));
        return std::unexpected(errno_error(ErrorCode::child_failure, "place task in process group"));
    }
    close_fd(stdin_pipe[0]);
    close_fd(stdout_pipe[1]);
    close_fd(stderr_pipe[1]);
    if (use_task_cgroup) {
        const auto placed = write_cgroup_control(config_.task_cgroup_procs_fd, std::to_string(child));
        if (!placed) {
            signal_process_group(child, SIGKILL);
            close_fd(start_pipe[1]);
            static_cast<void>(waitpid(child, nullptr, 0));
            close_fd(stdin_pipe[1]);
            close_fd(stdout_pipe[0]);
            close_fd(stderr_pipe[0]);
            return std::unexpected(placed.error());
        }
    }
    const char release = 'R';
    if (write(start_pipe[1], &release, 1U) != 1) {
        signal_process_group(child, SIGKILL);
        close_fd(start_pipe[1]);
        static_cast<void>(waitpid(child, nullptr, 0));
        close_fd(stdin_pipe[1]);
        close_fd(stdout_pipe[0]);
        close_fd(stderr_pipe[0]);
        return std::unexpected(errno_error(ErrorCode::child_failure, "release task after cgroup placement"));
    }
    close_fd(start_pipe[1]);
    if (const auto stdin_nonblocking = make_nonblocking(stdin_pipe[1]); !stdin_nonblocking) {
        signal_process_group(child, SIGKILL);
        static_cast<void>(waitpid(child, nullptr, 0));
        close_fd(stdin_pipe[1]);
        close_fd(stdout_pipe[0]);
        close_fd(stderr_pipe[0]);
        return std::unexpected(stdin_nonblocking.error());
    }
    if (const auto stdout_nonblocking = make_nonblocking(stdout_pipe[0]); !stdout_nonblocking) {
        signal_process_group(child, SIGKILL);
        static_cast<void>(waitpid(child, nullptr, 0));
        close_fd(stdin_pipe[1]);
        close_fd(stdout_pipe[0]);
        close_fd(stderr_pipe[0]);
        return std::unexpected(stdout_nonblocking.error());
    }
    if (const auto stderr_nonblocking = make_nonblocking(stderr_pipe[0]); !stderr_nonblocking) {
        signal_process_group(child, SIGKILL);
        static_cast<void>(waitpid(child, nullptr, 0));
        close_fd(stdin_pipe[1]);
        close_fd(stdout_pipe[0]);
        close_fd(stderr_pipe[0]);
        return std::unexpected(stderr_nonblocking.error());
    }

    ExecutionResult result{
        .request_id = request.request_id,
        .exit_code = std::nullopt,
        .timed_out = false,
        .truncated = false,
        .replayed = false,
        .stdout_data = {},
        .stderr_data = {},
    };
    std::size_t stdin_offset = 0;
    OutputBudget output_budget{.remaining = request.output_limit};
    Utf8OutputNormalizer stdout_normalizer;
    Utf8OutputNormalizer stderr_normalizer;
    int wait_status = 0;
    bool child_exited = false;
    bool termination_sent = false;
    bool kill_sent = false;
    const auto deadline = std::chrono::steady_clock::now() + request.timeout;
    auto termination_deadline = deadline;

    while (!child_exited || stdout_pipe[0] >= 0 || stderr_pipe[0] >= 0) {
        if (!child_exited) {
            const pid_t waited = waitpid(child, &wait_status, WNOHANG);
            if (waited == child) {
                child_exited = true;
                // A direct child exiting must not leave a background process holding workspace FDs.
                signal_process_group(child, SIGTERM);
                termination_sent = true;
                termination_deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(250);
            } else if (waited < 0 && errno != EINTR) {
                return std::unexpected(errno_error(ErrorCode::child_failure, "waitpid task"));
            }
        }
        const auto now = std::chrono::steady_clock::now();
        if (!termination_sent && now >= deadline) {
            result.timed_out = true;
            signal_process_group(child, SIGTERM);
            termination_sent = true;
            termination_deadline = now + std::chrono::milliseconds(250);
        }
        if (termination_sent && !kill_sent && now >= termination_deadline) {
            if (use_task_cgroup) {
                const auto killed = kill_task_cgroup_and_wait(config_);
                if (!killed) {
                    signal_process_group(child, SIGKILL);
                    return std::unexpected(killed.error());
                }
            } else {
                signal_process_group(child, SIGKILL);
            }
            kill_sent = true;
        }
        std::array<pollfd, 3> poll_fds{};
        nfds_t count = 0;
        if (stdin_pipe[1] >= 0) {
            poll_fds[count++] = pollfd{.fd = stdin_pipe[1], .events = POLLOUT, .revents = 0};
        }
        if (stdout_pipe[0] >= 0) {
            poll_fds[count++] = pollfd{.fd = stdout_pipe[0], .events = POLLIN | POLLHUP, .revents = 0};
        }
        if (stderr_pipe[0] >= 0) {
            poll_fds[count++] = pollfd{.fd = stderr_pipe[0], .events = POLLIN | POLLHUP, .revents = 0};
        }
        const int polled = poll(poll_fds.data(), count, 25);
        if (polled < 0 && errno != EINTR) {
            return std::unexpected(errno_error(ErrorCode::io_failure, "poll task pipes"));
        }
        if (stdin_pipe[1] >= 0) {
            if (const auto fed = feed_stdin(stdin_pipe[1], request.stdin_data, stdin_offset); !fed) {
                return std::unexpected(fed.error());
            }
        }
        if (stdout_pipe[0] >= 0) {
            if (const auto drained = drain_output(
                    stdout_pipe[0], result.stdout_data, stdout_normalizer, output_budget, result.truncated);
                !drained) {
                return std::unexpected(drained.error());
            }
        }
        if (stderr_pipe[0] >= 0) {
            if (const auto drained = drain_output(
                    stderr_pipe[0], result.stderr_data, stderr_normalizer, output_budget, result.truncated);
                !drained) {
                return std::unexpected(drained.error());
            }
        }
    }
    close_fd(stdin_pipe[1]);
    if (!child_exited) {
        for (;;) {
            const pid_t waited = waitpid(child, &wait_status, 0);
            if (waited == child) {
                break;
            }
            if (waited < 0 && errno != EINTR) {
                return std::unexpected(errno_error(ErrorCode::child_failure, "final waitpid task"));
            }
        }
    }
    // Even a task that exits normally may have double-forked descendants. The task leaf is the authority.
    if (use_task_cgroup && !kill_sent) {
        const auto killed = kill_task_cgroup_and_wait(config_);
        if (!killed) {
            return std::unexpected(killed.error());
        }
        kill_sent = true;
    }
    if (const auto synced = sync_workspace(config_.workspace_fd); !synced) {
        return std::unexpected(synced.error());
    }
    if (!result.timed_out && WIFEXITED(wait_status)) {
        result.exit_code = WEXITSTATUS(wait_status);
    } else if (!result.timed_out && WIFSIGNALED(wait_status)) {
        result.exit_code = 128 + WTERMSIG(wait_status);
    }
    static_cast<void>(reap_children());
    return result;
}

Result<PayloadAck> Supervisor::begin_payload(const PayloadBeginRequest& request) {
    if (const auto valid = validate_payload_begin_request(request); !valid) {
        return std::unexpected(valid.error());
    }
    if (active_payload_) {
        return std::unexpected(make_error(ErrorCode::busy, "a file ingress is already active"));
    }
    auto workspace_fd = open_workspace_root_for_payload(config_);
    if (!workspace_fd) {
        return std::unexpected(workspace_fd.error());
    }
    auto payload = std::make_unique<ActivePayload>();
    payload->workspace_fd = *workspace_fd;
    payload->request_id = request.request_id;
    payload->opaque_id = request.opaque_id;
    payload->runtime_path = "/workspace/uploads/" + request.opaque_id + "/payload";
    payload->expected_bytes = request.byte_size;
    payload->expected_sha256 = request.sha256;

    Result<void> preparation{};
    {
        const auto credentials = ScopedFsCredentials::enter(config_.sandbox_uid, config_.sandbox_gid);
        if (!credentials) {
            preparation = std::unexpected(credentials.error());
        } else {
            const auto uploads = ensure_workspace_directory(
                payload->workspace_fd,
                "uploads",
                config_.sandbox_uid,
                config_.sandbox_gid);
            if (!uploads) {
                preparation = std::unexpected(uploads.error());
            } else {
                int uploads_fd = *uploads;
                const auto opaque_directory = ensure_workspace_directory(
                    uploads_fd,
                    request.opaque_id,
                    config_.sandbox_uid,
                    config_.sandbox_gid);
                close_fd(uploads_fd);
                if (!opaque_directory) {
                    preparation = std::unexpected(opaque_directory.error());
                } else {
                    payload->directory_fd = *opaque_directory;
                    for (unsigned int attempt = 0U; attempt < 16U; ++attempt) {
                        const auto temporary_name = make_payload_temporary_name();
                        if (!temporary_name) {
                            preparation = std::unexpected(temporary_name.error());
                            break;
                        }
                        const int file_fd = openat(
                            payload->directory_fd,
                            temporary_name->c_str(),
                            O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW,
                            0600);
                        if (file_fd < 0 && errno == EEXIST) {
                            continue;
                        }
                        if (file_fd < 0) {
                            preparation = std::unexpected(errno_error(ErrorCode::io_failure, "create file temporary"));
                            break;
                        }
                        struct stat metadata {};
                        if (fstat(file_fd, &metadata) != 0 || !S_ISREG(metadata.st_mode) ||
                            metadata.st_uid != config_.sandbox_uid || metadata.st_gid != config_.sandbox_gid ||
                            fchmod(file_fd, 0600) != 0) {
                            const int saved_errno = errno;
                            static_cast<void>(close(file_fd));
                            errno = saved_errno;
                            preparation = std::unexpected(make_error(
                                ErrorCode::permission_denied,
                                "temporary file is not task-owned private regular storage"));
                            break;
                        }
                        payload->temporary_name = *temporary_name;
                        payload->file_fd = file_fd;
                        break;
                    }
                    if (preparation && payload->file_fd < 0) {
                        preparation = std::unexpected(make_error(
                            ErrorCode::io_failure,
                            "cannot allocate a collision-free file temporary"));
                    }
                }
            }
        }
    }
    if (!preparation) {
        active_payload_ = std::move(payload);
        static_cast<void>(discard_active_payload());
        return std::unexpected(preparation.error());
    }
    payload->digest = EVP_MD_CTX_new();
    if (payload->digest == nullptr || EVP_DigestInit_ex(payload->digest, EVP_sha256(), nullptr) != 1) {
        active_payload_ = std::move(payload);
        static_cast<void>(discard_active_payload());
        return std::unexpected(make_error(ErrorCode::internal, "initialize streaming SHA-256"));
    }
    active_payload_ = std::move(payload);
    return PayloadAck{
        .request_id = request.request_id,
        .stage = PayloadAckStage::begun,
        .received_bytes = 0U,
    };
}

Result<PayloadAck> Supervisor::append_payload(const PayloadChunk& chunk) {
    if (const auto valid = validate_payload_chunk(chunk); !valid) {
        return std::unexpected(valid.error());
    }
    if (!active_payload_ || active_payload_->request_id != chunk.request_id) {
        return std::unexpected(make_error(ErrorCode::protocol_violation, "file chunk has no matching active ingress"));
    }
    if (active_payload_->sealed) {
        return std::unexpected(make_error(ErrorCode::protocol_violation, "file chunk arrived after seal"));
    }
    if (active_payload_->received_bytes > active_payload_->expected_bytes ||
        chunk.bytes.size() > active_payload_->expected_bytes - active_payload_->received_bytes) {
        const Error error = make_error(ErrorCode::invalid_argument, "file chunks exceed declared byte size");
        static_cast<void>(discard_active_payload());
        return std::unexpected(error);
    }
    Result<void> appended{};
    {
        const auto credentials = ScopedFsCredentials::enter(config_.sandbox_uid, config_.sandbox_gid);
        if (!credentials) {
            appended = std::unexpected(credentials.error());
        } else if (const auto written = write_all_bytes(active_payload_->file_fd, chunk.bytes); !written) {
            appended = std::unexpected(written.error());
        } else if (EVP_DigestUpdate(
                       active_payload_->digest,
                       chunk.bytes.data(),
                       chunk.bytes.size()) != 1) {
            appended = std::unexpected(make_error(ErrorCode::internal, "update streaming SHA-256"));
        }
    }
    if (!appended) {
        const Error error = appended.error();
        static_cast<void>(discard_active_payload());
        return std::unexpected(error);
    }
    active_payload_->received_bytes += chunk.bytes.size();
    return PayloadAck{
        .request_id = chunk.request_id,
        .stage = PayloadAckStage::chunk_written,
        .received_bytes = active_payload_->received_bytes,
    };
}

Result<PayloadAck> Supervisor::seal_payload(const PayloadControlRequest& request) {
    if (const auto valid = validate_payload_control_request(request); !valid) {
        return std::unexpected(valid.error());
    }
    if (!active_payload_ || active_payload_->request_id != request.request_id) {
        return std::unexpected(make_error(ErrorCode::protocol_violation, "file seal has no matching active ingress"));
    }
    if (active_payload_->sealed) {
        return std::unexpected(make_error(ErrorCode::protocol_violation, "file ingress was sealed twice"));
    }
    if (active_payload_->received_bytes != active_payload_->expected_bytes) {
        const Error error = make_error(ErrorCode::invalid_argument, "file bytes do not match declared size");
        static_cast<void>(discard_active_payload());
        return std::unexpected(error);
    }
    std::array<unsigned char, EVP_MAX_MD_SIZE> digest{};
    unsigned int digest_size = 0U;
    if (EVP_DigestFinal_ex(active_payload_->digest, digest.data(), &digest_size) != 1 ||
        digest_size != 32U) {
        const Error error = make_error(ErrorCode::internal, "finalize streaming SHA-256");
        static_cast<void>(discard_active_payload());
        return std::unexpected(error);
    }
    const std::string actual_sha256 = render_sha256(digest.data(), digest_size);
    if (actual_sha256.size() != active_payload_->expected_sha256.size() ||
        CRYPTO_memcmp(
            actual_sha256.data(),
            active_payload_->expected_sha256.data(),
            actual_sha256.size()) != 0) {
        const Error error = make_error(ErrorCode::invalid_argument, "file content SHA-256 does not match declared digest");
        static_cast<void>(discard_active_payload());
        return std::unexpected(error);
    }
    if (fdatasync(active_payload_->file_fd) != 0) {
        const Error error = errno_error(ErrorCode::io_failure, "fdatasync sealed file temporary");
        static_cast<void>(discard_active_payload());
        return std::unexpected(error);
    }
    active_payload_->sealed = true;
    return PayloadAck{
        .request_id = request.request_id,
        .stage = PayloadAckStage::sealed,
        .received_bytes = active_payload_->received_bytes,
    };
}

Result<PayloadResult> Supervisor::publish_payload(const PayloadControlRequest& request) {
    if (const auto valid = validate_payload_control_request(request); !valid) {
        return std::unexpected(valid.error());
    }
    if (!active_payload_ || active_payload_->request_id != request.request_id || !active_payload_->sealed) {
        return std::unexpected(make_error(ErrorCode::protocol_violation, "file publish requires a matching sealed ingress"));
    }
    Result<void> published{};
    {
        const auto credentials = ScopedFsCredentials::enter(config_.sandbox_uid, config_.sandbox_gid);
        if (!credentials) {
            published = std::unexpected(credentials.error());
        } else if (const auto renamed = publish_payload_name(
                       active_payload_->directory_fd,
                       active_payload_->temporary_name);
                   !renamed) {
            published = std::unexpected(renamed.error());
        } else {
            active_payload_->temporary_name.clear();
            if (fsync(active_payload_->directory_fd) != 0) {
                published = std::unexpected(errno_error(ErrorCode::invocation_in_doubt, "fsync file directory after publish"));
            } else if (const auto synced = sync_workspace(active_payload_->workspace_fd); !synced) {
                published = std::unexpected(make_error(
                    ErrorCode::invocation_in_doubt,
                    "syncfs workspace after file publish failed: " + synced.error().message));
            }
        }
    }
    if (!published) {
        const Error error = published.error();
        if (!active_payload_->temporary_name.empty()) {
            if (const auto discarded = discard_active_payload(); !discarded) {
                return std::unexpected(make_error(
                    ErrorCode::invocation_in_doubt,
                    "file publish failed and staging cleanup could not be proven: " + discarded.error().message));
            }
        } else {
            active_payload_.reset();
        }
        return std::unexpected(error);
    }
    PayloadResult result{
        .request_id = active_payload_->request_id,
        .replayed = false,
        .path = active_payload_->runtime_path,
        .byte_size = active_payload_->received_bytes,
        .sha256 = active_payload_->expected_sha256,
    };
    active_payload_.reset();
    return result;
}

Result<PayloadAck> Supervisor::abort_payload(const PayloadControlRequest& request) {
    if (const auto valid = validate_payload_control_request(request); !valid) {
        return std::unexpected(valid.error());
    }
    if (!active_payload_ || active_payload_->request_id != request.request_id) {
        return std::unexpected(make_error(ErrorCode::protocol_violation, "file abort has no matching active ingress"));
    }
    const std::size_t received_bytes = active_payload_->received_bytes;
    if (const auto discarded = discard_active_payload(); !discarded) {
        return std::unexpected(make_error(
            ErrorCode::invocation_in_doubt,
            "cannot prove unpublished file temporary was removed: " + discarded.error().message));
    }
    return PayloadAck{
        .request_id = request.request_id,
        .stage = PayloadAckStage::aborted,
        .received_bytes = received_bytes,
    };
}

Result<void> Supervisor::serve() {
    if (config_.control_fd < 0) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "supervisor control_fd is invalid"));
    }
    if (const auto installed = install_sigchld_handler(); !installed) {
        return std::unexpected(installed.error());
    }
    for (;;) {
        if (g_sigchld_pending != 0) {
            static_cast<void>(reap_children());
        }
        pollfd control_poll{.fd = config_.control_fd, .events = POLLIN | POLLHUP, .revents = 0};
        const int control_ready = poll(&control_poll, 1U, 250);
        if (control_ready < 0) {
            if (errno == EINTR) {
                continue;
            }
            return std::unexpected(errno_error(ErrorCode::io_failure, "poll supervisor control socket"));
        }
        if (control_ready == 0) {
            continue;
        }
        const auto wire = receive_frame(config_.control_fd);
        if (!wire) {
            return std::unexpected(wire.error());
        }
        const auto frame = decode_frame(*wire);
        if (!frame) {
            const auto payload = encode_error(frame.error());
            if (payload) {
                const auto outbound = encode_frame(MessageKind::error, *payload);
                if (outbound) {
                    static_cast<void>(send_frame(config_.control_fd, *outbound));
                }
            }
            continue;
        }
        if (frame->kind == MessageKind::shutdown) {
            if (const auto discarded = discard_active_payload(); !discarded) {
                return std::unexpected(discarded.error());
            }
            return {};
        }
        if (frame->kind == MessageKind::payload_begin) {
            const auto request = decode_payload_begin_request(frame->payload);
            if (!request) {
                const auto payload = encode_error(request.error());
                if (payload) {
                    const auto outbound = encode_frame(MessageKind::error, *payload);
                    if (outbound) {
                        static_cast<void>(send_frame(config_.control_fd, *outbound));
                    }
                }
                continue;
            }
            const auto acknowledgement = begin_payload(*request);
            if (!acknowledgement) {
                const auto payload = encode_error(acknowledgement.error());
                if (payload) {
                    const auto outbound = encode_frame(MessageKind::error, *payload);
                    if (outbound) {
                        static_cast<void>(send_frame(config_.control_fd, *outbound));
                    }
                }
                continue;
            }
            if (const auto sent = send_payload_ack_frame(config_.control_fd, *acknowledgement); !sent) {
                return std::unexpected(sent.error());
            }
            continue;
        }
        if (frame->kind == MessageKind::payload_chunk) {
            const auto chunk = decode_payload_chunk(frame->payload);
            if (!chunk) {
                const auto payload = encode_error(chunk.error());
                if (payload) {
                    const auto outbound = encode_frame(MessageKind::error, *payload);
                    if (outbound) {
                        static_cast<void>(send_frame(config_.control_fd, *outbound));
                    }
                }
                continue;
            }
            const auto acknowledgement = append_payload(*chunk);
            if (!acknowledgement) {
                const auto payload = encode_error(acknowledgement.error());
                if (payload) {
                    const auto outbound = encode_frame(MessageKind::error, *payload);
                    if (outbound) {
                        static_cast<void>(send_frame(config_.control_fd, *outbound));
                    }
                }
                continue;
            }
            if (const auto sent = send_payload_ack_frame(config_.control_fd, *acknowledgement); !sent) {
                return std::unexpected(sent.error());
            }
            continue;
        }
        if (frame->kind == MessageKind::payload_seal || frame->kind == MessageKind::payload_publish ||
            frame->kind == MessageKind::payload_abort) {
            const auto request = decode_payload_control_request(frame->payload);
            if (!request) {
                const auto payload = encode_error(request.error());
                if (payload) {
                    const auto outbound = encode_frame(MessageKind::error, *payload);
                    if (outbound) {
                        static_cast<void>(send_frame(config_.control_fd, *outbound));
                    }
                }
                continue;
            }
            if (frame->kind == MessageKind::payload_publish) {
                const auto result = publish_payload(*request);
                if (!result) {
                    const auto payload = encode_error(result.error());
                    if (payload) {
                        const auto outbound = encode_frame(MessageKind::error, *payload);
                        if (outbound) {
                            static_cast<void>(send_frame(config_.control_fd, *outbound));
                        }
                    }
                    continue;
                }
                if (const auto sent = send_payload_result_frame(config_.control_fd, *result); !sent) {
                    return std::unexpected(sent.error());
                }
                continue;
            }
            const auto acknowledgement = frame->kind == MessageKind::payload_seal
                ? seal_payload(*request)
                : abort_payload(*request);
            if (!acknowledgement) {
                const auto payload = encode_error(acknowledgement.error());
                if (payload) {
                    const auto outbound = encode_frame(MessageKind::error, *payload);
                    if (outbound) {
                        static_cast<void>(send_frame(config_.control_fd, *outbound));
                    }
                }
                continue;
            }
            if (const auto sent = send_payload_ack_frame(config_.control_fd, *acknowledgement); !sent) {
                return std::unexpected(sent.error());
            }
            continue;
        }
        if (frame->kind != MessageKind::execute) {
            const Error error = make_error(ErrorCode::protocol_violation, "supervisor accepts execute, file ingress, or shutdown only");
            const auto payload = encode_error(error);
            if (payload) {
                const auto outbound = encode_frame(MessageKind::error, *payload);
                if (outbound) {
                    static_cast<void>(send_frame(config_.control_fd, *outbound));
                }
            }
            continue;
        }
        const auto request = decode_execute_request(frame->payload);
        if (!request) {
            const auto payload = encode_error(request.error());
            if (payload) {
                const auto outbound = encode_frame(MessageKind::error, *payload);
                if (outbound) {
                    static_cast<void>(send_frame(config_.control_fd, *outbound));
                }
            }
            continue;
        }
        const auto result = execute_once(*request);
        if (!result) {
            const auto payload = encode_error(result.error());
            if (payload) {
                const auto outbound = encode_frame(MessageKind::error, *payload);
                if (outbound) {
                    static_cast<void>(send_frame(config_.control_fd, *outbound));
                }
            }
            continue;
        }
        const auto payload = encode_execution_result(*result);
        if (!payload) {
            return std::unexpected(payload.error());
        }
        const auto outbound = encode_frame(MessageKind::result, *payload);
        if (!outbound) {
            return std::unexpected(outbound.error());
        }
        if (const auto sent = send_frame(config_.control_fd, *outbound); !sent) {
            return std::unexpected(sent.error());
        }
    }
}

}  // namespace wspctl
