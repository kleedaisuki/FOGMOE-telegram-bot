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

#include <sys/capability.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>

#include <cerrno>
#include <cstddef>
#include <cstdlib>
#include <cstring>
#include <filesystem>
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
