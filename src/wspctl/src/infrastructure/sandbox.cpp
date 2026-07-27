#include "wspctl/infrastructure/sandbox.hpp"

#include <openssl/sha.h>

#include <sys/capability.h>
#include <sys/mount.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <sys/syscall.h>
#include <sys/types.h>

#include <seccomp.h>

#include <array>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <fcntl.h>
#include <grp.h>
#include <fstream>
#include <linux/fs.h>
#include <linux/sched.h>
#include <linux/securebits.h>
#include <sched.h>
#include <string_view>
#include <sys/sysmacros.h>
#include <thread>
#include <unistd.h>

namespace wspctl {
namespace {

/** @brief 是否具有指定 effective Linux capability / Whether an effective Linux capability is present. */
[[nodiscard]] Result<bool> has_effective_capability(const cap_value_t capability) {
    cap_t capabilities = cap_get_proc();
    if (capabilities == nullptr) {
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed, "cap_get_proc"));
    }
    cap_flag_value_t enabled = CAP_CLEAR;
    const int status = cap_get_flag(capabilities, capability, CAP_EFFECTIVE, &enabled);
    const int saved_errno = errno;
    cap_free(capabilities);
    errno = saved_errno;
    if (status != 0) {
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed, "cap_get_flag"));
    }
    return enabled == CAP_SET;
}

/** @brief 校验 broker 必须持有的一组 capability / Validate the capability set mandatory for broker. */
[[nodiscard]] Result<void> require_broker_capabilities() {
    constexpr std::array<cap_value_t, 5> kRequired{
        CAP_SYS_ADMIN,
        CAP_SYS_CHROOT,
        CAP_SETUID,
        CAP_SETGID,
        CAP_MKNOD,
    };
    for (const cap_value_t capability : kRequired) {
        const auto present = has_effective_capability(capability);
        if (!present) {
            return std::unexpected(present.error());
        }
        if (!*present) {
            return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "required Linux capability is absent"));
        }
    }
    return {};
}

/** @brief 读取一个小文本文件 / Read a small text file. */
[[nodiscard]] Result<std::string> read_small_file(const std::filesystem::path& path) {
    std::ifstream input(path);
    if (!input.is_open()) {
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed, "open " + path.string()));
    }
    std::string contents((std::istreambuf_iterator<char>(input)), std::istreambuf_iterator<char>());
    if (contents.size() > 64U * 1024U || !input.eof()) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "cannot read bounded cgroup metadata"));
    }
    return contents;
}

/** @brief 判断文字列表是否包含 token / Check whether a textual list contains a token. */
[[nodiscard]] bool contains_token(const std::string_view text, const std::string_view token) {
    std::size_t position = 0;
    while (position < text.size()) {
        while (position < text.size() && (text[position] == ' ' || text[position] == '\n' || text[position] == '\t')) {
            ++position;
        }
        const std::size_t end = text.find_first_of(" \n\t", position);
        const std::string_view found = text.substr(position, end == std::string_view::npos ? text.size() - position : end - position);
        if (found == token) {
            return true;
        }
        if (end == std::string_view::npos) {
            break;
        }
        position = end + 1U;
    }
    return false;
}

/** @brief 把 path 解析成存在的 canonical absolute path / Resolve path to an existing canonical absolute path. */
[[nodiscard]] Result<std::filesystem::path> canonical_existing(const std::filesystem::path& path) {
    if (!path.is_absolute()) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "sandbox path must be absolute"));
    }
    std::error_code error;
    const std::filesystem::path canonical = std::filesystem::canonical(path, error);
    if (error) {
        return std::unexpected(make_error(ErrorCode::not_found, "canonical sandbox path: " + error.message()));
    }
    return canonical;
}

/** @brief 将文字 SHA-256 为不含用户路径的目录名 / SHA-256 text into a directory name without user path parts. */
[[nodiscard]] std::string hash_component(const std::string_view source) {
    std::array<unsigned char, SHA256_DIGEST_LENGTH> digest{};
    SHA256(reinterpret_cast<const unsigned char*>(source.data()), source.size(), digest.data());
    constexpr std::string_view kDigits{"0123456789abcdef"};
    std::string output;
    output.reserve(digest.size() * 2U);
    for (const unsigned char value : digest) {
        output.push_back(kDigits[(value >> 4U) & 0x0fU]);
        output.push_back(kDigits[value & 0x0fU]);
    }
    return output;
}

/**
 * @brief 判断两个 quota binding 是否描述同一个 runtime project pair / Check whether two quota bindings describe the same runtime project pair.
 * @param left 左侧 binding / Left binding.
 * @param right 右侧 binding / Right binding.
 * @return 所有路径及 project ID 都相同则为真 / True when every path and project ID matches.
 */
[[nodiscard]] bool same_runtime_quota_binding(
    const RuntimeQuotaBinding& left,
    const RuntimeQuotaBinding& right) noexcept {
    return left.runtime_dir == right.runtime_dir && left.control_dir == right.control_dir &&
           left.workspace_dir == right.workspace_dir && left.control_project_id == right.control_project_id &&
           left.workspace_project_id == right.workspace_project_id;
}

/** @brief 创建 cgroup 目录（不能 chmod cgroupfs） / Create a cgroup directory without chmod on cgroupfs. */
[[nodiscard]] Result<void> create_cgroup_directory(const std::filesystem::path& path) {
    std::error_code error;
    std::filesystem::create_directories(path, error);
    if (error) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "create cgroup directory: " + error.message()));
    }
    return {};
}

/** @brief 原子语义写 cgroup 控制文件 / Write a cgroup control file. */
[[nodiscard]] Result<void> write_cgroup_file(const std::filesystem::path& path, const std::string_view value) {
    const int fd = open(path.c_str(), O_WRONLY | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0) {
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed, "open cgroup control file"));
    }
    std::size_t offset = 0;
    while (offset < value.size()) {
        const ssize_t count = write(fd, value.data() + static_cast<std::ptrdiff_t>(offset), value.size() - offset);
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count <= 0) {
            const Error error = errno_error(ErrorCode::sandbox_preflight_failed, "write cgroup control file");
            static_cast<void>(close(fd));
            return std::unexpected(error);
        }
        offset += static_cast<std::size_t>(count);
    }
    if (close(fd) != 0) {
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed, "close cgroup control file"));
    }
    return {};
}

/** @brief 启用 delegated cgroup 所需 controllers / Enable controllers required in delegated cgroup. */
[[nodiscard]] Result<void> enable_runtime_controllers(const std::filesystem::path& cgroup_root) {
    const auto enabled = read_small_file(cgroup_root / "cgroup.subtree_control");
    if (!enabled) {
        return std::unexpected(enabled.error());
    }
    if (contains_token(*enabled, "cpu") && contains_token(*enabled, "memory") && contains_token(*enabled, "pids") &&
        contains_token(*enabled, "io")) {
        return {};
    }
    return write_cgroup_file(cgroup_root / "cgroup.subtree_control", "+cpu +memory +pids +io");
}

/** @brief 判断 cgroup.events 是否明确报告 populated 0 / Check whether cgroup.events explicitly reports populated 0. */
[[nodiscard]] bool is_cgroup_empty(const std::string_view events) {
    constexpr std::string_view kPopulation{"populated 0"};
    std::size_t position = 0;
    while (position < events.size()) {
        const std::size_t end = events.find('\n', position);
        const std::string_view line = events.substr(position, end == std::string_view::npos ? events.size() - position : end - position);
        if (line == kPopulation) {
            return true;
        }
        if (end == std::string_view::npos) {
            break;
        }
        position = end + 1U;
    }
    return false;
}

/** @brief 检查 OverlayFS option 中不安全的路径字符 / Check unsafe path characters for OverlayFS option syntax. */
[[nodiscard]] bool is_safe_overlay_path(const std::filesystem::path& path) {
    const std::string rendered = path.string();
    return rendered.find_first_of(",:\n\r") == std::string::npos;
}

/** @brief 添加一条 seccomp errno 拒绝规则 / Add a seccomp errno-deny rule. */
[[nodiscard]] Result<void> add_deny_rule(scmp_filter_ctx context, const int syscall_number) {
    if (seccomp_rule_add(context, SCMP_ACT_ERRNO(EPERM), syscall_number, 0) != 0) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "cannot add seccomp deny rule"));
    }
    return {};
}

/**
 * @brief 拒绝一个精确的 ioctl request 值 / Deny one exact ioctl request value.
 * @param context 待加载的 seccomp filter / Seccomp filter pending load.
 * @param request ioctl 的第二个参数 / Second argument of ioctl.
 * @return 成功或 fail-closed seccomp 错误 / Success or a fail-closed seccomp error.
 * @note 仅按 request 值拒绝，保留 Python 与 GNU 工具所需的无害 ioctl。
 *       This denies only the request value and preserves benign ioctls needed by Python and GNU tools.
 */
[[nodiscard]] Result<void> add_deny_ioctl_request_rule(const scmp_filter_ctx context, const unsigned long request) {
    if (seccomp_rule_add(
            context,
            SCMP_ACT_ERRNO(EPERM),
            SCMP_SYS(ioctl),
            1,
            SCMP_CMP(1, SCMP_CMP_EQ, request)) != 0) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "cannot add seccomp ioctl deny rule"));
    }
    return {};
}

/** @brief 清空 capability sets / Clear all capability sets. */
[[nodiscard]] Result<void> clear_capabilities() {
    cap_t empty = cap_init();
    if (empty == nullptr) {
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed, "cap_init"));
    }
    const int status = cap_set_proc(empty);
    const int saved_errno = errno;
    cap_free(empty);
    errno = saved_errno;
    if (status != 0) {
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed, "cap_set_proc"));
    }
    return {};
}

/** @brief 为 PID 1 安装最小 capability 集 / Install the minimal capability set for PID 1. */
[[nodiscard]] Result<void> install_supervisor_capabilities() {
    constexpr std::array<cap_value_t, 3> kSupervisorCapabilities{
        CAP_SETUID,
        CAP_SETGID,
        CAP_KILL,
    };
    cap_t capabilities = cap_init();
    if (capabilities == nullptr) {
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed, "cap_init supervisor"));
    }
    const int permitted = cap_set_flag(
        capabilities,
        CAP_PERMITTED,
        static_cast<int>(kSupervisorCapabilities.size()),
        kSupervisorCapabilities.data(),
        CAP_SET);
    const int effective = cap_set_flag(
        capabilities,
        CAP_EFFECTIVE,
        static_cast<int>(kSupervisorCapabilities.size()),
        kSupervisorCapabilities.data(),
        CAP_SET);
    const int applied = permitted == 0 && effective == 0 ? cap_set_proc(capabilities) : -1;
    const int saved_errno = errno;
    cap_free(capabilities);
    errno = saved_errno;
    if (applied != 0) {
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed, "install supervisor capabilities"));
    }
    return {};
}

}  // namespace

Result<void> validate_secure_directory_ancestry(
    const std::filesystem::path& path,
    const bool allow_insecure_dev_root) {
    const auto canonical = canonical_existing(path);
    if (!canonical) {
        return std::unexpected(canonical.error());
    }
    for (std::filesystem::path current = *canonical;; current = current.parent_path()) {
        struct stat metadata {};
        if (lstat(current.c_str(), &metadata) != 0) {
            return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed, "lstat secure directory ancestor"));
        }
        const bool terminal = current == *canonical;
        if (!S_ISDIR(metadata.st_mode) ||
            ((terminal || !allow_insecure_dev_root) &&
             (metadata.st_uid != 0U || (metadata.st_mode & (S_IWGRP | S_IWOTH)) != 0))) {
            return std::unexpected(make_error(
                ErrorCode::sandbox_preflight_failed,
                terminal
                    ? "secure directory must be root-owned and non-writable"
                    : "production directory ancestor must be root-owned and non-writable"));
        }
        if (current == current.root_path()) {
            return {};
        }
    }
}

Result<void> preflight_sandbox(const SandboxConfig& config) {
    if (geteuid() != 0) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "wspctld must run as root; no unprivileged fallback exists"));
    }
    if (const auto capabilities = require_broker_capabilities(); !capabilities) {
        return std::unexpected(capabilities.error());
    }
    const std::uint64_t effective_memory_high =
        config.memory_high_bytes == 0U ? config.memory_max_bytes : config.memory_high_bytes;
    if (config.sandbox_uid == 0U || config.sandbox_gid == 0U || config.memory_max_bytes == 0U ||
        effective_memory_high == 0U || effective_memory_high > config.memory_max_bytes ||
        config.cpu_max_quota_us == 0U || config.cpu_max_period_us < 1'000U || config.cpu_max_period_us > 1'000'000U ||
        config.pids_max == 0U || config.io_weight == 0U || config.io_weight > 10'000U) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "sandbox identity or cgroup resource policy is invalid"));
    }
    const auto image = validate_image_root(config.base_root, config.images_root);
    if (!image) {
        return std::unexpected(image.error());
    }
    const auto cgroup_root = canonical_existing(config.cgroup_root);
    if (!cgroup_root || !cgroup_root->string().starts_with("/sys/fs/cgroup/")) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "cgroup_root must be a delegated cgroup v2 subtree"));
    }
    const auto controllers = read_small_file(*cgroup_root / "cgroup.controllers");
    if (!controllers || !contains_token(*controllers, "cpu") || !contains_token(*controllers, "pids") || !contains_token(*controllers, "memory") ||
        !contains_token(*controllers, "io") ||
        access((*cgroup_root / "cgroup.procs").c_str(), W_OK) != 0 ||
        access((*cgroup_root / "cgroup.subtree_control").c_str(), W_OK) != 0 ||
        access((*cgroup_root / "cgroup.kill").c_str(), W_OK) != 0 ||
        access((*cgroup_root / "memory.high").c_str(), W_OK) != 0 ||
        access((*cgroup_root / "memory.swap.max").c_str(), W_OK) != 0 ||
        access((*cgroup_root / "memory.oom.group").c_str(), W_OK) != 0 ||
        access((*cgroup_root / "io.weight").c_str(), W_OK) != 0) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "cgroup v2 Delegate=yes subtree is unavailable"));
    }
    if (!config.state_root.is_absolute() || config.state_root == config.base_root ||
        !is_safe_overlay_path(config.base_root) || !is_safe_overlay_path(config.state_root)) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "unsafe state/base root configuration"));
    }
    const auto state_root = canonical_existing(config.state_root);
    if (!state_root) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "state_root must be pre-created by deployment"));
    }
    struct stat state_metadata {};
    if (lstat(state_root->c_str(), &state_metadata) != 0 || !S_ISDIR(state_metadata.st_mode) ||
        state_metadata.st_uid != 0U || (state_metadata.st_mode & (S_IWGRP | S_IWOTH)) != 0) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "state_root is not an owner-only root-owned directory"));
    }
    if (const auto quota = preflight_xfs_project_quota(config.xfs_project_quota, *state_root); !quota) {
        return std::unexpected(quota.error());
    }
    return {};
}

Result<void> prepare_broker_cgroup(const SandboxConfig& config) {
    const auto cgroup_root = canonical_existing(config.cgroup_root);
    if (!cgroup_root) {
        return std::unexpected(cgroup_root.error());
    }
    const std::filesystem::path manager_leaf = *cgroup_root / "wspctl-manager";
    if (const auto created = create_cgroup_directory(manager_leaf); !created) {
        return std::unexpected(created.error());
    }
    if (const auto moved = write_cgroup_file(manager_leaf / "cgroup.procs", std::to_string(getpid())); !moved) {
        return std::unexpected(moved.error());
    }
    return enable_runtime_controllers(*cgroup_root);
}

Result<TaskLayer> prepare_task_layer(
    const SandboxConfig& config,
    const XfsProjectQuota& quota,
    const RuntimeActivationLease& activation_lease,
    const std::string& activation_id) {
    if (activation_id.empty()) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "activation_id is required"));
    }
    const RuntimeQuotaBinding& binding = activation_lease.binding();
    const auto storage = quota.prepare_activation_storage(activation_lease, activation_id);
    if (!storage) {
        return std::unexpected(storage.error());
    }
    const TaskLayer layer{
        .quota_binding = binding,
        .activation_id = activation_id,
        .runtime_dir = binding.runtime_dir,
        .upper_dir = binding.workspace_dir / "upper",
        .work_dir = storage->workspace_work_dir,
        .root_dir = storage->control_activation_dir / "root",
        .workspace_lower_dir = storage->control_activation_dir / "workspace-lower",
        .merged_dir = storage->control_activation_dir / "root" / "workspace",
    };
    if (layer.runtime_dir.parent_path() != config.state_root / "runtimes" ||
        layer.upper_dir != binding.workspace_dir / "upper" ||
        layer.work_dir.parent_path() != binding.workspace_dir / "work" ||
        layer.root_dir.parent_path() != binding.control_dir / "mounts" / layer.root_dir.parent_path().filename() ||
        layer.workspace_lower_dir.parent_path() != layer.root_dir.parent_path() || layer.merged_dir != layer.root_dir / "workspace") {
        return std::unexpected(make_error(ErrorCode::internal, "XFS quota service returned an invalid task-layer layout"));
    }
    return layer;
}

Result<void> cleanup_task_layer(
    const SandboxConfig& config,
    const XfsProjectQuota& quota,
    const RuntimeActivationLease& activation_lease,
    const TaskLayer& layer) {
    const std::filesystem::path control_activation_dir = layer.root_dir.parent_path();
    if (!same_runtime_quota_binding(layer.quota_binding, activation_lease.binding()) ||
        layer.quota_binding.runtime_dir != layer.runtime_dir ||
        layer.runtime_dir.parent_path() != config.state_root / "runtimes" ||
        layer.upper_dir != layer.quota_binding.workspace_dir / "upper" ||
        layer.work_dir.parent_path() != layer.quota_binding.workspace_dir / "work" ||
        control_activation_dir.parent_path() != layer.quota_binding.control_dir / "mounts" ||
        layer.workspace_lower_dir.parent_path() != control_activation_dir || layer.root_dir != control_activation_dir / "root" ||
        layer.workspace_lower_dir != control_activation_dir / "workspace-lower" ||
        layer.merged_dir != layer.root_dir / "workspace") {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "task layer does not describe an owned staging tree"));
    }
    return quota.cleanup_activation_storage(activation_lease, layer.activation_id);
}

Result<void> reclaim_dead_task_layers(
    const SandboxConfig& config,
    const XfsProjectQuota& quota,
    const RuntimeActivationLease& activation_lease,
    const std::string& runtime_key) {
    if (runtime_key.empty() ||
        activation_lease.binding().runtime_dir != config.state_root / "runtimes" / hash_component(runtime_key)) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "runtime key does not match the activation lease binding"));
    }
    if (const auto task_free = wait_runtime_cgroup_empty(config, runtime_key); !task_free) {
        return std::unexpected(task_free.error());
    }
    return quota.reclaim_dead_activation_storage(activation_lease);
}

Result<void> setup_runtime_mounts(const SandboxConfig& config, const TaskLayer& layer) {
    if (mount(nullptr, "/", nullptr, MS_REC | MS_PRIVATE, nullptr) != 0) {
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed, "make mount propagation private"));
    }
    if (mount(config.base_root.c_str(), layer.root_dir.c_str(), nullptr, MS_BIND, nullptr) != 0) {
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed, "bind immutable base root"));
    }
    if (mount(nullptr, layer.root_dir.c_str(), nullptr, MS_BIND | MS_REMOUNT | MS_RDONLY | MS_NOSUID | MS_NODEV, nullptr) != 0) {
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed, "remount immutable base root readonly"));
    }
    const std::filesystem::path base_workspace = config.base_root / "workspace";
    struct stat workspace_metadata {};
    if (stat(base_workspace.c_str(), &workspace_metadata) != 0 || !S_ISDIR(workspace_metadata.st_mode)) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "immutable image must contain /workspace directory"));
    }
    if (mount(base_workspace.c_str(), layer.workspace_lower_dir.c_str(), nullptr, MS_BIND, nullptr) != 0 ||
        mount(nullptr, layer.workspace_lower_dir.c_str(), nullptr, MS_BIND | MS_REMOUNT | MS_RDONLY | MS_NOSUID | MS_NODEV, nullptr) != 0) {
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed, "bind readonly workspace lower"));
    }
    const std::string overlay_options = "lowerdir=" + layer.workspace_lower_dir.string() + ",upperdir=" + layer.upper_dir.string() +
                                        ",workdir=" + layer.work_dir.string() + ",metacopy=off,redirect_dir=nofollow,index=off";
    if (mount("overlay", layer.merged_dir.c_str(), "overlay", MS_NOSUID | MS_NODEV, overlay_options.c_str()) != 0) {
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed, "mount task OverlayFS"));
    }
    // /tmp must be present in the immutable image and becomes put_old before being replaced by a private tmpfs.
    if (syscall(SYS_pivot_root, layer.root_dir.c_str(), (layer.root_dir / "tmp").c_str()) != 0 || chdir("/") != 0 ||
        umount2("/tmp", MNT_DETACH) != 0) {
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed, "pivot into runtime root"));
    }
    /** @brief 以 hidepid=2 隔离 peer PID，并以 subset=pid 移除全局 proc 视图 / Isolate peer PIDs with hidepid=2 and remove global proc views with subset=pid. */
    if (mount("proc", "/proc", "proc", MS_NOSUID | MS_NODEV | MS_NOEXEC, "hidepid=2,subset=pid") != 0 ||
        mount("tmpfs", "/tmp", "tmpfs", MS_NOSUID | MS_NODEV | MS_NOEXEC, "mode=1777,size=64m") != 0 ||
        mount("tmpfs", "/dev", "tmpfs", MS_NOSUID | MS_NOEXEC, "mode=0755,size=64k") != 0) {
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed, "mount minimal proc/tmp/dev"));
    }
    if (mknod("/dev/null", S_IFCHR | 0666, makedev(1, 3)) != 0 ||
        mknod("/dev/zero", S_IFCHR | 0666, makedev(1, 5)) != 0 ||
        mknod("/dev/random", S_IFCHR | 0666, makedev(1, 8)) != 0 ||
        mknod("/dev/urandom", S_IFCHR | 0666, makedev(1, 9)) != 0) {
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed, "create minimal device nodes"));
    }
    return {};
}

Result<TaskCgroupControl> prepare_runtime_cgroup(
    const SandboxConfig& config,
    const std::string& runtime_key) {
    const std::filesystem::path runtime_cgroup = config.cgroup_root / "wspctl" / hash_component(runtime_key);
    const std::filesystem::path wspctl_cgroup = config.cgroup_root / "wspctl";
    const std::filesystem::path supervisor_leaf = runtime_cgroup / "supervisor";
    const std::filesystem::path task_leaf = runtime_cgroup / "task";
    std::error_code existing_error;
    const std::filesystem::file_status existing = std::filesystem::symlink_status(runtime_cgroup, existing_error);
    if (existing_error) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "inspect existing runtime cgroup: " + existing_error.message()));
    }
    if (std::filesystem::exists(existing)) {
        if (!std::filesystem::is_directory(existing)) {
            return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "existing runtime cgroup is not a directory"));
        }
        // A cold broker activation may follow an unclean broker restart.  Never merely observe
        // a stale runtime hierarchy: kill the whole runtime root (supervisor and task leaves)
        // and require cgroup.events populated 0 before reusing its persistent workspace key.
        if (const auto killed = kill_runtime_cgroup(config, runtime_key); !killed) {
            return std::unexpected(killed.error());
        }
    }
    if (const auto wspctl = create_cgroup_directory(wspctl_cgroup); !wspctl) {
        return std::unexpected(wspctl.error());
    }
    if (const auto controllers = enable_runtime_controllers(wspctl_cgroup); !controllers) {
        return std::unexpected(controllers.error());
    }
    if (const auto runtime = create_cgroup_directory(runtime_cgroup); !runtime) {
        return std::unexpected(runtime.error());
    }
    if (const auto controllers = enable_runtime_controllers(runtime_cgroup); !controllers) {
        return std::unexpected(controllers.error());
    }
    if (const auto supervisor = create_cgroup_directory(supervisor_leaf); !supervisor) {
        return std::unexpected(supervisor.error());
    }
    if (const auto task = create_cgroup_directory(task_leaf); !task) {
        return std::unexpected(task.error());
    }
    if (const auto memory = write_cgroup_file(runtime_cgroup / "memory.max", std::to_string(config.memory_max_bytes)); !memory) {
        return std::unexpected(memory.error());
    }
    const std::uint64_t effective_memory_high =
        config.memory_high_bytes == 0U ? config.memory_max_bytes : config.memory_high_bytes;
    if (const auto memory_high = write_cgroup_file(runtime_cgroup / "memory.high", std::to_string(effective_memory_high)); !memory_high) {
        return std::unexpected(memory_high.error());
    }
    if (const auto memory_swap = write_cgroup_file(runtime_cgroup / "memory.swap.max", std::to_string(config.memory_swap_max_bytes)); !memory_swap) {
        return std::unexpected(memory_swap.error());
    }
    if (const auto oom_group = write_cgroup_file(task_leaf / "memory.oom.group", "1"); !oom_group) {
        return std::unexpected(oom_group.error());
    }
    if (const auto cpu = write_cgroup_file(
            runtime_cgroup / "cpu.max",
            std::to_string(config.cpu_max_quota_us) + " " + std::to_string(config.cpu_max_period_us)); !cpu) {
        return std::unexpected(cpu.error());
    }
    if (const auto pids = write_cgroup_file(runtime_cgroup / "pids.max", std::to_string(config.pids_max)); !pids) {
        return std::unexpected(pids.error());
    }
    if (const auto io = write_cgroup_file(runtime_cgroup / "io.weight", std::to_string(config.io_weight)); !io) {
        return std::unexpected(io.error());
    }
    const int supervisor_procs_fd = open((supervisor_leaf / "cgroup.procs").c_str(), O_WRONLY | O_CLOEXEC | O_NOFOLLOW);
    if (supervisor_procs_fd < 0) {
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed, "open supervisor cgroup.procs"));
    }
    const int procs_fd = open((task_leaf / "cgroup.procs").c_str(), O_WRONLY | O_CLOEXEC | O_NOFOLLOW);
    if (procs_fd < 0) {
        static_cast<void>(close(supervisor_procs_fd));
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed, "open task cgroup.procs"));
    }
    const int kill_fd = open((task_leaf / "cgroup.kill").c_str(), O_WRONLY | O_CLOEXEC | O_NOFOLLOW);
    if (kill_fd < 0) {
        const Error error = errno_error(ErrorCode::sandbox_preflight_failed, "open task cgroup.kill");
        static_cast<void>(close(supervisor_procs_fd));
        static_cast<void>(close(procs_fd));
        return std::unexpected(error);
    }
    const int events_fd = open((task_leaf / "cgroup.events").c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (events_fd < 0) {
        const Error error = errno_error(ErrorCode::sandbox_preflight_failed, "open task cgroup.events");
        static_cast<void>(close(supervisor_procs_fd));
        static_cast<void>(close(procs_fd));
        static_cast<void>(close(kill_fd));
        return std::unexpected(error);
    }
    return TaskCgroupControl{
        .supervisor_procs_fd = supervisor_procs_fd,
        .procs_fd = procs_fd,
        .kill_fd = kill_fd,
        .events_fd = events_fd,
    };
}

Result<void> kill_runtime_cgroup(const SandboxConfig& config, const std::string& runtime_key) {
   const std::filesystem::path runtime_cgroup = config.cgroup_root / "wspctl" / hash_component(runtime_key);
    if (const auto killed = write_cgroup_file(runtime_cgroup / "cgroup.kill", "1"); !killed) {
        return std::unexpected(killed.error());
    }
    return wait_runtime_cgroup_empty(config, runtime_key);
}

Result<void> wait_runtime_cgroup_empty(const SandboxConfig& config, const std::string& runtime_key) {
    const std::filesystem::path events_path = config.cgroup_root / "wspctl" / hash_component(runtime_key) / "cgroup.events";
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(5);
    do {
        const auto events = read_small_file(events_path);
        if (!events) {
            return std::unexpected(events.error());
        }
        if (is_cgroup_empty(*events)) {
            return {};
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    } while (std::chrono::steady_clock::now() < deadline);
    return std::unexpected(make_error(ErrorCode::child_failure, "runtime cgroup remained populated after cgroup.kill"));
}

Result<void> harden_task(const uid_t uid, const gid_t gid) {
    if (geteuid() == 0U) {
        if (setgroups(0, nullptr) != 0 || setresgid(gid, gid, gid) != 0 || setresuid(uid, uid, uid) != 0) {
            return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed, "drop task identity"));
        }
    } else if (geteuid() != uid || getegid() != gid) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "unprivileged task identity mismatch"));
    }
    if (const auto capabilities = clear_capabilities(); !capabilities) {
        return std::unexpected(capabilities.error());
    }
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0) {
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed, "set no_new_privs"));
    }
    scmp_filter_ctx filter = seccomp_init(SCMP_ACT_ALLOW);
    if (filter == nullptr) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "initialize seccomp filter"));
    }
    /** @brief 不受 runtime namespace 充分隔离的高风险 syscall / High-risk syscalls not sufficiently isolated by runtime namespaces. */
    constexpr std::array<int, 18> kDeniedSyscalls{
        SCMP_SYS(mount),
        SCMP_SYS(umount2),
        SCMP_SYS(pivot_root),
        SCMP_SYS(unshare),
        SCMP_SYS(setns),
        SCMP_SYS(bpf),
        SCMP_SYS(ptrace),
        SCMP_SYS(kexec_load),
        SCMP_SYS(kexec_file_load),
        SCMP_SYS(open_by_handle_at),
        SCMP_SYS(keyctl),
        SCMP_SYS(add_key),
        SCMP_SYS(request_key),
        SCMP_SYS(perf_event_open),
        SCMP_SYS(userfaultfd),
        SCMP_SYS(init_module),
        SCMP_SYS(finit_module),
        SCMP_SYS(delete_module),
    };
    for (const int syscall_number : kDeniedSyscalls) {
        const auto rule = add_deny_rule(filter, syscall_number);
        if (!rule) {
            seccomp_release(filter);
            return std::unexpected(rule.error());
        }
    }
    // CLONE_NEWNET alone does not make every socket family harmless (notably AF_VSOCK can
    // have host-facing semantics on some kernels). Payload code receives no inherited network
    // sockets and may create only AF_UNIX endpoints inside its own mount namespace.
    if (seccomp_rule_add(
            filter,
            SCMP_ACT_ERRNO(EPERM),
            SCMP_SYS(socket),
            1,
            SCMP_CMP(0, SCMP_CMP_NE, AF_UNIX)) != 0 ||
        seccomp_rule_add(
            filter,
            SCMP_ACT_ERRNO(EPERM),
            SCMP_SYS(socketpair),
            1,
            SCMP_CMP(0, SCMP_CMP_NE, AF_UNIX)) != 0) {
        seccomp_release(filter);
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "cannot restrict task socket families"));
    }
    /** @brief XFS project quota 的 inode project-id/继承位变更必须由 broker 执行，payload 不能用 ioctl 篡改 / XFS project-quota inode project-id and inheritance changes must be broker-owned and cannot be changed by payload ioctl. */
#if defined(FS_IOC_FSSETXATTR) && defined(FS_IOC_SETFLAGS) && defined(FS_IOC32_SETFLAGS)
    if (const auto fssetxattr_rule = add_deny_ioctl_request_rule(filter, FS_IOC_FSSETXATTR); !fssetxattr_rule) {
        seccomp_release(filter);
        return std::unexpected(fssetxattr_rule.error());
    }
    /** @brief FS_IOC_SETFLAGS 是 legacy XFS_IOC_SETXFLAGS 的 generic UAPI spelling / FS_IOC_SETFLAGS is the generic UAPI spelling of legacy XFS_IOC_SETXFLAGS. */
    if (const auto setflags_rule = add_deny_ioctl_request_rule(filter, FS_IOC_SETFLAGS); !setflags_rule) {
        seccomp_release(filter);
        return std::unexpected(setflags_rule.error());
    }
    /** @brief 64-bit kernel 仍须拒绝 32-bit compat request；在同值 ABI 上避免重复 rule / A 64-bit kernel must also deny the 32-bit compat request; avoid a duplicate rule on equal-value ABIs. */
    if constexpr (FS_IOC32_SETFLAGS != FS_IOC_SETFLAGS) {
        if (const auto compat_setflags_rule = add_deny_ioctl_request_rule(filter, FS_IOC32_SETFLAGS); !compat_setflags_rule) {
            seccomp_release(filter);
            return std::unexpected(compat_setflags_rule.error());
        }
    }
#else
    seccomp_release(filter);
    return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "kernel headers do not expose required XFS project-quota ioctl constants"));
#endif
    constexpr std::array<unsigned long long, 8> kNamespaceFlags{
        CLONE_NEWNS,
        CLONE_NEWCGROUP,
        CLONE_NEWUTS,
        CLONE_NEWIPC,
        CLONE_NEWUSER,
        CLONE_NEWPID,
        CLONE_NEWNET,
#ifdef CLONE_NEWTIME
        CLONE_NEWTIME,
#else
        0U,
#endif
    };
    for (const unsigned long long flag : kNamespaceFlags) {
        if (flag == 0U) {
            continue;
        }
        if (seccomp_rule_add(
                filter,
                SCMP_ACT_ERRNO(EPERM),
                SCMP_SYS(clone),
                1,
                SCMP_CMP(0, SCMP_CMP_MASKED_EQ, flag, flag)) != 0) {
            seccomp_release(filter);
            return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "cannot deny namespace clone"));
        }
    }
#ifdef __NR_clone3
    if (const auto clone3_rule = add_deny_rule(filter, SCMP_SYS(clone3)); !clone3_rule) {
        seccomp_release(filter);
        return std::unexpected(clone3_rule.error());
    }
#endif
#ifdef __NR_process_vm_readv
    if (const auto process_vm_readv_rule = add_deny_rule(filter, SCMP_SYS(process_vm_readv)); !process_vm_readv_rule) {
        seccomp_release(filter);
        return std::unexpected(process_vm_readv_rule.error());
    }
#endif
#ifdef __NR_process_vm_writev
    if (const auto process_vm_writev_rule = add_deny_rule(filter, SCMP_SYS(process_vm_writev)); !process_vm_writev_rule) {
        seccomp_release(filter);
        return std::unexpected(process_vm_writev_rule.error());
    }
#endif
#ifdef __NR_pidfd_getfd
    if (const auto pidfd_getfd_rule = add_deny_rule(filter, SCMP_SYS(pidfd_getfd)); !pidfd_getfd_rule) {
        seccomp_release(filter);
        return std::unexpected(pidfd_getfd_rule.error());
    }
#endif
    /** @brief io_uring 可提交 IORING_OP_SOCKET，不能绕过仅 AF_UNIX 的 socket(2) 规则 / io_uring can submit IORING_OP_SOCKET and must not bypass the AF_UNIX-only socket(2) rule. */
#ifdef __NR_io_uring_setup
    if (const auto io_uring_setup_rule = add_deny_rule(filter, SCMP_SYS(io_uring_setup)); !io_uring_setup_rule) {
        seccomp_release(filter);
        return std::unexpected(io_uring_setup_rule.error());
    }
#endif
#ifdef __NR_io_uring_enter
    if (const auto io_uring_enter_rule = add_deny_rule(filter, SCMP_SYS(io_uring_enter)); !io_uring_enter_rule) {
        seccomp_release(filter);
        return std::unexpected(io_uring_enter_rule.error());
    }
#endif
#ifdef __NR_io_uring_register
    if (const auto io_uring_register_rule = add_deny_rule(filter, SCMP_SYS(io_uring_register)); !io_uring_register_rule) {
        seccomp_release(filter);
        return std::unexpected(io_uring_register_rule.error());
    }
#endif
#ifdef __NR_fsopen
    if (const auto fsopen_rule = add_deny_rule(filter, SCMP_SYS(fsopen)); !fsopen_rule) {
        seccomp_release(filter);
        return std::unexpected(fsopen_rule.error());
    }
#endif
#ifdef __NR_fsconfig
    if (const auto fsconfig_rule = add_deny_rule(filter, SCMP_SYS(fsconfig)); !fsconfig_rule) {
        seccomp_release(filter);
        return std::unexpected(fsconfig_rule.error());
    }
#endif
#ifdef __NR_fsmount
    if (const auto fsmount_rule = add_deny_rule(filter, SCMP_SYS(fsmount)); !fsmount_rule) {
        seccomp_release(filter);
        return std::unexpected(fsmount_rule.error());
    }
#endif
#ifdef __NR_move_mount
    if (const auto move_mount_rule = add_deny_rule(filter, SCMP_SYS(move_mount)); !move_mount_rule) {
        seccomp_release(filter);
        return std::unexpected(move_mount_rule.error());
    }
#endif
#ifdef __NR_open_tree
    if (const auto open_tree_rule = add_deny_rule(filter, SCMP_SYS(open_tree)); !open_tree_rule) {
        seccomp_release(filter);
        return std::unexpected(open_tree_rule.error());
    }
#endif
#ifdef __NR_mount_setattr
    if (const auto mount_setattr_rule = add_deny_rule(filter, SCMP_SYS(mount_setattr)); !mount_setattr_rule) {
        seccomp_release(filter);
        return std::unexpected(mount_setattr_rule.error());
    }
#endif
    if (seccomp_load(filter) != 0) {
        seccomp_release(filter);
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "load seccomp filter"));
    }
    seccomp_release(filter);
    return {};
}

Result<void> harden_supervisor() {
    if (geteuid() != 0U || getegid() != 0U) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed, "runtime PID 1 must start as root"));
    }
    // Lock away UID-0 implicit privilege regain before reducing the capability sets.  The
    // NO_SETUID_FIXUP bit deliberately lets the forked child retain the three temporary caps
    // while it calls setresgid/setresuid; harden_task immediately clears them afterwards.
    constexpr unsigned int kSecureBits =
        SECBIT_NOROOT | SECBIT_NOROOT_LOCKED | SECBIT_NO_SETUID_FIXUP | SECBIT_NO_SETUID_FIXUP_LOCKED;
    if (prctl(PR_SET_SECUREBITS, kSecureBits, 0, 0, 0) != 0) {
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed, "lock supervisor securebits"));
    }
    if (const auto capabilities = install_supervisor_capabilities(); !capabilities) {
        return std::unexpected(capabilities.error());
    }
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0) {
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed, "set supervisor no_new_privs"));
    }
    if (prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0) {
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed, "disable supervisor dumpability"));
    }
    return {};
}

}  // namespace wspctl
