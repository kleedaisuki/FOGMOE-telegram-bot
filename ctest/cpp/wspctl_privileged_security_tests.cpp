/**
 * @file wspctl_privileged_security_tests.cpp
 * @brief capability 与 seccomp 特权集成测试 / Capability and seccomp privileged integration test.
 *
 * 本测试刻意只传递无效 module/io_uring 参数，因此即使 seccomp 回归也不会加载、卸载或删除
 * 任何内核模块，更不会创建或提交 io_uring ring。 The test deliberately passes invalid
 * module/io_uring arguments, so it never loads, unloads, or removes a kernel module, nor creates
 * or submits an io_uring ring, even if the seccomp policy regresses.
 */

#include "wspctl/infrastructure/sandbox.hpp"

#include <sys/capability.h>
#include <sys/ioctl.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/wait.h>

#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iostream>
#include <optional>
#include <string>
#include <string_view>

#include <fcntl.h>
#include <linux/fs.h>
#include <unistd.h>

namespace {

/** @brief CTest 的约定 skip 返回码 / Conventional CTest skip return code. */
constexpr int kSkipReturnCode = 77;
/** @brief task 降权使用的无特权 numeric UID / Unprivileged numeric UID used for the task drop. */
constexpr uid_t kSandboxUid = 65'534U;
/** @brief task 降权使用的无特权 numeric GID / Unprivileged numeric GID used for the task drop. */
constexpr gid_t kSandboxGid = 65'534U;
/** @brief 强制 privileged CI 的环境变量 / Environment variable that makes privileged CI mandatory. */
constexpr std::string_view kRequireEnvironment{"WSPCTL_REQUIRE_PRIVILEGED_TESTS"};

/**
 * @brief privileged-test 的执行要求 / Execution requirement for the privileged test.
 */
enum class Requirement {
    /** @brief 无特权环境可 skip / An unprivileged environment may skip. */
    optional,
    /** @brief 无特权环境必须失败 / An unprivileged environment must fail. */
    required,
    /** @brief 环境变量取值不合法 / The environment-variable value is invalid. */
    invalid,
};

/**
 * @brief 从环境解析特权测试语义 / Parse privileged-test semantics from the environment.
 * @return optional、required 或 invalid / Optional, required, or invalid.
 * @note 只接受未设置、空、0 与 1，避免拼写错误把 CI 降格为 skip。
 *       Only unset, empty, 0, and 1 are accepted so a typo cannot silently downgrade CI to skip.
 */
[[nodiscard]] Requirement test_requirement() {
    /** @brief 原始环境变量值 / Raw environment-variable value. */
    const char* const raw_value = std::getenv(kRequireEnvironment.data());
    if (raw_value == nullptr || std::string_view(raw_value).empty() || std::string_view(raw_value) == "0") {
        return Requirement::optional;
    }
    if (std::string_view(raw_value) == "1") {
        return Requirement::required;
    }
    return Requirement::invalid;
}

/**
 * @brief 以 CTest 语义报告不可运行的特权前置条件 / Report an unavailable privileged prerequisite with CTest semantics.
 * @param requirement 当前执行要求 / Current execution requirement.
 * @param reason 不可运行原因 / Reason the test cannot run.
 * @return optional 时返回 77，required 时返回失败 / Returns 77 when optional and failure when required.
 */
[[nodiscard]] int unavailable(const Requirement requirement, const std::string_view reason) {
    if (requirement == Requirement::required) {
        std::cerr << "FAIL: " << kRequireEnvironment << "=1 but privileged security test cannot run: " << reason << '\n';
        return EXIT_FAILURE;
    }
    std::cerr << "SKIP: privileged security test unavailable: " << reason << '\n';
    return kSkipReturnCode;
}

/**
 * @brief 检查当前进程是否拥有一个 effective capability / Check whether the current process has one effective capability.
 * @param capability 待检查的 Linux capability / Linux capability to inspect.
 * @return 具有时 true；libcap 查询失败时 false / True when present; false when libcap cannot inspect it.
 */
[[nodiscard]] bool has_effective_capability(const cap_value_t capability) {
    /** @brief 当前 capability set / Current capability set. */
    cap_t capabilities = cap_get_proc();
    if (capabilities == nullptr) {
        return false;
    }
    /** @brief 目标 capability 的 effective 标志 / Effective flag for the target capability. */
    cap_flag_value_t enabled = CAP_CLEAR;
    /** @brief libcap 查询返回值 / Return value from the libcap query. */
    const int status = cap_get_flag(capabilities, capability, CAP_EFFECTIVE, &enabled);
    cap_free(capabilities);
    return status == 0 && enabled == CAP_SET;
}

/**
 * @brief 读取 `/proc/self/status` 的单个字段 / Read one field from `/proc/self/status`.
 * @param name 不含冒号的字段名 / Field name without the colon.
 * @return 去除前导空白后的值；字段不存在时为空 / Value with leading whitespace trimmed, or empty when absent.
 */
[[nodiscard]] std::optional<std::string> status_field(const std::string_view name) {
    /** @brief proc status 输入流 / proc status input stream. */
    std::ifstream input("/proc/self/status");
    if (!input.is_open()) {
        return std::nullopt;
    }
    /** @brief 当前读取的一行 / Current input line. */
    std::string line;
    /** @brief 与字段名匹配的前缀 / Prefix matching the field name. */
    const std::string prefix = std::string(name) + ":";
    while (std::getline(input, line)) {
        if (!line.starts_with(prefix)) {
            continue;
        }
        /** @brief 字段值开始位置 / Beginning offset of the field value. */
        std::size_t value_start = prefix.size();
        while (value_start < line.size() && (line[value_start] == ' ' || line[value_start] == '\t')) {
            ++value_start;
        }
        return line.substr(value_start);
    }
    return std::nullopt;
}

/**
 * @brief 判断 capability 十六进制字段是否全为零 / Determine whether a hexadecimal capability field is all zero.
 * @param value `/proc/self/status` 中的 capability 值 / Capability value from `/proc/self/status`.
 * @return 非空且每个字符为 0 时为 true / True when nonempty and every character is zero.
 */
[[nodiscard]] bool hexadecimal_is_zero(const std::string_view value) {
    return !value.empty() && value.find_first_not_of('0') == std::string_view::npos;
}

/**
 * @brief 断言 post-hardening status 不变量 / Assert post-hardening status invariants.
 * @return 所有不变量满足时为 true / True when every invariant holds.
 */
[[nodiscard]] bool verify_hardened_status() {
    /** @brief 需清空的 capability status 字段 / Capability status fields that must be empty. */
    constexpr std::string_view kEmptyCapabilityFields[]{"CapInh", "CapPrm", "CapEff", "CapAmb"};
    /** @brief 断言结果 / Accumulated assertion result. */
    bool valid = true;
    for (const std::string_view field : kEmptyCapabilityFields) {
        /** @brief 当前 capability 字段值 / Current capability field value. */
        const std::optional<std::string> value = status_field(field);
        if (!value.has_value() || !hexadecimal_is_zero(*value)) {
            std::cerr << "FAIL: " << field << " must be all zero after harden_task\n";
            valid = false;
        }
    }
    /** @brief `no_new_privs` 状态值 / `no_new_privs` status value. */
    const std::optional<std::string> no_new_privileges = status_field("NoNewPrivs");
    if (!no_new_privileges.has_value() || *no_new_privileges != "1") {
        std::cerr << "FAIL: NoNewPrivs must be 1 after harden_task\n";
        valid = false;
    }
    /** @brief seccomp mode 状态值 / Seccomp mode status value. */
    const std::optional<std::string> seccomp_mode = status_field("Seccomp");
    if (!seccomp_mode.has_value() || *seccomp_mode != "2") {
        std::cerr << "FAIL: Seccomp must be filter mode (2) after harden_task\n";
        valid = false;
    }
    return valid;
}

/**
 * @brief 断言一个故意无效的高风险 syscall 被 seccomp 以 EPERM 拒绝 / Assert seccomp rejects one deliberately invalid high-risk syscall with EPERM.
 * @param operation 供诊断显示的 syscall 名 / Syscall name shown in diagnostics.
 * @param result raw `syscall` 返回值 / Raw `syscall` return value.
 * @param error_number syscall 后立刻保存的 errno / errno saved immediately after the syscall.
 * @return 返回 -1 且 errno 为 EPERM 时为 true / True when result is -1 and errno is EPERM.
 */
[[nodiscard]] bool expect_syscall_eperm(
    const std::string_view operation,
    const long result,
    const int error_number) {
    if (result == -1L && error_number == EPERM) {
        return true;
    }
    std::cerr << "FAIL: " << operation << " must be rejected with EPERM, result=" << result
              << " errno=" << error_number << " (" << std::strerror(error_number) << ")\n";
    return false;
}

/**
 * @brief 在已 harden 的 child 中触发三条无害 module syscall 探针 / Run three harmless module-syscall probes in the hardened child.
 * @return 每条 syscall 都被 EPERM 拒绝时为 true / True when every syscall is rejected with EPERM.
 * @note `init_module` 使用空地址与零长度，`finit_module` 使用无效 FD，`delete_module` 使用空名；
 *       它们都不指向任何真实 module。 `init_module` uses a null address and zero length,
 *       `finit_module` uses an invalid FD, and `delete_module` uses an empty name; none names a real module.
 */
[[nodiscard]] bool verify_module_syscalls_are_denied() {
    /** @brief 三个探针的合并结果 / Accumulated result for the three probes. */
    bool valid = true;

    errno = 0;
    /** @brief 空 module image 的 init_module 返回值 / Return value for the null module-image init_module probe. */
    const long init_result = syscall(SYS_init_module, nullptr, 0UL, "");
    /** @brief init_module 调用后的 errno / errno after init_module. */
    const int init_errno = errno;
    valid = expect_syscall_eperm("init_module", init_result, init_errno) && valid;

    errno = 0;
    /** @brief 无效 FD 的 finit_module 返回值 / Return value for the invalid-FD finit_module probe. */
    const long finit_result = syscall(SYS_finit_module, -1, "", 0U);
    /** @brief finit_module 调用后的 errno / errno after finit_module. */
    const int finit_errno = errno;
    valid = expect_syscall_eperm("finit_module", finit_result, finit_errno) && valid;

    errno = 0;
    /** @brief 空 module 名的 delete_module 返回值 / Return value for the empty-name delete_module probe. */
    const long delete_result = syscall(SYS_delete_module, "", O_NONBLOCK);
    /** @brief delete_module 调用后的 errno / errno after delete_module. */
    const int delete_errno = errno;
    valid = expect_syscall_eperm("delete_module", delete_result, delete_errno) && valid;
    return valid;
}

/**
 * @brief 在已 harden 的 child 中触发无害 io_uring syscall 探针 / Run harmless io_uring syscall probes in the hardened child.
 * @return 当前 ABI 支持的每条 io_uring syscall 都被 EPERM 拒绝时为 true / True when every io_uring syscall supported by the ABI is rejected with EPERM.
 * @note 这些调用使用零 entries 或无效 FD，因此绝不创建 ring、更不提交 operation；其目的只是防止
 *       `IORING_OP_SOCKET` 等 operation 绕过 direct-syscall seccomp 策略。 These calls use zero
 *       entries or an invalid FD, so they never create a ring or submit an operation; they only
 *       ensure operations such as `IORING_OP_SOCKET` cannot bypass the direct-syscall policy.
 */
[[nodiscard]] bool verify_io_uring_syscalls_are_denied() {
    /** @brief 三个 io_uring 探针的合并结果 / Accumulated result for the io_uring probes. */
    bool valid = true;
#ifdef SYS_io_uring_setup
    errno = 0;
    /** @brief 零 entries 的 io_uring_setup 返回值 / Return value for the zero-entry io_uring_setup probe. */
    const long setup_result = syscall(SYS_io_uring_setup, 0U, nullptr);
    /** @brief io_uring_setup 调用后的 errno / errno after io_uring_setup. */
    const int setup_errno = errno;
    valid = expect_syscall_eperm("io_uring_setup", setup_result, setup_errno) && valid;
#endif
#ifdef SYS_io_uring_enter
    errno = 0;
    /** @brief 无效 FD 的 io_uring_enter 返回值 / Return value for the invalid-FD io_uring_enter probe. */
    const long enter_result = syscall(SYS_io_uring_enter, -1, 0U, 0U, 0U, nullptr, 0U);
    /** @brief io_uring_enter 调用后的 errno / errno after io_uring_enter. */
    const int enter_errno = errno;
    valid = expect_syscall_eperm("io_uring_enter", enter_result, enter_errno) && valid;
#endif
#ifdef SYS_io_uring_register
    errno = 0;
    /** @brief 无效 FD 的 io_uring_register 返回值 / Return value for the invalid-FD io_uring_register probe. */
    const long register_result = syscall(SYS_io_uring_register, -1, 0U, nullptr, 0U);
    /** @brief io_uring_register 调用后的 errno / errno after io_uring_register. */
    const int register_errno = errno;
    valid = expect_syscall_eperm("io_uring_register", register_result, register_errno) && valid;
#endif
    return valid;
}

/**
 * @brief 在已 harden 的 child 中触发无害 keyring syscall 探针 / Run harmless keyring-syscall probes in the hardened child.
 * @return 当前 ABI 支持的每条 keyring syscall 都被 EPERM 拒绝时为 true / True when every keyring syscall supported by the ABI is rejected with EPERM.
 * @note Linux keyring 并不随 mount/PID/network namespace 隔离。探针向两个 syscall 传入空
 *       type 指针，因此若 filter 意外回归，内核也只能返回参数错误，而不会创建 key、修改
 *       keyring 或触发 request-key helper。 Linux keyrings are not isolated by the mount,
 *       PID, or network namespaces. The probes pass a null type pointer to both syscalls, so a
 *       filter regression can only produce a parameter error; it cannot create a key, modify a
 *       keyring, or invoke a request-key helper.
 */
[[nodiscard]] bool verify_keyring_syscalls_are_denied() {
    /** @brief 两个 keyring 探针的合并结果 / Accumulated result for the keyring probes. */
    bool valid = true;
#ifdef SYS_add_key
    errno = 0;
    /** @brief 空 type 的 add_key 返回值 / Return value for the null-type add_key probe. */
    const long add_result = syscall(SYS_add_key, nullptr, nullptr, nullptr, 0U, -1);
    /** @brief add_key 调用后的 errno / errno after add_key. */
    const int add_errno = errno;
    valid = expect_syscall_eperm("add_key", add_result, add_errno) && valid;
#endif
#ifdef SYS_request_key
    errno = 0;
    /** @brief 空 type 的 request_key 返回值 / Return value for the null-type request_key probe. */
    const long request_result = syscall(SYS_request_key, nullptr, nullptr, nullptr, -1);
    /** @brief request_key 调用后的 errno / errno after request_key. */
    const int request_errno = errno;
    valid = expect_syscall_eperm("request_key", request_result, request_errno) && valid;
#endif
    return valid;
}

/**
 * @brief 阻止 payload 修改 XFS project quota 元数据 / Prevent the payload from modifying XFS project-quota metadata.
 * @return 所有能修改 project ID 或继承位的 ioctl 均被 EPERM 拒绝时为 true / True when every ioctl that can modify a project ID or inheritance bit is rejected with EPERM.
 * @note payload 会拥有其在 `/workspace` 创建的 inode；在 initial user namespace 中，inode
 *       owner 可以经此 ioctl 改变 `fsx_projid` 或 `FS_XFLAG_PROJINHERIT`。这里使用无效 FD
 *       和空参数，所以 filter 回归时最多返回 EBADF，不会改变任何 inode。 The payload owns
 *       inodes it creates under `/workspace`; in the initial user namespace an inode owner can use
 *       this ioctl to change `fsx_projid` or `FS_XFLAG_PROJINHERIT`. This probe uses an invalid FD
 *       and a null argument, so a filter regression can at most return EBADF and cannot modify an
 *       inode.
 */
[[nodiscard]] bool verify_project_quota_metadata_mutations_are_denied() {
    /** @brief 三个 project 元数据探针的合并结果 / Accumulated result for the project-metadata probes. */
    bool valid = true;
    errno = 0;
    /** @brief 无效 FD 的 FSSETXATTR 返回值 / Return value for the invalid-FD FSSETXATTR probe. */
    const int result = ioctl(-1, FS_IOC_FSSETXATTR, nullptr);
    /** @brief FSSETXATTR 调用后的 errno / errno after FSSETXATTR. */
    const int error_number = errno;
    valid = expect_syscall_eperm("ioctl(FS_IOC_FSSETXATTR)", result, error_number) && valid;

    errno = 0;
    /** @brief 无效 FD 的 native SETFLAGS 返回值 / Return value for the invalid-FD native SETFLAGS probe. */
    const int setflags_result = ioctl(-1, FS_IOC_SETFLAGS, nullptr);
    /** @brief native SETFLAGS 调用后的 errno / errno after native SETFLAGS. */
    const int setflags_errno = errno;
    valid = expect_syscall_eperm("ioctl(FS_IOC_SETFLAGS)", setflags_result, setflags_errno) && valid;

    errno = 0;
    /** @brief 无效 FD 的 compat SETFLAGS 返回值 / Return value for the invalid-FD compat SETFLAGS probe. */
    const int compat_setflags_result = ioctl(-1, FS_IOC32_SETFLAGS, nullptr);
    /** @brief compat SETFLAGS 调用后的 errno / errno after compat SETFLAGS. */
    const int compat_setflags_errno = errno;
    valid = expect_syscall_eperm("ioctl(FS_IOC32_SETFLAGS)", compat_setflags_result, compat_setflags_errno) && valid;
    return valid;
}

/**
 * @brief 在单独进程中执行不可逆 hardening / Execute irreversible hardening in an isolated child process.
 * @return 成功为 EXIT_SUCCESS，失败为 EXIT_FAILURE / EXIT_SUCCESS on success and EXIT_FAILURE on failure.
 */
[[nodiscard]] int run_hardened_child() {
    /** @brief native task hardening 的结果 / Result returned by native task hardening. */
    const auto hardened = wspctl::harden_task(kSandboxUid, kSandboxGid);
    if (!hardened) {
        std::cerr << "FAIL: harden_task failed: " << hardened.error().message << '\n';
        return EXIT_FAILURE;
    }
    /** @brief task hardening 后的 status 断言 / Status assertions after task hardening. */
    const bool status_valid = verify_hardened_status();
    /** @brief task hardening 后的 module syscall 断言 / Module-syscall assertions after task hardening. */
    const bool module_syscalls_valid = verify_module_syscalls_are_denied();
    /** @brief task hardening 后的 io_uring syscall 断言 / io_uring syscall assertions after task hardening. */
    const bool io_uring_syscalls_valid = verify_io_uring_syscalls_are_denied();
    /** @brief task hardening 后的 keyring syscall 断言 / Keyring-syscall assertions after task hardening. */
    const bool keyring_syscalls_valid = verify_keyring_syscalls_are_denied();
    /** @brief task hardening 后的 XFS project 元数据保护断言 / XFS project-metadata protection assertions after task hardening. */
    const bool quota_metadata_valid = verify_project_quota_metadata_mutations_are_denied();
    return status_valid && module_syscalls_valid && io_uring_syscalls_valid && keyring_syscalls_valid && quota_metadata_valid
        ? EXIT_SUCCESS
        : EXIT_FAILURE;
}

/**
 * @brief 等待 hardening child 并翻译其状态 / Wait for the hardening child and translate its status.
 * @param child_pid 已 fork 的子进程 PID / PID of the already-forked child.
 * @return child 成功时 EXIT_SUCCESS，否则 EXIT_FAILURE / EXIT_SUCCESS when the child succeeds, otherwise EXIT_FAILURE.
 */
[[nodiscard]] int wait_for_child(const pid_t child_pid) {
    /** @brief `waitpid` 填充的 child 状态 / Child status filled by `waitpid`. */
    int status = 0;
    while (waitpid(child_pid, &status, 0) < 0) {
        if (errno == EINTR) {
            continue;
        }
        std::cerr << "FAIL: waitpid failed: " << std::strerror(errno) << '\n';
        return EXIT_FAILURE;
    }
    if (!WIFEXITED(status) || WEXITSTATUS(status) != EXIT_SUCCESS) {
        std::cerr << "FAIL: hardened child did not exit successfully\n";
        return EXIT_FAILURE;
    }
    return EXIT_SUCCESS;
}

}  // namespace

/**
 * @brief privileged security CTest 入口 / Privileged security CTest entry point.
 * @return 成功为 0，可选环境 skip 为 77，失败为非零 / Zero on success, 77 for an optional skip, nonzero on failure.
 */
int main() {
    /** @brief 当前 privileged-test 要求 / Current privileged-test requirement. */
    const Requirement requirement = test_requirement();
    if (requirement == Requirement::invalid) {
        std::cerr << "FAIL: " << kRequireEnvironment << " accepts only 0 or 1\n";
        return EXIT_FAILURE;
    }
    if (geteuid() != 0U) {
        return unavailable(requirement, "effective UID is not root");
    }
    if (!has_effective_capability(CAP_SETUID) || !has_effective_capability(CAP_SETGID)) {
        return unavailable(requirement, "CAP_SETUID and CAP_SETGID are required for the real identity-drop path");
    }
    /** @brief 隔离不可逆 cap/seccomp 修改的 child PID / Child PID isolating irreversible capability/seccomp changes. */
    const pid_t child_pid = fork();
    if (child_pid < 0) {
        std::cerr << "FAIL: fork failed: " << std::strerror(errno) << '\n';
        return EXIT_FAILURE;
    }
    if (child_pid == 0) {
        const int child_status = run_hardened_child();
        std::cerr.flush();
        std::_Exit(child_status);
    }
    return wait_for_child(child_pid);
}
