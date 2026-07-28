#pragma once

#include "wspctl/infrastructure/common.hpp"
#include "wspctl/infrastructure/image.hpp"
#include "wspctl/infrastructure/xfs_project_quota.hpp"

#include <cstdint>
#include <filesystem>
#include <optional>
#include <string>
#include <sys/types.h>

namespace wspctl {

/**
 * @brief 特权 broker 的 sandbox 配置 / Sandbox configuration for the privileged broker.
 */
struct SandboxConfig final {
    /** @brief 仅 broker 管理的 content-addressed image store / Content-addressed image store managed only by broker operations. */
    std::filesystem::path images_root;
    /** @brief 唯一选中的 OCI image identity / Sole selected OCI image identity. */
    std::optional<OciImageDigest> image_digest;
    /** @brief journal、upperdir 与 runtime 元数据根 / Root for journal, upperdirs, and runtime metadata. */
    std::filesystem::path state_root;
    /** @brief 唯一允许的 XFS project-quota 容量边界 / Sole permitted XFS project-quota capacity boundary. */
    XfsProjectQuotaConfig xfs_project_quota;
    /** @brief systemd Delegate=yes 委派的 cgroup v2 根 / cgroup v2 root delegated by systemd Delegate=yes. */
    std::filesystem::path cgroup_root;
    /** @brief runtime cgroup 的 memory.max（字节） / runtime cgroup memory.max in bytes. */
    std::uint64_t memory_max_bytes{512U * 1024U * 1024U};
    /** @brief runtime cgroup 的 memory.high（0 表示与 memory.max 相同） / runtime cgroup memory.high (0 means memory.max). */
    std::uint64_t memory_high_bytes{};
    /** @brief runtime cgroup 的 memory.swap.max（默认 0，禁止 swap） / runtime cgroup memory.swap.max (default 0, disables swap). */
    std::uint64_t memory_swap_max_bytes{};
    /** @brief runtime cgroup 的 cpu.max 配额微秒 / runtime cgroup cpu.max quota in microseconds. */
    std::uint64_t cpu_max_quota_us{};
    /** @brief runtime cgroup 的 cpu.max period 微秒 / runtime cgroup cpu.max period in microseconds. */
    std::uint32_t cpu_max_period_us{};
    /** @brief runtime cgroup 的 pids.max / runtime cgroup pids.max. */
    std::uint32_t pids_max{128U};
    /** @brief runtime cgroup 的 io.weight / runtime cgroup io.weight. */
    std::uint16_t io_weight{100U};
    /** @brief runtime 内任务 UID / Task UID inside runtime. */
    uid_t sandbox_uid{};
    /** @brief runtime 内任务 GID / Task GID inside runtime. */
    gid_t sandbox_gid{};
};

/**
 * @brief 从强类型 identity 派生唯一 rootfs 路径 / Derive the sole rootfs path from a typed identity.
 * @param config sandbox 配置 / Sandbox configuration.
 * @return ``<images>/sha256/<hex>/rootfs`` 或缺失 identity 错误 /
 *         ``<images>/sha256/<hex>/rootfs`` or a missing-identity error.
 */
[[nodiscard]] Result<std::filesystem::path> image_root(const SandboxConfig& config);

/**
 * @brief 单个 activation 的持久 OverlayFS 层 / Persistent OverlayFS layer for one activation.
 */
struct TaskLayer final {
    /** @brief 已验证的 persistent quota pair binding / Verified persistent quota-pair binding. */
    RuntimeQuotaBinding quota_binding;
    /** @brief 已验证 activation 的原文本 / Original validated activation text. */
    std::string activation_id;
    /** @brief runtime 状态目录 / Runtime state directory. */
    std::filesystem::path runtime_dir;
    /** @brief runtime 持久 workspace upperdir / Runtime-persistent workspace upperdir. */
    std::filesystem::path upper_dir;
    /** @brief 每次 mount 新建且为空的 OverlayFS workdir / Fresh and empty OverlayFS workdir per mount. */
    std::filesystem::path work_dir;
    /** @brief base root 的新 root mountpoint / New-root mountpoint for the base root. */
    std::filesystem::path root_dir;
    /** @brief 只读 workspace lower bind / Read-only workspace lower bind. */
    std::filesystem::path workspace_lower_dir;
    /** @brief 临时 merged workspace mountpoint / Temporary merged workspace mountpoint. */
    std::filesystem::path merged_dir;
};

/**
 * @brief broker 传给 PID 1 的 task cgroup 控制 FD / Task-cgroup control FDs passed from broker to PID 1.
 */
struct TaskCgroupControl final {
    /** @brief supervisor cgroup.procs 的可写 FD / Writable supervisor cgroup.procs FD. */
    int supervisor_procs_fd{-1};
    /** @brief task cgroup.procs 的可写 FD / Writable task cgroup.procs FD. */
    int procs_fd{-1};
    /** @brief task cgroup.kill 的可写 FD / Writable task cgroup.kill FD. */
    int kill_fd{-1};
    /** @brief task cgroup.events 的只读 FD / Read-only task cgroup.events FD. */
    int events_fd{-1};
};

/**
 * @brief 校验目录自身与生产祖先链的所有权 / Validate directory ownership and its production ancestor chain.
 * @param path 已存在的绝对目录 / Existing absolute directory.
 * @param allow_insecure_dev_root 是否只放宽非终点祖先 / Whether to relax only non-terminal ancestors.
 * @return 成功或 fail-closed 拒绝 / Success or a fail-closed rejection.
 * @note 终点目录始终必须为 root-owned 且不可 group/other 写；开发开关只将 checkout
 *       ancestors 纳入本机 TCB。 The endpoint is always root-owned and non-writable by
 *       group/other; the development flag only puts checkout ancestors into the local TCB.
 */
[[nodiscard]] Result<void> validate_secure_directory_ancestry(
    const std::filesystem::path& path,
    bool allow_insecure_dev_root);

/**
 * @brief 执行 fail-closed 运行前检查 / Execute fail-closed preflight checks.
 * @param config sandbox 配置 / Sandbox configuration.
 * @return 成功或拒绝原因 / Success or rejection reason.
 * @note 缺少 CAP_SYS_ADMIN、cgroup delegation、镜像证明或内核能力一律失败。
 *       Missing CAP_SYS_ADMIN, cgroup delegation, image attestation, or kernel capability always fails.
 */
[[nodiscard]] Result<void> preflight_sandbox(const SandboxConfig& config);

/**
 * @brief 将 broker 移入 manager leaf 后启用 delegated controllers / Move broker to a manager leaf then enable delegated controllers.
 * @param config sandbox 配置 / Sandbox configuration.
 * @return 成功或拒绝 / Success or rejection.
 * @note 这样 cgroup_root 内部节点没有进程，cpu/memory/pids controller 可以沿树启用。
 *       This leaves cgroup_root internal nodes process-free so cpu/memory/pids controllers can be enabled down the tree.
 */
[[nodiscard]] Result<void> prepare_broker_cgroup(const SandboxConfig& config);

/**
 * @brief 为 activation 创建 quota-verified workspace OverlayFS 挂载目录 / Create quota-verified workspace OverlayFS mount directories for an activation.
 * @param config sandbox 配置 / Sandbox configuration.
 * @param quota 已 preflight 的 XFS quota 服务 / Preflighted XFS quota service.
 * @param activation_lease 为该 runtime 持有的 activation 排他租约 / Exclusive activation lease held for this runtime.
 * @param activation_id 激活标识 / Activation ID.
 * @return 不含未经哈希用户路径的层路径 / Layer paths without unhashed user paths.
 * @note upperdir 按 runtime 持久化，因此新 activation 可恢复 workspace；workdir 每次创建且必须为空。
 *       The upperdir persists per runtime so a new activation recovers its workspace; workdir is fresh and empty each time.
 */
[[nodiscard]] Result<TaskLayer> prepare_task_layer(
    const SandboxConfig& config,
    const XfsProjectQuota& quota,
    const RuntimeActivationLease& activation_lease,
    const std::string& activation_id);

/**
 * @brief 仅删除一个已退出 activation 的 transient mount staging / Remove transient mount staging for one exited activation only.
 * @param config sandbox 配置 / Sandbox configuration.
 * @param quota 已 preflight 的 XFS quota 服务 / Preflighted XFS quota service.
 * @param activation_lease 为该 runtime 持有的 activation 排他租约 / Exclusive activation lease held for this runtime.
 * @param layer 要清理的 activation 层 / Activation layer to clean.
 * @return 成功或 fail-closed 错误 / Success or fail-closed error.
 * @note 调用者必须先证明 runtime cgroup 为 populated 0 且 launcher PID 已退出；此函数
 *       永不删除 runtime-persistent workspace upperdir。 The caller must first prove the
 *       runtime cgroup is populated 0 and the launcher has exited; this never deletes the
 *       runtime-persistent workspace upperdir.
 */
[[nodiscard]] Result<void> cleanup_task_layer(
    const SandboxConfig& config,
    const XfsProjectQuota& quota,
    const RuntimeActivationLease& activation_lease,
    const TaskLayer& layer);

/**
 * @brief 在 cgroup task-free 证明之后回收 crash 遗留的 transient staging / Reclaim crash-left transient staging after a cgroup task-free proof.
 * @param config sandbox 配置 / Sandbox configuration.
 * @param quota 已 preflight 的 XFS quota 服务 / Preflighted XFS quota service.
 * @param activation_lease 为该 runtime 持有的 activation 排他租约 / Exclusive activation lease held for this runtime.
 * @param runtime_key runtime 标识 / Runtime key.
 * @return 成功或 fail-closed recovery 错误 / Success or a fail-closed recovery error.
 * @note 此 wrapper 将同一 runtime 的 ``cgroup.events: populated 0`` 与 quota GC 绑定，
 *       防止调用方只凭路径调用清理。/ This wrapper binds ``cgroup.events: populated 0`` for
 *       the same runtime to quota GC, preventing path-only cleanup by callers.
 */
[[nodiscard]] Result<void> reclaim_dead_task_layers(
    const SandboxConfig& config,
    const XfsProjectQuota& quota,
    const RuntimeActivationLease& activation_lease,
    const std::string& runtime_key);

/**
 * @brief 新 PID/mount namespace 中建立只读 lower 与 OverlayFS / Establish readonly lower and OverlayFS in new PID/mount namespace.
 * @param config sandbox 配置 / Sandbox configuration.
 * @param layer activation 层 / Activation layer.
 * @return 成功或拒绝 / Success or rejection.
 * @note 必先将传播改为 MS_PRIVATE，任何 mount 失败都会让子进程退出。
 *       Propagation is first changed to MS_PRIVATE; any mount failure makes the child exit.
 */
[[nodiscard]] Result<void> setup_runtime_mounts(const SandboxConfig& config, const TaskLayer& layer);

/**
 * @brief 创建 runtime 的 supervisor/task cgroup 分层并打开控制 FD / Create runtime supervisor/task cgroup hierarchy and open control FDs.
 * @param config sandbox 配置 / Sandbox configuration.
 * @param runtime_key runtime 标识 / Runtime key.
 * @return launcher/PID 1 使用的 supervisor/task cgroup FD / Supervisor/task cgroup FDs used by the launcher/PID 1.
 */
[[nodiscard]] Result<TaskCgroupControl> prepare_runtime_cgroup(
    const SandboxConfig& config,
    const std::string& runtime_key);

/**
 * @brief 用 cgroup.kill 清理整个 runtime / Tear down an entire runtime with cgroup.kill.
 * @param config sandbox 配置 / Sandbox configuration.
 * @param runtime_key runtime 标识 / Runtime key.
 * @return 成功或拒绝 / Success or rejection.
 */
[[nodiscard]] Result<void> kill_runtime_cgroup(const SandboxConfig& config, const std::string& runtime_key);

/**
 * @brief 等待 runtime cgroup 不再包含任何进程 / Wait until a runtime cgroup has no remaining processes.
 * @param config sandbox 配置 / Sandbox configuration.
 * @param runtime_key runtime 标识 / Runtime key.
 * @return cgroup.events 报告 populated 0，或 fail-closed 错误 / populated 0 from cgroup.events, or a fail-closed error.
 * @note 调用方在成功前不得复用或删除该 runtime cgroup。
 *       Callers must not reuse or delete the runtime cgroup before success.
 */
[[nodiscard]] Result<void> wait_runtime_cgroup_empty(const SandboxConfig& config, const std::string& runtime_key);

/**
 * @brief 丢弃 capability、设 no_new_privs 并加载 seccomp / Drop capabilities, set no_new_privs, and load seccomp.
 * @param uid 目标 UID / Target UID.
 * @param gid 目标 GID / Target GID.
 * @return 成功或拒绝 / Success or rejection.
 * @note filter 显式拒绝 mount、unshare、setns、pivot_root、bpf 与 ptrace 路径。
 *       The filter explicitly denies mount, unshare, setns, pivot_root, bpf, and ptrace paths.
 */
[[nodiscard]] Result<void> harden_task(uid_t uid, gid_t gid);

/**
 * @brief 将 runtime PID 1 缩减为最小 supervisor 权限 / Reduce runtime PID 1 to minimal supervisor privileges.
 * @return 成功或 fail-closed 错误 / Success or fail-closed error.
 * @note PID 1 保持 UID 0，但只保留 CAP_SETUID、CAP_SETGID、CAP_KILL；随后 fork 的
 *       task child 用 harden_task 清空这些 capability 并降到 sandbox identity。
 *       PID 1 stays UID 0 but retains only CAP_SETUID, CAP_SETGID, and CAP_KILL; a forked
 *       task child clears them through harden_task and drops to the sandbox identity.
 */
[[nodiscard]] Result<void> harden_supervisor();

}  // namespace wspctl
