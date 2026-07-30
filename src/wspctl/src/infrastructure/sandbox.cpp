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
#include <sys/vfs.h>

#include <seccomp.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <dirent.h>
#include <fcntl.h>
#include <fstream>
#include <grp.h>
#include <limits>
#include <linux/fs.h>
#include <linux/magic.h>
#include <linux/sched.h>
#include <linux/securebits.h>
#include <memory>
#include <mntent.h>
#include <sched.h>
#include <string>
#include <string_view>
#include <sys/sysmacros.h>
#include <thread>
#include <unistd.h>
#include <vector>

namespace wspctl {
namespace {

/**
 * @brief 一个由 LXCFS 动态映射的 procfs 文件 / One procfs file dynamically mapped from LXCFS.
 */
struct ProcVirtualFile final {
    /** @brief LXCFS root 下的相对源路径 / Relative source path below the LXCFS root. */
    std::string_view source;
    /** @brief runtime 内的 procfs 目标路径 / Procfs target path inside the runtime. */
    std::string_view target;
};

/** @brief 每个受支持 LXCFS 版本都必须动态呈现的核心 procfs 性能面 /
 * Core procfs performance surfaces that every supported LXCFS version must render dynamically. */
constexpr std::array<ProcVirtualFile, 8U> kRequiredCgroupAwareProcFiles{{
    {.source = "proc/cpuinfo", .target = "/proc/cpuinfo"},
    {.source = "proc/diskstats", .target = "/proc/diskstats"},
    {.source = "proc/loadavg", .target = "/proc/loadavg"},
    {.source = "proc/meminfo", .target = "/proc/meminfo"},
    {.source = "proc/slabinfo", .target = "/proc/slabinfo"},
    {.source = "proc/stat", .target = "/proc/stat"},
    {.source = "proc/swaps", .target = "/proc/swaps"},
    {.source = "proc/uptime", .target = "/proc/uptime"},
}};

/** @brief 仅较新 LXCFS 作为完整能力组提供的 PSI 节点 /
 * PSI nodes exposed as one complete capability group only by newer LXCFS versions. */
constexpr std::array<ProcVirtualFile, 3U> kPressureCgroupAwareProcFiles{{
    {.source = "proc/pressure/cpu", .target = "/proc/pressure/cpu"},
    {.source = "proc/pressure/io", .target = "/proc/pressure/io"},
    {.source = "proc/pressure/memory", .target = "/proc/pressure/memory"},
}};

/**
 * @brief 已验证的 LXCFS mount 能力 / Capabilities of a validated LXCFS mount.
 */
struct LxcfsMountProfile final {
    /** @brief canonical LXCFS root / 规范化 LXCFS root。 */
    std::filesystem::path root;
    /** @brief 是否完整提供三个 PSI 节点 / Whether all three PSI nodes are available. */
    bool pressure_available{};
};

/**
 * @brief LXCFS 可选 PSI 能力组状态 / State of the optional LXCFS PSI capability group.
 */
enum class PressureCapabilityState {
    /** @brief 三项均不存在，可安全遮蔽 / All three are absent and may be safely masked. */
    absent,
    /** @brief 三项全部存在，可安全映射 / All three are present and may be safely mapped. */
    complete,
    /** @brief 只存在部分节点，必须拒绝 / Only some nodes exist and must be rejected. */
    partial,
};

/**
 * @brief 将 PSI 节点数量归一化为不可拆分能力组状态 /
 * Normalize a PSI-node count into an indivisible capability-group state.
 * @param present_nodes 已验证为安全且可读的 PSI 节点数 / Number of safe and readable PSI nodes.
 * @return absent、complete 或 partial / Absent, complete, or partial.
 */
[[nodiscard]] constexpr PressureCapabilityState
classify_pressure_capability(const std::size_t present_nodes) noexcept {
    if (present_nodes == 0U) {
        return PressureCapabilityState::absent;
    }
    if (present_nodes == kPressureCgroupAwareProcFiles.size()) {
        return PressureCapabilityState::complete;
    }
    return PressureCapabilityState::partial;
}

static_assert(classify_pressure_capability(0U) == PressureCapabilityState::absent);
static_assert(classify_pressure_capability(1U) == PressureCapabilityState::partial);
static_assert(classify_pressure_capability(2U) == PressureCapabilityState::partial);
static_assert(classify_pressure_capability(3U) == PressureCapabilityState::complete);

/** @brief pivot 前固定 LXCFS mount 的私有 staging 路径 / Private staging path pinning the LXCFS
 * mount before pivot. */
constexpr std::string_view kStagedLxcfsRoot{"/run/.wspctl-lxcfs"};

/**
 * @brief 局部 FD 的 RAII owner / RAII owner for a local file descriptor.
 */
class OwnedFileDescriptor final {
public:
    /**
     * @brief 接管 FD / Take ownership of an FD.
     * @param descriptor 要接管的 FD / FD to own.
     */
    explicit OwnedFileDescriptor(const int descriptor = -1) noexcept : descriptor_(descriptor) {}

    /** @brief 析构时关闭 FD / Close the FD on destruction. */
    ~OwnedFileDescriptor() {
        if (descriptor_ >= 0) {
            static_cast<void>(close(descriptor_));
        }
    }

    /** @brief 禁止复制 / Disable copying. */
    OwnedFileDescriptor(const OwnedFileDescriptor&) = delete;
    /** @brief 禁止复制赋值 / Disable copy assignment. */
    OwnedFileDescriptor& operator=(const OwnedFileDescriptor&) = delete;

    /**
     * @brief 取得借用 FD / Get the borrowed FD.
     * @return 借用 FD / Borrowed FD.
     */
    [[nodiscard]] int get() const noexcept { return descriptor_; }

private:
    /** @brief 被持有的 FD / Owned FD. */
    int descriptor_;
};

/**
 * @brief ``DIR`` stream 的 RAII deleter / RAII deleter for a ``DIR`` stream.
 */
struct DirectoryStreamCloser final {
    /**
     * @brief 关闭目录流 / Close a directory stream.
     * @param directory 可为空的目录流 / Nullable directory stream.
     */
    void operator()(DIR* const directory) const noexcept {
        if (directory != nullptr) {
            static_cast<void>(closedir(directory));
        }
    }
};

/** @brief 是否具有指定 effective Linux capability / Whether an effective Linux capability is
 * present. */
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

/**
 * @brief broker 启动所需的 capability 描述 / Capability required during broker startup.
 */
struct RequiredCapability final {
    /** @brief Linux capability 数值 / Linux capability value. */
    cap_value_t value;
    /** @brief 稳定诊断名称 / Stable diagnostic name. */
    std::string_view name;
};

/** @brief 校验 broker 必须持有的一组 capability / Validate the capability set mandatory for broker.
 */
[[nodiscard]] Result<void> require_broker_capabilities() {
    // CAP_SETPCAP is deliberately startup-only: namespace PID 1 needs it to lock securebits and
    // remove every non-supervisor capability from its bounding set, then drops CAP_SETPCAP too.
    constexpr std::array<RequiredCapability, 6> kRequired{{
        {.value = CAP_SYS_ADMIN, .name = "CAP_SYS_ADMIN"},
        {.value = CAP_SYS_CHROOT, .name = "CAP_SYS_CHROOT"},
        {.value = CAP_SETUID, .name = "CAP_SETUID"},
        {.value = CAP_SETGID, .name = "CAP_SETGID"},
        {.value = CAP_SETPCAP, .name = "CAP_SETPCAP"},
        {.value = CAP_MKNOD, .name = "CAP_MKNOD"},
    }};
    for (const RequiredCapability& capability : kRequired) {
        const auto present = has_effective_capability(capability.value);
        if (!present) {
            return std::unexpected(present.error());
        }
        if (!*present) {
            return std::unexpected(
                make_error(ErrorCode::sandbox_preflight_failed,
                           "required Linux capability is absent: " + std::string(capability.name)));
        }
    }
    return {};
}

/** @brief 读取一个小文本文件 / Read a small text file. */
[[nodiscard]] Result<std::string> read_small_file(const std::filesystem::path& path) {
    constexpr std::size_t kMaximumSize = 64U * 1024U;
    std::ifstream input(path);
    if (!input.is_open()) {
        return std::unexpected(
            errno_error(ErrorCode::sandbox_preflight_failed, "open " + path.string()));
    }

    std::string contents;
    std::array<char, 4096U> buffer{};
    while (input) {
        input.read(buffer.data(), static_cast<std::streamsize>(buffer.size()));
        const std::streamsize count = input.gcount();
        if (count <= 0) {
            continue;
        }
        const auto byte_count = static_cast<std::size_t>(count);
        if (byte_count > kMaximumSize - contents.size()) {
            return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed,
                                              "cgroup metadata exceeds 64 KiB: " + path.string()));
        }
        contents.append(buffer.data(), byte_count);
    }
    if (input.bad() || (input.fail() && !input.eof())) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed,
                                          "read cgroup metadata: " + path.string()));
    }
    return contents;
}

/** @brief 判断文字列表是否包含 token / Check whether a textual list contains a token. */
[[nodiscard]] bool contains_token(const std::string_view text, const std::string_view token) {
    std::size_t position = 0;
    while (position < text.size()) {
        while (position < text.size() &&
               (text[position] == ' ' || text[position] == '\n' || text[position] == '\t')) {
            ++position;
        }
        const std::size_t end = text.find_first_of(" \n\t", position);
        const std::string_view found = text.substr(
            position, end == std::string_view::npos ? text.size() - position : end - position);
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

/** @brief 把 path 解析成存在的 canonical absolute path / Resolve path to an existing canonical
 * absolute path. */
[[nodiscard]] Result<std::filesystem::path> canonical_existing(const std::filesystem::path& path) {
    if (!path.is_absolute()) {
        return std::unexpected(
            make_error(ErrorCode::invalid_argument, "sandbox path must be absolute"));
    }
    std::error_code error;
    const std::filesystem::path canonical = std::filesystem::canonical(path, error);
    if (error) {
        return std::unexpected(
            make_error(ErrorCode::not_found, "canonical sandbox path: " + error.message()));
    }
    return canonical;
}

/** @brief 将文字 SHA-256 为不含用户路径的目录名 / SHA-256 text into a directory name without user
 * path parts. */
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
 * @brief 按 CPU quota 计算 runtime 可并行调度的 CPU 数 /
 * Compute the runtime's schedulable CPU parallelism from its CPU quota.
 * @param quota_us 每 period 可运行的微秒数 / Runnable microseconds per period.
 * @param period_us CFS quota period 微秒数 / CFS quota period in microseconds.
 * @param available_cpus delegated cpuset 中可用的 CPU 数 / CPU count available in the delegated
 * cpuset.
 * @return 向上取整并限制到 delegated cpuset 的 CPU 数 / Ceiling CPU count clamped to the delegated
 * cpuset.
 * @note fractional quota 仍需要一个 CPU；cpu.max 继续负责时间份额限制 /
 * Fractional quota still needs one CPU; cpu.max remains responsible for time-share enforcement.
 */
[[nodiscard]] constexpr std::size_t
runtime_cpu_parallelism(const std::uint64_t quota_us, const std::uint64_t period_us,
                        const std::size_t available_cpus) noexcept {
    if (period_us == 0U || available_cpus == 0U) {
        return 0U;
    }
    const std::uint64_t quota_cpus =
        quota_us / period_us + static_cast<std::uint64_t>(quota_us % period_us != 0U);
    return std::min<std::size_t>(available_cpus, static_cast<std::size_t>(std::min<std::uint64_t>(
                                                     std::max<std::uint64_t>(quota_cpus, 1U),
                                                     std::numeric_limits<std::size_t>::max())));
}

static_assert(runtime_cpu_parallelism(50'000U, 100'000U, 20U) == 1U);
static_assert(runtime_cpu_parallelism(200'000U, 100'000U, 20U) == 2U);
static_assert(runtime_cpu_parallelism(250'000U, 100'000U, 20U) == 3U);
static_assert(runtime_cpu_parallelism(4'000'000U, 100'000U, 20U) == 20U);
static_assert(runtime_cpu_parallelism(200'000U, 0U, 20U) == 0U);
static_assert(runtime_cpu_parallelism(200'000U, 100'000U, 0U) == 0U);

/**
 * @brief 解析内核 cpuset CPU list 格式 / Parse the kernel cpuset CPU-list format.
 * @param text 例如 ``0-3,8,10-11`` 的内核文本 / Kernel text such as ``0-3,8,10-11``.
 * @return 严格递增且去重的 CPU ID，或 fail-closed 错误 / Sorted unique CPU IDs or a fail-closed
 * error.
 */
[[nodiscard]] Result<std::vector<std::uint32_t>> parse_cpuset_cpu_list(std::string_view text) {
    constexpr std::uint32_t kMaximumCpuId = 1U << 20U;
    while (!text.empty() && (text.back() == '\n' || text.back() == '\r' || text.back() == ' ' ||
                             text.back() == '\t')) {
        text.remove_suffix(1U);
    }
    while (!text.empty() && (text.front() == ' ' || text.front() == '\t')) {
        text.remove_prefix(1U);
    }
    if (text.empty()) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed,
                                          "delegated cpuset has no effective CPUs"));
    }

    /** @brief 展开后的 CPU ID / Expanded CPU IDs. */
    std::vector<std::uint32_t> cpus;
    std::size_t position = 0U;
    while (position < text.size()) {
        const std::size_t comma = text.find(',', position);
        const std::size_t end = comma == std::string_view::npos ? text.size() : comma;
        const std::string_view token = text.substr(position, end - position);
        const std::size_t dash = token.find('-');
        if (token.empty() || (dash != std::string_view::npos &&
                              token.find('-', dash + 1U) != std::string_view::npos)) {
            return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed,
                                              "delegated cpuset CPU list is malformed"));
        }

        const auto parse_id = [](const std::string_view value) -> std::optional<std::uint32_t> {
            if (value.empty()) {
                return std::nullopt;
            }
            std::uint64_t parsed = 0U;
            for (const char digit : value) {
                if (digit < '0' || digit > '9') {
                    return std::nullopt;
                }
                const std::uint64_t value_digit = static_cast<std::uint64_t>(digit - '0');
                if (parsed > (kMaximumCpuId - value_digit) / 10U) {
                    return std::nullopt;
                }
                parsed = parsed * 10U + value_digit;
            }
            return static_cast<std::uint32_t>(parsed);
        };

        const auto first = parse_id(token.substr(0U, dash));
        const auto last =
            dash == std::string_view::npos ? first : parse_id(token.substr(dash + 1U));
        if (!first.has_value() || !last.has_value() || *last < *first) {
            return std::unexpected(
                make_error(ErrorCode::sandbox_preflight_failed,
                           "delegated cpuset CPU list contains an invalid range"));
        }
        if (static_cast<std::uint64_t>(*last) - *first + 1U >
            static_cast<std::uint64_t>(kMaximumCpuId) + 1U - cpus.size()) {
            return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed,
                                              "delegated cpuset CPU list is unreasonably large"));
        }
        for (std::uint32_t cpu = *first;; ++cpu) {
            cpus.push_back(cpu);
            if (cpu == *last) {
                break;
            }
        }
        if (comma == std::string_view::npos) {
            break;
        }
        if (comma + 1U == text.size()) {
            return std::unexpected(
                make_error(ErrorCode::sandbox_preflight_failed,
                           "delegated cpuset CPU list has a trailing separator"));
        }
        position = comma + 1U;
    }
    std::ranges::sort(cpus);
    if (std::ranges::adjacent_find(cpus) != cpus.end()) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed,
                                          "delegated cpuset CPU list contains duplicate CPUs"));
    }
    return cpus;
}

/**
 * @brief 为 runtime 选择稳定且分散的 cpuset / Select a stable, distributed cpuset for one runtime.
 * @param effective_cpus delegated parent 的 cpuset.cpus.effective / Delegated parent's
 * cpuset.cpus.effective.
 * @param quota_us cpu.max quota 微秒数 / cpu.max quota in microseconds.
 * @param period_us cpu.max period 微秒数 / cpu.max period in microseconds.
 * @param runtime_key runtime 隔离键 / Runtime isolation key.
 * @return 可直接写入 cpuset.cpus 的 CPU list / CPU list ready for cpuset.cpus.
 */
[[nodiscard]] Result<std::string> select_runtime_cpuset(const std::string_view effective_cpus,
                                                        const std::uint64_t quota_us,
                                                        const std::uint64_t period_us,
                                                        const std::string_view runtime_key) {
    const auto available = parse_cpuset_cpu_list(effective_cpus);
    if (!available) {
        return std::unexpected(available.error());
    }
    const std::size_t count = runtime_cpu_parallelism(quota_us, period_us, available->size());
    if (count == 0U) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed,
                                          "runtime CPU parallelism resolved to zero"));
    }

    const std::string digest = hash_component(runtime_key);
    std::uint64_t seed = 0U;
    for (std::size_t index = 0U; index < 16U; ++index) {
        const char digit = digest[index];
        seed = (seed << 4U) |
               static_cast<std::uint64_t>(digit <= '9' ? digit - '0' : digit - 'a' + 10);
    }
    const std::size_t offset = static_cast<std::size_t>(seed % available->size());
    std::vector<std::uint32_t> selected;
    selected.reserve(count);
    for (std::size_t index = 0U; index < count; ++index) {
        selected.push_back((*available)[(offset + index) % available->size()]);
    }
    std::ranges::sort(selected);

    std::string rendered;
    for (const std::uint32_t cpu : selected) {
        if (!rendered.empty()) {
            rendered.push_back(',');
        }
        rendered += std::to_string(cpu);
    }
    return rendered;
}

/**
 * @brief 判断两个 quota binding 是否描述同一个 runtime project pair / Check whether two quota
 * bindings describe the same runtime project pair.
 * @param left 左侧 binding / Left binding.
 * @param right 右侧 binding / Right binding.
 * @return 所有路径及 project ID 都相同则为真 / True when every path and project ID matches.
 */
[[nodiscard]] bool same_runtime_quota_binding(const RuntimeQuotaBinding& left,
                                              const RuntimeQuotaBinding& right) noexcept {
    return left.runtime_dir == right.runtime_dir && left.control_dir == right.control_dir &&
           left.workspace_dir == right.workspace_dir &&
           left.control_project_id == right.control_project_id &&
           left.workspace_project_id == right.workspace_project_id;
}

/** @brief 创建 cgroup 目录（不能 chmod cgroupfs） / Create a cgroup directory without chmod on
 * cgroupfs. */
[[nodiscard]] Result<void> create_cgroup_directory(const std::filesystem::path& path) {
    std::error_code error;
    std::filesystem::create_directories(path, error);
    if (error) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed,
                                          "create cgroup directory: " + error.message()));
    }
    return {};
}

/** @brief 原子语义写 cgroup 控制文件 / Write a cgroup control file. */
[[nodiscard]] Result<void> write_cgroup_file(const std::filesystem::path& path,
                                             const std::string_view value) {
    const int fd = open(path.c_str(), O_WRONLY | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0) {
        return std::unexpected(
            errno_error(ErrorCode::sandbox_preflight_failed, "open cgroup control file"));
    }
    std::size_t offset = 0;
    while (offset < value.size()) {
        const ssize_t count =
            write(fd, value.data() + static_cast<std::ptrdiff_t>(offset), value.size() - offset);
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count <= 0) {
            const Error error =
                errno_error(ErrorCode::sandbox_preflight_failed, "write cgroup control file");
            static_cast<void>(close(fd));
            return std::unexpected(error);
        }
        offset += static_cast<std::size_t>(count);
    }
    if (close(fd) != 0) {
        return std::unexpected(
            errno_error(ErrorCode::sandbox_preflight_failed, "close cgroup control file"));
    }
    return {};
}

/**
 * @brief 启用 delegated cgroup 所需 controllers / Enable controllers required in a delegated
 * cgroup.
 * @param cgroup_root delegated cgroup 根 / Delegated cgroup root.
 * @param enable_io host 支持且配置了相对 I/O 权重 / Host supports and configures relative I/O
 * weighting.
 * @return 成功或精确 cgroup 写入错误 / Success or a precise cgroup-write error.
 */
[[nodiscard]] Result<void> enable_runtime_controllers(const std::filesystem::path& cgroup_root,
                                                      const bool enable_io) {
    const auto enabled = read_small_file(cgroup_root / "cgroup.subtree_control");
    if (!enabled) {
        return std::unexpected(enabled.error());
    }
    const bool core_enabled =
        contains_token(*enabled, "cpuset") && contains_token(*enabled, "cpu") &&
        contains_token(*enabled, "memory") && contains_token(*enabled, "pids");
    if (core_enabled && (!enable_io || contains_token(*enabled, "io"))) {
        return {};
    }
    const std::string requested =
        enable_io ? "+cpuset +cpu +memory +pids +io" : "+cpuset +cpu +memory +pids";
    return write_cgroup_file(cgroup_root / "cgroup.subtree_control", requested);
}

/** @brief 判断 cgroup.events 是否明确报告 populated 0 / Check whether cgroup.events explicitly
 * reports populated 0. */
[[nodiscard]] bool is_cgroup_empty(const std::string_view events) {
    constexpr std::string_view kPopulation{"populated 0"};
    std::size_t position = 0;
    while (position < events.size()) {
        const std::size_t end = events.find('\n', position);
        const std::string_view line = events.substr(
            position, end == std::string_view::npos ? events.size() - position : end - position);
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

/** @brief 检查 OverlayFS option 中不安全的路径字符 / Check unsafe path characters for OverlayFS
 * option syntax. */
[[nodiscard]] bool is_safe_overlay_path(const std::filesystem::path& path) {
    const std::string rendered = path.string();
    return rendered.find_first_of(",:\n\r") == std::string::npos;
}

/** @brief 添加一条 seccomp errno 拒绝规则 / Add a seccomp errno-deny rule. */
[[nodiscard]] Result<void> add_deny_rule(scmp_filter_ctx context, const int syscall_number) {
    if (seccomp_rule_add(context, SCMP_ACT_ERRNO(EPERM), syscall_number, 0) != 0) {
        return std::unexpected(
            make_error(ErrorCode::sandbox_preflight_failed, "cannot add seccomp deny rule"));
    }
    return {};
}

/**
 * @brief 拒绝一个精确的 ioctl request 值 / Deny one exact ioctl request value.
 * @param context 待加载的 seccomp filter / Seccomp filter pending load.
 * @param request ioctl 的第二个参数 / Second argument of ioctl.
 * @return 成功或 fail-closed seccomp 错误 / Success or a fail-closed seccomp error.
 * @note 仅按 request 值拒绝，保留 Python 与 GNU 工具所需的无害 ioctl。
 *       This denies only the request value and preserves benign ioctls needed by Python and GNU
 * tools.
 */
[[nodiscard]] Result<void> add_deny_ioctl_request_rule(const scmp_filter_ctx context,
                                                       const unsigned long request) {
    if (seccomp_rule_add(context, SCMP_ACT_ERRNO(EPERM), SCMP_SYS(ioctl), 1,
                         SCMP_CMP(1, SCMP_CMP_EQ, request)) != 0) {
        return std::unexpected(
            make_error(ErrorCode::sandbox_preflight_failed, "cannot add seccomp ioctl deny rule"));
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
        return std::unexpected(
            errno_error(ErrorCode::sandbox_preflight_failed, "cap_init supervisor"));
    }
    const int permitted =
        cap_set_flag(capabilities, CAP_PERMITTED, static_cast<int>(kSupervisorCapabilities.size()),
                     kSupervisorCapabilities.data(), CAP_SET);
    if (permitted != 0) {
        const int saved_errno = errno;
        cap_free(capabilities);
        errno = saved_errno;
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed,
                                           "configure supervisor permitted capabilities"));
    }
    const int effective =
        cap_set_flag(capabilities, CAP_EFFECTIVE, static_cast<int>(kSupervisorCapabilities.size()),
                     kSupervisorCapabilities.data(), CAP_SET);
    if (effective != 0) {
        const int saved_errno = errno;
        cap_free(capabilities);
        errno = saved_errno;
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed,
                                           "configure supervisor effective capabilities"));
    }
    const int applied = cap_set_proc(capabilities);
    const int saved_errno = errno;
    cap_free(capabilities);
    errno = saved_errno;
    if (applied != 0) {
        return std::unexpected(
            errno_error(ErrorCode::sandbox_preflight_failed, "install supervisor capabilities"));
    }
    return {};
}

/** @brief 判断 PID 1 完成 hardening 后是否仍需一个 capability / Check whether PID 1 still needs a
 * capability after hardening. */
[[nodiscard]] bool supervisor_retains_capability(const int capability) noexcept {
    return capability == CAP_SETUID || capability == CAP_SETGID || capability == CAP_KILL;
}

/**
 * @brief 将 PID 1 capability bounding set 收口到最终最小集合 /
 * Restrict the PID 1 capability bounding set to its final minimal set.
 * @return 成功或 fail-closed prctl 错误 / Success or a fail-closed prctl error.
 * @note capability 编号由内核按 0..cap_last_cap 连续分配；探测到首个 EINVAL 即到达运行中
 *       内核的上界，避免编译期 headers 落后于 host kernel 时遗漏新 capability。/
 *       Capability numbers are contiguous from zero through cap_last_cap; the first EINVAL is
 *       therefore the running kernel boundary, avoiding leaks when build-time headers lag the host.
 */
[[nodiscard]] Result<void> constrain_supervisor_bounding_set() {
    /** @brief 防止异常内核接口导致无界探测的保守上限 / Conservative guard against an unbounded
     * anomalous kernel interface. */
    constexpr int kCapabilityProbeLimit = 1024;
    /** @brief 是否已观察到运行中内核的 capability 上界 / Whether the running-kernel capability
     * boundary was observed. */
    bool found_kernel_boundary = false;
    for (int capability = 0; capability < kCapabilityProbeLimit; ++capability) {
        errno = 0;
        /** @brief 当前 capability 是否存在于 bounding set / Whether this capability is in the
         * bounding set. */
        const int present = prctl(PR_CAPBSET_READ, capability, 0, 0, 0);
        if (present < 0) {
            if (errno == EINVAL) {
                found_kernel_boundary = true;
                break;
            }
            return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed,
                                               "inspect supervisor capability bounding set"));
        }
        if (present == 0 || supervisor_retains_capability(capability) ||
            capability == CAP_SETPCAP) {
            continue;
        }
        if (prctl(PR_CAPBSET_DROP, capability, 0, 0, 0) != 0) {
            return std::unexpected(
                errno_error(ErrorCode::sandbox_preflight_failed, "drop non-supervisor capability " +
                                                                     std::to_string(capability) +
                                                                     " from bounding set"));
        }
    }
    if (!found_kernel_boundary) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed,
                                          "kernel capability boundary exceeds hard safety limit"));
    }
    // CAP_SETPCAP must be last: every preceding PR_CAPBSET_DROP requires it in the effective set.
    if (prctl(PR_CAPBSET_DROP, CAP_SETPCAP, 0, 0, 0) != 0) {
        return std::unexpected(
            errno_error(ErrorCode::sandbox_preflight_failed,
                        "drop transient CAP_SETPCAP from supervisor bounding set"));
    }
    for (const int capability : {CAP_SETUID, CAP_SETGID, CAP_KILL}) {
        /** @brief retained capability 的 readback / Readback of a retained capability. */
        const int retained = prctl(PR_CAPBSET_READ, capability, 0, 0, 0);
        if (retained < 0) {
            return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed,
                                               "verify retained supervisor capability " +
                                                   std::to_string(capability) +
                                                   " in bounding set"));
        }
        if (retained != 1) {
            return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed,
                                              "required supervisor capability " +
                                                  std::to_string(capability) +
                                                  " is absent from bounding set"));
        }
    }
    /** @brief CAP_SETPCAP bounding-set readback / CAP_SETPCAP bounding-set readback. */
    const int transient_capability = prctl(PR_CAPBSET_READ, CAP_SETPCAP, 0, 0, 0);
    if (transient_capability < 0) {
        return std::unexpected(
            errno_error(ErrorCode::sandbox_preflight_failed,
                        "verify transient CAP_SETPCAP removal from supervisor bounding set"));
    }
    if (transient_capability != 0) {
        return std::unexpected(
            make_error(ErrorCode::sandbox_preflight_failed,
                       "transient CAP_SETPCAP remains in supervisor bounding set"));
    }
    return {};
}

/**
 * @brief 确认路径本身是 ``fuse.lxcfs`` mountpoint / Confirm that the path itself is a
 * ``fuse.lxcfs`` mountpoint.
 * @param canonical_root 已规范化的候选 mountpoint / Canonical candidate mountpoint.
 * @return 仅当 mount table 中存在完全匹配的 LXCFS mount 时为真 /
 * True only when the mount table contains an exact LXCFS mount match.
 */
[[nodiscard]] bool is_lxcfs_mount(const std::filesystem::path& canonical_root) {
    /** @brief 当前 mount namespace 的 mount table / Mount table for the current mount namespace. */
    FILE* const mounts = setmntent("/proc/self/mounts", "re");
    if (mounts == nullptr) {
        return false;
    }
    /** @brief ``getmntent_r`` 的解析 buffer / Parsing buffer for ``getmntent_r``. */
    std::array<char, 4096U> buffer{};
    /** @brief 只计算一次的 canonical mountpoint 文本 / Canonical mountpoint text computed once. */
    const std::string canonical_text = canonical_root.string();
    /** @brief 当前 mount table entry / Current mount-table entry. */
    struct mntent entry {};
    /** @brief 是否找到精确 LXCFS mount / Whether an exact LXCFS mount was found. */
    bool found = false;
    while (getmntent_r(mounts, &entry, buffer.data(), static_cast<int>(buffer.size())) != nullptr) {
        if (canonical_text == entry.mnt_dir && std::string_view(entry.mnt_type) == "fuse.lxcfs") {
            found = true;
            break;
        }
    }
    static_cast<void>(endmntent(mounts));
    return found;
}

/**
 * @brief 验证专用 LXCFS mount 与全部 cgroup-aware procfs 节点 /
 * Validate the dedicated LXCFS mount and every cgroup-aware procfs node.
 * @param configured_root operator 固定的 LXCFS root / Operator-fixed LXCFS root.
 * @return 节点安全且能力组完整的 LXCFS profile 或 fail-closed 错误 /
 * LXCFS profile with safe nodes and complete capability groups, or a fail-closed error.
 */
[[nodiscard]] Result<LxcfsMountProfile>
validate_lxcfs_root(const std::filesystem::path& configured_root) {
    const auto canonical = canonical_existing(configured_root);
    if (!canonical) {
        return std::unexpected(
            make_error(ErrorCode::sandbox_preflight_failed,
                       "dedicated LXCFS root is unavailable: " + canonical.error().message));
    }
    /** @brief LXCFS root metadata / LXCFS root metadata. */
    struct stat metadata {};
    /** @brief LXCFS FUSE superblock metadata / LXCFS FUSE superblock metadata. */
    struct statfs filesystem {};
    if (lstat(canonical->c_str(), &metadata) != 0 || !S_ISDIR(metadata.st_mode) ||
        metadata.st_uid != 0U || metadata.st_gid != 0U ||
        (metadata.st_mode & (S_IWGRP | S_IWOTH)) != 0 ||
        statfs(canonical->c_str(), &filesystem) != 0 || filesystem.f_type != FUSE_SUPER_MAGIC ||
        !is_lxcfs_mount(*canonical)) {
        return std::unexpected(
            make_error(ErrorCode::sandbox_preflight_failed,
                       "dedicated LXCFS root must be a root-owned non-writable FUSE mount"));
    }
    /**
     * @brief 探测并验证一个 LXCFS 动态文件 / Probe and validate one dynamic LXCFS file.
     * @param mapping procfs source/target 描述 / Procfs source/target description.
     * @return 安全且能读取动态数据时为 true，不存在时为 false，其他情况报错 /
     * True when safe and dynamically readable, false when absent, or an error otherwise.
     */
    const auto validate_file = [&canonical](const ProcVirtualFile& mapping) -> Result<bool> {
        /** @brief 当前 LXCFS 动态文件路径 / Current LXCFS dynamic-file path. */
        const std::filesystem::path source = *canonical / mapping.source;
        /** @brief 防 symlink 打开的动态文件 FD / Dynamic-file FD opened without following symlinks.
         */
        OwnedFileDescriptor descriptor(open(source.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW));
        if (descriptor.get() < 0 && errno == ENOENT) {
            return false;
        }
        struct stat source_metadata {};
        if (descriptor.get() < 0 || fstat(descriptor.get(), &source_metadata) != 0 ||
            !S_ISREG(source_metadata.st_mode) || source_metadata.st_uid != 0U ||
            source_metadata.st_gid != 0U) {
            return std::unexpected(
                make_error(ErrorCode::sandbox_preflight_failed,
                           "LXCFS procfs source is missing or unsafe: " + source.string()));
        }
        /** @brief 非空动态响应的单字节探针 / One-byte probe requiring a nonempty dynamic response.
         */
        char probe{};
        ssize_t count{};
        do {
            count = read(descriptor.get(), &probe, 1U);
        } while (count < 0 && errno == EINTR);
        if (count != 1) {
            return std::unexpected(
                make_error(ErrorCode::sandbox_preflight_failed,
                           "LXCFS procfs source did not produce dynamic data: " + source.string()));
        }
        return true;
    };
    for (const ProcVirtualFile& mapping : kRequiredCgroupAwareProcFiles) {
        const auto present = validate_file(mapping);
        if (!present) {
            return std::unexpected(present.error());
        }
        if (!*present) {
            return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed,
                                              "required LXCFS procfs source is missing: " +
                                                  (*canonical / mapping.source).string()));
        }
    }
    /** @brief 当前 LXCFS 实际提供的 PSI 节点数 / Number of PSI nodes actually exposed by this
     * LXCFS. */
    std::size_t pressure_nodes = 0U;
    for (const ProcVirtualFile& mapping : kPressureCgroupAwareProcFiles) {
        const auto present = validate_file(mapping);
        if (!present) {
            return std::unexpected(present.error());
        }
        pressure_nodes += *present ? 1U : 0U;
    }
    /** @brief 原子 PSI 能力组的分类结果 / Classification of the atomic PSI capability group. */
    const PressureCapabilityState pressure_state = classify_pressure_capability(pressure_nodes);
    if (pressure_state == PressureCapabilityState::partial) {
        return std::unexpected(
            make_error(ErrorCode::sandbox_preflight_failed,
                       "LXCFS exposes only part of the procfs pressure capability group"));
    }
    return LxcfsMountProfile{
        .root = *canonical,
        .pressure_available = pressure_state == PressureCapabilityState::complete,
    };
}

} // namespace

Result<void> validate_secure_directory_ancestry(const std::filesystem::path& path,
                                                const bool allow_insecure_dev_root) {
    const auto canonical = canonical_existing(path);
    if (!canonical) {
        return std::unexpected(canonical.error());
    }
    for (std::filesystem::path current = *canonical;; current = current.parent_path()) {
        struct stat metadata {};
        if (lstat(current.c_str(), &metadata) != 0) {
            return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed,
                                               "lstat secure directory ancestor"));
        }
        const bool terminal = current == *canonical;
        if (!S_ISDIR(metadata.st_mode) ||
            ((terminal || !allow_insecure_dev_root) &&
             (metadata.st_uid != 0U || (metadata.st_mode & (S_IWGRP | S_IWOTH)) != 0))) {
            return std::unexpected(make_error(
                ErrorCode::sandbox_preflight_failed,
                terminal ? "secure directory must be root-owned and non-writable"
                         : "production directory ancestor must be root-owned and non-writable"));
        }
        if (current == current.root_path()) {
            return {};
        }
    }
}

Result<std::filesystem::path> image_root(const SandboxConfig& config) {
    if (!config.image_digest.has_value() || !config.images_root.is_absolute()) {
        return std::unexpected(
            make_error(ErrorCode::invalid_argument,
                       "sandbox requires an absolute image store and typed OCI image digest"));
    }
    return config.images_root / "sha256" / std::string{config.image_digest->hex()} / "rootfs";
}

Result<void> preflight_sandbox(const SandboxConfig& config) {
    if (geteuid() != 0) {
        return std::unexpected(
            make_error(ErrorCode::sandbox_preflight_failed,
                       "wspctld must run as root; no unprivileged fallback exists"));
    }
    if (const auto capabilities = require_broker_capabilities(); !capabilities) {
        return std::unexpected(capabilities.error());
    }
    if (const auto lxcfs = validate_lxcfs_root(config.lxcfs_root); !lxcfs) {
        return std::unexpected(lxcfs.error());
    }
    const std::uint64_t effective_memory_high =
        config.memory_high_bytes == 0U ? config.memory_max_bytes : config.memory_high_bytes;
    if (config.sandbox_uid == 0U || config.sandbox_gid == 0U || config.memory_max_bytes == 0U ||
        effective_memory_high == 0U || effective_memory_high > config.memory_max_bytes ||
        config.tmp_size_bytes == 0U || config.cpu_max_quota_us == 0U ||
        config.cpu_max_period_us < 1'000U || config.cpu_max_period_us > 1'000'000U ||
        config.pids_max == 0U || config.io_weight > 10'000U) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed,
                                          "sandbox identity or cgroup resource policy is invalid"));
    }
    const auto base_root = image_root(config);
    if (!base_root) {
        return std::unexpected(base_root.error());
    }
    const auto image = validate_image_root(*base_root, config.images_root);
    if (!image) {
        return std::unexpected(image.error());
    }
    const auto cgroup_root = canonical_existing(config.cgroup_root);
    if (!cgroup_root || !cgroup_root->string().starts_with("/sys/fs/cgroup/")) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed,
                                          "cgroup_root must be a delegated cgroup v2 subtree"));
    }
    const auto controllers = read_small_file(*cgroup_root / "cgroup.controllers");
    if (!controllers) {
        return std::unexpected(controllers.error());
    }
    std::string missing_controllers;
    for (const std::string_view required : {"cpuset", "cpu", "memory", "pids"}) {
        if (contains_token(*controllers, required)) {
            continue;
        }
        if (!missing_controllers.empty()) {
            missing_controllers += '/';
        }
        missing_controllers += required;
    }
    if (!missing_controllers.empty()) {
        return std::unexpected(make_error(
            ErrorCode::sandbox_preflight_failed,
            "cgroup v2 delegated subtree lacks required controllers: " + missing_controllers));
    }
    if (access((*cgroup_root / "cgroup.procs").c_str(), W_OK) != 0 ||
        access((*cgroup_root / "cgroup.subtree_control").c_str(), W_OK) != 0 ||
        access((*cgroup_root / "cgroup.kill").c_str(), W_OK) != 0 ||
        access((*cgroup_root / "cpuset.cpus").c_str(), W_OK) != 0 ||
        access((*cgroup_root / "cpuset.mems").c_str(), W_OK) != 0 ||
        access((*cgroup_root / "memory.high").c_str(), W_OK) != 0 ||
        access((*cgroup_root / "memory.swap.max").c_str(), W_OK) != 0 ||
        access((*cgroup_root / "memory.oom.group").c_str(), W_OK) != 0) {
        return std::unexpected(
            make_error(ErrorCode::sandbox_preflight_failed,
                       "cgroup v2 Delegate=yes subtree lacks required writable controls"));
    }
    if (config.io_weight != 0U && (!contains_token(*controllers, "io") ||
                                   access((*cgroup_root / "io.weight").c_str(), W_OK) != 0)) {
        return std::unexpected(make_error(
            ErrorCode::sandbox_preflight_failed,
            "configured cgroup io.weight is unavailable; set io weight to 0 on this host"));
    }
    if (!config.state_root.is_absolute() || config.state_root == *base_root ||
        !is_safe_overlay_path(*base_root) || !is_safe_overlay_path(config.state_root)) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed,
                                          "unsafe state/base root configuration"));
    }
    const auto state_root = canonical_existing(config.state_root);
    if (!state_root) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed,
                                          "state_root must be pre-created by deployment"));
    }
    struct stat state_metadata {};
    if (lstat(state_root->c_str(), &state_metadata) != 0 || !S_ISDIR(state_metadata.st_mode) ||
        state_metadata.st_uid != 0U || (state_metadata.st_mode & (S_IWGRP | S_IWOTH)) != 0) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed,
                                          "state_root is not an owner-only root-owned directory"));
    }
    if (const auto quota = preflight_xfs_project_quota(config.xfs_project_quota, *state_root);
        !quota) {
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
    if (const auto moved =
            write_cgroup_file(manager_leaf / "cgroup.procs", std::to_string(getpid()));
        !moved) {
        return std::unexpected(moved.error());
    }
    return enable_runtime_controllers(*cgroup_root, config.io_weight != 0U);
}

Result<TaskLayer> prepare_task_layer(const SandboxConfig& config, const XfsProjectQuota& quota,
                                     const RuntimeActivationLease& activation_lease,
                                     const std::string& activation_id) {
    if (activation_id.empty()) {
        return std::unexpected(
            make_error(ErrorCode::invalid_argument, "activation_id is required"));
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
        layer.root_dir.parent_path() !=
            binding.control_dir / "mounts" / layer.root_dir.parent_path().filename() ||
        layer.workspace_lower_dir.parent_path() != layer.root_dir.parent_path() ||
        layer.merged_dir != layer.root_dir / "workspace") {
        return std::unexpected(make_error(
            ErrorCode::internal, "XFS quota service returned an invalid task-layer layout"));
    }
    return layer;
}

Result<void> cleanup_task_layer(const SandboxConfig& config, const XfsProjectQuota& quota,
                                const RuntimeActivationLease& activation_lease,
                                const TaskLayer& layer) {
    const std::filesystem::path control_activation_dir = layer.root_dir.parent_path();
    if (!same_runtime_quota_binding(layer.quota_binding, activation_lease.binding()) ||
        layer.quota_binding.runtime_dir != layer.runtime_dir ||
        layer.runtime_dir.parent_path() != config.state_root / "runtimes" ||
        layer.upper_dir != layer.quota_binding.workspace_dir / "upper" ||
        layer.work_dir.parent_path() != layer.quota_binding.workspace_dir / "work" ||
        control_activation_dir.parent_path() != layer.quota_binding.control_dir / "mounts" ||
        layer.workspace_lower_dir.parent_path() != control_activation_dir ||
        layer.root_dir != control_activation_dir / "root" ||
        layer.workspace_lower_dir != control_activation_dir / "workspace-lower" ||
        layer.merged_dir != layer.root_dir / "workspace") {
        return std::unexpected(make_error(ErrorCode::invalid_argument,
                                          "task layer does not describe an owned staging tree"));
    }
    return quota.cleanup_activation_storage(activation_lease, layer.activation_id);
}

Result<void> reclaim_dead_task_layers(const SandboxConfig& config, const XfsProjectQuota& quota,
                                      const RuntimeActivationLease& activation_lease,
                                      const std::string& runtime_key) {
    if (runtime_key.empty() || activation_lease.binding().runtime_dir !=
                                   config.state_root / "runtimes" / hash_component(runtime_key)) {
        return std::unexpected(
            make_error(ErrorCode::invalid_argument,
                       "runtime key does not match the activation lease binding"));
    }
    if (const auto task_free = wait_runtime_cgroup_empty(config, runtime_key); !task_free) {
        return std::unexpected(task_free.error());
    }
    return quota.reclaim_dead_activation_storage(activation_lease);
}

/**
 * @brief 判断 inode owner 是否属于可恢复的 workspace 状态 / Check whether an inode owner is a
 * recoverable workspace state.
 * @param metadata inode metadata / Inode metadata.
 * @param target_uid 当前 Agent UID / Current Agent UID.
 * @param target_gid 当前 Agent GID / Current Agent GID.
 * @return root、旧 nobody 或当前 Agent 时为真 / True for root, legacy nobody, or the current Agent.
 */
[[nodiscard]] bool is_migratable_workspace_owner(const struct stat& metadata,
                                                 const uid_t target_uid,
                                                 const gid_t target_gid) noexcept {
    constexpr uid_t kLegacyNobodyUid{65534U};
    constexpr gid_t kLegacyNobodyGid{65534U};
    return (metadata.st_uid == 0U && metadata.st_gid == 0U) ||
           (metadata.st_uid == kLegacyNobodyUid && metadata.st_gid == kLegacyNobodyGid) ||
           (metadata.st_uid == target_uid && metadata.st_gid == target_gid);
}

/**
 * @brief fd-relative 递归迁移 workspace 子树 owner / Recursively migrate workspace subtree owners
 * relative to an FD.
 * @param directory_fd 已验证父目录 FD / Verified parent-directory FD.
 * @param target_uid 当前 Agent UID / Current Agent UID.
 * @param target_gid 当前 Agent GID / Current Agent GID.
 * @return 成功或 fail-closed 错误 / Success or a fail-closed error.
 * @note 不跟随 symlink；每个目录在其子项完成后才改 owner，使中断后的重试保持可判定。/
 *       Symlinks are never followed; each directory changes owner only after its children, keeping
 *       interrupted retries unambiguous.
 */
[[nodiscard]] Result<void>
migrate_workspace_children(const int directory_fd, const uid_t target_uid, const gid_t target_gid) {
    /** @brief 供 ``fdopendir`` 消费的独立扫描 FD / Independent scan FD consumed by ``fdopendir``.
     */
    const int scan_fd = fcntl(directory_fd, F_DUPFD_CLOEXEC, 3);
    if (scan_fd < 0) {
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed,
                                           "duplicate workspace directory FD for owner migration"));
    }
    /** @brief 自动关闭的目录流 / Automatically closed directory stream. */
    std::unique_ptr<DIR, DirectoryStreamCloser> directory(fdopendir(scan_fd));
    if (!directory) {
        const int saved_errno = errno;
        static_cast<void>(close(scan_fd));
        errno = saved_errno;
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed,
                                           "open workspace directory stream for owner migration"));
    }
    for (;;) {
        errno = 0;
        /** @brief 当前目录项 / Current directory entry. */
        dirent* const entry = readdir(directory.get());
        if (entry == nullptr) {
            if (errno != 0) {
                return std::unexpected(
                    errno_error(ErrorCode::sandbox_preflight_failed,
                                "read workspace directory during owner migration"));
            }
            return {};
        }
        const std::string_view name{entry->d_name};
        if (name == "." || name == "..") {
            continue;
        }
        /** @brief 不跟随 symlink 的目录项 metadata / No-follow directory-entry metadata. */
        struct stat metadata {};
        if (fstatat(directory_fd, entry->d_name, &metadata, AT_SYMLINK_NOFOLLOW) != 0) {
            return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed,
                                               "stat workspace entry during owner migration"));
        }
        if (!is_migratable_workspace_owner(metadata, target_uid, target_gid)) {
            return std::unexpected(make_error(
                ErrorCode::sandbox_preflight_failed,
                "workspace entry has an unexpected owner during Agent identity migration"));
        }
        if (S_ISDIR(metadata.st_mode)) {
            /** @brief 不跟随 symlink 的子目录 FD / No-follow child-directory FD. */
            OwnedFileDescriptor child(openat(directory_fd, entry->d_name,
                                             O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW));
            if (child.get() < 0) {
                return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed,
                                                   "open workspace child during owner migration"));
            }
            if (const auto migrated =
                    migrate_workspace_children(child.get(), target_uid, target_gid);
                !migrated) {
                return std::unexpected(migrated.error());
            }
        }
        if (fchownat(directory_fd, entry->d_name, target_uid, target_gid, AT_SYMLINK_NOFOLLOW) !=
            0) {
            return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed,
                                               "change workspace entry owner to Agent"));
        }
    }
}

/**
 * @brief 把 merged workspace 准备为具名 Agent 的 private home / Prepare the merged workspace as the
 * named Agent's private home.
 * @param merged_dir 已挂载 OverlayFS workspace / Mounted OverlayFS workspace.
 * @param target_uid 当前 Agent UID / Current Agent UID.
 * @param target_gid 当前 Agent GID / Current Agent GID.
 * @return 成功或 fail-closed 错误 / Success or a fail-closed error.
 */
[[nodiscard]] Result<void> prepare_agent_workspace(const std::filesystem::path& merged_dir,
                                                   const uid_t target_uid, const gid_t target_gid) {
    /** @brief merged workspace 根 FD / Merged-workspace root FD. */
    OwnedFileDescriptor root(
        open(merged_dir.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW));
    if (root.get() < 0) {
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed,
                                           "open merged workspace for Agent identity migration"));
    }
    /** @brief workspace 根 metadata / Workspace-root metadata. */
    struct stat metadata {};
    if (fstat(root.get(), &metadata) != 0 || !S_ISDIR(metadata.st_mode) ||
        !is_migratable_workspace_owner(metadata, target_uid, target_gid)) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed,
                                          "merged workspace root has an unexpected type or owner"));
    }
    if (metadata.st_uid == target_uid && metadata.st_gid == target_gid &&
        (metadata.st_mode & 0777U) == 0700U) {
        return {};
    }
    if (const auto migrated = migrate_workspace_children(root.get(), target_uid, target_gid);
        !migrated) {
        return std::unexpected(migrated.error());
    }
    if (fchown(root.get(), target_uid, target_gid) != 0 || fchmod(root.get(), 0700) != 0 ||
        fsync(root.get()) != 0) {
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed,
                                           "commit merged workspace Agent identity migration"));
    }
    return {};
}

namespace {

/**
 * @brief procfs mask 的 inode 类型 / Inode type used by a procfs mask.
 */
enum class ProcMaskKind {
    /** @brief 以不可读空文件覆盖 / Cover with an unreadable empty file. */
    file,
    /** @brief 以不可遍历空目录覆盖 / Cover with an unsearchable empty directory. */
    directory,
};

/**
 * @brief 一个需要从 Agent 视图移除的 procfs 路径 / One procfs path removed from the Agent view.
 */
struct ProcMask final {
    /** @brief runtime 内绝对路径 / Absolute path inside the runtime. */
    std::string_view path;
    /** @brief 目标 inode 类型 / Target inode type. */
    ProcMaskKind kind;
};

/** @brief procfs mask 临时来源目录 / Temporary source directory for procfs masks. */
constexpr std::string_view kProcMaskRoot{"/run/.wspctl-proc-mask"};
/** @brief procfs file mask 的临时来源 / Temporary source for procfs file masks. */
constexpr std::string_view kProcFileMaskSource{"/run/.wspctl-proc-mask/file"};
/** @brief procfs directory mask 的临时来源 / Temporary source for procfs directory masks. */
constexpr std::string_view kProcDirectoryMaskSource{"/run/.wspctl-proc-mask/directory"};

/**
 * @brief 在 pivot 前把专用 LXCFS mount 固定到 runtime 私有 ``/run`` /
 * Pin the dedicated LXCFS mount in the runtime-private ``/run`` before pivot.
 * @param config 已完成 broker preflight 的 sandbox 配置 / Sandbox configuration already accepted by
 * broker preflight.
 * @param runtime_root 即将成为 ``/`` 的 runtime root / Runtime root that will become ``/``.
 * @return staging mount 完整建立时返回 PSI 能力 / PSI capability when the staging mount is fully
 * established.
 * @note 整棵 LXCFS 只作为 mount source 暂存；逐文件 bind 完成后 ``/run`` 会整体 detach，
 *       Agent 不会获得 LXCFS 控制面路径。/ The whole LXCFS tree is staged only as a mount
 *       source. ``/run`` is detached after per-file binds, so the Agent never receives its control
 * path.
 */
[[nodiscard]] Result<bool> stage_lxcfs_root(const SandboxConfig& config,
                                            const std::filesystem::path& runtime_root) {
    const auto lxcfs_root = validate_lxcfs_root(config.lxcfs_root);
    if (!lxcfs_root) {
        return std::unexpected(lxcfs_root.error());
    }
    /** @brief runtime 内的私有 ``/run`` / Private ``/run`` inside the runtime. */
    const std::filesystem::path runtime_run = runtime_root / "run";
    /** @brief runtime 内的 LXCFS staging mountpoint / LXCFS staging mountpoint inside the runtime.
     */
    const std::filesystem::path staged_root =
        runtime_root / std::filesystem::path(kStagedLxcfsRoot).relative_path();
    struct stat run_metadata {};
    if (lstat(runtime_run.c_str(), &run_metadata) != 0 || !S_ISDIR(run_metadata.st_mode) ||
        S_ISLNK(run_metadata.st_mode)) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed,
                                          "immutable image must contain a real /run directory"));
    }
    if (mount("tmpfs", runtime_run.c_str(), "tmpfs", MS_NOSUID | MS_NODEV | MS_NOEXEC,
              "mode=0700,size=64k") != 0 ||
        mkdir(staged_root.c_str(), 0700) != 0 ||
        mount(lxcfs_root->root.c_str(), staged_root.c_str(), nullptr, MS_BIND | MS_REC, nullptr) !=
            0 ||
        mount(nullptr, staged_root.c_str(), nullptr,
              MS_BIND | MS_REMOUNT | MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC, nullptr) != 0) {
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed,
                                           "stage dedicated LXCFS mount inside runtime"));
    }
    return lxcfs_root->pressure_available;
}

/**
 * @brief 用一个 LXCFS 动态文件覆盖 host-global procfs 节点 /
 * Cover one host-global procfs node with a dynamic LXCFS file.
 * @param mapping 已验证能力集中的 source/target / Source/target in the validated capability set.
 * @return 节点成为只读、不可执行 bind mount 时成功 / Success when the node is a readonly,
 * non-executable bind mount.
 */
[[nodiscard]] Result<void> map_cgroup_aware_proc_file(const ProcVirtualFile& mapping) {
    /** @brief runtime 私有 staging 中的源文件 / Source file in runtime-private staging. */
    const std::filesystem::path source = std::filesystem::path(kStagedLxcfsRoot) / mapping.source;
    struct stat source_metadata {};
    struct stat target_metadata {};
    if (lstat(source.c_str(), &source_metadata) != 0 ||
        lstat(mapping.target.data(), &target_metadata) != 0 || S_ISLNK(source_metadata.st_mode) ||
        S_ISLNK(target_metadata.st_mode) || !S_ISREG(source_metadata.st_mode) ||
        !S_ISREG(target_metadata.st_mode)) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed,
                                          "LXCFS procfs mapping has an unexpected inode type: " +
                                              std::string(mapping.target)));
    }
    if (mount(source.c_str(), mapping.target.data(), nullptr, MS_BIND, nullptr) != 0 ||
        mount(nullptr, mapping.target.data(), nullptr,
              MS_BIND | MS_REMOUNT | MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC, nullptr) != 0) {
        return std::unexpected(
            errno_error(ErrorCode::sandbox_preflight_failed,
                        "map cgroup-aware procfs file " + std::string(mapping.target)));
    }
    return {};
}

/**
 * @brief 映射当前 LXCFS profile 完整支持的 procfs 性能节点 /
 * Map procfs performance nodes completely supported by the current LXCFS profile.
 * @param pressure_available 是否完整支持 PSI 能力组 / Whether the complete PSI capability group is
 * supported.
 * @return 所有受支持节点均映射成功 / Success when every supported node is mapped.
 */
[[nodiscard]] Result<void> map_cgroup_aware_procfs(const bool pressure_available) {
    for (const ProcVirtualFile& mapping : kRequiredCgroupAwareProcFiles) {
        if (const auto mapped = map_cgroup_aware_proc_file(mapping); !mapped) {
            return std::unexpected(mapped.error());
        }
    }
    if (pressure_available) {
        for (const ProcVirtualFile& mapping : kPressureCgroupAwareProcFiles) {
            if (const auto mapped = map_cgroup_aware_proc_file(mapping); !mapped) {
                return std::unexpected(mapped.error());
            }
        }
    }
    return {};
}

/**
 * @brief 用不可读空 inode 覆盖一个可选 procfs 路径 / Cover an optional procfs path with an
 * unreadable empty inode.
 * @param mask 路径及预期 inode 类型 / Path and expected inode type.
 * @return 成功、路径在本内核不存在，或 fail-closed mount 错误 / Success, an absent kernel path, or
 * a fail-closed mount error.
 */
[[nodiscard]] Result<void> mask_proc_path(const ProcMask& mask) {
    /** @brief 不跟随 symlink 的目标 metadata / No-follow target metadata. */
    struct stat metadata {};
    if (lstat(mask.path.data(), &metadata) != 0) {
        if (errno == ENOENT) {
            return {};
        }
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed,
                                           "inspect procfs mask target " + std::string(mask.path)));
    }
    /** @brief mask 期望目标为目录 / Whether the mask expects a directory target. */
    const bool expects_directory = mask.kind == ProcMaskKind::directory;
    if (S_ISLNK(metadata.st_mode) || S_ISDIR(metadata.st_mode) != expects_directory) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed,
                                          "procfs mask target has an unexpected inode type: " +
                                              std::string(mask.path)));
    }
    /** @brief 与目标类型匹配的空 mask 来源 / Empty mask source matching the target type. */
    const std::string_view source =
        expects_directory ? kProcDirectoryMaskSource : kProcFileMaskSource;
    /** @brief 目录 mask 递归绑定，文件 mask 普通绑定 / Recursive bind for directories and plain
     * bind for files. */
    const unsigned long bind_flags = static_cast<unsigned long>(MS_BIND) |
                                     (expects_directory ? static_cast<unsigned long>(MS_REC) : 0UL);
    if (mount(source.data(), mask.path.data(), nullptr, bind_flags, nullptr) != 0 ||
        mount(nullptr, mask.path.data(), nullptr,
              MS_BIND | MS_REMOUNT | MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC, nullptr) != 0) {
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed,
                                           "mask procfs path " + std::string(mask.path)));
    }
    return {};
}

/**
 * @brief 把一个可选 procfs 子树变为只读 bind mount / Turn an optional procfs subtree into a
 * readonly bind mount.
 * @param path runtime 内绝对 procfs 目录 / Absolute procfs directory inside the runtime.
 * @return 成功、路径在本内核不存在，或 fail-closed mount 错误 / Success, an absent kernel path, or
 * a fail-closed mount error.
 */
[[nodiscard]] Result<void> make_proc_path_readonly(const std::string_view path) {
    /** @brief 不跟随 symlink 的目标 metadata / No-follow target metadata. */
    struct stat metadata {};
    if (lstat(path.data(), &metadata) != 0) {
        if (errno == ENOENT) {
            return {};
        }
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed,
                                           "inspect readonly procfs target " + std::string(path)));
    }
    if (!S_ISDIR(metadata.st_mode)) {
        return std::unexpected(
            make_error(ErrorCode::sandbox_preflight_failed,
                       "readonly procfs target is not a directory: " + std::string(path)));
    }
    if (mount(path.data(), path.data(), nullptr, MS_BIND | MS_REC, nullptr) != 0 ||
        mount(nullptr, path.data(), nullptr,
              MS_BIND | MS_REMOUNT | MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC, nullptr) != 0) {
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed,
                                           "make procfs subtree readonly " + std::string(path)));
    }
    return {};
}

/**
 * @brief 建立 Agent 可用但去除宿主敏感面的 procfs 视图 /
 * Build an Agent-usable procfs view with host-sensitive surfaces removed.
 * @return 所有 mask、只读边界与临时来源清理均成功 / Success when every mask, readonly boundary, and
 * source cleanup succeeds.
 * @note 性能节点由 LXCFS 按调用进程 cgroup 动态呈现；同 PID namespace task 目录继续可用。
 *       无可靠虚拟化语义的 host-global 性能节点与宿主身份面会被隐藏。/
 *       Performance nodes are dynamically rendered by LXCFS for the caller's cgroup, while
 *       task directories in the same PID namespace remain available. Host-global performance
 *       nodes without reliable virtualization semantics and host identity surfaces are hidden.
 */
[[nodiscard]] Result<void> harden_runtime_procfs(const bool pressure_available) {
    if (const auto mapped = map_cgroup_aware_procfs(pressure_available); !mapped) {
        return std::unexpected(mapped.error());
    }
    if (mkdir(kProcMaskRoot.data(), 0700) != 0 ||
        mkdir(kProcDirectoryMaskSource.data(), 0000) != 0) {
        return std::unexpected(
            errno_error(ErrorCode::sandbox_preflight_failed, "create procfs mask sources"));
    }
    /** @brief 不可读空文件 mask 的创建 FD / Creation FD for the unreadable empty file mask. */
    const int file_mask = open(kProcFileMaskSource.data(),
                               O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0000);
    if (file_mask < 0) {
        return std::unexpected(
            errno_error(ErrorCode::sandbox_preflight_failed, "create procfs file mask source"));
    }
    if (close(file_mask) != 0) {
        return std::unexpected(
            errno_error(ErrorCode::sandbox_preflight_failed, "close procfs file mask source"));
    }
    if (!pressure_available) {
        const ProcMask pressure_mask{
            .path = "/proc/pressure",
            .kind = ProcMaskKind::directory,
        };
        if (const auto masked = mask_proc_path(pressure_mask); !masked) {
            return std::unexpected(masked.error());
        }
    }

    /** @brief 不应暴露给 Agent 的 host-global procfs 面 / Host-global procfs surfaces hidden from
     * the Agent. */
    constexpr std::array<ProcMask, 28U> kMaskedPaths{{
        {.path = "/proc/acpi", .kind = ProcMaskKind::directory},
        {.path = "/proc/asound", .kind = ProcMaskKind::directory},
        {.path = "/proc/driver", .kind = ProcMaskKind::directory},
        {.path = "/proc/scsi", .kind = ProcMaskKind::directory},
        {.path = "/proc/tty/driver", .kind = ProcMaskKind::directory},
        {.path = "/proc/allocinfo", .kind = ProcMaskKind::file},
        {.path = "/proc/buddyinfo", .kind = ProcMaskKind::file},
        {.path = "/proc/cmdline", .kind = ProcMaskKind::file},
        {.path = "/proc/config.gz", .kind = ProcMaskKind::file},
        {.path = "/proc/interrupts", .kind = ProcMaskKind::file},
        {.path = "/proc/iomem", .kind = ProcMaskKind::file},
        {.path = "/proc/ioports", .kind = ProcMaskKind::file},
        {.path = "/proc/kallsyms", .kind = ProcMaskKind::file},
        {.path = "/proc/kcore", .kind = ProcMaskKind::file},
        {.path = "/proc/kpagecount", .kind = ProcMaskKind::file},
        {.path = "/proc/kpageflags", .kind = ProcMaskKind::file},
        {.path = "/proc/keys", .kind = ProcMaskKind::file},
        {.path = "/proc/latency_stats", .kind = ProcMaskKind::file},
        {.path = "/proc/mdstat", .kind = ProcMaskKind::file},
        {.path = "/proc/modules", .kind = ProcMaskKind::file},
        {.path = "/proc/pagetypeinfo", .kind = ProcMaskKind::file},
        {.path = "/proc/partitions", .kind = ProcMaskKind::file},
        {.path = "/proc/schedstat", .kind = ProcMaskKind::file},
        {.path = "/proc/sched_debug", .kind = ProcMaskKind::file},
        {.path = "/proc/softirqs", .kind = ProcMaskKind::file},
        {.path = "/proc/vmallocinfo", .kind = ProcMaskKind::file},
        {.path = "/proc/vmstat", .kind = ProcMaskKind::file},
        {.path = "/proc/zoneinfo", .kind = ProcMaskKind::file},
    }};
    for (const ProcMask& mask : kMaskedPaths) {
        if (const auto masked = mask_proc_path(mask); !masked) {
            return std::unexpected(masked.error());
        }
    }
    /** @brief 独立于文件列表、但同样可关联宿主启动的 procfs 文件 / Additional procfs files that
     * identify the host boot. */
    constexpr std::array<ProcMask, 4U> kBootAndControlMasks{{
        {.path = "/proc/sys/kernel/random/boot_id", .kind = ProcMaskKind::file},
        {.path = "/proc/sysrq-trigger", .kind = ProcMaskKind::file},
        {.path = "/proc/timer_list", .kind = ProcMaskKind::file},
        {.path = "/proc/timer_stats", .kind = ProcMaskKind::file},
    }};
    for (const ProcMask& mask : kBootAndControlMasks) {
        if (const auto masked = mask_proc_path(mask); !masked) {
            return std::unexpected(masked.error());
        }
    }
    /** @brief Agent 可读取但不可写的 procfs 控制子树 / Procfs control subtrees readable but not
     * writable by the Agent. */
    constexpr std::array<std::string_view, 4U> kReadonlyPaths{
        "/proc/bus",
        "/proc/fs",
        "/proc/irq",
        "/proc/sys",
    };
    for (const std::string_view path : kReadonlyPaths) {
        if (const auto readonly = make_proc_path_readonly(path); !readonly) {
            return std::unexpected(readonly.error());
        }
    }
    if (umount2("/run", MNT_DETACH) != 0) {
        return std::unexpected(
            errno_error(ErrorCode::sandbox_preflight_failed, "detach private procfs mask source"));
    }
    return {};
}

/**
 * @brief 创建一个显式白名单且不受 broker umask 影响的 runtime 字符设备 /
 * Create one explicitly allow-listed runtime character device independent of the broker umask.
 * @param path runtime 内设备路径 / Device path inside the runtime.
 * @param device 字符设备 major/minor 编码 / Encoded character-device major/minor number.
 * @return 节点类型、设备号与最终权限均正确时成功 / Success when type, device number, and final
 * permissions are correct.
 */
[[nodiscard]] Result<void> create_runtime_character_device(const std::string_view path,
                                                           const dev_t device) {
    if (mknod(path.data(), S_IFCHR | 0600, device) != 0 || chmod(path.data(), 0666) != 0) {
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed,
                                           "create runtime character device " + std::string(path)));
    }
    /** @brief 创建后重新读取的设备 metadata / Device metadata read back after creation. */
    struct stat metadata {};
    if (lstat(path.data(), &metadata) != 0 || !S_ISCHR(metadata.st_mode) ||
        metadata.st_rdev != device || metadata.st_uid != 0U || metadata.st_gid != 0U ||
        (metadata.st_mode & 07777U) != 0666U) {
        return std::unexpected(
            make_error(ErrorCode::sandbox_preflight_failed,
                       "runtime character device contract failed: " + std::string(path)));
    }
    return {};
}

} // namespace

Result<void> setup_runtime_mounts(const SandboxConfig& config, const TaskLayer& layer) {
    const auto base_root = image_root(config);
    if (!base_root) {
        return std::unexpected(base_root.error());
    }
    if (mount(nullptr, "/", nullptr, MS_REC | MS_PRIVATE, nullptr) != 0) {
        return std::unexpected(
            errno_error(ErrorCode::sandbox_preflight_failed, "make mount propagation private"));
    }
    if (mount(base_root->c_str(), layer.root_dir.c_str(), nullptr, MS_BIND, nullptr) != 0) {
        return std::unexpected(
            errno_error(ErrorCode::sandbox_preflight_failed, "bind immutable base root"));
    }
    if (mount(nullptr, layer.root_dir.c_str(), nullptr,
              MS_BIND | MS_REMOUNT | MS_RDONLY | MS_NOSUID | MS_NODEV, nullptr) != 0) {
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed,
                                           "remount immutable base root readonly"));
    }
    const std::filesystem::path base_workspace = *base_root / "workspace";
    struct stat workspace_metadata {};
    if (stat(base_workspace.c_str(), &workspace_metadata) != 0 ||
        !S_ISDIR(workspace_metadata.st_mode)) {
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed,
                                          "immutable image must contain /workspace directory"));
    }
    if (mount(base_workspace.c_str(), layer.workspace_lower_dir.c_str(), nullptr, MS_BIND,
              nullptr) != 0 ||
        mount(nullptr, layer.workspace_lower_dir.c_str(), nullptr,
              MS_BIND | MS_REMOUNT | MS_RDONLY | MS_NOSUID | MS_NODEV, nullptr) != 0) {
        return std::unexpected(
            errno_error(ErrorCode::sandbox_preflight_failed, "bind readonly workspace lower"));
    }
    const std::string overlay_options =
        "lowerdir=" + layer.workspace_lower_dir.string() + ",upperdir=" + layer.upper_dir.string() +
        ",workdir=" + layer.work_dir.string() + ",metacopy=off,redirect_dir=nofollow,index=off";
    if (mount("overlay", layer.merged_dir.c_str(), "overlay", MS_NOSUID | MS_NODEV,
              overlay_options.c_str()) != 0) {
        return std::unexpected(
            errno_error(ErrorCode::sandbox_preflight_failed, "mount task OverlayFS"));
    }
    if (const auto workspace =
            prepare_agent_workspace(layer.merged_dir, config.sandbox_uid, config.sandbox_gid);
        !workspace) {
        return std::unexpected(workspace.error());
    }
    const auto lxcfs = stage_lxcfs_root(config, layer.root_dir);
    if (!lxcfs) {
        return std::unexpected(lxcfs.error());
    }
    // /tmp must be present in the immutable image and becomes put_old before being replaced by a
    // private tmpfs.
    if (syscall(SYS_pivot_root, layer.root_dir.c_str(), (layer.root_dir / "tmp").c_str()) != 0 ||
        chdir("/") != 0 || umount2("/tmp", MNT_DETACH) != 0) {
        return std::unexpected(
            errno_error(ErrorCode::sandbox_preflight_failed, "pivot into runtime root"));
    }
    /** @brief 私有 /tmp tmpfs 的显式容量选项 / Explicit capacity option for the private /tmp tmpfs.
     */
    const std::string tmp_options = "mode=1777,size=" + std::to_string(config.tmp_size_bytes);
    /** @brief PID namespace 已排除宿主 task；挂载完整 procfs，交由标准 DAC 保护 root-only 内容 /
     * The PID namespace already excludes host tasks; mount full procfs and leave root-only content
     * to standard DAC. */
    if (mount("proc", "/proc", "proc", MS_NOSUID | MS_NODEV | MS_NOEXEC, nullptr) != 0 ||
        mount("tmpfs", "/tmp", "tmpfs", MS_NOSUID | MS_NODEV | MS_NOEXEC, tmp_options.c_str()) !=
            0 ||
        mount("tmpfs", "/dev", "tmpfs", MS_NOSUID | MS_NOEXEC, "mode=0755,size=64k") != 0) {
        return std::unexpected(
            errno_error(ErrorCode::sandbox_preflight_failed, "mount minimal proc/tmp/dev/run"));
    }
    for (const auto& [path, device] : std::array<std::pair<std::string_view, dev_t>, 4U>{{
             {"/dev/null", makedev(1, 3)},
             {"/dev/zero", makedev(1, 5)},
             {"/dev/random", makedev(1, 8)},
             {"/dev/urandom", makedev(1, 9)},
         }}) {
        if (const auto created = create_runtime_character_device(path, device); !created) {
            return std::unexpected(created.error());
        }
    }
    if (const auto hardened_procfs = harden_runtime_procfs(*lxcfs); !hardened_procfs) {
        return std::unexpected(hardened_procfs.error());
    }
    return {};
}

Result<TaskCgroupControl> prepare_runtime_cgroup(const SandboxConfig& config,
                                                 const std::string& runtime_key) {
    const std::filesystem::path runtime_cgroup =
        config.cgroup_root / "wspctl" / hash_component(runtime_key);
    const std::filesystem::path wspctl_cgroup = config.cgroup_root / "wspctl";
    const std::filesystem::path supervisor_leaf = runtime_cgroup / "supervisor";
    const std::filesystem::path task_leaf = runtime_cgroup / "task";
    std::error_code existing_error;
    const std::filesystem::file_status existing =
        std::filesystem::symlink_status(runtime_cgroup, existing_error);
    // ``symlink_status(path, ec)`` may report a missing path both as ``file_type::not_found``
    // and ENOENT.  A fresh runtime is the normal creation path, not a failed inspection.
    if (existing_error && existing_error != std::errc::no_such_file_or_directory) {
        return std::unexpected(
            make_error(ErrorCode::sandbox_preflight_failed,
                       "inspect existing runtime cgroup: " + existing_error.message()));
    }
    if (std::filesystem::exists(existing)) {
        if (!std::filesystem::is_directory(existing)) {
            return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed,
                                              "existing runtime cgroup is not a directory"));
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
    if (const auto controllers = enable_runtime_controllers(wspctl_cgroup, config.io_weight != 0U);
        !controllers) {
        return std::unexpected(controllers.error());
    }
    if (const auto runtime = create_cgroup_directory(runtime_cgroup); !runtime) {
        return std::unexpected(runtime.error());
    }
    if (const auto controllers = enable_runtime_controllers(runtime_cgroup, config.io_weight != 0U);
        !controllers) {
        return std::unexpected(controllers.error());
    }
    // Always reselect from the delegated parent. Reading a reused runtime's old effective set
    // would make a prior quota or CPU-hotplug decision permanently sticky.
    const auto effective_cpus = read_small_file(wspctl_cgroup / "cpuset.cpus.effective");
    const auto effective_mems = read_small_file(wspctl_cgroup / "cpuset.mems.effective");
    if (!effective_cpus) {
        return std::unexpected(effective_cpus.error());
    }
    if (!effective_mems) {
        return std::unexpected(effective_mems.error());
    }
    const auto selected_cpus = select_runtime_cpuset(*effective_cpus, config.cpu_max_quota_us,
                                                     config.cpu_max_period_us, runtime_key);
    if (!selected_cpus) {
        return std::unexpected(selected_cpus.error());
    }
    if (const auto mems = write_cgroup_file(runtime_cgroup / "cpuset.mems", *effective_mems);
        !mems) {
        return std::unexpected(mems.error());
    }
    if (const auto cpus = write_cgroup_file(runtime_cgroup / "cpuset.cpus", *selected_cpus);
        !cpus) {
        return std::unexpected(cpus.error());
    }
    if (const auto supervisor = create_cgroup_directory(supervisor_leaf); !supervisor) {
        return std::unexpected(supervisor.error());
    }
    if (const auto task = create_cgroup_directory(task_leaf); !task) {
        return std::unexpected(task.error());
    }
    if (const auto memory = write_cgroup_file(runtime_cgroup / "memory.max",
                                              std::to_string(config.memory_max_bytes));
        !memory) {
        return std::unexpected(memory.error());
    }
    const std::uint64_t effective_memory_high =
        config.memory_high_bytes == 0U ? config.memory_max_bytes : config.memory_high_bytes;
    if (const auto memory_high = write_cgroup_file(runtime_cgroup / "memory.high",
                                                   std::to_string(effective_memory_high));
        !memory_high) {
        return std::unexpected(memory_high.error());
    }
    if (const auto memory_swap = write_cgroup_file(runtime_cgroup / "memory.swap.max",
                                                   std::to_string(config.memory_swap_max_bytes));
        !memory_swap) {
        return std::unexpected(memory_swap.error());
    }
    if (const auto oom_group = write_cgroup_file(task_leaf / "memory.oom.group", "1"); !oom_group) {
        return std::unexpected(oom_group.error());
    }
    if (const auto cpu = write_cgroup_file(runtime_cgroup / "cpu.max",
                                           std::to_string(config.cpu_max_quota_us) + " " +
                                               std::to_string(config.cpu_max_period_us));
        !cpu) {
        return std::unexpected(cpu.error());
    }
    if (const auto pids =
            write_cgroup_file(runtime_cgroup / "pids.max", std::to_string(config.pids_max));
        !pids) {
        return std::unexpected(pids.error());
    }
    if (config.io_weight != 0U) {
        if (const auto io =
                write_cgroup_file(runtime_cgroup / "io.weight", std::to_string(config.io_weight));
            !io) {
            return std::unexpected(io.error());
        }
    }
    const int supervisor_procs_fd =
        open((supervisor_leaf / "cgroup.procs").c_str(), O_WRONLY | O_CLOEXEC | O_NOFOLLOW);
    if (supervisor_procs_fd < 0) {
        return std::unexpected(
            errno_error(ErrorCode::sandbox_preflight_failed, "open supervisor cgroup.procs"));
    }
    const int procs_fd =
        open((task_leaf / "cgroup.procs").c_str(), O_WRONLY | O_CLOEXEC | O_NOFOLLOW);
    if (procs_fd < 0) {
        static_cast<void>(close(supervisor_procs_fd));
        return std::unexpected(
            errno_error(ErrorCode::sandbox_preflight_failed, "open task cgroup.procs"));
    }
    const int kill_fd =
        open((task_leaf / "cgroup.kill").c_str(), O_WRONLY | O_CLOEXEC | O_NOFOLLOW);
    if (kill_fd < 0) {
        const Error error =
            errno_error(ErrorCode::sandbox_preflight_failed, "open task cgroup.kill");
        static_cast<void>(close(supervisor_procs_fd));
        static_cast<void>(close(procs_fd));
        return std::unexpected(error);
    }
    const int events_fd =
        open((task_leaf / "cgroup.events").c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (events_fd < 0) {
        const Error error =
            errno_error(ErrorCode::sandbox_preflight_failed, "open task cgroup.events");
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
    const std::filesystem::path runtime_cgroup =
        config.cgroup_root / "wspctl" / hash_component(runtime_key);
    if (const auto killed = write_cgroup_file(runtime_cgroup / "cgroup.kill", "1"); !killed) {
        return std::unexpected(killed.error());
    }
    return wait_runtime_cgroup_empty(config, runtime_key);
}

Result<void> wait_runtime_cgroup_empty(const SandboxConfig& config,
                                       const std::string& runtime_key) {
    const std::filesystem::path events_path =
        config.cgroup_root / "wspctl" / hash_component(runtime_key) / "cgroup.events";
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
    return std::unexpected(make_error(ErrorCode::child_failure,
                                      "runtime cgroup remained populated after cgroup.kill"));
}

Result<void> harden_task(const uid_t uid, const gid_t gid) {
    if (geteuid() == 0U) {
        if (setgroups(0, nullptr) != 0 || setresgid(gid, gid, gid) != 0 ||
            setresuid(uid, uid, uid) != 0) {
            return std::unexpected(
                errno_error(ErrorCode::sandbox_preflight_failed, "drop task identity"));
        }
    } else if (geteuid() != uid || getegid() != gid) {
        return std::unexpected(
            make_error(ErrorCode::sandbox_preflight_failed, "unprivileged task identity mismatch"));
    }
    if (const auto capabilities = clear_capabilities(); !capabilities) {
        return std::unexpected(capabilities.error());
    }
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0) {
        return std::unexpected(
            errno_error(ErrorCode::sandbox_preflight_failed, "set no_new_privs"));
    }
    scmp_filter_ctx filter = seccomp_init(SCMP_ACT_ALLOW);
    if (filter == nullptr) {
        return std::unexpected(
            make_error(ErrorCode::sandbox_preflight_failed, "initialize seccomp filter"));
    }
    /** @brief 不受 runtime namespace 充分隔离的高风险 syscall / High-risk syscalls not sufficiently
     * isolated by runtime namespaces. */
    constexpr std::array<int, 18> kDeniedSyscalls{
        SCMP_SYS(mount),           SCMP_SYS(umount2),
        SCMP_SYS(pivot_root),      SCMP_SYS(unshare),
        SCMP_SYS(setns),           SCMP_SYS(bpf),
        SCMP_SYS(ptrace),          SCMP_SYS(kexec_load),
        SCMP_SYS(kexec_file_load), SCMP_SYS(open_by_handle_at),
        SCMP_SYS(keyctl),          SCMP_SYS(add_key),
        SCMP_SYS(request_key),     SCMP_SYS(perf_event_open),
        SCMP_SYS(userfaultfd),     SCMP_SYS(init_module),
        SCMP_SYS(finit_module),    SCMP_SYS(delete_module),
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
    if (seccomp_rule_add(filter, SCMP_ACT_ERRNO(EPERM), SCMP_SYS(socket), 1,
                         SCMP_CMP(0, SCMP_CMP_NE, AF_UNIX)) != 0 ||
        seccomp_rule_add(filter, SCMP_ACT_ERRNO(EPERM), SCMP_SYS(socketpair), 1,
                         SCMP_CMP(0, SCMP_CMP_NE, AF_UNIX)) != 0) {
        seccomp_release(filter);
        return std::unexpected(make_error(ErrorCode::sandbox_preflight_failed,
                                          "cannot restrict task socket families"));
    }
    /** @brief XFS project quota 的 inode project-id/继承位变更必须由 broker 执行，payload 不能用
     * ioctl 篡改 / XFS project-quota inode project-id and inheritance changes must be broker-owned
     * and cannot be changed by payload ioctl. */
#if defined(FS_IOC_FSSETXATTR) && defined(FS_IOC_SETFLAGS) && defined(FS_IOC32_SETFLAGS)
    if (const auto fssetxattr_rule = add_deny_ioctl_request_rule(filter, FS_IOC_FSSETXATTR);
        !fssetxattr_rule) {
        seccomp_release(filter);
        return std::unexpected(fssetxattr_rule.error());
    }
    /** @brief FS_IOC_SETFLAGS 是 legacy XFS_IOC_SETXFLAGS 的 generic UAPI spelling /
     * FS_IOC_SETFLAGS is the generic UAPI spelling of legacy XFS_IOC_SETXFLAGS. */
    if (const auto setflags_rule = add_deny_ioctl_request_rule(filter, FS_IOC_SETFLAGS);
        !setflags_rule) {
        seccomp_release(filter);
        return std::unexpected(setflags_rule.error());
    }
    /** @brief 64-bit kernel 仍须拒绝 32-bit compat request；在同值 ABI 上避免重复 rule / A 64-bit
     * kernel must also deny the 32-bit compat request; avoid a duplicate rule on equal-value ABIs.
     */
    if constexpr (FS_IOC32_SETFLAGS != FS_IOC_SETFLAGS) {
        if (const auto compat_setflags_rule =
                add_deny_ioctl_request_rule(filter, FS_IOC32_SETFLAGS);
            !compat_setflags_rule) {
            seccomp_release(filter);
            return std::unexpected(compat_setflags_rule.error());
        }
    }
#else
    seccomp_release(filter);
    return std::unexpected(
        make_error(ErrorCode::sandbox_preflight_failed,
                   "kernel headers do not expose required XFS project-quota ioctl constants"));
#endif
    constexpr std::array<unsigned long long, 8> kNamespaceFlags{
        CLONE_NEWNS,   CLONE_NEWCGROUP, CLONE_NEWUTS, CLONE_NEWIPC,
        CLONE_NEWUSER, CLONE_NEWPID,    CLONE_NEWNET,
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
        if (seccomp_rule_add(filter, SCMP_ACT_ERRNO(EPERM), SCMP_SYS(clone), 1,
                             SCMP_CMP(0, SCMP_CMP_MASKED_EQ, flag, flag)) != 0) {
            seccomp_release(filter);
            return std::unexpected(
                make_error(ErrorCode::sandbox_preflight_failed, "cannot deny namespace clone"));
        }
    }
#ifdef __NR_clone3
    if (const auto clone3_rule = add_deny_rule(filter, SCMP_SYS(clone3)); !clone3_rule) {
        seccomp_release(filter);
        return std::unexpected(clone3_rule.error());
    }
#endif
#ifdef __NR_process_vm_readv
    if (const auto process_vm_readv_rule = add_deny_rule(filter, SCMP_SYS(process_vm_readv));
        !process_vm_readv_rule) {
        seccomp_release(filter);
        return std::unexpected(process_vm_readv_rule.error());
    }
#endif
#ifdef __NR_process_vm_writev
    if (const auto process_vm_writev_rule = add_deny_rule(filter, SCMP_SYS(process_vm_writev));
        !process_vm_writev_rule) {
        seccomp_release(filter);
        return std::unexpected(process_vm_writev_rule.error());
    }
#endif
#ifdef __NR_pidfd_getfd
    if (const auto pidfd_getfd_rule = add_deny_rule(filter, SCMP_SYS(pidfd_getfd));
        !pidfd_getfd_rule) {
        seccomp_release(filter);
        return std::unexpected(pidfd_getfd_rule.error());
    }
#endif
    /** @brief io_uring 可提交 IORING_OP_SOCKET，不能绕过仅 AF_UNIX 的 socket(2) 规则 / io_uring can
     * submit IORING_OP_SOCKET and must not bypass the AF_UNIX-only socket(2) rule. */
#ifdef __NR_io_uring_setup
    if (const auto io_uring_setup_rule = add_deny_rule(filter, SCMP_SYS(io_uring_setup));
        !io_uring_setup_rule) {
        seccomp_release(filter);
        return std::unexpected(io_uring_setup_rule.error());
    }
#endif
#ifdef __NR_io_uring_enter
    if (const auto io_uring_enter_rule = add_deny_rule(filter, SCMP_SYS(io_uring_enter));
        !io_uring_enter_rule) {
        seccomp_release(filter);
        return std::unexpected(io_uring_enter_rule.error());
    }
#endif
#ifdef __NR_io_uring_register
    if (const auto io_uring_register_rule = add_deny_rule(filter, SCMP_SYS(io_uring_register));
        !io_uring_register_rule) {
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
    if (const auto move_mount_rule = add_deny_rule(filter, SCMP_SYS(move_mount));
        !move_mount_rule) {
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
    if (const auto mount_setattr_rule = add_deny_rule(filter, SCMP_SYS(mount_setattr));
        !mount_setattr_rule) {
        seccomp_release(filter);
        return std::unexpected(mount_setattr_rule.error());
    }
#endif
    if (seccomp_load(filter) != 0) {
        seccomp_release(filter);
        return std::unexpected(
            make_error(ErrorCode::sandbox_preflight_failed, "load seccomp filter"));
    }
    seccomp_release(filter);
    return {};
}

Result<void> harden_supervisor() {
    if (geteuid() != 0U || getegid() != 0U) {
        return std::unexpected(
            make_error(ErrorCode::sandbox_preflight_failed, "runtime PID 1 must start as root"));
    }
    // Lock away UID-0 implicit privilege regain before reducing the capability sets.  The
    // NO_SETUID_FIXUP bit deliberately lets the forked child retain the three temporary caps
    // while it calls setresgid/setresuid; harden_task immediately clears them afterwards.
    constexpr unsigned int kSecureBits =
        SECBIT_KEEP_CAPS_LOCKED | SECBIT_NOROOT | SECBIT_NOROOT_LOCKED | SECBIT_NO_SETUID_FIXUP |
        SECBIT_NO_SETUID_FIXUP_LOCKED | SECBIT_NO_CAP_AMBIENT_RAISE |
        SECBIT_NO_CAP_AMBIENT_RAISE_LOCKED;
    if (prctl(PR_SET_SECUREBITS, kSecureBits, 0, 0, 0) != 0) {
        return std::unexpected(
            errno_error(ErrorCode::sandbox_preflight_failed, "lock supervisor securebits"));
    }
    if (prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0) != 0) {
        return std::unexpected(errno_error(ErrorCode::sandbox_preflight_failed,
                                           "clear supervisor ambient capabilities"));
    }
    if (const auto bounding_set = constrain_supervisor_bounding_set(); !bounding_set) {
        return std::unexpected(bounding_set.error());
    }
    if (const auto capabilities = install_supervisor_capabilities(); !capabilities) {
        return std::unexpected(capabilities.error());
    }
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0) {
        return std::unexpected(
            errno_error(ErrorCode::sandbox_preflight_failed, "set supervisor no_new_privs"));
    }
    if (prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0) {
        return std::unexpected(
            errno_error(ErrorCode::sandbox_preflight_failed, "disable supervisor dumpability"));
    }
    return {};
}

} // namespace wspctl
