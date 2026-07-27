#pragma once

#include "wspctl/domain/operator_workspace.hpp"

#include <cstdint>
#include <expected>
#include <string>
#include <utility>

namespace wspctl::application {

/** @brief operator 只读查询的应用层错误分类 / Application error categories for operator read-only queries. */
enum class OperatorWorkspaceQueryErrorCode : std::uint8_t {
    /** @brief 请求的持久 workspace 尚不存在 / The requested persistent workspace does not exist. */
    not_found = 1,
    /** @brief 已知数据违反 read-model 不变量 / Known data violates a read-model invariant. */
    inconsistent = 2,
    /** @brief 受控 read-model 暂时不可用 / The controlled read model is temporarily unavailable. */
    unavailable = 3,
};

/** @brief operator 只读查询的应用层错误值 / Application error value for operator read-only queries. */
struct OperatorWorkspaceQueryError final {
    /** @brief 可供协议映射的稳定错误分类 / Stable category for protocol mapping. */
    OperatorWorkspaceQueryErrorCode code;
    /** @brief 不得含 host path 或 payload 内容的诊断 / Diagnostic without host paths or payload content. */
    std::string message;
};

/** @brief 携带 operator 查询错误的结果 / Result carrying an operator-query error. */
template <typename Value>
using OperatorWorkspaceQueryResult = std::expected<Value, OperatorWorkspaceQueryError>;

/**
 * @brief operator workspace 读模型出站端口 / Operator-workspace read-model outbound port.
 *
 * 此端口只表达经 allowlist 的观测效果。实现不得 allocation runtime、启动 RuntimeProcess、写
 * journal、访问 Bot control socket，或读取文件内容。/ This port expresses only allowlisted
 * observations. Implementations must not allocate a runtime, start a RuntimeProcess, write a
 * journal, access the Bot control socket, or read file contents.
 */
class OperatorWorkspaceReadPort {
public:
    /** @brief 支持经接口安全析构 / Support safe destruction through the interface. */
    virtual ~OperatorWorkspaceReadPort() = default;

    /**
     * @brief 读取一条 runtime 的 allowlisted 状态 / Read one runtime's allowlisted status.
     * @param runtime 已验证长期 runtime 标识 / Validated long-lived runtime identity.
     * @return 状态或归一化的只读查询错误 / Status or a normalized read-only query error.
     */
    [[nodiscard]] virtual OperatorWorkspaceQueryResult<domain::OperatorWorkspaceStatus> status(
        const domain::RuntimeId& runtime) const = 0;

    /**
     * @brief 读取一层 workspace 目录 / Read one workspace directory level.
     * @param runtime 已验证长期 runtime 标识 / Validated long-lived runtime identity.
     * @param path 已验证 `/workspace` 逻辑路径 / Validated `/workspace` logical path.
     * @return 有界目录项或归一化的只读查询错误 / Bounded directory entries or a normalized read-only query error.
     */
    [[nodiscard]] virtual OperatorWorkspaceQueryResult<domain::WorkspaceListing> list(
        const domain::RuntimeId& runtime,
        const domain::OperatorWorkspacePath& path) const = 0;
};

/**
 * @brief operator workspace 只读查询用例 / Operator-workspace read-only query use case.
 *
 * 该服务是 presentation 与 infrastructure read model 之间的应用层边界。它复核返回对象的
 * runtime/path 对应关系，避免错误 adapter 把某个 workspace 的内容归因到另一个 runtime。
 * This service is the application boundary between presentation and the infrastructure read
 * model. It rechecks returned runtime/path correspondence so a faulty adapter cannot attribute one
 * workspace's contents to another runtime.
 */
class OperatorWorkspaceQueryService final {
public:
    /**
     * @brief 查询 runtime 的状态与配额快照 / Query a runtime's status and quota snapshot.
     * @param runtime 已验证 runtime 标识 / Validated runtime identity.
     * @param port 只读 read-model 端口 / Read-only read-model port.
     * @return allowlisted 状态或查询错误 / Allowlisted status or a query error.
     */
    [[nodiscard]] OperatorWorkspaceQueryResult<domain::OperatorWorkspaceStatus> status(
        const domain::RuntimeId& runtime,
        const OperatorWorkspaceReadPort& port) const;

    /**
     * @brief 查询一层 workspace 目录 / Query one workspace directory level.
     * @param runtime 已验证 runtime 标识 / Validated runtime identity.
     * @param path 已验证逻辑路径 / Validated logical path.
     * @param port 只读 read-model 端口 / Read-only read-model port.
     * @return 有界目录项或查询错误 / Bounded directory entries or a query error.
     */
    [[nodiscard]] OperatorWorkspaceQueryResult<domain::WorkspaceListing> list(
        const domain::RuntimeId& runtime,
        const domain::OperatorWorkspacePath& path,
        const OperatorWorkspaceReadPort& port) const;
};

/**
 * @brief 构造 operator 查询错误 / Construct an operator-query error.
 * @param code 稳定错误分类 / Stable error category.
 * @param message 不含敏感内容的诊断 / Diagnostic without sensitive content.
 * @return 可传播查询错误 / Propagatable query error.
 */
[[nodiscard]] OperatorWorkspaceQueryError make_operator_workspace_query_error(
    OperatorWorkspaceQueryErrorCode code,
    std::string message);

}  // namespace wspctl::application
