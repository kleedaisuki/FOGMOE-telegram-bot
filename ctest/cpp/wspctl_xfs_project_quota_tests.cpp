/**
 * @file wspctl_xfs_project_quota_tests.cpp
 * @brief XFS project quota 的显式特权集成测试 / Explicit privileged integration test for XFS
 * project quota.
 *
 * 本测试只在 operator/CI 明确提供一次性 dedicated XFS mount 与 private parent directory 时运行。
 * 它会在该 parent 下用 ``mkdtemp`` 创建唯一状态根，并且只删除这个自己创建的精确目录；没有环境变量
 * 时返回 CTest skip code 77。 This test runs only when an operator/CI explicitly supplies a
 * disposable dedicated XFS mount and private parent directory. It creates one unique state root
 * below that parent with ``mkdtemp`` and deletes only that exact self-created directory; absent
 * the environment it returns CTest skip code 77.
 */

#include "wspctl/infrastructure/xfs_project_quota.hpp"

#include <linux/fs.h>

#include <sys/capability.h>
#include <sys/file.h>
#include <sys/ioctl.h>
#include <sys/poll.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <sys/wait.h>

#include <array>
#include <cerrno>
#include <csignal>
#include <cstddef>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <limits>
#include <optional>
#include <string>
#include <string_view>

#include <fcntl.h>
#include <unistd.h>

namespace {

/** @brief CTest 的约定 skip 返回码 / Conventional CTest skip return code. */
constexpr int kSkipReturnCode{77};
/** @brief 强制 XFS quota CI 的环境变量 / Environment variable requiring XFS quota CI. */
constexpr std::string_view kRequireEnvironment{"WSPCTL_REQUIRE_XFS_QUOTA_TESTS"};
/** @brief XFS quota mount 的环境变量 / Environment variable for the XFS quota mount. */
constexpr std::string_view kMountEnvironment{"WSPCTL_XFS_QUOTA_TEST_MOUNT"};
/** @brief disposable state-root parent 的环境变量 / Environment variable for the disposable
 * state-root parent. */
constexpr std::string_view kParentEnvironment{"WSPCTL_XFS_QUOTA_TEST_PARENT"};
/** @brief test control tree 的 byte hard limit / Byte hard limit for the test control tree. */
constexpr std::uint64_t kControlHardBytes{1U * 1024U * 1024U};
/** @brief test control tree 的 inode hard limit / Inode hard limit for the test control tree. */
constexpr std::uint64_t kControlHardInodes{256U};
/** @brief test workspace tree 的 byte hard limit / Byte hard limit for the test workspace tree. */
constexpr std::uint64_t kWorkspaceHardBytes{8U * 1024U * 1024U};
/** @brief test workspace tree 的 inode hard limit / Inode hard limit for the test workspace tree.
 */
constexpr std::uint64_t kWorkspaceHardInodes{64U};
/** @brief XFS quota basic block size / XFS quota basic-block size. */
constexpr std::uint64_t kQuotaBlockBytes{512U};
/** @brief 模拟生产具名 Agent 的非 root UID / Non-root UID simulating the production named Agent. */
constexpr uid_t kAgentUid{65'533U};
/** @brief 模拟生产具名 Agent 的非 root GID / Non-root GID simulating the production named Agent. */
constexpr gid_t kAgentGid{65'533U};
/** @brief 用于拒绝测试的非 allowlist UID / Non-allowlisted UID used by the rejection test. */
constexpr uid_t kUnexpectedUid{65'532U};
/** @brief 用于拒绝测试的非 allowlist GID / Non-allowlisted GID used by the rejection test. */
constexpr gid_t kUnexpectedGid{65'532U};

/**
 * @brief 特权测试是否被 CI 强制 / Whether privileged test execution is required by CI.
 * @return 环境值为 ``1`` 时为真 / True when the environment is ``1``.
 */
[[nodiscard]] bool quota_test_is_required() {
    /** @brief raw environment value / 原始环境变量值。 */
    const char* const raw_value = std::getenv(kRequireEnvironment.data());
    return raw_value != nullptr && std::string_view(raw_value) == "1";
}

/**
 * @brief 报告不可用的 privileged prerequisite / Report an unavailable privileged prerequisite.
 * @param reason 不可用原因 / Reason the prerequisite is unavailable.
 * @return optional 时为 77，required 时为失败 / 77 when optional, failure when required.
 */
[[nodiscard]] int unavailable(const std::string_view reason) {
    if (quota_test_is_required()) {
        std::cerr << "FAIL: " << kRequireEnvironment
                  << "=1 but XFS quota test cannot run: " << reason << '\n';
        return EXIT_FAILURE;
    }
    std::cerr << "SKIP: XFS quota test unavailable: " << reason << '\n';
    return kSkipReturnCode;
}

/**
 * @brief 读取一个非空环境路径 / Read one nonempty environment path.
 * @param name 环境变量名 / Environment-variable name.
 * @return 路径文本或空值 / Path text or no value.
 */
[[nodiscard]] std::optional<std::filesystem::path> environment_path(const std::string_view name) {
    /** @brief raw environment value / 原始环境变量值。 */
    const char* const value = std::getenv(name.data());
    if (value == nullptr || std::string_view(value).empty()) {
        return std::nullopt;
    }
    return std::filesystem::path(value);
}

/**
 * @brief 规范化并验证 private test parent / Canonicalize and validate the private test parent.
 * @param configured_parent operator 指定的 parent / Operator-specified parent.
 * @return canonical parent 或错误文本 / Canonical parent or error text.
 */
[[nodiscard]] wspctl::Result<std::filesystem::path>
canonical_private_parent(const std::filesystem::path& configured_parent) {
    if (!configured_parent.is_absolute()) {
        return std::unexpected(wspctl::make_error(wspctl::ErrorCode::invalid_argument,
                                                  "XFS quota test parent must be absolute"));
    }
    /** @brief canonicalization error / 规范化错误。 */
    std::error_code error;
    const std::filesystem::path parent = std::filesystem::canonical(configured_parent, error);
    if (error || parent == parent.root_path()) {
        return std::unexpected(
            wspctl::make_error(wspctl::ErrorCode::invalid_argument,
                               "XFS quota test parent is not a usable non-root directory"));
    }
    /** @brief parent metadata / 父目录元数据。 */
    struct stat metadata {};
    if (lstat(parent.c_str(), &metadata) != 0 || !S_ISDIR(metadata.st_mode) ||
        metadata.st_uid != 0U || metadata.st_gid != 0U ||
        (metadata.st_mode & (S_IWGRP | S_IWOTH)) != 0) {
        return std::unexpected(
            wspctl::make_error(wspctl::ErrorCode::permission_denied,
                               "XFS quota test parent must be a private root:root directory"));
    }
    return parent;
}

/**
 * @brief 创建仅测试可删除的唯一状态根 / Create a unique state root that only this test may delete.
 * @param parent 已验证 test parent / Verified test parent.
 * @return created root 或错误 / Created root or an error.
 */
[[nodiscard]] wspctl::Result<std::filesystem::path>
create_test_state_root(const std::filesystem::path& parent) {
    /** @brief mkdtemp 模板 / Template consumed by mkdtemp. */
    std::string template_path = (parent / "wspctl-xfs-quota-XXXXXX").string();
    template_path.push_back('\0');
    /** @brief created path buffer / 创建路径缓冲。 */
    char* const created = mkdtemp(template_path.data());
    if (created == nullptr) {
        return std::unexpected(
            wspctl::errno_error(wspctl::ErrorCode::io_failure, "mkdtemp XFS quota state root"));
    }
    const std::filesystem::path state_root(created);
    if (chmod(state_root.c_str(), 0700) != 0) {
        return std::unexpected(
            wspctl::errno_error(wspctl::ErrorCode::io_failure, "chmod XFS quota test state root"));
    }
    return state_root;
}

/**
 * @brief 删除唯一的 self-created test state root / Remove the unique self-created test state root.
 * @param parent 已验证 test parent / Verified test parent.
 * @param state_root mkdtemp 生成的精确 child / Exact child generated by mkdtemp.
 * @return 成功或安全/IO 错误 / Success or a safety/I/O error.
 */
[[nodiscard]] wspctl::Result<void> remove_test_state_root(const std::filesystem::path& parent,
                                                          const std::filesystem::path& state_root) {
    if (state_root.parent_path() != parent ||
        !state_root.filename().string().starts_with("wspctl-xfs-quota-")) {
        return std::unexpected(
            wspctl::make_error(wspctl::ErrorCode::invalid_argument,
                               "refusing to delete a non-test XFS quota state root"));
    }
    /** @brief deletion error / 删除错误。 */
    std::error_code error;
    const std::uintmax_t removed = std::filesystem::remove_all(state_root, error);
    static_cast<void>(removed);
    if (error) {
        return std::unexpected(wspctl::make_error(
            wspctl::ErrorCode::io_failure,
            "remove self-created XFS quota test state root: " + error.message()));
    }
    return {};
}

/**
 * @brief child 中清除 CAP_SYS_RESOURCE / Clear CAP_SYS_RESOURCE in the child process.
 * @return 成功时 true / True on success.
 * @note root 可绕开部分 quota 语义；本测试仅清除该 capability，保留读写 private test tree
 *       所需的其余 credential。 Root can bypass some quota semantics; this test clears only
 *       that capability while retaining the other credentials needed to access the private test
 * tree.
 */
[[nodiscard]] bool drop_quota_bypass_capability() {
    /** @brief current capability set / 当前 capability 集。 */
    cap_t capabilities = cap_get_proc();
    if (capabilities == nullptr) {
        return false;
    }
    /** @brief 要清除的 capability / Capability to clear. */
    const cap_value_t capability = CAP_SYS_RESOURCE;
    const int effective = cap_set_flag(capabilities, CAP_EFFECTIVE, 1, &capability, CAP_CLEAR);
    const int permitted = cap_set_flag(capabilities, CAP_PERMITTED, 1, &capability, CAP_CLEAR);
    const int applied = (effective == 0 && permitted == 0) ? cap_set_proc(capabilities) : -1;
    cap_free(capabilities);
    return applied == 0;
}

/**
 * @brief 在无 quota bypass capability 的 child 中验证 inode hard limit / Verify the inode hard
 * limit in a child without quota bypass capability.
 * @param upper_dir project-inheriting upper directory / 带 project inheritance 的 upper 目录。
 * @return child 成功时为 0，否则为非零 / 0 when the child succeeds, nonzero otherwise.
 */
[[nodiscard]] int verify_inode_hard_limit_child(const std::filesystem::path& upper_dir) {
    if (!drop_quota_bypass_capability()) {
        return 10;
    }
    /** @brief 是否观察到 kernel EDQUOT / Whether kernel EDQUOT was observed. */
    bool reached_limit{false};
    for (std::uint32_t index{0U}; index < 256U; ++index) {
        const std::filesystem::path file = upper_dir / ("inode-" + std::to_string(index));
        const int descriptor = open(file.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
        if (descriptor >= 0) {
            static_cast<void>(close(descriptor));
            continue;
        }
        if (errno == EDQUOT) {
            reached_limit = true;
            break;
        }
        return 11;
    }
    for (std::uint32_t index{0U}; index < 256U; ++index) {
        const std::filesystem::path file = upper_dir / ("inode-" + std::to_string(index));
        if (unlink(file.c_str()) != 0 && errno != ENOENT) {
            return 12;
        }
    }
    return reached_limit ? EXIT_SUCCESS : 13;
}

/**
 * @brief 在无 quota bypass capability 的 child 中验证 byte hard limit / Verify the byte hard limit
 * in a child without quota bypass capability.
 * @param upper_dir project-inheriting upper directory / 带 project inheritance 的 upper 目录。
 * @return child 成功时为 0，否则为非零 / 0 when the child succeeds, nonzero otherwise.
 */
[[nodiscard]] int verify_byte_hard_limit_child(const std::filesystem::path& upper_dir) {
    if (!drop_quota_bypass_capability()) {
        return 20;
    }
    /** @brief quota test allocation path / 配额测试分配路径。 */
    const std::filesystem::path file = upper_dir / "bytes-over-limit";
    const int descriptor = open(file.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
    if (descriptor < 0) {
        return 21;
    }
    const std::uint64_t requested_bytes = kWorkspaceHardBytes + kQuotaBlockBytes;
    if (requested_bytes > static_cast<std::uint64_t>(std::numeric_limits<off_t>::max())) {
        static_cast<void>(close(descriptor));
        return 22;
    }
    /** @brief posix_fallocate 返回的 errno-style status / errno-style status returned by
     * posix_fallocate. */
    const int allocation = posix_fallocate(descriptor, 0, static_cast<off_t>(requested_bytes));
    static_cast<void>(close(descriptor));
    const int unlink_status = unlink(file.c_str());
    if (allocation != EDQUOT) {
        return 23;
    }
    return unlink_status == 0 ? EXIT_SUCCESS : 24;
}

/**
 * @brief fork 并执行一项 quota boundary probe / Fork and run one quota-boundary probe.
 * @param upper_dir project-inheriting upper directory / 带 project inheritance 的 upper 目录。
 * @param probe child probe function / Child probe function.
 * @param name probe 的诊断名称 / Diagnostic probe name.
 * @return 成功或断言错误 / Success or an assertion error.
 */
[[nodiscard]] wspctl::Result<void> run_boundary_probe(const std::filesystem::path& upper_dir,
                                                      int (*probe)(const std::filesystem::path&),
                                                      const std::string_view name) {
    const pid_t child = fork();
    if (child < 0) {
        return std::unexpected(
            wspctl::errno_error(wspctl::ErrorCode::child_failure, "fork XFS quota boundary probe"));
    }
    if (child == 0) {
        _exit(probe(upper_dir));
    }
    /** @brief child wait status / 子进程等待状态。 */
    int status{0};
    pid_t waited = -1;
    do {
        waited = waitpid(child, &status, 0);
    } while (waited < 0 && errno == EINTR);
    if (waited != child) {
        return std::unexpected(
            wspctl::errno_error(wspctl::ErrorCode::child_failure, "wait XFS quota boundary probe"));
    }
    if (!WIFEXITED(status) || WEXITSTATUS(status) != EXIT_SUCCESS) {
        return std::unexpected(
            wspctl::make_error(wspctl::ErrorCode::child_failure,
                               std::string(name) + " did not observe kernel EDQUOT"));
    }
    return {};
}

/**
 * @brief 读取一个已存在私有目录的直接 child 数 / Count direct children of one existing private
 * directory.
 * @param directory 待枚举目录 / Directory to enumerate.
 * @param purpose 诊断语义 / Diagnostic purpose.
 * @return 直接 child 数，或枚举错误 / Direct-child count, or an enumeration error.
 * @note 该 helper 只读取目录项，用于证明只读 quota lookup 没有偷偷 provision 新状态。
 *       This helper only reads directory entries and proves a read-only quota lookup did not
 *       silently provision new state.
 */
[[nodiscard]] wspctl::Result<std::size_t> direct_entry_count(const std::filesystem::path& directory,
                                                             const std::string_view purpose) {
    /** @brief directory-iterator construction error / Directory-iterator construction error. */
    std::error_code error;
    std::filesystem::directory_iterator iterator(directory,
                                                 std::filesystem::directory_options::none, error);
    if (error) {
        return std::unexpected(wspctl::make_error(wspctl::ErrorCode::io_failure,
                                                  std::string("enumerate ") + std::string(purpose) +
                                                      ": " + error.message()));
    }
    /** @brief observed direct-child count / Observed direct-child count. */
    std::size_t count{0U};
    for (const std::filesystem::directory_entry& entry : iterator) {
        static_cast<void>(entry);
        ++count;
    }
    return count;
}

/**
 * @brief 改写唯一测试 registry record 的状态 / Rewrite the sole test registry record state.
 * @param state_root 一次性测试状态根 / Disposable test state root.
 * @param source_state 当前预期状态 / Expected current state.
 * @param target_state 要模拟的持久状态 / Persisted state to simulate.
 * @return 成功或严格测试夹具错误 / Success or a strict fixture error.
 */
[[nodiscard]] wspctl::Result<void>
rewrite_only_registry_record_state(const std::filesystem::path& state_root,
                                   const std::string_view source_state,
                                   const std::string_view target_state) {
    /** @brief 测试 registry records 目录 / Test registry records directory. */
    const std::filesystem::path records = state_root / "quota-registry" / "runtimes";
    /** @brief filesystem 枚举错误 / Filesystem enumeration error. */
    std::error_code error;
    /** @brief 唯一 record 的目录迭代器 / Directory iterator for the sole record. */
    std::filesystem::directory_iterator iterator(records, error);
    if (error || iterator == std::filesystem::directory_iterator{}) {
        return std::unexpected(
            wspctl::make_error(wspctl::ErrorCode::io_failure,
                               "test quota registry does not contain exactly one record"));
    }
    /** @brief 唯一测试 record 路径 / Sole test record path. */
    const std::filesystem::path record = iterator->path();
    ++iterator;
    if (iterator != std::filesystem::directory_iterator{}) {
        return std::unexpected(wspctl::make_error(
            wspctl::ErrorCode::io_failure, "test quota registry contains more than one record"));
    }
    /** @brief record 输入流 / Record input stream. */
    std::ifstream input(record);
    /** @brief 原始 record 内容 / Original record contents. */
    const std::string contents((std::istreambuf_iterator<char>(input)),
                               std::istreambuf_iterator<char>());
    /** @brief 唯一预期状态行 / Unique expected state line. */
    const std::string source = "state=" + std::string(source_state) + "\n";
    /** @brief 替换后的状态行 / Replacement state line. */
    const std::string target = "state=" + std::string(target_state) + "\n";
    /** @brief 预期状态行偏移 / Expected state-line offset. */
    const std::size_t offset = contents.find(source);
    if (!input.eof() || offset == std::string::npos ||
        contents.find(source, offset + source.size()) != std::string::npos) {
        return std::unexpected(
            wspctl::make_error(wspctl::ErrorCode::io_failure,
                               "test quota registry state is not uniquely replaceable"));
    }
    /** @brief 改写后的完整 record / Complete rewritten record. */
    std::string replacement = contents;
    replacement.replace(offset, source.size(), target);
    /** @brief 截断重写输出流 / Truncating rewrite output stream. */
    std::ofstream output(record, std::ios::trunc);
    output << replacement;
    output.flush();
    if (!output) {
        return std::unexpected(
            wspctl::make_error(wspctl::ErrorCode::io_failure, "rewrite test quota registry state"));
    }
    return {};
}

/**
 * @brief 在单个测试 inode 上设置并读回 XFS project ID / Set and read back the XFS project ID on
 * one test inode.
 * @param path 测试拥有的真实 inode 路径 / Path to a real inode owned by this test.
 * @param project_id required project ID / Required project ID.
 * @return 成功或 ioctl/I/O 错误 / Success or an ioctl/I/O error.
 */
[[nodiscard]] wspctl::Result<void> set_test_inode_project_id(const std::filesystem::path& path,
                                                             const std::uint32_t project_id) {
    /** @brief no-follow test inode FD / No-follow test-inode FD. */
    const int descriptor = open(path.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (descriptor < 0) {
        return std::unexpected(
            wspctl::errno_error(wspctl::ErrorCode::io_failure, "open project-ID test inode"));
    }
    /** @brief current XFS attributes / Current XFS attributes. */
    fsxattr attributes{};
    const int loaded = ioctl(descriptor, FS_IOC_FSGETXATTR, &attributes);
    if (loaded == 0) {
        attributes.fsx_projid = project_id;
    }
    const int assigned = loaded == 0 ? ioctl(descriptor, FS_IOC_FSSETXATTR, &attributes) : -1;
    /** @brief XFS attribute readback / XFS attribute readback. */
    fsxattr readback{};
    const int verified = assigned == 0 ? ioctl(descriptor, FS_IOC_FSGETXATTR, &readback) : -1;
    /** @brief errno saved before closing / errno saved before closing. */
    const int saved_errno = errno;
    static_cast<void>(close(descriptor));
    if (loaded != 0 || assigned != 0 || verified != 0) {
        errno = saved_errno;
        return std::unexpected(
            wspctl::errno_error(wspctl::ErrorCode::io_failure, "set test inode project ID"));
    }
    if (readback.fsx_projid != project_id) {
        return std::unexpected(wspctl::make_error(wspctl::ErrorCode::io_failure,
                                                  "test inode project-ID readback differs"));
    }
    return {};
}

/**
 * @brief 证明独立进程会阻塞在 registry flock 上 / Prove an independent process blocks on the
 * registry flock.
 * @param quota fork 前构造的 quota service / Quota service constructed before fork.
 * @param state_root disposable state root / Disposable state root.
 * @param runtime_key canonical runtime UUID / Canonical runtime UUID.
 * @return child 在解锁前不能完成、解锁后成功时成功 / Success when the child cannot complete
 * before unlock and succeeds after unlock.
 */
[[nodiscard]] wspctl::Result<void>
prove_registry_lock_blocks_cross_process(const wspctl::XfsProjectQuota& quota,
                                         const std::filesystem::path& state_root,
                                         const std::string_view runtime_key) {
    /** @brief independently opened registry lock FD / Independently opened registry-lock FD. */
    const int lock_fd =
        open((state_root / "quota-registry" / "lock").c_str(), O_RDWR | O_CLOEXEC | O_NOFOLLOW);
    if (lock_fd < 0 || flock(lock_fd, LOCK_EX) != 0) {
        if (lock_fd >= 0) {
            static_cast<void>(close(lock_fd));
        }
        return std::unexpected(
            wspctl::errno_error(wspctl::ErrorCode::io_failure, "hold test registry flock"));
    }
    /** @brief child-start signal pipe / Child-start signal pipe. */
    int started_pipe[2]{-1, -1};
    /** @brief child-completion signal pipe / Child-completion signal pipe. */
    int completed_pipe[2]{-1, -1};
    if (pipe2(started_pipe, O_CLOEXEC) != 0) {
        static_cast<void>(flock(lock_fd, LOCK_UN));
        static_cast<void>(close(lock_fd));
        return std::unexpected(
            wspctl::errno_error(wspctl::ErrorCode::io_failure, "create flock proof pipes"));
    }
    if (pipe2(completed_pipe, O_CLOEXEC) != 0) {
        static_cast<void>(close(started_pipe[0]));
        static_cast<void>(close(started_pipe[1]));
        static_cast<void>(flock(lock_fd, LOCK_UN));
        static_cast<void>(close(lock_fd));
        return std::unexpected(
            wspctl::errno_error(wspctl::ErrorCode::io_failure, "create flock proof pipes"));
    }
    /** @brief independent broker-like child / Independent broker-like child. */
    const pid_t child = fork();
    if (child == 0) {
        static_cast<void>(close(started_pipe[0]));
        static_cast<void>(close(completed_pipe[0]));
        static_cast<void>(close(lock_fd));
        /** @brief child-start byte / Child-start byte. */
        constexpr char kStarted{'S'};
        if (write(started_pipe[1], &kStarted, 1U) != 1) {
            _exit(30);
        }
        static_cast<void>(close(started_pipe[1]));
        /** @brief ensure result after waiting for the production lock /
         * Ensure result after waiting for the production lock. */
        const auto ensured = quota.ensure_runtime(runtime_key);
        /** @brief child-completion byte / Child-completion byte. */
        const char completed = ensured ? 'Y' : 'N';
        const ssize_t sent = write(completed_pipe[1], &completed, 1U);
        static_cast<void>(close(completed_pipe[1]));
        _exit(ensured && sent == 1 ? EXIT_SUCCESS : 31);
    }
    static_cast<void>(close(started_pipe[1]));
    static_cast<void>(close(completed_pipe[1]));
    if (child < 0) {
        static_cast<void>(close(started_pipe[0]));
        static_cast<void>(close(completed_pipe[0]));
        static_cast<void>(flock(lock_fd, LOCK_UN));
        static_cast<void>(close(lock_fd));
        return std::unexpected(
            wspctl::errno_error(wspctl::ErrorCode::child_failure, "fork flock proof child"));
    }
    /** @brief observed child-start byte / Observed child-start byte. */
    char started{};
    /** @brief child-start read result / Child-start read result. */
    ssize_t start_count{-1};
    do {
        start_count = read(started_pipe[0], &started, 1U);
    } while (start_count < 0 && errno == EINTR);
    if (start_count != 1 || started != 'S') {
        static_cast<void>(kill(child, SIGKILL));
        static_cast<void>(waitpid(child, nullptr, 0));
        static_cast<void>(close(started_pipe[0]));
        static_cast<void>(close(completed_pipe[0]));
        static_cast<void>(flock(lock_fd, LOCK_UN));
        static_cast<void>(close(lock_fd));
        return std::unexpected(wspctl::make_error(wspctl::ErrorCode::child_failure,
                                                  "flock proof child did not start"));
    }
    static_cast<void>(close(started_pipe[0]));
    /** @brief completion pipe poll descriptor / Completion-pipe poll descriptor. */
    pollfd completion_poll{
        .fd = completed_pipe[0],
        .events = POLLIN | POLLHUP,
        .revents = 0,
    };
    /** @brief pre-unlock poll result / Pre-unlock poll result. */
    const int premature = poll(&completion_poll, 1U, 200);
    const bool blocked = premature == 0;
    const int unlocked = flock(lock_fd, LOCK_UN);
    static_cast<void>(close(lock_fd));
    completion_poll.revents = 0;
    /** @brief post-unlock poll result / Post-unlock poll result. */
    const int completed = poll(&completion_poll, 1U, 5'000);
    /** @brief child result byte / Child result byte. */
    char completion{};
    const ssize_t received = completed > 0 ? read(completed_pipe[0], &completion, 1U) : -1;
    static_cast<void>(close(completed_pipe[0]));
    if (completed <= 0) {
        static_cast<void>(kill(child, SIGKILL));
    }
    /** @brief child wait status / Child wait status. */
    int status{};
    /** @brief waited child PID / Waited child PID. */
    pid_t waited{-1};
    do {
        waited = waitpid(child, &status, 0);
    } while (waited < 0 && errno == EINTR);
    if (!blocked || unlocked != 0 || completed <= 0 || received != 1 || completion != 'Y' ||
        waited != child || !WIFEXITED(status) || WEXITSTATUS(status) != EXIT_SUCCESS) {
        return std::unexpected(wspctl::make_error(
            wspctl::ErrorCode::io_failure,
            "registry flock did not demonstrably serialize an independent process"));
    }
    return {};
}

/**
 * @brief 让两个独立进程并发 reconcile 同一 runtime / Have two independent processes reconcile
 * the same runtime concurrently.
 * @param quota fork 前构造且尚未持有 registry FD 的 quota 服务 / Quota service constructed before
 * fork and not holding a registry FD.
 * @param runtime_key canonical runtime UUID / Canonical runtime UUID.
 * @return 两个进程均完成幂等恢复时成功 / Success when both processes complete idempotent recovery.
 * @note 该夹具模拟两个 broker 同时遇到同一 allocating/quarantined residue；生产 registry
 *       ``flock`` 必须把 mutation 串行化。/ This fixture simulates two brokers observing the same
 *       allocating/quarantined residue; the production registry ``flock`` must serialize mutation.
 */
[[nodiscard]] wspctl::Result<void>
run_concurrent_reconciliation(const wspctl::XfsProjectQuota& quota,
                              const std::string_view runtime_key) {
    /** @brief 同步两个 child 起跑的 pipe / Pipe synchronizing both child starts. */
    int start_pipe[2]{-1, -1};
    if (pipe2(start_pipe, O_CLOEXEC) != 0) {
        return std::unexpected(
            wspctl::errno_error(wspctl::ErrorCode::io_failure, "create reconcile start pipe"));
    }
    /** @brief 两个模拟 broker child PID / PIDs of the two simulated broker children. */
    std::array<pid_t, 2U> children{-1, -1};
    /** @brief 当前要创建的 child slot / Current child slot being created. */
    std::size_t index{0U};
    for (; index < children.size(); ++index) {
        children[index] = fork();
        if (children[index] == 0) {
            static_cast<void>(close(start_pipe[1]));
            /** @brief parent 发出的单字节起跑信号 / One-byte start signal from the parent. */
            char signal{};
            /** @brief start pipe 的读取结果 / Read result from the start pipe. */
            ssize_t received{-1};
            do {
                received = read(start_pipe[0], &signal, 1U);
            } while (received < 0 && errno == EINTR);
            static_cast<void>(close(start_pipe[0]));
            if (received != 1) {
                _exit(20);
            }
            /** @brief child 的 reconcile 结果 / Child reconciliation result. */
            const auto reconciled = quota.ensure_runtime(runtime_key);
            _exit(reconciled ? EXIT_SUCCESS : 21);
        }
        if (children[index] < 0) {
            /** @brief fork 失败时保留的 errno / Saved errno when fork fails. */
            const int fork_error = errno;
            static_cast<void>(close(start_pipe[0]));
            static_cast<void>(close(start_pipe[1]));
            /** @brief 已创建 child 的清理 slot / Cleanup slot for already-created children. */
            std::size_t cleanup_index{0U};
            for (; cleanup_index < children.size(); ++cleanup_index) {
                /** @brief 当前待清理 child / Current child to clean up. */
                const pid_t child = children[cleanup_index];
                if (child > 0) {
                    static_cast<void>(kill(child, SIGKILL));
                    while (waitpid(child, nullptr, 0) < 0 && errno == EINTR) {
                    }
                }
            }
            errno = fork_error;
            return std::unexpected(
                wspctl::errno_error(wspctl::ErrorCode::io_failure, "fork reconcile broker"));
        }
    }
    static_cast<void>(close(start_pipe[0]));
    /** @brief 每个 child 一个字节的起跑信号 / One start byte for each child. */
    constexpr std::array<char, 2U> kSignals{'R', 'R'};
    /** @brief start pipe 写入结果 / Write result for the start pipe. */
    ssize_t written{-1};
    do {
        written = write(start_pipe[1], kSignals.data(), kSignals.size());
    } while (written < 0 && errno == EINTR);
    static_cast<void>(close(start_pipe[1]));
    /** @brief 所有 child 是否成功 / Whether every child succeeded. */
    bool succeeded = written == static_cast<ssize_t>(kSignals.size());
    /** @brief 当前等待的 child slot / Current child slot being waited for. */
    std::size_t wait_index{0U};
    for (; wait_index < children.size(); ++wait_index) {
        /** @brief 当前等待的 child PID / Current child PID being waited for. */
        const pid_t child = children[wait_index];
        /** @brief 当前 child 的 wait status / Wait status of the current child. */
        int status{};
        /** @brief waitpid 的返回值 / Return value from waitpid. */
        pid_t waited{-1};
        do {
            waited = waitpid(child, &status, 0);
        } while (waited < 0 && errno == EINTR);
        succeeded = succeeded && waited == child && WIFEXITED(status) &&
                    WEXITSTATUS(status) == EXIT_SUCCESS;
    }
    if (!succeeded) {
        return std::unexpected(
            wspctl::make_error(wspctl::ErrorCode::io_failure,
                               "concurrent quota reconciliation was not serialized idempotently"));
    }
    return {};
}

/**
 * @brief 运行显式 XFS project-quota integration test / Run the explicit XFS project-quota
 * integration test.
 * @return POSIX/CTest exit status / POSIX/CTest exit status.
 */
[[nodiscard]] int run_xfs_project_quota_test() {
    if (geteuid() != 0U) {
        return unavailable("effective UID is not root");
    }
    const std::optional<std::filesystem::path> mount_path = environment_path(kMountEnvironment);
    const std::optional<std::filesystem::path> configured_parent =
        environment_path(kParentEnvironment);
    if (!mount_path.has_value() || !configured_parent.has_value()) {
        return unavailable("set WSPCTL_XFS_QUOTA_TEST_MOUNT and WSPCTL_XFS_QUOTA_TEST_PARENT");
    }
    const auto parent = canonical_private_parent(*configured_parent);
    if (!parent) {
        std::cerr << "FAIL: " << parent.error().message << '\n';
        return EXIT_FAILURE;
    }
    const auto state_root = create_test_state_root(*parent);
    if (!state_root) {
        std::cerr << "FAIL: " << state_root.error().message << '\n';
        return EXIT_FAILURE;
    }
    /** @brief test completion status / 测试完成状态。 */
    int result{EXIT_FAILURE};
    do {
        const wspctl::XfsProjectQuotaConfig config{
            .mount_path = *mount_path,
            .project_id_min = 200'000U,
            .project_id_max = 200'003U,
            .control_hard_bytes = kControlHardBytes,
            .control_hard_inodes = kControlHardInodes,
            .workspace_hard_bytes = kWorkspaceHardBytes,
            .workspace_hard_inodes = kWorkspaceHardInodes,
            .global_admission_bytes = kControlHardBytes + kWorkspaceHardBytes,
            .global_admission_inodes = kControlHardInodes + kWorkspaceHardInodes,
            .system_reserve_bytes = 1U * 1024U * 1024U,
            .system_reserve_inodes = 256U,
            .workspace_uid = kAgentUid,
            .workspace_gid = kAgentGid,
        };
        if (const auto preflight = wspctl::preflight_xfs_project_quota(config, *state_root);
            !preflight) {
            std::cerr << "FAIL: XFS quota preflight: " << preflight.error().message << '\n';
            break;
        }
        const wspctl::XfsProjectQuota quota(*state_root, config);
        constexpr std::string_view kRuntime{"123e4567-e89b-42d3-a456-426614174001"};
        constexpr std::string_view kMissingRuntime{"123e4567-e89b-42d3-a456-426614174002"};
        // A replay lookup before any allocation is the critical non-provisioning contract:
        // it must not manufacture quota-registry, registry lock/records, or a runtime layout.
        const auto absent_without_registry = quota.find_ready_runtime(kMissingRuntime);
        if (absent_without_registry ||
            absent_without_registry.error().code != wspctl::ErrorCode::not_found) {
            std::cerr << "FAIL: no-record read-only lookup did not return not_found before "
                         "registry provisioning\n";
            break;
        }
        /** @brief no-registry postcondition error / No-registry postcondition error. */
        std::error_code absent_state_error;
        if (std::filesystem::exists(*state_root / "quota-registry", absent_state_error) ||
            std::filesystem::exists(*state_root / "runtimes", absent_state_error) ||
            absent_state_error) {
            std::cerr
                << "FAIL: no-record read-only lookup created quota registry or runtime state\n";
            break;
        }
        const auto binding = quota.ensure_runtime(kRuntime);
        if (!binding) {
            std::cerr << "FAIL: provision XFS project pair: " << binding.error().message << '\n';
            break;
        }
        /** @brief 首次 activation 后由具名 Agent 持有的 upper / Upper owned by the named Agent
         * after first activation. */
        const std::filesystem::path upper = binding->workspace_dir / "upper";
        if (lchown(upper.c_str(), kAgentUid, kAgentGid) != 0) {
            std::cerr << "FAIL: simulate named-Agent ownership of persistent upper\n";
            break;
        }
        /** @brief reconcile 全程不得删除的既有业务文件 / Existing business file that
         * reconciliation must never delete. */
        const std::filesystem::path sentinel = upper / "reconcile-preserves-data.txt";
        {
            /** @brief sentinel 写入流 / Sentinel output stream. */
            std::ofstream output(sentinel, std::ios::binary | std::ios::trunc);
            output << "persistent-workspace-data\n";
            output.flush();
            if (!output || lchown(sentinel.c_str(), kAgentUid, kAgentGid) != 0) {
                std::cerr << "FAIL: create Agent-owned reconciliation sentinel\n";
                break;
            }
        }
        /** @brief bulkstat 必须无跟随校验的 symlink inode /
         * Symlink inode that bulkstat must validate without following. */
        const std::filesystem::path sentinel_link = upper / "bulkstat-no-follow-link";
        if (symlink(sentinel.filename().c_str(), sentinel_link.c_str()) != 0 ||
            lchown(sentinel_link.c_str(), kAgentUid, kAgentGid) != 0) {
            std::cerr << "FAIL: create project-inheriting symlink sentinel\n";
            break;
        }
        /** @brief bulkstat 必须直接校验的 Unix socket inode /
         * Unix-socket inode that bulkstat must validate directly. */
        const std::filesystem::path socket_path = upper / "bulkstat-unix-socket";
        /** @brief temporary Unix socket FD / Temporary Unix-socket FD. */
        const int socket_fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
        /** @brief Unix socket address / Unix-socket address. */
        sockaddr_un socket_address{};
        socket_address.sun_family = AF_UNIX;
        const std::string socket_name = socket_path.string();
        if (socket_fd < 0 || socket_name.size() >= sizeof(socket_address.sun_path)) {
            if (socket_fd >= 0) {
                static_cast<void>(close(socket_fd));
            }
            std::cerr << "FAIL: prepare project-inheriting Unix socket sentinel\n";
            break;
        }
        std::memcpy(socket_address.sun_path, socket_name.c_str(), socket_name.size() + 1U);
        const int socket_bound = bind(socket_fd, reinterpret_cast<const sockaddr*>(&socket_address),
                                      static_cast<socklen_t>(sizeof(socket_address)));
        static_cast<void>(close(socket_fd));
        if (socket_bound != 0 || lchown(socket_path.c_str(), kAgentUid, kAgentGid) != 0) {
            std::cerr << "FAIL: create project-inheriting Unix socket sentinel\n";
            break;
        }
        const auto lookup_ready = quota.find_ready_runtime(kRuntime);
        if (!lookup_ready || lookup_ready->control_project_id != binding->control_project_id ||
            lookup_ready->workspace_project_id != binding->workspace_project_id) {
            std::cerr << "FAIL: read-only lookup did not recover the existing ready binding\n";
            break;
        }
        const auto runtime_entries_before = direct_entry_count(
            *state_root / "runtimes", "runtime state root before missing lookup");
        const auto record_entries_before =
            direct_entry_count(*state_root / "quota-registry" / "runtimes",
                               "quota registry records before missing lookup");
        if (!runtime_entries_before || !record_entries_before) {
            std::cerr << "FAIL: cannot snapshot quota state before missing-binding lookup\n";
            break;
        }
        const auto absent_with_registry = quota.find_ready_runtime(kMissingRuntime);
        if (absent_with_registry ||
            absent_with_registry.error().code != wspctl::ErrorCode::not_found) {
            std::cerr << "FAIL: missing ready binding did not return not_found\n";
            break;
        }
        const auto runtime_entries_after =
            direct_entry_count(*state_root / "runtimes", "runtime state root after missing lookup");
        const auto record_entries_after =
            direct_entry_count(*state_root / "quota-registry" / "runtimes",
                               "quota registry records after missing lookup");
        if (!runtime_entries_after || !record_entries_after ||
            *runtime_entries_before != *runtime_entries_after ||
            *record_entries_before != *record_entries_after) {
            std::cerr
                << "FAIL: missing ready-binding lookup provisioned a runtime or registry record\n";
            break;
        }
        const auto recovered = quota.ensure_runtime(kRuntime);
        if (!recovered || recovered->control_project_id != binding->control_project_id ||
            recovered->workspace_project_id != binding->workspace_project_id) {
            std::cerr << "FAIL: XFS project pair was not stable across recovery verification\n";
            break;
        }
        if (const auto lock_proof =
                prove_registry_lock_blocks_cross_process(quota, *state_root, kRuntime);
            !lock_proof) {
            std::cerr << "FAIL: cross-process registry lock proof: " << lock_proof.error().message
                      << '\n';
            break;
        }
        /** @brief 可安全收紧的 0755 layout drift / Safely tighten-able 0755 layout drift. */
        const std::filesystem::path journal = binding->control_dir / "journal";
        {
            /** @brief 模拟正在合法使用 runtime 的 activation lease /
             * Activation lease simulating legitimate live runtime use. */
            const auto live_lease = quota.acquire_activation_lease(kRuntime);
            if (!live_lease || chmod(journal.c_str(), 0755) != 0) {
                std::cerr << "FAIL: simulate trusted 0755 journal drift under live activation\n";
                break;
            }
            const auto deferred_recovery = quota.ensure_runtime(kRuntime);
            if (deferred_recovery || deferred_recovery.error().code != wspctl::ErrorCode::busy) {
                std::cerr << "FAIL: strict recovery raced a live activation instead of deferring\n";
                break;
            }
        }
        const auto tightened_mode = quota.ensure_runtime(kRuntime);
        /** @brief 收紧后的 journal metadata / Journal metadata after tightening. */
        struct stat tightened_journal {};
        if (!tightened_mode || lstat(journal.c_str(), &tightened_journal) != 0 ||
            tightened_journal.st_uid != 0U || tightened_journal.st_gid != 0U ||
            (tightened_journal.st_mode & 07777U) != 0700U) {
            std::cerr << "FAIL: trusted 0755 layout drift was not tightened via strict recovery\n";
            break;
        }
        /** @brief 暂存既有 journal 的非 project-root 路径 / Non-project-root path temporarily
         * holding the existing journal. */
        const std::filesystem::path journal_backup =
            binding->runtime_dir / "journal-preserved-for-recovery-test";
        if (rename(journal.c_str(), journal_backup.c_str()) != 0) {
            std::cerr << "FAIL: preserve journal while simulating deletion\n";
            break;
        }
        const auto rejected_missing_journal = quota.ensure_runtime(kRuntime);
        /** @brief missing journal 查询错误 / Missing-journal existence-query error. */
        std::error_code missing_journal_error;
        if (rejected_missing_journal ||
            rejected_missing_journal.error().code != wspctl::ErrorCode::binding_quarantined ||
            std::filesystem::exists(journal, missing_journal_error) ||
            !std::filesystem::is_directory(journal_backup, missing_journal_error) ||
            missing_journal_error) {
            std::cerr << "FAIL: ready recovery recreated a missing durable journal\n";
            break;
        }
        if (rename(journal_backup.c_str(), journal.c_str()) != 0 ||
            !quota.ensure_runtime(kRuntime)) {
            std::cerr << "FAIL: restored journal did not promote quarantined binding\n";
            break;
        }
        /** @brief 暂存既有 Agent-owned upper 的非 project-root 路径 /
         * Non-project-root path temporarily holding the existing Agent-owned upper. */
        const std::filesystem::path upper_backup =
            binding->runtime_dir / "upper-preserved-for-recovery-test";
        if (rename(upper.c_str(), upper_backup.c_str()) != 0) {
            std::cerr << "FAIL: preserve upper while simulating deletion\n";
            break;
        }
        const auto rejected_missing_upper = quota.ensure_runtime(kRuntime);
        /** @brief missing upper 查询错误 / Missing-upper existence-query error. */
        std::error_code missing_upper_error;
        if (rejected_missing_upper ||
            rejected_missing_upper.error().code != wspctl::ErrorCode::binding_quarantined ||
            std::filesystem::exists(upper, missing_upper_error) ||
            !std::filesystem::is_directory(upper_backup, missing_upper_error) ||
            missing_upper_error) {
            std::cerr << "FAIL: ready recovery recreated a missing Agent-owned upper\n";
            break;
        }
        if (rename(upper_backup.c_str(), upper.c_str()) != 0 || !quota.ensure_runtime(kRuntime) ||
            !std::filesystem::exists(sentinel)) {
            std::cerr << "FAIL: restored nonempty Agent upper did not recover with data intact\n";
            break;
        }
        if (const auto changed_project =
                set_test_inode_project_id(sentinel, binding->control_project_id);
            !changed_project) {
            std::cerr << "FAIL: simulate wrong-project descendant: "
                      << changed_project.error().message << '\n';
            break;
        }
        if (const auto simulated =
                rewrite_only_registry_record_state(*state_root, "ready", "quarantined");
            !simulated) {
            std::cerr << "FAIL: stage wrong-project descendant for promotion proof: "
                      << simulated.error().message << '\n';
            break;
        }
        const auto rejected_wrong_descendant = quota.ensure_runtime(kRuntime);
        if (rejected_wrong_descendant ||
            rejected_wrong_descendant.error().code != wspctl::ErrorCode::binding_quarantined ||
            !std::filesystem::exists(sentinel)) {
            std::cerr << "FAIL: recursive verification accepted or mutated wrong-project data\n";
            break;
        }
        if (const auto restored_project =
                set_test_inode_project_id(sentinel, binding->workspace_project_id);
            !restored_project || !quota.ensure_runtime(kRuntime)) {
            std::cerr << "FAIL: restored descendant project ID did not promote binding\n";
            break;
        }
        /** @brief 模拟部署后 hard-limit 变化的 quota 配置 / Quota configuration simulating
         * post-deployment hard-limit changes. */
        const wspctl::XfsProjectQuotaConfig changed_limits_config{
            .mount_path = config.mount_path,
            .project_id_min = config.project_id_min,
            .project_id_max = config.project_id_max,
            .control_hard_bytes = config.control_hard_bytes,
            .control_hard_inodes = config.control_hard_inodes,
            .workspace_hard_bytes = config.workspace_hard_bytes + 512U * 1024U,
            .workspace_hard_inodes = config.workspace_hard_inodes + 16U,
            .global_admission_bytes =
                config.control_hard_bytes + config.workspace_hard_bytes + 512U * 1024U,
            .global_admission_inodes =
                config.control_hard_inodes + config.workspace_hard_inodes + 16U,
            .system_reserve_bytes = config.system_reserve_bytes,
            .system_reserve_inodes = config.system_reserve_inodes,
            .workspace_uid = config.workspace_uid,
            .workspace_gid = config.workspace_gid,
        };
        /** @brief 使用变化后策略的 quota 服务 / Quota service using the changed policy. */
        const wspctl::XfsProjectQuota changed_limits_quota(*state_root, changed_limits_config);
        /** @brief 变化后策略的 reconcile 结果 / Reconciliation result under the changed policy. */
        const auto reconciled_changed_limits = changed_limits_quota.ensure_runtime(kRuntime);
        if (!reconciled_changed_limits ||
            reconciled_changed_limits->control_project_id != binding->control_project_id ||
            reconciled_changed_limits->workspace_project_id != binding->workspace_project_id) {
            std::cerr << "FAIL: existing quota binding was not reconciled to changed hard limits\n";
            break;
        }
        /** @brief 恢复原策略的 reconcile 结果 / Reconciliation result restoring the original
         * policy. */
        const auto reconciled_original_limits = quota.ensure_runtime(kRuntime);
        if (!reconciled_original_limits ||
            reconciled_original_limits->control_project_id != binding->control_project_id ||
            reconciled_original_limits->workspace_project_id != binding->workspace_project_id) {
            std::cerr
                << "FAIL: existing quota binding was not reconciled back to original limits\n";
            break;
        }
        if (const auto simulated =
                rewrite_only_registry_record_state(*state_root, "ready", "allocating");
            !simulated) {
            std::cerr << "FAIL: simulate allocating registry residue: " << simulated.error().message
                      << '\n';
            break;
        }
        /** @brief 模拟 crash 时尚未创建的空 work 根 / Empty work root omitted by a simulated
         * provisioning crash. */
        const std::filesystem::path partial_work = binding->workspace_dir / "work";
        if (rmdir(partial_work.c_str()) != 0) {
            std::cerr << "FAIL: simulate partial provisioning residue\n";
            break;
        }
        if (const auto concurrent = run_concurrent_reconciliation(quota, kRuntime); !concurrent) {
            std::cerr << "FAIL: concurrent allocating recovery: " << concurrent.error().message
                      << '\n';
            break;
        }
        const auto recovered_allocating = quota.find_ready_runtime(kRuntime);
        if (!recovered_allocating ||
            recovered_allocating->control_project_id != binding->control_project_id ||
            recovered_allocating->workspace_project_id != binding->workspace_project_id ||
            !std::filesystem::is_directory(partial_work)) {
            std::cerr << "FAIL: concurrent partial provisioning was not promoted to ready\n";
            break;
        }
        if (const auto simulated =
                rewrite_only_registry_record_state(*state_root, "ready", "quarantined");
            !simulated) {
            std::cerr << "FAIL: simulate quarantined registry residue: "
                      << simulated.error().message << '\n';
            break;
        }
        /** @brief reconcile 前的只读 quarantined 状态 / Read-only quarantined state before
         * reconciliation. */
        const auto observed_quarantined = quota.find_ready_runtime(kRuntime);
        if (observed_quarantined ||
            observed_quarantined.error().code != wspctl::ErrorCode::binding_quarantined) {
            std::cerr << "FAIL: read-only lookup did not expose quarantined recovery state\n";
            break;
        }
        const auto recovered_quarantined = quota.ensure_runtime(kRuntime);
        if (!recovered_quarantined ||
            recovered_quarantined->control_project_id != binding->control_project_id ||
            recovered_quarantined->workspace_project_id != binding->workspace_project_id) {
            std::cerr << "FAIL: verified quarantined quota binding was not promoted to ready\n";
            break;
        }
        if (lchown(upper.c_str(), kUnexpectedUid, kUnexpectedGid) != 0) {
            std::cerr << "FAIL: simulate unexpected upper owner\n";
            break;
        }
        /** @brief status/replay 所用只读 lookup 的 owner 拒绝结果 / Owner rejection from the
         * read-only lookup used by status and replay. */
        const auto read_only_rejected_owner = quota.find_ready_runtime(kRuntime);
        if (read_only_rejected_owner ||
            read_only_rejected_owner.error().code != wspctl::ErrorCode::quota_recovery_required) {
            std::cerr << "FAIL: read-only lookup accepted an unsafe upper owner\n";
            break;
        }
        /** @brief 非 allowlist owner 下的 reconcile 拒绝结果 / Reconciliation rejection under a
         * non-allowlisted owner. */
        const auto rejected_owner = quota.ensure_runtime(kRuntime);
        /** @brief owner 拒绝后的 upper metadata / Upper metadata after owner rejection. */
        struct stat rejected_metadata {};
        if (rejected_owner ||
            rejected_owner.error().code != wspctl::ErrorCode::binding_quarantined ||
            rejected_owner.error().message.find("unexpected owner") == std::string::npos ||
            lstat(upper.c_str(), &rejected_metadata) != 0 ||
            rejected_metadata.st_uid != kUnexpectedUid ||
            rejected_metadata.st_gid != kUnexpectedGid) {
            std::cerr << "FAIL: unsafe upper owner was repaired or not quarantined fail-closed\n";
            break;
        }
        if (lchown(upper.c_str(), kAgentUid, kAgentGid) != 0) {
            std::cerr << "FAIL: restore named-Agent upper owner after rejection test\n";
            break;
        }
        const auto recovered_owner = quota.ensure_runtime(kRuntime);
        if (!recovered_owner ||
            recovered_owner->control_project_id != binding->control_project_id ||
            recovered_owner->workspace_project_id != binding->workspace_project_id) {
            std::cerr << "FAIL: quarantined owner residue did not recover after safe restoration\n";
            break;
        }
        {
            /** @brief reconcile 后的 sentinel 输入流 / Sentinel input stream after reconciliation.
             */
            std::ifstream input(sentinel, std::ios::binary);
            /** @brief reconcile 后的 sentinel 内容 / Sentinel contents after reconciliation. */
            const std::string contents((std::istreambuf_iterator<char>(input)),
                                       std::istreambuf_iterator<char>());
            if (!input.eof() || contents != "persistent-workspace-data\n") {
                std::cerr << "FAIL: quota reconciliation changed existing workspace data\n";
                break;
            }
        }
        constexpr std::string_view kLeaseActivation{"xfs-quota-lease-regression"};
        {
            /** @brief first broker-like activation lease / First broker-like activation lease. */
            const auto lease = quota.acquire_activation_lease(kRuntime);
            if (!lease) {
                std::cerr << "FAIL: acquire initial XFS activation lease: " << lease.error().message
                          << '\n';
                break;
            }
            /** @brief competing independent lock acquisition / Competing independent lock
             * acquisition. */
            const auto competing = quota.acquire_activation_lease(kRuntime);
            if (competing || competing.error().code != wspctl::ErrorCode::busy) {
                std::cerr << "FAIL: second XFS activation lease was not rejected as busy\n";
                break;
            }
            /** @brief lease-owned transient staging storage / Lease-owned transient staging
             * storage. */
            const auto storage = quota.prepare_activation_storage(*lease, kLeaseActivation);
            if (!storage) {
                std::cerr << "FAIL: create lease-owned activation staging: "
                          << storage.error().message << '\n';
                break;
            }
            /** @brief staging-existence query error / Staging-existence query error. */
            std::error_code staging_error;
            if (!std::filesystem::is_directory(storage->control_activation_dir, staging_error) ||
                !std::filesystem::is_directory(storage->workspace_work_dir, staging_error) ||
                staging_error) {
                std::cerr
                    << "FAIL: activation lease did not own both transient staging directories\n";
                break;
            }
            if (const auto reclaimed = quota.reclaim_dead_activation_storage(*lease); !reclaimed) {
                std::cerr << "FAIL: reclaim task-free lease staging: " << reclaimed.error().message
                          << '\n';
                break;
            }
            /** @brief post-reclaim existence query error / Post-reclaim existence query error. */
            std::error_code reclaimed_error;
            if (std::filesystem::exists(storage->control_activation_dir, reclaimed_error) ||
                std::filesystem::exists(storage->workspace_work_dir, reclaimed_error) ||
                reclaimed_error) {
                std::cerr
                    << "FAIL: reclaim left an activation transient staging directory behind\n";
                break;
            }
        }
        /** @brief post-destruction lease acquisition / Lease acquisition after the first RAII lease
         * is destroyed. */
        const auto reacquired_lease = quota.acquire_activation_lease(kRuntime);
        if (!reacquired_lease) {
            std::cerr << "FAIL: activation lease was not released by RAII destruction: "
                      << reacquired_lease.error().message << '\n';
            break;
        }
        if (const auto inode_probe =
                run_boundary_probe(binding->workspace_dir / "upper", verify_inode_hard_limit_child,
                                   "XFS inode hard-limit probe");
            !inode_probe) {
            std::cerr << "FAIL: " << inode_probe.error().message << '\n';
            break;
        }
        if (const auto byte_probe =
                run_boundary_probe(binding->workspace_dir / "upper", verify_byte_hard_limit_child,
                                   "XFS byte hard-limit probe");
            !byte_probe) {
            std::cerr << "FAIL: " << byte_probe.error().message << '\n';
            break;
        }
        result = EXIT_SUCCESS;
    } while (false);
    const auto cleaned = remove_test_state_root(*parent, *state_root);
    if (!cleaned) {
        std::cerr << "FAIL: " << cleaned.error().message << '\n';
        return EXIT_FAILURE;
    }
    return result;
}

} // namespace

/**
 * @brief XFS project quota CTest 入口 / XFS project-quota CTest entry point.
 * @return POSIX/CTest exit status / POSIX/CTest exit status.
 */
int main() { return run_xfs_project_quota_test(); }
