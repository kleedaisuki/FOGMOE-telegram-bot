#pragma once

#include "wspctl/domain/runtime.hpp"

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace wspctl::domain {

/** @brief 单次 operator 目录查询允许返回的最大条目数 / Maximum entries returned by one operator
 * directory query. */
inline constexpr std::size_t kOperatorWorkspaceListingLimit{128U};

/** @brief operator 可见的 workspace 持久化状态 / Persistent-workspace state visible to an operator.
 */
enum class WorkspacePersistence : std::uint8_t {
    /** @brief 尚无已持久化 workspace / No persistent workspace exists yet. */
    absent = 0,
    /** @brief 已验证的持久化 workspace 已存在 / A verified persistent workspace exists. */
    ready = 1,
};

/** @brief operator 可见的 RuntimeProcess 活动状态 / RuntimeProcess activity state visible to an
 * operator. */
enum class WorkspaceActivity : std::uint8_t {
    /** @brief 没有当前 RuntimeProcess / There is no current RuntimeProcess. */
    inactive = 0,
    /** @brief 正创建 RuntimeProcess / A RuntimeProcess is being created. */
    activating = 1,
    /** @brief RuntimeProcess 已就绪 / The RuntimeProcess is ready. */
    ready = 2,
    /** @brief RuntimeProcess 正执行任务 / The RuntimeProcess is executing a task. */
    executing = 3,
    /** @brief RuntimeProcess 正被回收 / The RuntimeProcess is being retired. */
    retiring = 4,
    /** @brief 当前 RuntimeProcess 已失败且等待清理 / The current RuntimeProcess failed and awaits
       cleanup. */
    failed = 5,
};

/** @brief 经内核 XFS project quota 读取的已验证 workspace 用量 / Validated workspace usage read
 * from kernel XFS project quota. */
class WorkspaceQuotaUsage final {
public:
    /**
     * @brief 验证并创建 kernel quota 快照 / Validate and create a kernel quota snapshot.
     * @param used_bytes 当前已计费字节数 / Currently accounted bytes.
     * @param hard_bytes workspace 的字节 hard limit / Workspace byte hard limit.
     * @param used_inodes 当前已计费 inode 数 / Currently accounted inode count.
     * @param hard_inodes workspace 的 inode hard limit / Workspace inode hard limit.
     * @return 已验证配额快照或领域错误 / Validated quota snapshot or a domain error.
     * @note usage 可以暂时超过 limit，以便如实呈现已有 XFS overage；hard limit 自身不得为零。
     *       Usage may temporarily exceed a limit to faithfully expose an existing XFS overage;
     *       the hard limit itself must not be zero.
     */
    [[nodiscard]] static Result<WorkspaceQuotaUsage> create(std::uint64_t used_bytes,
                                                            std::uint64_t hard_bytes,
                                                            std::uint64_t used_inodes,
                                                            std::uint64_t hard_inodes);

    /** @brief 取得当前已计费字节数 / Get currently accounted bytes. */
    [[nodiscard]] std::uint64_t used_bytes() const noexcept;
    /** @brief 取得 byte hard limit / Get the byte hard limit. */
    [[nodiscard]] std::uint64_t hard_bytes() const noexcept;
    /** @brief 取得当前已计费 inode 数 / Get currently accounted inode count. */
    [[nodiscard]] std::uint64_t used_inodes() const noexcept;
    /** @brief 取得 inode hard limit / Get the inode hard limit. */
    [[nodiscard]] std::uint64_t hard_inodes() const noexcept;

    /** @brief 比较 quota 用量快照 / Compare quota-usage snapshots. */
    [[nodiscard]] bool operator==(const WorkspaceQuotaUsage&) const noexcept = default;

private:
    /**
     * @brief 以已验证字段构造 quota 快照 / Construct a quota snapshot from validated fields.
     * @param used_bytes 已验证已计费字节数 / Validated accounted bytes.
     * @param hard_bytes 已验证 byte hard limit / Validated byte hard limit.
     * @param used_inodes 已验证已计费 inode 数 / Validated accounted inode count.
     * @param hard_inodes 已验证 inode hard limit / Validated inode hard limit.
     */
    WorkspaceQuotaUsage(std::uint64_t used_bytes, std::uint64_t hard_bytes,
                        std::uint64_t used_inodes, std::uint64_t hard_inodes) noexcept;

    /** @brief 当前已计费字节数 / Currently accounted bytes. */
    std::uint64_t used_bytes_{};
    /** @brief workspace 的 byte hard limit / Workspace byte hard limit. */
    std::uint64_t hard_bytes_{};
    /** @brief 当前已计费 inode 数 / Currently accounted inode count. */
    std::uint64_t used_inodes_{};
    /** @brief workspace 的 inode hard limit / Workspace inode hard limit. */
    std::uint64_t hard_inodes_{};
};

/** @brief operator 的已验证 allowlisted runtime 状态读模型 / Validated allowlisted runtime-status
 * read model for an operator. */
class OperatorWorkspaceStatus final {
public:
    /**
     * @brief 验证并创建 operator runtime 状态 / Validate and create an operator runtime status.
     * @param runtime 查询的长期 runtime 标识 / Queried long-lived runtime identity.
     * @param persistence 持久 workspace 存在状态 / Persistent-workspace existence state.
     * @param activity 当前 RuntimeProcess 活动状态 / Current RuntimeProcess activity state.
     * @param quota ready workspace 的 kernel quota；absent workspace 必须为空 /
     *     Kernel quota for a ready workspace; it must be empty for an absent workspace.
     * @return 已验证状态或领域错误 / Validated status or a domain error.
     */
    [[nodiscard]] static Result<OperatorWorkspaceStatus>
    create(RuntimeId runtime, WorkspacePersistence persistence, WorkspaceActivity activity,
           std::optional<WorkspaceQuotaUsage> quota);

    /** @brief 取得查询的长期 runtime 标识 / Get the queried long-lived runtime identity. */
    [[nodiscard]] const RuntimeId& runtime() const noexcept;
    /** @brief 取得持久 workspace 的存在状态 / Get the persistent-workspace existence state. */
    [[nodiscard]] WorkspacePersistence persistence() const noexcept;
    /** @brief 取得当前 RuntimeProcess 活动状态 / Get the current RuntimeProcess activity state. */
    [[nodiscard]] WorkspaceActivity activity() const noexcept;
    /** @brief 取得可选 kernel quota 快照 / Get the optional kernel quota snapshot. */
    [[nodiscard]] const std::optional<WorkspaceQuotaUsage>& quota() const noexcept;

    /** @brief 比较 operator workspace 状态 / Compare operator workspace statuses. */
    [[nodiscard]] bool operator==(const OperatorWorkspaceStatus&) const noexcept = default;

private:
    /**
     * @brief 从已验证字段构造状态 / Construct a status from validated fields.
     * @param runtime 已验证 runtime 标识 / Validated runtime identity.
     * @param persistence 已验证 persistence / Validated persistence.
     * @param activity 已验证 activity / Validated activity.
     * @param quota 已验证 quota presence / Validated quota presence.
     */
    OperatorWorkspaceStatus(RuntimeId runtime, WorkspacePersistence persistence,
                            WorkspaceActivity activity,
                            std::optional<WorkspaceQuotaUsage> quota) noexcept;

    /** @brief 查询的长期 runtime 标识 / Queried long-lived runtime identity. */
    RuntimeId runtime_;
    /** @brief 持久 workspace 的存在状态 / Persistent-workspace existence state. */
    WorkspacePersistence persistence_{};
    /** @brief 当前 RuntimeProcess 活动状态 / Current RuntimeProcess activity state. */
    WorkspaceActivity activity_{};
    /** @brief ready workspace 的可选 kernel quota / Optional kernel quota for a ready workspace. */
    std::optional<WorkspaceQuotaUsage> quota_;
};

/**
 * @brief 仅指向 runtime `/workspace` 树的规范路径值对象 / Canonical path value object confined to a
 * runtime `/workspace` tree.
 *
 * 它不是 host filesystem path：它只表示 namespace 内的逻辑路径。基础设施必须从已经验证的
 * `upper` directory FD 开始，以 `openat2` 逐分量解析它。/ This is not a host filesystem path:
 * it represents only a logical in-namespace path. Infrastructure must start at a verified `upper`
 * directory FD and resolve it component by component through `openat2`.
 */
class OperatorWorkspacePath final {
public:
    /**
     * @brief 解析规范的 `/workspace` 内路径 / Parse a canonical path inside `/workspace`.
     * @param value 待验证逻辑路径 / Logical path to validate.
     * @return 已验证路径或领域错误 / Validated path or a domain error.
     * @note 不接受 host-absolute path、`.`、`..`、重复分隔符、尾随分隔符或 NUL。
     *       Host-absolute paths, `.`, `..`, repeated separators, trailing separators, and NUL are
     * rejected.
     */
    [[nodiscard]] static Result<OperatorWorkspacePath> parse(std::string value);

    /** @brief 取得规范逻辑路径 / Get the canonical logical path. */
    [[nodiscard]] const std::string& value() const noexcept;

    /**
     * @brief 取得 `/workspace` 之后的受限路径分量 / Get constrained components after `/workspace`.
     * @return 不包含根名的有序分量 / Ordered components excluding the root name.
     */
    [[nodiscard]] std::vector<std::string_view> relative_components() const;

    /** @brief 比较规范逻辑路径 / Compare canonical logical paths. */
    [[nodiscard]] bool operator==(const OperatorWorkspacePath&) const noexcept = default;

private:
    /**
     * @brief 以已验证路径构造 / Construct from a validated path.
     * @param value 已验证逻辑路径 / Validated logical path.
     */
    explicit OperatorWorkspacePath(std::string value);

    /** @brief 已验证的 `/workspace` 路径 / Validated `/workspace` path. */
    std::string value_;
};

/** @brief operator 目录列出的节点类型 / Node kind listed by the operator directory view. */
enum class WorkspaceEntryKind : std::uint8_t {
    /** @brief 普通文件 / Regular file. */
    regular_file = 1,
    /** @brief 目录 / Directory. */
    directory = 2,
    /** @brief 符号链接（绝不读取 target） / Symbolic link (whose target is never read). */
    symbolic_link = 3,
};

/**
 * @brief 一条经过命名语义验证的 workspace 目录项 / One workspace directory entry with validated
 * naming semantics.
 *
 * `encoded_name` 采用可逆 ASCII percent encoding；它不是原始 filename，避免不可信 filename
 * 进入 terminal 或 operator protocol 的文本显示边界。/ `encoded_name` uses reversible ASCII
 * percent encoding; it is not the raw filename, keeping untrusted filenames out of terminal and
 * operator-protocol text display boundaries.
 */
class WorkspaceEntry final {
public:
    /**
     * @brief 构造一个已安全编码的目录项 / Construct a safely encoded directory entry.
     * @param encoded_name 已 percent-encoded 的单一 filename / Percent-encoded single filename.
     * @param kind 不跟随链接观测到的节点类型 / Node kind observed without following links.
     * @param size_bytes 普通文件的逻辑字节数；其他类型为零 / Logical size for a regular file; zero
     * otherwise.
     * @return 已验证条目或领域错误 / Validated entry or a domain error.
     */
    [[nodiscard]] static Result<WorkspaceEntry>
    create(std::string encoded_name, WorkspaceEntryKind kind, std::uint64_t size_bytes);

    /** @brief 取得安全显示名 / Get the safe display name. */
    [[nodiscard]] const std::string& encoded_name() const noexcept;
    /** @brief 取得节点类型 / Get the node kind. */
    [[nodiscard]] WorkspaceEntryKind kind() const noexcept;
    /** @brief 取得逻辑字节数 / Get the logical byte size. */
    [[nodiscard]] std::uint64_t size_bytes() const noexcept;

    /** @brief 比较目录项 / Compare directory entries. */
    [[nodiscard]] bool operator==(const WorkspaceEntry&) const noexcept = default;

private:
    /**
     * @brief 从已验证字段构造 / Construct from validated fields.
     * @param encoded_name 已验证安全显示名 / Validated safe display name.
     * @param kind 已验证节点类型 / Validated node kind.
     * @param size_bytes 已验证逻辑字节数 / Validated logical byte size.
     */
    WorkspaceEntry(std::string encoded_name, WorkspaceEntryKind kind,
                   std::uint64_t size_bytes) noexcept;

    /** @brief 安全、可逆编码的 filename / Safe reversible encoded filename. */
    std::string encoded_name_;
    /** @brief 不跟随链接观测到的节点类型 / Node kind observed without following links. */
    WorkspaceEntryKind kind_{};
    /** @brief 普通文件逻辑字节数 / Regular-file logical byte size. */
    std::uint64_t size_bytes_{};
};

/** @brief 一次有界、只读的 workspace 目录列举结果 / One bounded read-only workspace directory
 * listing. */
struct WorkspaceListing final {
    /** @brief 已验证的被列举路径 / Validated path being listed. */
    OperatorWorkspacePath path;
    /** @brief 按 encoded_name 字节序排序的目录项 / Directory entries sorted by encoded-name byte
     * order. */
    std::vector<WorkspaceEntry> entries;
    /** @brief 是否因固定上限而省略后续项 / Whether later entries were omitted by the fixed cap. */
    bool truncated{false};
};

/**
 * @brief 将原始 POSIX filename 编码为安全可逆 ASCII / Encode a raw POSIX filename into safe
 * reversible ASCII.
 * @param raw_name 不含 `/` 与 NUL 的原始目录项名称 / Raw directory-entry name without `/` or NUL.
 * @return percent-encoded ASCII 名称或领域错误 / Percent-encoded ASCII name or a domain error.
 * @note 编码规则固定为保留 `[A-Za-z0-9._-]`，其余每个 byte 写为大写 `%HH`。
 *       The fixed rule preserves `[A-Za-z0-9._-]` and renders every other byte as uppercase `%HH`.
 */
[[nodiscard]] Result<std::string> encode_workspace_entry_name(std::string_view raw_name);

} // namespace wspctl::domain
