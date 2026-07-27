#pragma once

#include "wspctl/infrastructure/common.hpp"

#include <cstdint>
#include <filesystem>
#include <string_view>

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
    /** @brief 专用、可写 XFS mount 的挂载点 / Mountpoint of the dedicated writable XFS filesystem. */
    std::filesystem::path mount_path;
    /** @brief 可分配 project-ID range 的首个非零偶数 ID / First nonzero even project ID in the allocatable range. */
    std::uint32_t project_id_min{};
    /** @brief 可分配 project-ID range 的末个奇数 ID / Last odd project ID in the allocatable range. */
    std::uint32_t project_id_max{};
    /** @brief 每个 runtime control tree 的 block hard limit（字节） / Per-runtime control-tree block hard limit in bytes. */
    std::uint64_t control_hard_bytes{};
    /** @brief 每个 runtime control tree 的 inode hard limit / Per-runtime control-tree inode hard limit. */
    std::uint64_t control_hard_inodes{};
    /** @brief 每个 runtime workspace tree 的 block hard limit（字节） / Per-runtime workspace-tree block hard limit in bytes. */
    std::uint64_t workspace_hard_bytes{};
    /** @brief 每个 runtime workspace tree 的 inode hard limit / Per-runtime workspace-tree inode hard limit. */
    std::uint64_t workspace_hard_inodes{};
    /** @brief 所有 project pair 可保守预留的总字节预算 / Total bytes conservatively reservable by all project pairs. */
    std::uint64_t global_admission_bytes{};
    /** @brief 所有 project pair 可保守预留的总 inode 预算 / Total inodes conservatively reservable by all project pairs. */
    std::uint64_t global_admission_inodes{};
    /** @brief 不给 runtime 分配的 XFS 字节保留量 / XFS bytes reserved outside runtime admission. */
    std::uint64_t system_reserve_bytes{};
    /** @brief 不给 runtime 分配的 XFS inode 保留量 / XFS inodes reserved outside runtime admission. */
    std::uint64_t system_reserve_inodes{};
};

/**
 * @brief 一个 runtime 已持久化的 XFS project pair 与目录根 / Persisted XFS project pair and directory roots for one runtime.
 */
struct RuntimeQuotaBinding final {
    /** @brief opaque runtime key 的 SHA-256 命名目录 / SHA-256-named directory for the opaque runtime key. */
    std::filesystem::path runtime_dir;
    /** @brief journal/mount staging 所属 control project root / Control-project root for journals and mount staging. */
    std::filesystem::path control_dir;
    /** @brief upper/work 所属 workspace project root / Workspace-project root for upper and work paths. */
    std::filesystem::path workspace_dir;
    /** @brief control tree 的持久 XFS project ID / Persistent XFS project ID for the control tree. */
    std::uint32_t control_project_id{};
    /** @brief workspace tree 的持久 XFS project ID / Persistent XFS project ID for the workspace tree. */
    std::uint32_t workspace_project_id{};
};

/**
 * @brief 单次 activation 的 quota-owned transient 目录 / Quota-owned transient directories for one activation.
 */
struct RuntimeQuotaActivationStorage final {
    /** @brief control/mounts 下的 activation 专有目录 / Activation-specific directory below control/mounts. */
    std::filesystem::path control_activation_dir;
    /** @brief workspace/work 下的 activation 专有 OverlayFS workdir / Activation-specific OverlayFS workdir below workspace/work. */
    std::filesystem::path workspace_work_dir;
};

/**
 * @brief 对 XFS-only quota 配置与挂载状态执行 fail-closed 预检 / Fail-closed preflight for XFS-only quota configuration and mount state.
 * @param config XFS project-quota 配置 / XFS project-quota configuration.
 * @param state_root broker 的持久状态根 / Broker persistent state root.
 * @return 成功或不可接受的 quota/mount 原因 / Success or an unacceptable quota/mount reason.
 * @note 此函数要求真实 XFS、可写 mount、project accounting 和 enforcement；没有 fallback。
 *       This requires real XFS, a writable mount, project accounting, and enforcement; there is no fallback.
 */
[[nodiscard]] Result<void> preflight_xfs_project_quota(
    const XfsProjectQuotaConfig& config,
    const std::filesystem::path& state_root);

/**
 * @brief XFS project-quota 持久化基础设施服务 / Persistent XFS project-quota infrastructure service.
 *
 * registry 以 ``allocating``、``ready``、``quarantined`` 三态保存 project pair。任一不确定
 * provisioning 结果只会进入 quarantine，绝不复用 ID 或创建无 quota upperdir。/ The registry
 * persists project pairs in ``allocating``, ``ready``, and ``quarantined`` states. Any uncertain
 * provisioning result enters quarantine only; it never reuses an ID or creates an unquoted upperdir.
 */
class XfsProjectQuota final {
public:
    /**
     * @brief 构造一个绑定到 state root 的 XFS quota 服务 / Construct an XFS quota service bound to a state root.
     * @param state_root broker 的持久状态根 / Broker persistent state root.
     * @param config 唯一允许的 XFS quota 配置 / The sole permitted XFS quota configuration.
     */
    XfsProjectQuota(std::filesystem::path state_root, XfsProjectQuotaConfig config);

    /**
     * @brief 在 journal lookup 前确保 runtime 已有 verified project pair / Ensure a runtime has a verified project pair before journal lookup.
     * @param runtime_key canonical runtime UUID / Canonical runtime UUID.
     * @return 已 ready 的 binding，或 fail-closed 错误 / Ready binding, or a fail-closed error.
     * @note 该操作在 durable registry lock 下完成；``allocating`` 的 crash residue 进入
     *       quarantine，而不是猜测恢复。 This completes under the durable registry lock;
     *       crash residue in ``allocating`` is quarantined rather than guessed into recovery.
     */
    [[nodiscard]] Result<RuntimeQuotaBinding> ensure_runtime(std::string_view runtime_key) const;

    /**
     * @brief 创建并验证一次 activation 的 quota-owned transient 目录 / Create and verify quota-owned transient directories for one activation.
     * @param binding 已 ready 的 runtime binding / Ready runtime binding.
     * @param activation_id validated activation identifier / Validated activation identifier.
     * @return activation 的 control/work 路径 / Activation control/work paths.
     * @note ``workdir`` 总是 workspace project 下的新空目录，满足 OverlayFS same-filesystem
     *       constraint。 ``workdir`` is always a fresh empty directory under the workspace
     *       project, satisfying OverlayFS's same-filesystem constraint.
     */
    [[nodiscard]] Result<RuntimeQuotaActivationStorage> prepare_activation_storage(
        const RuntimeQuotaBinding& binding,
        std::string_view activation_id) const;

    /**
     * @brief 删除已退出 activation 的 transient quota storage / Remove transient quota storage of an exited activation.
     * @param binding 已 ready 的 runtime binding / Ready runtime binding.
     * @param activation_id validated activation identifier / Validated activation identifier.
     * @return 成功或 fail-closed cleanup 错误 / Success or a fail-closed cleanup error.
     * @note 永不删除 workspace ``upper``、journal 或 project pair。 This never deletes the
     *       workspace ``upper``, journal, or project pair.
     */
    [[nodiscard]] Result<void> cleanup_activation_storage(
        const RuntimeQuotaBinding& binding,
        std::string_view activation_id) const;

private:
    /** @brief broker 的持久状态根 / Broker persistent state root. */
    std::filesystem::path state_root_;
    /** @brief 唯一允许的 XFS quota 配置 / The sole permitted XFS quota configuration. */
    XfsProjectQuotaConfig config_;
};

}  // namespace wspctl
