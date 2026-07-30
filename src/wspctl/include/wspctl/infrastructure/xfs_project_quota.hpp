#pragma once

#include "wspctl/domain/operator_workspace.hpp"
#include "wspctl/infrastructure/common.hpp"

#include <cstdint>
#include <filesystem>
#include <string_view>
#include <sys/types.h>

namespace wspctl {

/**
 * @brief XFS project-quota 的唯一生产配置 / Sole production configuration for XFS project quota.
 *
 * 这里刻意不是可插拔的 ``QuotaBackend``：workspace 的容量隔离语义依赖 XFS project ID、
 * ``PROJINHERIT`` 和 kernel-enforced hard limit，不能以 ``du``、ext4 或 best-effort
 * adapter 等价替换。/ This is deliberately not a pluggable ``QuotaBackend``: workspace
 * capacity isolation relies on XFS project IDs, ``PROJINHERIT``, and kernel-enforced hard
 * limits, none of which can be equivalently replaced by ``du``, ext4, or a best-effort adapter.
 */
struct XfsProjectQuotaConfig final {
    /** @brief 专用、可写 XFS mount 的挂载点 / Mountpoint of the dedicated writable XFS filesystem.
     */
    std::filesystem::path mount_path;
    /** @brief 可分配 project-ID range 的首个非零偶数 ID / First nonzero even project ID in the
     * allocatable range. */
    std::uint32_t project_id_min{};
    /** @brief 可分配 project-ID range 的末个奇数 ID / Last odd project ID in the allocatable range.
     */
    std::uint32_t project_id_max{};
    /** @brief 每个 runtime control tree 的 block hard limit（字节） / Per-runtime control-tree
     * block hard limit in bytes. */
    std::uint64_t control_hard_bytes{};
    /** @brief 每个 runtime control tree 的 inode hard limit / Per-runtime control-tree inode hard
     * limit. */
    std::uint64_t control_hard_inodes{};
    /** @brief 每个 runtime workspace tree 的 block hard limit（字节） / Per-runtime workspace-tree
     * block hard limit in bytes. */
    std::uint64_t workspace_hard_bytes{};
    /** @brief 每个 runtime workspace tree 的 inode hard limit / Per-runtime workspace-tree inode
     * hard limit. */
    std::uint64_t workspace_hard_inodes{};
    /** @brief 所有 project pair 可保守预留的总字节预算 / Total bytes conservatively reservable by
     * all project pairs. */
    std::uint64_t global_admission_bytes{};
    /** @brief 所有 project pair 可保守预留的总 inode 预算 / Total inodes conservatively reservable
     * by all project pairs. */
    std::uint64_t global_admission_inodes{};
    /** @brief 不给 runtime 分配的 XFS 字节保留量 / XFS bytes reserved outside runtime admission. */
    std::uint64_t system_reserve_bytes{};
    /** @brief 不给 runtime 分配的 XFS inode 保留量 / XFS inodes reserved outside runtime admission.
     */
    std::uint64_t system_reserve_inodes{};
    /**
     * @brief Overlay upper 根的具名 Agent UID / Named Agent UID owning the Overlay upper root.
     * @note broker 从同一 ``SandboxConfig`` 派生该值，不能由 Telegram 或 runtime key
     *       决定。/ The broker derives this value from the same ``SandboxConfig``; Telegram
     *       input and runtime keys cannot select it.
     */
    uid_t workspace_uid{};
    /** @brief Overlay upper 根的具名 Agent GID / Named Agent GID owning the Overlay upper root. */
    gid_t workspace_gid{};
};

/**
 * @brief 一个 runtime 已持久化的 XFS project pair 与目录根 / Persisted XFS project pair and
 * directory roots for one runtime.
 */
struct RuntimeQuotaBinding final {
    /** @brief opaque runtime key 的 SHA-256 命名目录 / SHA-256-named directory for the opaque
     * runtime key. */
    std::filesystem::path runtime_dir;
    /** @brief journal/mount staging 所属 control project root / Control-project root for journals
     * and mount staging. */
    std::filesystem::path control_dir;
    /** @brief upper/work 所属 workspace project root / Workspace-project root for upper and work
     * paths. */
    std::filesystem::path workspace_dir;
    /** @brief control tree 的持久 XFS project ID / Persistent XFS project ID for the control tree.
     */
    std::uint32_t control_project_id{};
    /** @brief workspace tree 的持久 XFS project ID / Persistent XFS project ID for the workspace
     * tree. */
    std::uint32_t workspace_project_id{};
};

/**
 * @brief 单次 activation 的 quota-owned transient 目录 / Quota-owned transient directories for one
 * activation.
 */
struct RuntimeQuotaActivationStorage final {
    /** @brief control/mounts 下的 activation 专有目录 / Activation-specific directory below
     * control/mounts. */
    std::filesystem::path control_activation_dir;
    /** @brief workspace/work 下的 activation 专有 OverlayFS workdir / Activation-specific OverlayFS
     * workdir below workspace/work. */
    std::filesystem::path workspace_work_dir;
};

class XfsProjectQuota;

/**
 * @brief 一个 runtime 的跨 broker activation 排他租约 / Cross-broker exclusive activation lease for
 * one runtime.
 *
 * 租约持有 ``control/activation.lock`` 的 ``flock``，生命周期覆盖存活 PID 1 与其 task
 * cgroup。崩溃会由内核关闭 FD 并释放锁；下一 broker 只能在取得租约、杀死旧 cgroup 并
 * 观察 ``populated 0`` 后回收 transient staging。/ The lease holds an ``flock`` on
 * ``control/activation.lock`` for the lifetime of the live PID 1 and task cgroup. A crash
 * closes the FD and releases the lock in the kernel; the next broker may reclaim transient
 * staging only after acquiring the lease, killing the old cgroup, and observing ``populated 0``.
 */
class RuntimeActivationLease final {
public:
    /** @brief 析构时释放 activation ``flock`` / Release the activation ``flock`` on destruction. */
    ~RuntimeActivationLease();

    /** @brief 禁止复制 OS lock ownership / Copying OS lock ownership is forbidden. */
    RuntimeActivationLease(const RuntimeActivationLease&) = delete;
    /** @brief 禁止复制赋值 OS lock ownership / Copy-assigning OS lock ownership is forbidden. */
    RuntimeActivationLease& operator=(const RuntimeActivationLease&) = delete;

    /**
     * @brief 移交 activation lock ownership / Transfer activation-lock ownership.
     * @param other 被移交的租约 / Lease being transferred.
     */
    RuntimeActivationLease(RuntimeActivationLease&& other) noexcept;

    /**
     * @brief 移动赋值 activation lock ownership / Move-assign activation-lock ownership.
     * @param other 被移交的租约 / Lease being transferred.
     * @return 当前租约 / This lease.
     */
    RuntimeActivationLease& operator=(RuntimeActivationLease&& other) noexcept;

    /**
     * @brief 取得租约绑定的 verified quota roots / Get the verified quota roots bound to this
     * lease.
     * @return 不可变 runtime quota binding / Immutable runtime quota binding.
     */
    [[nodiscard]] const RuntimeQuotaBinding& binding() const noexcept;

private:
    friend class XfsProjectQuota;

    /**
     * @brief 仅由 quota service 构造已锁定租约 / Construct a locked lease only from the quota
     * service.
     * @param binding 已验证 runtime quota binding / Verified runtime quota binding.
     * @param lock_fd 已持有 ``LOCK_EX`` 的私有 regular-file FD / Private regular-file FD holding
     * ``LOCK_EX``.
     */
    RuntimeActivationLease(RuntimeQuotaBinding binding, int lock_fd) noexcept;

    /** @brief 租约绑定的 runtime quota roots / Runtime quota roots bound to the lease. */
    RuntimeQuotaBinding binding_;
    /** @brief 持有 flock 的 private activation-lock FD / Private activation-lock FD holding flock.
     */
    int lock_fd_{-1};
};

/**
 * @brief 对 XFS-only quota 配置与挂载状态执行 fail-closed 预检 / Fail-closed preflight for XFS-only
 * quota configuration and mount state.
 * @param config XFS project-quota 配置 / XFS project-quota configuration.
 * @param state_root broker 的持久状态根 / Broker persistent state root.
 * @return 成功或不可接受的 quota/mount 原因 / Success or an unacceptable quota/mount reason.
 * @note 此函数要求真实 XFS、可写 mount、project accounting 和 enforcement；没有 fallback。
 *       This requires real XFS, a writable mount, project accounting, and enforcement; there is no
 * fallback.
 */
[[nodiscard]] Result<void> preflight_xfs_project_quota(const XfsProjectQuotaConfig& config,
                                                       const std::filesystem::path& state_root);

/**
 * @brief XFS project-quota 持久化基础设施服务 / Persistent XFS project-quota infrastructure
 * service.
 *
 * registry 以 ``allocating``、``ready``、``quarantined`` 三态保存 project pair。任一不确定
 * provisioning 结果只会进入 quarantine，绝不复用 ID 或创建无 quota upperdir。/ The registry
 * persists project pairs in ``allocating``, ``ready``, and ``quarantined`` states. Any uncertain
 * provisioning result enters quarantine only; it never reuses an ID or creates an unquoted
 * upperdir.
 */
class XfsProjectQuota final {
public:
    /**
     * @brief 构造一个绑定到 state root 的 XFS quota 服务 / Construct an XFS quota service bound to
     * a state root.
     * @param state_root broker 的持久状态根 / Broker persistent state root.
     * @param config 唯一允许的 XFS quota 配置 / The sole permitted XFS quota configuration.
     */
    XfsProjectQuota(std::filesystem::path state_root, XfsProjectQuotaConfig config);

    /**
     * @brief 在 journal lookup 前确保 runtime 已有 verified project pair / Ensure a runtime has a
     * verified project pair before journal lookup.
     * @param runtime_key canonical runtime UUID / Canonical runtime UUID.
     * @return 已 ready 的 binding，或 fail-closed 错误 / Ready binding, or a fail-closed error.
     * @note 该操作在 durable registry lock 下完成；``allocating`` 的 crash residue 进入
     *       quarantine，而不是猜测恢复。 This completes under the durable registry lock;
     *       crash residue in ``allocating`` is quarantined rather than guessed into recovery.
     */
    [[nodiscard]] Result<RuntimeQuotaBinding> ensure_runtime(std::string_view runtime_key) const;

    /**
     * @brief 只读查找并读回一个已 ready 的 runtime quota binding / Look up and read back one ready
     * runtime quota binding read-only.
     * @param runtime_key canonical runtime UUID / Canonical runtime UUID.
     * @return 已验证 binding，或不存在/未 ready/不一致错误 / Verified binding, or
     * absent/not-ready/inconsistent error.
     * @note 此 API 绝不创建 registry 目录、lock file、project pair 或 runtime layout；它只打开现有
     *       registry 并以 shared ``flock`` 读取。/ This API never creates registry directories,
     *       a lock file, a project pair, or a runtime layout; it only opens the existing registry
     *       and reads it under a shared ``flock``.
     */
    [[nodiscard]] Result<RuntimeQuotaBinding>
    find_ready_runtime(std::string_view runtime_key) const;

    /**
     * @brief 只读读取一个 verified workspace project 的 kernel quota 用量 / Read kernel quota usage
     * for one verified workspace project.
     * @param binding 已由 `find_ready_runtime` 返回的 ready binding / Ready binding returned by
     * `find_ready_runtime`.
     * @return XFS `Q_XGETQUOTA` 的 bytes/inodes 用量与 hard limits / XFS `Q_XGETQUOTA` byte/inode
     * usage and hard limits.
     * @note 此 API 会再次 read-back 验证 binding，且只使用 `quotactl_fd`；它绝不递归扫描
     *       directory 或以 `du` 猜测用量。/ This API read-back verifies the binding again and uses
     *       only `quotactl_fd`; it never recursively scans directories or guesses usage via `du`.
     */
    [[nodiscard]] Result<domain::WorkspaceQuotaUsage>
    read_workspace_quota_usage(const RuntimeQuotaBinding& binding) const;

    /**
     * @brief 取得一个 runtime 的非阻塞 activation 排他租约 / Acquire a nonblocking exclusive
     * activation lease for one runtime.
     * @param runtime_key canonical runtime UUID / Canonical runtime UUID.
     * @return 持锁租约，或另一 broker 已持有该 runtime 时的 ``busy`` / Locked lease, or ``busy``
     * when another broker owns this runtime.
     * @note 租约必须从 cgroup recovery 到正常 cleanup 全程持有。它只互斥 activation
     *       staging 变更，不替代 journal 的 invocation 幂等性。/ The lease must be held from
     *       cgroup recovery through normal cleanup. It serializes activation-staging mutations
     *       only; it does not replace journal invocation idempotency.
     */
    [[nodiscard]] Result<RuntimeActivationLease>
    acquire_activation_lease(std::string_view runtime_key) const;

    /**
     * @brief 创建并验证一次 activation 的 quota-owned transient 目录 / Create and verify
     * quota-owned transient directories for one activation.
     * @param lease 为该 runtime 持有的 activation 租约 / Activation lease held for this runtime.
     * @param activation_id validated activation identifier / Validated activation identifier.
     * @return activation 的 control/work 路径 / Activation control/work paths.
     * @note ``workdir`` 总是 workspace project 下的新空目录，满足 OverlayFS same-filesystem
     *       constraint。 ``workdir`` is always a fresh empty directory under the workspace
     *       project, satisfying OverlayFS's same-filesystem constraint.
     */
    [[nodiscard]] Result<RuntimeQuotaActivationStorage>
    prepare_activation_storage(const RuntimeActivationLease& lease,
                               std::string_view activation_id) const;

    /**
     * @brief 删除已退出 activation 的 transient quota storage / Remove transient quota storage of
     * an exited activation.
     * @param lease 为该 runtime 持有的 activation 租约 / Activation lease held for this runtime.
     * @param activation_id validated activation identifier / Validated activation identifier.
     * @return 成功或 fail-closed cleanup 错误 / Success or a fail-closed cleanup error.
     * @note 永不删除 workspace ``upper``、journal 或 project pair。 This never deletes the
     *       workspace ``upper``, journal, or project pair.
     */
    [[nodiscard]] Result<void> cleanup_activation_storage(const RuntimeActivationLease& lease,
                                                          std::string_view activation_id) const;

    /**
     * @brief 回收一个已证明无 task 的 runtime 的所有残留 transient activation storage / Reclaim all
     * residual transient activation storage for a runtime proven task-free.
     * @param lease 为该 runtime 持有的 activation 租约 / Activation lease held for this runtime.
     * @return 成功或 fail-closed cleanup 错误 / Success or a fail-closed cleanup error.
     * @note 调用者必须紧邻本调用前以同一 runtime cgroup 观察 ``populated 0``。此函数只会
     *       枚举 ``control/mounts/<sha256>`` 和 ``workspace/work/<sha256>`` 的直接子目录；
     *       从不扫描或删除 ``upper``、journal、registry 或 runtime root。/ The caller must
     *       observe ``populated 0`` for the same runtime cgroup immediately before this call.
     *       This enumerates only direct children of ``control/mounts/<sha256>`` and
     *       ``workspace/work/<sha256>``; it never scans or deletes ``upper``, journals, the
     *       registry, or the runtime root.
     */
    [[nodiscard]] Result<void>
    reclaim_dead_activation_storage(const RuntimeActivationLease& lease) const;

private:
    /**
     * @brief 验证仍由本 service 持有的 activation 租约 / Validate an activation lease still held by
     * this service.
     * @param lease 待验证的 runtime activation 租约 / Runtime activation lease to validate.
     * @return 成功或 fail-closed 验证错误 / Success or a fail-closed validation error.
     * @note 该检查将 lock FD、XFS preflight 及持久 binding 的 readback 绑定在每次 staging
     *       变更前。/ This binds the lock FD, XFS preflight, and persistent-binding readback
     *       before every staging mutation.
     */
    [[nodiscard]] Result<void> validate_activation_lease(const RuntimeActivationLease& lease) const;

    /** @brief broker 的持久状态根 / Broker persistent state root. */
    std::filesystem::path state_root_;
    /** @brief 唯一允许的 XFS quota 配置 / The sole permitted XFS quota configuration. */
    XfsProjectQuotaConfig config_;
};

} // namespace wspctl
