/**
 * @file wspctl_privileged_e2e_tests.cpp
 * @brief wspctld 真实特权端到端验收 / Real privileged end-to-end acceptance for wspctld.
 *
 * 此测试不模拟 namespace、OverlayFS、cgroup 或 XFS project quota。它只在 operator 明确
 * 提供 disposable XFS state parent、只读且已 seal 的 image generation、私有 socket parent
 * 与可写 delegated cgroup parent 时运行；所有可删除对象都由本测试以 ``mkdtemp`` 创建。
 * This test does not mock namespaces, OverlayFS, cgroups, or XFS project quota. It runs only
 * when an operator explicitly provides a disposable XFS state parent, a readonly sealed image
 * generation, a private socket parent, and a writable delegated cgroup parent; every deletable
 * object is created by this test through ``mkdtemp``.
 */

#include "wspctl/infrastructure/image.hpp"
#include "wspctl/presentation/operator_gateway.hpp"
#include "wspctl/presentation/unix_gateway.hpp"

#include <openssl/sha.h>

#include <sys/capability.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <sys/statfs.h>
#include <sys/statvfs.h>
#include <sys/types.h>
#include <sys/wait.h>

#include <cerrno>
#include <charconv>
#include <chrono>
#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <limits>
#include <optional>
#include <span>
#include <string>
#include <string_view>
#include <thread>
#include <utility>
#include <vector>

#include <fcntl.h>
#include <grp.h>
#include <linux/magic.h>
#include <signal.h>
#include <unistd.h>

namespace {

/** @brief CTest 的标准 skip 返回码 / Conventional CTest skip return code. */
constexpr int kSkipReturnCode{77};
/** @brief CI 强制真实 E2E 验收的环境变量 / Environment variable requiring real E2E acceptance in CI. */
constexpr std::string_view kRequireEnvironment{"WSPCTL_REQUIRE_PRIVILEGED_E2E"};
/** @brief disposable XFS mount 的环境变量 / Environment variable for the disposable XFS mount. */
constexpr std::string_view kXfsMountEnvironment{"WSPCTL_PRIVILEGED_E2E_XFS_MOUNT"};
/** @brief disposable state parent 的环境变量 / Environment variable for the disposable state parent. */
constexpr std::string_view kStateParentEnvironment{"WSPCTL_PRIVILEGED_E2E_STATE_PARENT"};
/** @brief 仅供本测试创建 socket directory 的 parent / Parent used only to create this test's socket directory. */
constexpr std::string_view kSocketParentEnvironment{"WSPCTL_PRIVILEGED_E2E_SOCKET_PARENT"};
/** @brief delegated cgroup parent 的环境变量 / Environment variable for the delegated cgroup parent. */
constexpr std::string_view kCgroupParentEnvironment{"WSPCTL_PRIVILEGED_E2E_CGROUP_PARENT"};
/** @brief 受控 image 根的环境变量 / Environment variable for the controlled images root. */
constexpr std::string_view kImagesRootEnvironment{"WSPCTL_PRIVILEGED_E2E_IMAGES_ROOT"};
/** @brief 只读 sealed rootfs 的环境变量 / Environment variable for the readonly sealed rootfs. */
constexpr std::string_view kBaseRootEnvironment{"WSPCTL_PRIVILEGED_E2E_BASE_ROOT"};
/** @brief image-internal wsp-systemd 路径的可选环境变量 / Optional environment variable for the image-internal wsp-systemd path. */
constexpr std::string_view kSupervisorEnvironment{"WSPCTL_PRIVILEGED_E2E_SUPERVISOR"};
/** @brief 为本测试专属保留的 XFS project-ID 首值 / First XFS project ID reserved exclusively for this test. */
constexpr std::string_view kProjectIdMinEnvironment{"WSPCTL_PRIVILEGED_E2E_XFS_PROJECT_ID_MIN"};
/** @brief 为本测试专属保留的 XFS project-ID 末值 / Last XFS project ID reserved exclusively for this test. */
constexpr std::string_view kProjectIdMaxEnvironment{"WSPCTL_PRIVILEGED_E2E_XFS_PROJECT_ID_MAX"};
/** @brief native gateway 与 task 使用的非 root UID / Non-root UID used by the native gateway and task. */
constexpr uid_t kClientUid{65'532U};
/** @brief native gateway 与 task 使用的非 root GID / Non-root GID used by the native gateway and task. */
constexpr gid_t kClientGid{65'532U};
/** @brief 单个 runtime 的稳定 UUID / Stable UUID for the one runtime exercised by this test. */
constexpr std::string_view kRuntimeKey{"123e4567-e89b-42d3-a456-4266141740e2"};
/** @brief 首次 broker activation / First broker activation. */
constexpr std::string_view kFirstActivation{"e2e-activation-one"};
/** @brief restart 后的第二次 activation / Second activation after restart. */
constexpr std::string_view kSecondActivation{"e2e-activation-two"};
/** @brief 固定 opaque payload directory ID / Fixed opaque payload directory ID. */
constexpr std::string_view kPayloadOpaqueId{"e2e-payload"};
/** @brief add_file 应返回的唯一 workspace 路径 / Sole workspace path expected from add_file. */
constexpr std::string_view kPayloadWorkspacePath{"/workspace/uploads/e2e-payload/payload"};
/** @brief runtime control tree 的 hard byte quota / Hard byte quota for one runtime control tree. */
constexpr std::uint64_t kControlHardBytes{8U * 1024U * 1024U};
/** @brief runtime control tree 的 hard inode quota / Hard inode quota for one runtime control tree. */
constexpr std::uint64_t kControlHardInodes{512U};
/** @brief runtime workspace tree 的 hard byte quota / Hard byte quota for one runtime workspace tree. */
constexpr std::uint64_t kWorkspaceHardBytes{32U * 1024U * 1024U};
/** @brief runtime workspace tree 的 hard inode quota / Hard inode quota for one runtime workspace tree. */
constexpr std::uint64_t kWorkspaceHardInodes{1'024U};
/** @brief XFS 系统保留字节数 / Bytes reserved outside the test runtime admission. */
constexpr std::uint64_t kSystemReserveBytes{1U * 1024U * 1024U};
/** @brief XFS 系统保留 inode 数 / Inodes reserved outside the test runtime admission. */
constexpr std::uint64_t kSystemReserveInodes{256U};
/** @brief broker 启动等待上限 / Maximum wait for broker startup. */
constexpr auto kBrokerStartDeadline{std::chrono::seconds(15)};
/** @brief broker crash 后 cgroup 清空等待上限 / Maximum wait for cgroup drain after a broker crash. */
constexpr auto kCgroupDrainDeadline{std::chrono::seconds(10)};

/**
 * @brief 特权 E2E 的执行要求 / Execution requirement for the privileged E2E.
 */
enum class Requirement {
    /** @brief 缺少前置条件时可 skip / Missing prerequisites may skip. */
    optional,
    /** @brief 缺少前置条件时必须失败 / Missing prerequisites must fail. */
    required,
    /** @brief 环境变量拼写非法 / Environment variable spelling is invalid. */
    invalid,
};

/**
 * @brief operator 提供且已规范化的 E2E 输入 / Canonicalized E2E inputs provided by the operator.
 */
struct E2eEnvironment final {
    /** @brief disposable dedicated XFS mount / 一次性专用 XFS mount。 */
    std::filesystem::path xfs_mount;
    /** @brief state root 的测试专属 parent / Test-exclusive parent for the state root. */
    std::filesystem::path state_parent;
    /** @brief socket directory 的测试专属 parent / Test-exclusive parent for the socket directory. */
    std::filesystem::path socket_parent;
    /** @brief cgroup child 的 delegated parent / Delegated parent for the cgroup child. */
    std::filesystem::path cgroup_parent;
    /** @brief 受控 image generations 根 / Root of controlled image generations. */
    std::filesystem::path images_root;
    /** @brief readonly sealed rootfs / 只读且 sealed 的 rootfs。 */
    std::filesystem::path base_root;
    /** @brief rootfs 内 wsp-systemd 的绝对路径 / Absolute in-rootfs path to wsp-systemd. */
    std::string supervisor_path;
    /** @brief 测试独占 XFS range 的最小 project ID / Minimum project ID in the test-exclusive XFS range. */
    std::uint32_t project_id_min{};
    /** @brief 测试独占 XFS range 的最大 project ID / Maximum project ID in the test-exclusive XFS range. */
    std::uint32_t project_id_max{};
};

/**
 * @brief 一个 self-created test directory 的不可替换身份 / Non-replaceable identity of one self-created test directory.
 */
struct TestDirectoryIdentity final {
    /** @brief 创建时的所在 filesystem device / Filesystem device at creation. */
    dev_t device{};
    /** @brief 创建时的 inode / Inode at creation. */
    ino_t inode{};
};

/**
 * @brief 已创建且可安全清理的 test directory / Created test directory eligible for safe cleanup.
 */
struct CreatedTestDirectory final {
    /** @brief canonical child path / 已规范化 child 路径。 */
    std::filesystem::path path;
    /** @brief 防止 path replacement 的创建身份 / Creation identity preventing path replacement. */
    TestDirectoryIdentity identity;
};

/**
 * @brief 本测试自己创建的可清理路径 / Paths created by this test and eligible for cleanup.
 */
struct E2eFixture final {
    /** @brief 已加载的 operator 输入 / Loaded operator inputs. */
    E2eEnvironment environment;
    /** @brief self-created XFS state root / 本测试创建的 XFS state root。 */
    std::filesystem::path state_root;
    /** @brief state root 的创建身份 / State-root creation identity. */
    std::optional<TestDirectoryIdentity> state_root_identity;
    /** @brief self-created socket directory / 本测试创建的 socket directory。 */
    std::filesystem::path socket_directory;
    /** @brief socket directory 的创建身份 / Socket-directory creation identity. */
    std::optional<TestDirectoryIdentity> socket_directory_identity;
    /** @brief self-created cgroup subtree / 本测试创建的 cgroup subtree。 */
    std::filesystem::path cgroup_root;
    /** @brief cgroup subtree 的创建身份 / Cgroup-subtree creation identity. */
    std::optional<TestDirectoryIdentity> cgroup_root_identity;
    /** @brief 当前活跃 broker child PID / Current active broker child PID. */
    pid_t broker_pid{-1};
};

/**
 * @brief 一次实际 wspctld child 的启动描述 / Launch description for one real wspctld child.
 */
struct BrokerLaunch final {
    /** @brief broker 可执行文件的 host 路径 / Host path of the broker executable. */
    std::filesystem::path executable;
    /** @brief 本次实例的 Bot 专属 socket 路径 / Bot-exclusive socket path for this instance. */
    std::filesystem::path bot_socket_path;
    /** @brief 本次实例的 operator 专属 socket 路径 / Operator-exclusive socket path for this instance. */
    std::filesystem::path operator_socket_path;
};

/**
 * @brief 单次 add_file 的内存分块源 / In-memory chunk source for one add_file call.
 */
class OneChunkPayloadSource final : public wspctl::presentation::PayloadChunkSource {
public:
    /**
     * @brief 以 immutable payload 构造一次性 source / Construct a single-use source from immutable payload bytes.
     * @param bytes 待发送的完整 raw bytes / Complete raw bytes to send.
     */
    explicit OneChunkPayloadSource(std::vector<std::byte> bytes) : bytes_(std::move(bytes)) {}

    /**
     * @brief 返回唯一分块，再返回 EOF / Return the sole chunk, then EOF.
     * @return 唯一 raw chunk 或 EOF / The sole raw chunk or EOF.
     */
    [[nodiscard]] wspctl::Result<std::optional<std::vector<std::byte>>> next_chunk() override {
        if (emitted_) {
            return std::optional<std::vector<std::byte>>{};
        }
        emitted_ = true;
        return std::optional<std::vector<std::byte>>{std::move(bytes_)};
    }

private:
    /** @brief 尚未发出的 payload bytes / Payload bytes not yet emitted. */
    std::vector<std::byte> bytes_;
    /** @brief 是否已经返回过唯一分块 / Whether the sole chunk was already returned. */
    bool emitted_{false};
};

/**
 * @brief 解析特权 E2E 的严格 required/optional 语义 / Parse strict required/optional semantics for privileged E2E.
 * @return optional、required 或 invalid / Optional, required, or invalid.
 */
[[nodiscard]] Requirement test_requirement() {
    /** @brief 原始环境值 / Raw environment value. */
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
 * @brief 以 CTest 语义报告尚未具备的前置条件 / Report an unavailable prerequisite with CTest semantics.
 * @param requirement 当前 required/optional 要求 / Current required/optional requirement.
 * @param reason 缺失原因 / Reason the prerequisite is unavailable.
 * @return optional 时 77，required 时失败 / 77 when optional, failure when required.
 */
[[nodiscard]] int unavailable(const Requirement requirement, const std::string_view reason) {
    if (requirement == Requirement::required) {
        std::cerr << "FAIL: " << kRequireEnvironment << "=1 but privileged E2E cannot run: " << reason << '\n';
        return EXIT_FAILURE;
    }
    std::cerr << "SKIP: privileged E2E unavailable: " << reason << '\n';
    return kSkipReturnCode;
}

/**
 * @brief 取得一个 effective Linux capability / Read one effective Linux capability.
 * @param capability 要查询的 capability / Capability to query.
 * @return 当前有效集包含该 capability 时为真 / True when the effective set contains the capability.
 */
[[nodiscard]] bool has_effective_capability(const cap_value_t capability) {
    /** @brief 当前进程 capability set / Current process capability set. */
    cap_t capabilities = cap_get_proc();
    if (capabilities == nullptr) {
        return false;
    }
    /** @brief 查询到的 effective flag / Queried effective flag. */
    cap_flag_value_t enabled = CAP_CLEAR;
    /** @brief libcap 查询状态 / libcap query status. */
    const int status = cap_get_flag(capabilities, capability, CAP_EFFECTIVE, &enabled);
    cap_free(capabilities);
    return status == 0 && enabled == CAP_SET;
}

/**
 * @brief 读取非空环境变量 / Read a nonempty environment variable.
 * @param name 环境变量名 / Environment variable name.
 * @return 非空文本或空值 / Nonempty text or no value.
 */
[[nodiscard]] std::optional<std::string> environment_text(const std::string_view name) {
    /** @brief 原始环境值 / Raw environment value. */
    const char* const value = std::getenv(name.data());
    if (value == nullptr || std::string_view(value).empty()) {
        return std::nullopt;
    }
    return std::string(value);
}

/**
 * @brief 规范化已有绝对目录 / Canonicalize an existing absolute directory.
 * @param value operator 提供的路径 / Operator-provided path.
 * @param description 诊断语义 / Diagnostic purpose.
 * @param reason 输出失败原因 / Output failure reason.
 * @return canonical directory 或空值 / Canonical directory or no value.
 */
[[nodiscard]] std::optional<std::filesystem::path> canonical_directory(
    const std::string_view value,
    const std::string_view description,
    std::string& reason) {
    /** @brief 原始 path 对象 / Raw path object. */
    const std::filesystem::path candidate(value);
    if (!candidate.is_absolute()) {
        reason = std::string(description) + " must be an absolute directory";
        return std::nullopt;
    }
    /** @brief canonicalization error / 规范化错误。 */
    std::error_code error;
    const std::filesystem::path canonical = std::filesystem::canonical(candidate, error);
    if (error) {
        reason = std::string(description) + " cannot be canonicalized: " + error.message();
        return std::nullopt;
    }
    /** @brief directory metadata / 目录元数据。 */
    struct stat metadata {};
    if (lstat(canonical.c_str(), &metadata) != 0 || !S_ISDIR(metadata.st_mode)) {
        reason = std::string(description) + " must name an existing directory";
        return std::nullopt;
    }
    return canonical;
}

/**
 * @brief 验证 operator parent 是 root-owned 且不可 group/other 写 / Validate an operator parent is root-owned and not group/other writable.
 * @param parent 已 canonical 的 parent / Canonical parent.
 * @param description 诊断语义 / Diagnostic purpose.
 * @param reason 输出失败原因 / Output failure reason.
 * @return 满足所有权契约时为真 / True when the ownership contract holds.
 */
[[nodiscard]] bool validate_private_parent(
    const std::filesystem::path& parent,
    const std::string_view description,
    std::string& reason) {
    if (parent == parent.root_path()) {
        reason = std::string(description) + " cannot be the host root directory";
        return false;
    }
    /** @brief parent metadata / parent 元数据。 */
    struct stat metadata {};
    if (lstat(parent.c_str(), &metadata) != 0 || !S_ISDIR(metadata.st_mode) || metadata.st_uid != 0U ||
        (metadata.st_mode & (S_IWGRP | S_IWOTH)) != 0) {
        reason = std::string(description) + " must be a root-owned non-group/world-writable directory";
        return false;
    }
    return true;
}

/**
 * @brief 判断 canonical child 是否位于 canonical parent 内 / Check whether a canonical child lies within a canonical parent.
 * @param child 已规范化的待检查 child / Canonical child to inspect.
 * @param parent 已规范化的祖先目录 / Canonical ancestor directory.
 * @return child 等于或位于 parent 下时为真 / True when child equals or lies below parent.
 */
[[nodiscard]] bool is_below_or_equal(const std::filesystem::path& child, const std::filesystem::path& parent) {
    /** @brief child 的当前 path component / Current path component of the child. */
    auto child_component = child.begin();
    /** @brief parent 的当前 path component / Current path component of the parent. */
    auto parent_component = parent.begin();
    while (parent_component != parent.end()) {
        if (child_component == child.end() || *child_component != *parent_component) {
            return false;
        }
        ++child_component;
        ++parent_component;
    }
    return true;
}

/**
 * @brief 在创建任何 child 前验证 state parent 与 XFS quota mount 同属一个专用 XFS / Validate state parent and XFS quota mount before creating children.
 * @param environment 已 canonical 的 operator E2E 输入 / Canonicalized operator E2E inputs.
 * @param reason 失败时的 operator 可操作诊断 / Operator-actionable diagnostic on failure.
 * @return XFS containment、superblock 与专用 mount 契约满足时为真 / True when XFS containment, superblock, and dedicated-mount contracts hold.
 * @note 这里是 E2E preflight，而不是替代 broker 的 production preflight；它保证本测试不会在错误
 *       filesystem 创建可配额 state child。 This is E2E preflight, not a substitute for the
 *       broker's production preflight; it prevents this test from creating quota state on the
 *       wrong filesystem.
 */
[[nodiscard]] bool validate_xfs_state_parent_contract(const E2eEnvironment& environment, std::string& reason) {
    if (!is_below_or_equal(environment.state_parent, environment.xfs_mount)) {
        reason = "WSPCTL_PRIVILEGED_E2E_STATE_PARENT must lie below WSPCTL_PRIVILEGED_E2E_XFS_MOUNT";
        return false;
    }
    if (environment.xfs_mount == environment.xfs_mount.root_path()) {
        reason = "WSPCTL_PRIVILEGED_E2E_XFS_MOUNT must be a dedicated non-root mountpoint";
        return false;
    }
    /** @brief XFS mount 与 state parent 的 filesystem metadata / Filesystem metadata for XFS mount and state parent. */
    struct statfs mount_filesystem {};
    struct statfs state_filesystem {};
    /** @brief XFS mount 的容量/只读标志 / Capacity and readonly flags for the XFS mount. */
    struct statvfs mount_capacity {};
    /** @brief mount、mount parent 与 state parent 的 device metadata / Device metadata for mount, mount parent, and state parent. */
    struct stat mount_metadata {};
    struct stat mount_parent_metadata {};
    struct stat state_metadata {};
    if (statfs(environment.xfs_mount.c_str(), &mount_filesystem) != 0 ||
        statfs(environment.state_parent.c_str(), &state_filesystem) != 0 ||
        statvfs(environment.xfs_mount.c_str(), &mount_capacity) != 0 ||
        stat(environment.xfs_mount.c_str(), &mount_metadata) != 0 ||
        stat(environment.xfs_mount.parent_path().c_str(), &mount_parent_metadata) != 0 ||
        stat(environment.state_parent.c_str(), &state_metadata) != 0) {
        reason = "cannot inspect privileged E2E XFS mount/state parent: " + std::string(std::strerror(errno));
        return false;
    }
    if (mount_filesystem.f_type != XFS_SUPER_MAGIC || state_filesystem.f_type != XFS_SUPER_MAGIC ||
        (mount_capacity.f_flag & ST_RDONLY) != 0U) {
        reason = "WSPCTL_PRIVILEGED_E2E_XFS_MOUNT and STATE_PARENT must be on a writable XFS filesystem";
        return false;
    }
    if (mount_metadata.st_dev == mount_parent_metadata.st_dev) {
        reason = "WSPCTL_PRIVILEGED_E2E_XFS_MOUNT must itself be a dedicated mountpoint";
        return false;
    }
    if (mount_metadata.st_dev != state_metadata.st_dev ||
        std::memcmp(&mount_filesystem.f_fsid, &state_filesystem.f_fsid, sizeof(mount_filesystem.f_fsid)) != 0) {
        reason = "WSPCTL_PRIVILEGED_E2E_STATE_PARENT must share the XFS superblock/fsid of XFS_MOUNT";
        return false;
    }
    return true;
}

/**
 * @brief 严格解析无符号 32-bit 环境值 / Strictly parse an unsigned 32-bit environment value.
 * @param name 环境变量名 / Environment variable name.
 * @param reason 输出失败原因 / Output failure reason.
 * @param missing 输出是否仅为缺失变量 / Whether the failure is only a missing variable.
 * @return 已解析值或空值 / Parsed value or no value.
 */
[[nodiscard]] std::optional<std::uint32_t> environment_u32(
    const std::string_view name,
    std::string& reason,
    bool& missing) {
    const std::optional<std::string> value = environment_text(name);
    if (!value.has_value()) {
        missing = true;
        reason = "set " + std::string(name);
        return std::nullopt;
    }
    /** @brief 临时 64-bit 解析槽 / Temporary 64-bit parse slot. */
    std::uint64_t parsed{};
    /** @brief `from_chars` 的结束指针与错误 / End pointer and error from `from_chars`. */
    const auto [end, error] = std::from_chars(value->data(), value->data() + value->size(), parsed);
    if (error != std::errc{} || end != value->data() + value->size() ||
        parsed > static_cast<std::uint64_t>(std::numeric_limits<std::uint32_t>::max())) {
        reason = std::string(name) + " must be a uint32 decimal value";
        return std::nullopt;
    }
    return static_cast<std::uint32_t>(parsed);
}

/**
 * @brief 加载并核验 operator 输入的环境契约 / Load and validate the operator input contract.
 * @param reason 输出失败原因 / Output failure reason.
 * @param missing 输出是否只是缺失前置变量 / Whether failure is only a missing prerequisite.
 * @return 完整环境或空值 / Complete environment or no value.
 */
[[nodiscard]] std::optional<E2eEnvironment> load_environment(std::string& reason, bool& missing) {
    /** @brief 所有必须存在的路径变量 / All required path variables. */
    constexpr std::array<std::pair<std::string_view, std::string_view>, 6U> kPathVariables{{
        {kXfsMountEnvironment, "WSPCTL privileged E2E XFS mount"},
        {kStateParentEnvironment, "WSPCTL privileged E2E state parent"},
        {kSocketParentEnvironment, "WSPCTL privileged E2E socket parent"},
        {kCgroupParentEnvironment, "WSPCTL privileged E2E cgroup parent"},
        {kImagesRootEnvironment, "WSPCTL privileged E2E images root"},
        {kBaseRootEnvironment, "WSPCTL privileged E2E base root"},
    }};
    /** @brief canonical path slots in the same order / 同顺序的 canonical path 槽。 */
    std::array<std::filesystem::path, kPathVariables.size()> paths{};
    for (std::size_t index = 0U; index < kPathVariables.size(); ++index) {
        const auto& [name, description] = kPathVariables[index];
        const std::optional<std::string> value = environment_text(name);
        if (!value.has_value()) {
            missing = true;
            reason = "set " + std::string(name);
            return std::nullopt;
        }
        const auto path = canonical_directory(*value, description, reason);
        if (!path.has_value()) {
            return std::nullopt;
        }
        paths[index] = *path;
    }
    if (!validate_private_parent(paths[1], "WSPCTL privileged E2E state parent", reason) ||
        !validate_private_parent(paths[2], "WSPCTL privileged E2E socket parent", reason)) {
        return std::nullopt;
    }
    if (!paths[3].string().starts_with("/sys/fs/cgroup/")) {
        reason = "WSPCTL privileged E2E cgroup parent must lie below /sys/fs/cgroup";
        return std::nullopt;
    }
    const auto project_min = environment_u32(kProjectIdMinEnvironment, reason, missing);
    if (!project_min.has_value()) {
        return std::nullopt;
    }
    const auto project_max = environment_u32(kProjectIdMaxEnvironment, reason, missing);
    if (!project_max.has_value()) {
        return std::nullopt;
    }
    if (*project_min == 0U || (*project_min & 1U) != 0U || (*project_max & 1U) == 0U ||
        *project_max < *project_min + 1U) {
        reason = "privileged E2E XFS project-ID range must be a nonzero even-to-odd complete pair";
        return std::nullopt;
    }
    /** @brief 可选 in-image supervisor 路径 / Optional in-image supervisor path. */
    const std::string supervisor = environment_text(kSupervisorEnvironment).value_or("/libexec/wspctl/wsp-systemd");
    if (supervisor.empty() || supervisor.front() != '/' || supervisor.find('\0') != std::string::npos) {
        reason = "WSPCTL_PRIVILEGED_E2E_SUPERVISOR must be an absolute in-image path";
        return std::nullopt;
    }
    return E2eEnvironment{
        .xfs_mount = paths[0],
        .state_parent = paths[1],
        .socket_parent = paths[2],
        .cgroup_parent = paths[3],
        .images_root = paths[4],
        .base_root = paths[5],
        .supervisor_path = supervisor,
        .project_id_min = *project_min,
        .project_id_max = *project_max,
    };
}

/**
 * @brief 验证 delegated cgroup parent 暴露必要控制文件 / Verify a delegated cgroup parent exposes required control files.
 * @param parent 已 canonical 的 cgroup parent / Canonical cgroup parent.
 * @return 可创建并委派 test child 时为真 / True when a test child can be created and delegated.
 */
[[nodiscard]] bool cgroup_parent_is_usable(const std::filesystem::path& parent) {
    /** @brief 必须可读的 controller 列表文件 / Controller list file that must be readable. */
    std::ifstream controllers(parent / "cgroup.controllers");
    if (!controllers.is_open()) {
        return false;
    }
    /** @brief controller 文本 / Controller text. */
    std::string controller_text((std::istreambuf_iterator<char>(controllers)), std::istreambuf_iterator<char>());
    for (const std::string_view controller : {"cpu", "memory", "pids", "io"}) {
        if (controller_text.find(controller) == std::string::npos) {
            return false;
        }
    }
    for (const std::string_view file : {
             "cgroup.procs",
             "cgroup.subtree_control",
             "cgroup.kill",
             "memory.high",
             "memory.swap.max",
             "memory.oom.group",
             "io.weight",
         }) {
        if (access((parent / file).c_str(), W_OK) != 0) {
            return false;
        }
    }
    return true;
}

/**
 * @brief 在已验证 parent 下创建一个唯一目录 / Create one unique directory below a verified parent.
 * @param parent 已验证、不可删除的 parent / Verified parent that must never be deleted.
 * @param prefix self-created child 的固定前缀 / Fixed prefix for the self-created child.
 * @param mode 创建后显式收紧/开放的 mode / Mode explicitly applied after creation.
 * @param apply_mode 是否在创建后调用 chmod；cgroupfs child 不应 chmod / Whether to call chmod after creation; a cgroupfs child must not be chmod'ed.
 * @param reason 输出失败原因 / Output failure reason.
 * @return canonical self-created child 与其 inode identity，或空值 / Canonical self-created child and its inode identity, or no value.
 */
[[nodiscard]] std::optional<CreatedTestDirectory> create_test_directory(
    const std::filesystem::path& parent,
    const std::string_view prefix,
    const mode_t mode,
    const bool apply_mode,
    std::string& reason) {
    /** @brief mkdtemp 消费的 NUL-terminated template / NUL-terminated template consumed by mkdtemp. */
    std::string template_path = (parent / (std::string(prefix) + "XXXXXX")).string();
    template_path.push_back('\0');
    /** @brief mkdtemp 返回的已创建目录 / Directory created by mkdtemp. */
    char* const created = mkdtemp(template_path.data());
    if (created == nullptr) {
        reason = "mkdtemp failed: " + std::string(std::strerror(errno));
        return std::nullopt;
    }
    const std::filesystem::path created_path(created);
    if (apply_mode && chmod(created_path.c_str(), mode) != 0) {
        reason = "chmod self-created test directory failed: " + std::string(std::strerror(errno));
        static_cast<void>(rmdir(created_path.c_str()));
        return std::nullopt;
    }
    /** @brief canonicalization error / 规范化错误。 */
    std::error_code error;
    const std::filesystem::path canonical = std::filesystem::canonical(created_path, error);
    /** @brief canonical child metadata / 已规范化 child 元数据。 */
    struct stat metadata {};
    if (error || canonical.parent_path() != parent || !canonical.filename().string().starts_with(prefix) ||
        lstat(canonical.c_str(), &metadata) != 0 || !S_ISDIR(metadata.st_mode) || metadata.st_uid != 0U) {
        reason = "mkdtemp did not create the expected self-owned child";
        static_cast<void>(rmdir(created_path.c_str()));
        return std::nullopt;
    }
    return CreatedTestDirectory{
        .path = canonical,
        .identity = TestDirectoryIdentity{.device = metadata.st_dev, .inode = metadata.st_ino},
    };
}

/**
 * @brief 在 self-created socket root 中建立一个固定权限的 endpoint 子目录 / Create a fixed-mode endpoint child beneath a self-created socket root.
 * @param parent 本测试创建、root-owned 且不可被他人写的 socket root / Test-created root-owned socket root not writable by others.
 * @param leaf 固定单分量 endpoint 目录名 / Fixed single-component endpoint-directory name.
 * @param mode endpoint 目录期望的 POSIX mode / Required POSIX mode of the endpoint directory.
 * @param reason 失败时的可操作诊断 / Actionable diagnostic on failure.
 * @return 已建立的 endpoint 目录或空值 / Created endpoint directory or no value.
 * @note 这不是 production directory factory；它只服务于本测试已经控制的 parent，目的是让真实
 *       broker 经过与部署相同的 Bot/operator sibling-directory preflight。/ This is not a
 *       production directory factory. It only serves a parent already controlled by this test,
 *       so the real broker traverses the same Bot/operator sibling-directory preflight as deployment.
 */
[[nodiscard]] std::optional<std::filesystem::path> create_endpoint_directory(
    const std::filesystem::path& parent,
    const std::string_view leaf,
    const mode_t mode,
    std::string& reason) {
    if (leaf.empty() || leaf == "." || leaf == ".." || leaf.find('/') != std::string_view::npos ||
        leaf.find('\0') != std::string_view::npos) {
        reason = "test endpoint directory leaf is not a safe single path component";
        return std::nullopt;
    }
    const std::filesystem::path child = parent / std::string(leaf);
    if (mkdir(child.c_str(), mode) != 0) {
        reason = "mkdir test endpoint directory failed: " + std::string(std::strerror(errno));
        return std::nullopt;
    }
    if (chmod(child.c_str(), mode) != 0) {
        reason = "chmod test endpoint directory failed: " + std::string(std::strerror(errno));
        static_cast<void>(rmdir(child.c_str()));
        return std::nullopt;
    }
    struct stat metadata {};
    if (lstat(child.c_str(), &metadata) != 0 || !S_ISDIR(metadata.st_mode) || metadata.st_uid != 0U ||
        (metadata.st_mode & 0777U) != mode) {
        reason = "test endpoint directory does not have the requested root-owned mode";
        static_cast<void>(rmdir(child.c_str()));
        return std::nullopt;
    }
    return child;
}

/**
 * @brief 仅删除严格匹配 self-created contract 的普通目录树 / Remove only a directory tree matching the self-created contract.
 * @param parent operator 给定且绝不删除的 parent / Operator-provided parent that is never deleted.
 * @param child 测试创建的精确 child / Exact child created by this test.
 * @param prefix 测试 child 的固定前缀 / Fixed prefix for the test child.
 * @param identity 创建时记录的 device/inode identity / Device/inode identity recorded at creation.
 * @param report 是否输出 cleanup 诊断 / Whether to report cleanup diagnostics.
 * @return 删除成功时为真 / True when deletion succeeds.
 */
[[nodiscard]] bool remove_test_directory(
    const std::filesystem::path& parent,
    const std::filesystem::path& child,
    const std::string_view prefix,
    const std::optional<TestDirectoryIdentity>& identity,
    const bool report) {
    if (child.empty()) {
        return true;
    }
    if (!identity.has_value() || child.parent_path() != parent || !child.filename().string().starts_with(prefix)) {
        if (report) {
            std::cerr << "FAIL: refusing to delete a non-test directory\n";
        }
        return false;
    }
    /** @brief child metadata / child 元数据。 */
    struct stat metadata {};
    if (lstat(child.c_str(), &metadata) != 0) {
        if (errno == ENOENT) {
            return true;
        }
        if (report) {
            std::cerr << "FAIL: cannot inspect self-created test directory before cleanup\n";
        }
        return false;
    }
    if (!S_ISDIR(metadata.st_mode) || metadata.st_uid != 0U || metadata.st_dev != identity->device ||
        metadata.st_ino != identity->inode) {
        if (report) {
            std::cerr << "FAIL: refusing to delete a changed self-created test directory\n";
        }
        return false;
    }
    /** @brief filesystem cleanup error / 文件系统 cleanup 错误。 */
    std::error_code error;
    static_cast<void>(std::filesystem::remove_all(child, error));
    if (error) {
        if (report) {
            std::cerr << "FAIL: remove self-created test directory: " << error.message() << '\n';
        }
        return false;
    }
    return true;
}

/**
 * @brief 将文字 SHA-256 渲染为小写十六进制 / Render a text SHA-256 in lowercase hexadecimal.
 * @param value 待哈希文本 / Text to hash.
 * @return 64-character lowercase digest / 64-character lowercase digest.
 */
[[nodiscard]] std::string sha256_hex(const std::string_view value) {
    /** @brief OpenSSL digest bytes / OpenSSL 摘要 bytes。 */
    std::array<unsigned char, SHA256_DIGEST_LENGTH> digest{};
    SHA256(reinterpret_cast<const unsigned char*>(value.data()), value.size(), digest.data());
    /** @brief 十六进制字符表 / Hexadecimal digit table. */
    constexpr std::string_view kDigits{"0123456789abcdef"};
    /** @brief 渲染后的 SHA-256 / Rendered SHA-256. */
    std::string output;
    output.reserve(digest.size() * 2U);
    for (const unsigned char byte : digest) {
        output.push_back(kDigits[(byte >> 4U) & 0x0fU]);
        output.push_back(kDigits[byte & 0x0fU]);
    }
    return output;
}

/**
 * @brief 将 ASCII 文本转为 raw payload bytes / Convert ASCII text into raw payload bytes.
 * @param value 待转换文本 / Text to convert.
 * @return 等值 raw bytes / Equivalent raw bytes.
 */
[[nodiscard]] std::vector<std::byte> payload_bytes(const std::string_view value) {
    /** @brief 输出 bytes / Output bytes. */
    std::vector<std::byte> output;
    output.reserve(value.size());
    for (const unsigned char character : value) {
        output.push_back(static_cast<std::byte>(character));
    }
    return output;
}

/**
 * @brief 读取 cgroup.events 的 populated 状态 / Read the populated state in cgroup.events.
 * @param cgroup_root 要检查的 cgroup 根 / Cgroup root to inspect.
 * @return populated 0 时为真 / True when populated is zero.
 */
[[nodiscard]] bool cgroup_is_empty(const std::filesystem::path& cgroup_root) {
    /** @brief events 输入流 / Events input stream. */
    std::ifstream input(cgroup_root / "cgroup.events");
    if (!input.is_open()) {
        return false;
    }
    /** @brief 单个 events 行 / One events line. */
    std::string line;
    while (std::getline(input, line)) {
        if (line == "populated 0") {
            return true;
        }
    }
    return false;
}

/**
 * @brief 等待 self-created cgroup subtree 没有进程 / Wait until the self-created cgroup subtree has no processes.
 * @param cgroup_root self-created cgroup root / Self-created cgroup root.
 * @param deadline 最大等待时长 / Maximum wait duration.
 * @return cgroup 已空时为真 / True when the cgroup becomes empty.
 */
[[nodiscard]] bool wait_for_empty_cgroup(
    const std::filesystem::path& cgroup_root,
    const std::chrono::steady_clock::duration deadline) {
    if (cgroup_root.empty()) {
        return true;
    }
    /** @brief 绝对截止时刻 / Absolute deadline. */
    const auto until = std::chrono::steady_clock::now() + deadline;
    do {
        if (cgroup_is_empty(cgroup_root)) {
            return true;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
    } while (std::chrono::steady_clock::now() < until);
    return cgroup_is_empty(cgroup_root);
}

/**
 * @brief 终止并等待实际 broker child / Terminate and wait for a real broker child.
 * @param fixture 包含 broker PID 的 fixture / Fixture holding the broker PID.
 * @param report 是否输出诊断 / Whether to output diagnostics.
 * @return child 已经退出时为真 / True when the child has exited.
 */
[[nodiscard]] bool stop_broker(E2eFixture& fixture, const bool report) {
    if (fixture.broker_pid <= 0) {
        return true;
    }
    /** @brief 待停止 broker PID / Broker PID to stop. */
    const pid_t broker_pid = fixture.broker_pid;
    if (kill(broker_pid, SIGTERM) != 0 && errno != ESRCH) {
        if (report) {
            std::cerr << "FAIL: SIGTERM wspctld failed: " << std::strerror(errno) << '\n';
        }
        return false;
    }
    /** @brief broker child wait status / Broker child wait status. */
    int status{};
    pid_t waited = -1;
    do {
        waited = waitpid(broker_pid, &status, 0);
    } while (waited < 0 && errno == EINTR);
    fixture.broker_pid = -1;
    if (waited != broker_pid) {
        if (report) {
            std::cerr << "FAIL: waitpid wspctld failed: " << std::strerror(errno) << '\n';
        }
        return false;
    }
    if (!WIFSIGNALED(status) && !WIFEXITED(status)) {
        if (report) {
            std::cerr << "FAIL: wspctld did not reach a terminal status\n";
        }
        return false;
    }
    return true;
}

/**
 * @brief 移除本测试精确创建的 cgroup 层级 / Remove the exact cgroup hierarchy created by this test.
 * @param fixture 含 self-created cgroup root 的 fixture / Fixture containing the self-created cgroup root.
 * @param report 是否输出诊断 / Whether to output diagnostics.
 * @return 所有已知 test cgroup 目录均已移除时为真 / True when every known test cgroup directory is removed.
 * @note 此函数只尝试固定的 broker-owned names；遇到未知 child 时会失败而不是递归删除。
 *       This function only attempts fixed broker-owned names; an unknown child causes failure rather than recursive deletion.
 */
[[nodiscard]] bool remove_test_cgroup_hierarchy(const E2eFixture& fixture, const bool report) {
    if (fixture.cgroup_root.empty()) {
        return true;
    }
    /** @brief 先确认 cgroup root 仍是 mkdtemp 创建的那个 inode / Verify the cgroup root is still the mkdtemp-created inode before any rmdir. */
    struct stat root_metadata {};
    if (!fixture.cgroup_root_identity.has_value() || lstat(fixture.cgroup_root.c_str(), &root_metadata) != 0 ||
        !S_ISDIR(root_metadata.st_mode) || root_metadata.st_dev != fixture.cgroup_root_identity->device ||
        root_metadata.st_ino != fixture.cgroup_root_identity->inode) {
        if (report) {
            std::cerr << "FAIL: refusing to remove a replaced or non-test cgroup root\n";
        }
        return false;
    }
    /** @brief runtime hash directory name / Runtime hash directory name. */
    const std::string runtime_hash = sha256_hex(kRuntimeKey);
    /** @brief runtime cgroup directory / Runtime cgroup directory. */
    const std::filesystem::path runtime = fixture.cgroup_root / "wspctl" / runtime_hash;
    /** @brief 仅允许删除的自创建 cgroup 目录，按 leaf-to-root 排序 / Only self-created cgroup directories permitted for deletion, leaf-to-root. */
    const std::array<std::filesystem::path, 6U> directories{{
        runtime / "task",
        runtime / "supervisor",
        runtime,
        fixture.cgroup_root / "wspctl",
        fixture.cgroup_root / "wspctl-manager",
        fixture.cgroup_root,
    }};
    /** @brief 所有目录清理结果 / Aggregate cleanup result. */
    bool success = true;
    for (const std::filesystem::path& directory : directories) {
        if (rmdir(directory.c_str()) == 0 || errno == ENOENT) {
            continue;
        }
        success = false;
        if (report) {
            std::cerr << "FAIL: cannot remove self-created cgroup directory " << directory << ": "
                      << std::strerror(errno) << '\n';
        }
    }
    return success;
}

/**
 * @brief 清理所有 self-created fixture 资源 / Clean up every self-created fixture resource.
 * @param fixture 测试 fixture / Test fixture.
 * @param report 是否输出 cleanup 诊断 / Whether to output cleanup diagnostics.
 * @return 全部资源已经安全清理时为真 / True when all resources are safely cleaned.
 */
[[nodiscard]] bool cleanup_fixture(E2eFixture& fixture, const bool report) {
    /** @brief 聚合 cleanup 结果 / Aggregate cleanup result. */
    bool success = stop_broker(fixture, report);
    if (!wait_for_empty_cgroup(fixture.cgroup_root, kCgroupDrainDeadline)) {
        success = false;
        if (report) {
            std::cerr << "FAIL: self-created cgroup subtree remained populated after broker shutdown\n";
        }
    }
    success = remove_test_cgroup_hierarchy(fixture, report) && success;
    success = remove_test_directory(
                  fixture.environment.socket_parent,
                  fixture.socket_directory,
                  "wspctl-e2e-socket-",
                  fixture.socket_directory_identity,
                  report) &&
              success;
    success = remove_test_directory(
                  fixture.environment.state_parent,
                  fixture.state_root,
                  "wspctl-e2e-state-",
                  fixture.state_root_identity,
                  report) &&
              success;
    return success;
}

/**
 * @brief 异常路径下仍清理 self-created fixture 的 RAII guard / RAII guard cleaning self-created fixture paths on exceptional paths.
 */
class FixtureCleanupGuard final {
public:
    /**
     * @brief 绑定一个 fixture / Bind one fixture.
     * @param fixture 要在析构时清理的 fixture / Fixture to clean at destruction.
     */
    explicit FixtureCleanupGuard(E2eFixture& fixture) : fixture_(fixture) {}

    /** @brief 禁止复制 / Copying is forbidden. */
    FixtureCleanupGuard(const FixtureCleanupGuard&) = delete;
    /** @brief 禁止复制赋值 / Copy assignment is forbidden. */
    FixtureCleanupGuard& operator=(const FixtureCleanupGuard&) = delete;

    /**
     * @brief 显式完成并报告 cleanup / Explicitly complete and report cleanup.
     * @return cleanup 成功时为真 / True when cleanup succeeds.
     */
    [[nodiscard]] bool finish() {
        if (!active_) {
            return true;
        }
        active_ = false;
        return cleanup_fixture(fixture_, true);
    }

    /** @brief 析构时尽力清理，不覆盖原始失败 / Best-effort cleanup on destruction without masking original failure. */
    ~FixtureCleanupGuard() {
        if (active_) {
            static_cast<void>(cleanup_fixture(fixture_, false));
        }
    }

private:
    /** @brief 被守卫的 fixture / Guarded fixture. */
    E2eFixture& fixture_;
    /** @brief 是否尚需析构 cleanup / Whether destructor cleanup remains required. */
    bool active_{true};
};

/**
 * @brief fork/exec 一个真实 wspctld 进程 / Fork and exec one real wspctld process.
 * @param fixture 包含实际 state/cgroup 路径的 fixture / Fixture holding actual state and cgroup paths.
 * @param launch broker executable 与 socket 描述 / Broker executable and socket description.
 * @return 成功 fork 时为真 / True when fork succeeds.
 */
[[nodiscard]] bool launch_broker(E2eFixture& fixture, const BrokerLaunch& launch) {
    /** @brief 传给 broker 的完整 argv 文本 / Complete argv text passed to broker. */
    std::vector<std::string> arguments{
        launch.executable.string(),
        "--socket", launch.bot_socket_path.string(),
        "--operator-socket", launch.operator_socket_path.string(),
        "--state-root", fixture.state_root.string(),
        "--base-root", fixture.environment.base_root.string(),
        "--images-root", fixture.environment.images_root.string(),
        "--client-uid", std::to_string(kClientUid),
        "--operator-uid", "0",
        "--cgroup-root", fixture.cgroup_root.string(),
        "--supervisor", fixture.environment.supervisor_path,
        "--sandbox-uid", std::to_string(kClientUid),
        "--sandbox-gid", std::to_string(kClientGid),
        "--memory-max", "268435456",
        "--memory-high", "134217728",
        "--memory-swap-max", "0",
        "--cpu-max-us", "50000",
        "--cpu-period-us", "100000",
        "--pids-max", "64",
        "--io-weight", "100",
        "--idle-minutes", "1",
        "--quota-backend", "xfs_project_v1",
        "--xfs-quota-mount", fixture.environment.xfs_mount.string(),
        "--xfs-project-id-min", std::to_string(fixture.environment.project_id_min),
        "--xfs-project-id-max", std::to_string(fixture.environment.project_id_max),
        "--runtime-control-hard-bytes", std::to_string(kControlHardBytes),
        "--runtime-control-hard-inodes", std::to_string(kControlHardInodes),
        "--runtime-workspace-hard-bytes", std::to_string(kWorkspaceHardBytes),
        "--runtime-workspace-hard-inodes", std::to_string(kWorkspaceHardInodes),
        "--xfs-global-admission-bytes", std::to_string(kControlHardBytes + kWorkspaceHardBytes),
        "--xfs-global-admission-inodes", std::to_string(kControlHardInodes + kWorkspaceHardInodes),
        "--xfs-system-reserve-bytes", std::to_string(kSystemReserveBytes),
        "--xfs-system-reserve-inodes", std::to_string(kSystemReserveInodes),
    };
    /** @brief execv 所需的可变 argv 指针 / Mutable argv pointers required by execv. */
    std::vector<char*> argv;
    argv.reserve(arguments.size() + 1U);
    for (std::string& argument : arguments) {
        argv.push_back(argument.data());
    }
    argv.push_back(nullptr);
    /** @brief 测试 child fork 之前的 parent PID / Parent PID before the test child fork. */
    const pid_t expected_parent = getpid();
    /** @brief 新 broker child PID / New broker child PID. */
    const pid_t child = fork();
    if (child < 0) {
        std::cerr << "FAIL: fork wspctld: " << std::strerror(errno) << '\n';
        return false;
    }
    if (child == 0) {
        if (prctl(PR_SET_PDEATHSIG, SIGTERM) != 0 || getppid() != expected_parent) {
            _exit(125);
        }
        execv(launch.executable.c_str(), argv.data());
        _exit(127);
    }
    fixture.broker_pid = child;
    return true;
}

/**
 * @brief 等待 broker 绑定并保护其两个 Unix socket / Wait for broker binding and protecting both Unix sockets.
 * @param fixture 包含 broker PID 的 fixture / Fixture containing the broker PID.
 * @param bot_socket_path 期待出现的 Bot socket / Expected Bot socket path.
 * @param operator_socket_path 期待出现的 root-owned operator socket / Expected root-owned operator socket path.
 * @return 两 socket 均以预期 owner/mode 就绪时为真 / True when both sockets are ready with expected owner/mode.
 */
[[nodiscard]] bool wait_for_broker_sockets(
    E2eFixture& fixture,
    const std::filesystem::path& bot_socket_path,
    const std::filesystem::path& operator_socket_path) {
    /** @brief 启动等待绝对截止 / Absolute startup deadline. */
    const auto deadline = std::chrono::steady_clock::now() + kBrokerStartDeadline;
    do {
        /** @brief Bot socket metadata / Bot socket 元数据。 */
        struct stat bot_metadata {};
        /** @brief operator socket metadata / operator socket 元数据。 */
        struct stat operator_metadata {};
        if (lstat(bot_socket_path.c_str(), &bot_metadata) == 0 && S_ISSOCK(bot_metadata.st_mode) &&
            bot_metadata.st_uid == kClientUid && (bot_metadata.st_mode & 0777) == 0600 &&
            lstat(operator_socket_path.c_str(), &operator_metadata) == 0 && S_ISSOCK(operator_metadata.st_mode) &&
            operator_metadata.st_uid == 0U && (operator_metadata.st_mode & 0777) == 0600) {
            return true;
        }
        /** @brief broker 的非阻塞 wait 状态 / Nonblocking wait state for broker. */
        int status{};
        const pid_t waited = waitpid(fixture.broker_pid, &status, WNOHANG);
        if (waited == fixture.broker_pid) {
            fixture.broker_pid = -1;
            std::cerr << "FAIL: wspctld exited before binding its socket, status=" << status << '\n';
            return false;
        }
        if (waited < 0 && errno != EINTR) {
            std::cerr << "FAIL: waitpid wspctld startup: " << std::strerror(errno) << '\n';
            return false;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
    } while (std::chrono::steady_clock::now() < deadline);
    std::cerr << "FAIL: wspctld did not bind protected Bot/operator sockets before deadline\n";
    return false;
}

/**
 * @brief 降为真实 native gateway client identity / Drop to the real native gateway client identity.
 * @return 降权完成时为真 / True when the identity drop completes.
 */
[[nodiscard]] bool drop_to_client_identity() {
    if (setgroups(0, nullptr) != 0 || setresgid(kClientGid, kClientGid, kClientGid) != 0 ||
        setresuid(kClientUid, kClientUid, kClientUid) != 0) {
        std::cerr << "FAIL: cannot drop E2E gateway child to configured client UID/GID\n";
        return false;
    }
    /** @brief post-drop capability set / 降权后的 capability set。 */
    cap_t empty = cap_init();
    if (empty == nullptr) {
        std::cerr << "FAIL: cap_init after client identity drop\n";
        return false;
    }
    /** @brief 清空 capability 的状态 / Status while clearing capabilities. */
    const int applied = cap_set_proc(empty);
    cap_free(empty);
    if (applied != 0 || prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0) {
        std::cerr << "FAIL: cannot make E2E gateway child unprivileged\n";
        return false;
    }
    return geteuid() == kClientUid && getegid() == kClientGid;
}

/**
 * @brief 构造一条 native gateway execute 请求 / Construct one native gateway execute request.
 * @param activation 当前 RuntimeProcess activation / Current RuntimeProcess activation.
 * @param request_id 稳定测试 request ID / Stable test request ID.
 * @param script 交给 image Bash 的脚本 / Script passed to image Bash.
 * @param timeout 墙钟超时 / Wall-clock timeout.
 * @return 已填充的 gateway DTO / Filled gateway DTO.
 */
[[nodiscard]] wspctl::presentation::ClientExecuteRequest make_execute_request(
    const std::string_view activation,
    const std::string_view request_id,
    std::string script,
    const std::chrono::milliseconds timeout) {
    return wspctl::presentation::ClientExecuteRequest{
        .runtime_key = std::string(kRuntimeKey),
        .activation_id = std::string(activation),
        .request_id = std::string(request_id),
        .request_hash = sha256_hex("wspctl-privileged-e2e:" + std::string(request_id)),
        .argv = {"/bin/bash", "-c", std::move(script)},
        .stdin_data = {},
        .cwd = "/workspace",
        .timeout = timeout,
        .output_limit = 16U * 1024U,
    };
}

/**
 * @brief 从 timeout 输出解析 orphan PID / Parse the orphan PID from timeout output.
 * @param output timeout task 的 stdout / Stdout from the timeout task.
 * @return 正 PID 或空值 / Positive PID or no value.
 */
[[nodiscard]] std::optional<pid_t> parse_orphan_pid(const std::string_view output) {
    /** @brief 首行结尾位置 / End position of the first line. */
    const std::size_t end = output.find('\n');
    const std::string_view line = output.substr(0U, end == std::string_view::npos ? output.size() : end);
    /** @brief 临时 64-bit PID / Temporary 64-bit PID. */
    std::uint64_t parsed{};
    /** @brief `from_chars` 结果 / `from_chars` result. */
    const auto [parsed_end, error] = std::from_chars(line.data(), line.data() + line.size(), parsed);
    if (error != std::errc{} || parsed_end != line.data() + line.size() || parsed == 0U ||
        parsed > static_cast<std::uint64_t>(std::numeric_limits<pid_t>::max())) {
        return std::nullopt;
    }
    return static_cast<pid_t>(parsed);
}

/**
 * @brief 验证一次成功且未回放的 task 结果 / Verify one successful non-replayed task result.
 * @param result native gateway 返回值 / Native gateway result.
 * @param marker stdout 中必须存在的 marker / Marker that must appear in stdout.
 * @param operation 诊断操作名 / Diagnostic operation name.
 * @return 所有断言满足时为真 / True when all assertions hold.
 */
[[nodiscard]] bool expect_successful_result(
    const wspctl::presentation::ClientExecutionResult& result,
    const std::string_view marker,
    const std::string_view operation) {
    if (result.timed_out || result.replayed || !result.exit_code.has_value() || *result.exit_code != 0 ||
        result.stdout_data.find(marker) == std::string::npos) {
        std::cerr << "FAIL: " << operation << " did not return the expected successful task result\n";
        return false;
    }
    return true;
}

/**
 * @brief 验收 operator status 不会为缺失 runtime 激活 RuntimeProcess / Accept that operator status does not activate a missing runtime.
 * @param operator_socket root-only operator endpoint / Root-only operator endpoint.
 * @return 返回 absent/inactive/no-quota 读模型时为真 / True when the result is absent, inactive, and quota-free.
 */
[[nodiscard]] bool run_operator_absent_read_phase(const std::filesystem::path& operator_socket) {
    const auto runtime = wspctl::domain::RuntimeId::parse(std::string(kRuntimeKey));
    if (!runtime) {
        std::cerr << "FAIL: cannot parse fixed operator runtime key\n";
        return false;
    }
    const wspctl::presentation::OperatorGatewayClient client(operator_socket.string());
    const auto status = client.status(*runtime);
    if (!status) {
        std::cerr << "FAIL: root operator status before activation: " << status.error().message << '\n';
        return false;
    }
    if (status->persistence() != wspctl::domain::WorkspacePersistence::absent ||
        status->activity() != wspctl::domain::WorkspaceActivity::inactive || status->quota().has_value()) {
        std::cerr << "FAIL: operator status unexpectedly activated or materialized a missing runtime\n";
        return false;
    }
    const auto root = wspctl::domain::OperatorWorkspacePath::parse("/workspace");
    if (!root) {
        std::cerr << "FAIL: cannot parse fixed operator workspace root\n";
        return false;
    }
    const auto listing = client.list(*runtime, *root);
    if (listing || listing.error().code != wspctl::ErrorCode::not_found) {
        std::cerr << "FAIL: operator list for an absent workspace must fail without activation\n";
        return false;
    }
    return true;
}

/**
 * @brief 验收 operator 能只读查看已持久化的 upper layer / Accept that the operator can read a persisted upper layer.
 * @param operator_socket root-only operator endpoint / Root-only operator endpoint.
 * @return status/list 都符合只读 workspace 合同则为真 / True when both status and list satisfy the readonly workspace contract.
 */
[[nodiscard]] bool run_operator_ready_read_phase(const std::filesystem::path& operator_socket) {
    const auto runtime = wspctl::domain::RuntimeId::parse(std::string(kRuntimeKey));
    const auto root = wspctl::domain::OperatorWorkspacePath::parse("/workspace");
    if (!runtime || !root) {
        std::cerr << "FAIL: cannot construct fixed operator read inputs\n";
        return false;
    }
    const wspctl::presentation::OperatorGatewayClient client(operator_socket.string());
    const auto status = client.status(*runtime);
    if (!status || status->persistence() != wspctl::domain::WorkspacePersistence::ready ||
        !status->quota().has_value()) {
        if (!status) {
            std::cerr << "FAIL: root operator status after persistence: " << status.error().message << '\n';
        } else {
            std::cerr << "FAIL: operator status omitted ready workspace quota\n";
        }
        return false;
    }
    const auto listing = client.list(*runtime, *root);
    if (!listing) {
        std::cerr << "FAIL: root operator workspace listing: " << listing.error().message << '\n';
        return false;
    }
    const bool contains_persisted_file = std::any_of(
        listing->entries.begin(),
        listing->entries.end(),
        [](const wspctl::domain::WorkspaceEntry& entry) { return entry.encoded_name() == "e2e-persist.txt"; });
    if (!contains_persisted_file) {
        std::cerr << "FAIL: operator upper-layer listing omitted persisted workspace file\n";
        return false;
    }
    return true;
}

/**
 * @brief 验收 Bot UID 不能访问 root-only operator endpoint / Accept that the Bot UID cannot access the root-only operator endpoint.
 * @param fixture 未使用的 fixture / Unused fixture.
 * @param operator_socket root-only operator endpoint / Root-only operator endpoint.
 * @return 降权后的连接无法读取 status 时为真 / True when the identity-dropped client cannot read status.
 */
[[nodiscard]] bool run_operator_denial_phase(
    const E2eFixture& fixture,
    const std::filesystem::path& operator_socket) {
    static_cast<void>(fixture);
    if (!drop_to_client_identity()) {
        return false;
    }
    const auto runtime = wspctl::domain::RuntimeId::parse(std::string(kRuntimeKey));
    if (!runtime) {
        std::cerr << "FAIL: cannot parse fixed operator denial runtime key\n";
        return false;
    }
    const wspctl::presentation::OperatorGatewayClient client(operator_socket.string());
    const auto status = client.status(*runtime);
    if (status) {
        std::cerr << "FAIL: Bot UID unexpectedly read root-only operator status\n";
        return false;
    }
    return true;
}

/**
 * @brief 运行首个 broker 的 gateway 验收序列 / Run the native-gateway acceptance sequence against the first broker.
 * @param fixture 实际测试 fixture / Actual test fixture.
 * @param socket_path 首个 broker 的 socket / First broker socket.
 * @return 所有首阶段断言成立时为真 / True when every first-phase assertion holds.
 */
[[nodiscard]] bool run_first_client_phase(const E2eFixture& fixture, const std::filesystem::path& socket_path) {
    if (!drop_to_client_identity()) {
        return false;
    }
    /** @brief 对 client UID 不可遍历的 host state root probe / Host-state traversal probe for the client UID. */
    const int state_probe = open(fixture.state_root.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (state_probe >= 0) {
        static_cast<void>(close(state_probe));
        std::cerr << "FAIL: native gateway client unexpectedly traversed host state root\n";
        return false;
    }
    /** @brief 真实非特权 Unix gateway client / Real unprivileged Unix gateway client. */
    const wspctl::presentation::UnixGatewayClient client(socket_path.string());
    /** @brief 在 PID namespace 内验证 PID1 父进程及 task hardening 的脚本 / Script validating PID1 parentage and task hardening inside the PID namespace. */
    const std::string namespace_script{
        "test \"$PPID\" = 1 || exit 11; "
        "cap=''; nnp=''; sec=''; "
        "while IFS=$'\\t' read -r key value; do "
        "case \"$key\" in CapEff:) cap=\"$value\";; NoNewPrivs:) nnp=\"$value\";; Seccomp:) sec=\"$value\";; esac; "
        "done < /proc/self/status; "
        "case \"$cap\" in ''|*[!0]*) exit 12;; esac; "
        "test \"$nnp\" = 1 || exit 13; test \"$sec\" = 2 || exit 14; "
        "printf 'pid-namespace-task-hardened\\n'"};
    const auto namespace_result = client.execute(make_execute_request(
        kFirstActivation,
        "e2e-namespace-task",
        namespace_script,
        std::chrono::seconds(5)));
    if (!namespace_result || !expect_successful_result(*namespace_result, "pid-namespace-task-hardened", "namespace/PID1 task")) {
        if (!namespace_result) {
            std::cerr << "FAIL: native gateway namespace task: " << namespace_result.error().message << '\n';
        }
        return false;
    }
    /** @brief Overlay 写入脚本 / Overlay write script. */
    const std::string overlay_script{
        "printf 'persisted-e2e\\n' > /workspace/e2e-persist.txt; "
        "test -f /workspace/e2e-persist.txt; printf 'overlay-written\\n'"};
    const auto overlay_result = client.execute(make_execute_request(
        kFirstActivation,
        "e2e-overlay-write",
        overlay_script,
        std::chrono::seconds(5)));
    if (!overlay_result || !expect_successful_result(*overlay_result, "overlay-written", "Overlay write")) {
        if (!overlay_result) {
            std::cerr << "FAIL: native gateway Overlay write: " << overlay_result.error().message << '\n';
        }
        return false;
    }
    /** @brief 要经 add_file 导入的可执行 workspace payload / Executable workspace payload imported through add_file. */
    const std::string payload{"#!/bin/bash\nprintf 'ingress-executed\\n'\n"};
    /** @brief add_file request / add_file 请求。 */
    const wspctl::presentation::ClientAddFileRequest add_file_request{
        .runtime_key = std::string(kRuntimeKey),
        .activation_id = std::string(kFirstActivation),
        .request_id = "e2e-add-file",
        .request_hash = sha256_hex("wspctl-privileged-e2e:e2e-add-file"),
        .opaque_id = std::string(kPayloadOpaqueId),
        .byte_size = payload.size(),
        .sha256 = sha256_hex(payload),
    };
    /** @brief 单次 raw-byte source / Single raw-byte source. */
    OneChunkPayloadSource source(payload_bytes(payload));
    const auto add_file_result = client.add_file(add_file_request, source);
    if (!add_file_result || add_file_result->replayed || add_file_result->path != kPayloadWorkspacePath ||
        add_file_result->byte_size != payload.size() || add_file_result->sha256 != add_file_request.sha256) {
        if (!add_file_result) {
            std::cerr << "FAIL: native gateway add_file: " << add_file_result.error().message << '\n';
        } else {
            std::cerr << "FAIL: native gateway add_file returned an unsafe or unexpected receipt\n";
        }
        return false;
    }
    /** @brief 仅在 workspace task 内 chmod 并直接执行导入 payload 的脚本 / Script chmodding and directly executing the imported payload only inside the workspace task. */
    const std::string execute_payload_script{
        "payload=/workspace/uploads/e2e-payload/payload; "
        "test ! -x \"$payload\" || exit 21; chmod 700 \"$payload\"; \"$payload\""};
    const auto payload_result = client.execute(make_execute_request(
        kFirstActivation,
        "e2e-workspace-chmod-exec",
        execute_payload_script,
        std::chrono::seconds(5)));
    if (!payload_result || !expect_successful_result(*payload_result, "ingress-executed", "workspace-only chmod/execute")) {
        if (!payload_result) {
            std::cerr << "FAIL: native gateway workspace payload execution: " << payload_result.error().message << '\n';
        }
        return false;
    }
    /** @brief 开启 Bash job control，让 background orphan 脱离前台 process group 后再让 task 超时的脚本 / Script enabling Bash job control so the background orphan leaves the foreground process group before timing out the task. */
    const std::string timeout_script{
        "set -m; (while :; do :; done) & orphan=$!; printf '%s\\n' \"$orphan\"; while :; do :; done"};
    const auto timeout_result = client.execute(make_execute_request(
        kFirstActivation,
        "e2e-timeout-orphan",
        timeout_script,
        std::chrono::milliseconds(300)));
    if (!timeout_result || !timeout_result->timed_out || timeout_result->replayed) {
        if (!timeout_result) {
            std::cerr << "FAIL: native gateway timeout/orphan task: " << timeout_result.error().message << '\n';
        } else {
            std::cerr << "FAIL: timeout/orphan task did not time out\n";
        }
        return false;
    }
    const std::optional<pid_t> orphan_pid = parse_orphan_pid(timeout_result->stdout_data);
    if (!orphan_pid.has_value()) {
        std::cerr << "FAIL: timeout/orphan task did not publish a valid orphan PID\n";
        return false;
    }
    /** @brief 后续 task 检查 prior orphan 已被 task cgroup kill 的脚本 / Follow-up task script checking that task cgroup kill removed the prior orphan. */
    const std::string cleanup_script{
        "test ! -e /proc/" + std::to_string(*orphan_pid) + " || exit 31; printf 'orphan-cleaned\\n'"};
    const auto cleanup_result = client.execute(make_execute_request(
        kFirstActivation,
        "e2e-orphan-cleanup",
        cleanup_script,
        std::chrono::seconds(5)));
    if (!cleanup_result || !expect_successful_result(*cleanup_result, "orphan-cleaned", "timeout orphan cleanup")) {
        if (!cleanup_result) {
            std::cerr << "FAIL: native gateway orphan cleanup check: " << cleanup_result.error().message << '\n';
        }
        return false;
    }
    return true;
}

/**
 * @brief 运行 restart 后的恢复验证 / Run recovery verification after broker restart.
 * @param socket_path 崩溃重启后重新绑定的 broker socket / Broker socket rebound after a crash restart.
 * @return workspace upper、payload mode 与 PID namespace 均恢复时为真 / True when workspace upper, payload mode, and PID namespace recover.
 */
[[nodiscard]] bool run_recovery_client_phase(const std::filesystem::path& socket_path) {
    if (!drop_to_client_identity()) {
        return false;
    }
    /** @brief restart 后的真实非特权 Unix gateway client / Real unprivileged Unix gateway client after restart. */
    const wspctl::presentation::UnixGatewayClient client(socket_path.string());
    /** @brief 同 key、新 activation 恢复 upper 与 payload 的脚本 / Script recovering upper and payload under the same key and a new activation. */
    const std::string recovery_script{
        "test \"$PPID\" = 1 || exit 41; "
        "IFS= read -r persisted < /workspace/e2e-persist.txt; test \"$persisted\" = persisted-e2e || exit 42; "
        "payload=/workspace/uploads/e2e-payload/payload; test -x \"$payload\" || exit 43; \"$payload\"; "
        "printf 'overlay-recovered\\n'"};
    const auto result = client.execute(make_execute_request(
        kSecondActivation,
        "e2e-recovery",
        recovery_script,
        std::chrono::seconds(5)));
    if (!result || !expect_successful_result(*result, "overlay-recovered", "broker restart recovery")) {
        if (!result) {
            std::cerr << "FAIL: native gateway recovery task: " << result.error().message << '\n';
        }
        return false;
    }
    if (result->stdout_data.find("ingress-executed") == std::string::npos) {
        std::cerr << "FAIL: recovered workspace payload was not directly executable inside the new workspace activation\n";
        return false;
    }
    return true;
}

/**
 * @brief fork 一个已降权 native gateway phase / Fork one identity-dropped native gateway phase.
 * @param phase 要运行的 child phase / Child phase to run.
 * @param fixture 测试 fixture / Test fixture.
 * @param socket_path phase 使用的 socket / Socket used by the phase.
 * @return child phase 成功时为真 / True when the child phase succeeds.
 */
[[nodiscard]] bool run_client_phase(
    bool (*phase)(const E2eFixture&, const std::filesystem::path&),
    const E2eFixture& fixture,
    const std::filesystem::path& socket_path) {
    /** @brief gateway child PID / Gateway child PID. */
    const pid_t child = fork();
    if (child < 0) {
        std::cerr << "FAIL: fork native gateway phase: " << std::strerror(errno) << '\n';
        return false;
    }
    if (child == 0) {
        std::cerr.flush();
        std::_Exit(phase(fixture, socket_path) ? EXIT_SUCCESS : EXIT_FAILURE);
    }
    /** @brief gateway child wait status / Gateway child wait status. */
    int status{};
    pid_t waited = -1;
    do {
        waited = waitpid(child, &status, 0);
    } while (waited < 0 && errno == EINTR);
    if (waited != child || !WIFEXITED(status) || WEXITSTATUS(status) != EXIT_SUCCESS) {
        std::cerr << "FAIL: native gateway phase failed\n";
        return false;
    }
    return true;
}

/**
 * @brief 将只需 socket 的恢复 phase 适配为统一 phase 签名 / Adapt the socket-only recovery phase to the common phase signature.
 * @param fixture 未使用的 fixture / Unused fixture.
 * @param socket_path crash-recovered socket / Socket recovered after a broker crash.
 * @return recovery phase 的结果 / Result of the recovery phase.
 */
[[nodiscard]] bool run_recovery_phase_adapter(const E2eFixture& fixture, const std::filesystem::path& socket_path) {
    static_cast<void>(fixture);
    return run_recovery_client_phase(socket_path);
}

/**
 * @brief 运行真实特权 E2E 验收 / Run the real privileged E2E acceptance.
 * @param broker_executable CMake 构建出的 wspctld 可执行文件 / CMake-built wspctld executable.
 * @param requirement 当前 CTest required/optional 要求 / Current CTest required/optional requirement.
 * @return POSIX/CTest 退出状态 / POSIX/CTest exit status.
 */
[[nodiscard]] int run_privileged_e2e(const std::filesystem::path& broker_executable, const Requirement requirement) {
    if (geteuid() != 0U) {
        return unavailable(requirement, "effective UID is not root");
    }
    for (const cap_value_t capability : {CAP_SYS_ADMIN, CAP_SYS_CHROOT, CAP_SETUID, CAP_SETGID, CAP_MKNOD}) {
        if (!has_effective_capability(capability)) {
            return unavailable(requirement, "required broker Linux capabilities are absent");
        }
    }
    /** @brief 配置加载失败原因 / Configuration-load failure reason. */
    std::string reason;
    /** @brief 是否仅为缺失 operator 前置条件 / Whether failure is only a missing operator prerequisite. */
    bool missing = false;
    const auto environment = load_environment(reason, missing);
    if (!environment.has_value()) {
        return missing ? unavailable(requirement, reason) : (std::cerr << "FAIL: " << reason << '\n', EXIT_FAILURE);
    }
    if (!validate_xfs_state_parent_contract(*environment, reason)) {
        return unavailable(requirement, reason);
    }
    /** @brief broker executable metadata / broker executable 元数据。 */
    struct stat executable_metadata {};
    if (!broker_executable.is_absolute() || lstat(broker_executable.c_str(), &executable_metadata) != 0 ||
        !S_ISREG(executable_metadata.st_mode) || (executable_metadata.st_mode & S_IXUSR) == 0) {
        std::cerr << "FAIL: CMake did not provide an executable wspctld path\n";
        return EXIT_FAILURE;
    }
    /** @brief readonly image mount state / 只读 image mount 状态。 */
    struct statvfs image_filesystem {};
    if (statvfs(environment->base_root.c_str(), &image_filesystem) != 0 || (image_filesystem.f_flag & ST_RDONLY) == 0U) {
        std::cerr << "FAIL: WSPCTL_PRIVILEGED_E2E_BASE_ROOT is not on a real readonly mount\n";
        return EXIT_FAILURE;
    }
    if (const auto image = wspctl::validate_image_root(environment->base_root, environment->images_root); !image) {
        std::cerr << "FAIL: supplied image generation is not a sealed broker-valid image: " << image.error().message << '\n';
        return EXIT_FAILURE;
    }
    if (!cgroup_parent_is_usable(environment->cgroup_parent)) {
        return unavailable(requirement, "delegated cgroup parent is not writable with cpu/memory/pids/io controls");
    }
    E2eFixture fixture{
        .environment = *environment,
        .state_root = {},
        .state_root_identity = std::nullopt,
        .socket_directory = {},
        .socket_directory_identity = std::nullopt,
        .cgroup_root = {},
        .cgroup_root_identity = std::nullopt,
        .broker_pid = -1,
    };
    FixtureCleanupGuard cleanup_guard(fixture);
    const auto cgroup_child = create_test_directory(
        environment->cgroup_parent,
        "wspctl-e2e-cgroup-",
        0700,
        false,
        reason);
    if (!cgroup_child.has_value()) {
        return unavailable(requirement, "cannot create a self-owned child below delegated cgroup parent");
    }
    fixture.cgroup_root = cgroup_child->path;
    fixture.cgroup_root_identity = cgroup_child->identity;
    if (!cgroup_parent_is_usable(fixture.cgroup_root)) {
        std::cerr << "FAIL: self-created cgroup child is not delegated for cpu/memory/pids/io\n";
        return EXIT_FAILURE;
    }
    const auto state_root = create_test_directory(
        environment->state_parent,
        "wspctl-e2e-state-",
        0700,
        true,
        reason);
    if (!state_root.has_value()) {
        std::cerr << "FAIL: cannot create self-owned XFS state root: " << reason << '\n';
        return EXIT_FAILURE;
    }
    fixture.state_root = state_root->path;
    fixture.state_root_identity = state_root->identity;
    const auto socket_directory = create_test_directory(
        environment->socket_parent,
        "wspctl-e2e-socket-",
        0711,
        true,
        reason);
    if (!socket_directory.has_value()) {
        std::cerr << "FAIL: cannot create self-owned socket directory: " << reason << '\n';
        return EXIT_FAILURE;
    }
    fixture.socket_directory = socket_directory->path;
    fixture.socket_directory_identity = socket_directory->identity;
    const auto bot_socket_directory = create_endpoint_directory(fixture.socket_directory, "bot", 0711, reason);
    const auto operator_socket_directory = create_endpoint_directory(fixture.socket_directory, "operator", 0700, reason);
    if (!bot_socket_directory.has_value() || !operator_socket_directory.has_value()) {
        std::cerr << "FAIL: cannot create disjoint self-owned Bot/operator socket directories: " << reason << '\n';
        return EXIT_FAILURE;
    }
    /** @brief 首次 broker socket / First broker socket. */
    const std::filesystem::path first_socket = *bot_socket_directory / "wspctld.sock";
    /** @brief 首次 root-only operator socket / First root-only operator socket. */
    const std::filesystem::path first_operator_socket = *operator_socket_directory / "wspctld.sock";
    if (!launch_broker(
            fixture,
            BrokerLaunch{
                .executable = broker_executable,
                .bot_socket_path = first_socket,
                .operator_socket_path = first_operator_socket,
            }) ||
        !wait_for_broker_sockets(fixture, first_socket, first_operator_socket)) {
        return EXIT_FAILURE;
    }
    if (!run_operator_absent_read_phase(first_operator_socket) ||
        !run_client_phase(run_operator_denial_phase, fixture, first_operator_socket)) {
        return EXIT_FAILURE;
    }
    if (!run_client_phase(run_first_client_phase, fixture, first_socket)) {
        return EXIT_FAILURE;
    }
    if (!run_operator_ready_read_phase(first_operator_socket)) {
        return EXIT_FAILURE;
    }
    // SIGTERM is deliberately an ungraceful broker restart here: the next broker must reclaim
    // the stale Bot and operator socket names, kill stale cgroup state, and reconstruct a new
    // activation from the persistent upper layer.
    if (!stop_broker(fixture, true) || !wait_for_empty_cgroup(fixture.cgroup_root, kCgroupDrainDeadline)) {
        std::cerr << "FAIL: first broker restart did not prove cgroup drain\n";
        return EXIT_FAILURE;
    }
    /** @brief 重启后复用的 Bot socket，直接验收 stale listener pathname 回收 / Bot socket reused after restart to directly accept stale listener pathname recovery. */
    const std::filesystem::path second_socket = first_socket;
    /** @brief 重启后复用的 operator socket，直接验收独立 endpoint 回收 / Operator socket reused after restart to directly accept independent endpoint recovery. */
    const std::filesystem::path second_operator_socket = first_operator_socket;
    if (!launch_broker(
            fixture,
            BrokerLaunch{
                .executable = broker_executable,
                .bot_socket_path = second_socket,
                .operator_socket_path = second_operator_socket,
            }) ||
        !wait_for_broker_sockets(fixture, second_socket, second_operator_socket)) {
        return EXIT_FAILURE;
    }
    if (!run_client_phase(run_recovery_phase_adapter, fixture, second_socket)) {
        return EXIT_FAILURE;
    }
    if (!cleanup_guard.finish()) {
        return EXIT_FAILURE;
    }
    std::cout << "PASS: privileged wspctld E2E exercised real PID namespace, OverlayFS, cgroup cleanup, add_file, and restart recovery\n";
    return EXIT_SUCCESS;
}

}  // namespace

/**
 * @brief privileged E2E CTest 入口 / Privileged E2E CTest entry point.
 * @param argc 参数数 / Argument count.
 * @param argv 参数数组 / Argument values.
 * @return POSIX/CTest 退出状态 / POSIX/CTest exit status.
 */
int main(const int argc, char* argv[]) {
    /** @brief 当前 required/optional 设置 / Current required/optional setting. */
    const Requirement requirement = test_requirement();
    if (requirement == Requirement::invalid) {
        std::cerr << "FAIL: " << kRequireEnvironment << " accepts only 0 or 1\n";
        return EXIT_FAILURE;
    }
    if (argc != 2) {
        std::cerr << "FAIL: privileged E2E requires the CMake-built wspctld executable argument\n";
        return EXIT_FAILURE;
    }
    return run_privileged_e2e(std::filesystem::path(argv[1]), requirement);
}
