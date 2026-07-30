#pragma once

#include "wspctl/domain/operator_workspace.hpp"
#include "wspctl/infrastructure/common.hpp"

#include <string>
#include <string_view>

namespace wspctl::presentation {

/**
 * @brief 非特权 operator UNIX gateway client / Unprivileged operator UNIX gateway client.
 *
 * 该 client 只连接独立 operator endpoint，并只发送 operator protocol 的 `status` 与 `list`。
 * 它没有 Bot protocol、payload source、RuntimeProcess activation 或 host filesystem API。
 * This client connects only to the independent operator endpoint and sends only operator-protocol
 * `status` and `list`. It has no Bot protocol, payload source, RuntimeProcess activation, or host
 * filesystem API.
 */
class OperatorGatewayClient final {
public:
    /**
     * @brief 以独立 operator socket 构造 client / Construct a client with an independent operator
     * socket.
     * @param socket_path operator UNIX socket 的绝对路径 / Absolute operator UNIX socket path.
     */
    explicit OperatorGatewayClient(std::string socket_path);

    /** @brief 取得不可变 operator endpoint / Get the immutable operator endpoint. */
    [[nodiscard]] const std::string& socket_path() const noexcept;

    /**
     * @brief 验证绝对 AF_UNIX operator endpoint / Validate an absolute AF_UNIX operator endpoint.
     * @param socket_path endpoint 路径 / Endpoint path.
     * @return 成功或参数错误 / Success or an invalid-argument error.
     */
    [[nodiscard]] static Result<void> validate_socket_path(std::string_view socket_path);

    /**
     * @brief 查询一条 runtime 的只读状态 / Query one runtime's read-only status.
     * @param runtime 已验证 runtime 标识 / Validated runtime identity.
     * @return allowlisted 状态或 operator 错误 / Allowlisted status or an operator error.
     */
    [[nodiscard]] Result<domain::OperatorWorkspaceStatus>
    status(const domain::RuntimeId& runtime) const;

    /**
     * @brief 列举一层持久 OverlayFS upper workspace / List one persistent OverlayFS-upper workspace
     * level.
     * @param runtime 已验证 runtime 标识 / Validated runtime identity.
     * @param path 已验证 `/workspace` 逻辑路径 / Validated `/workspace` logical path.
     * @return 有界安全目录列举或 operator 错误 / Bounded safe directory listing or an operator
     * error.
     * @note 返回的是持久 upper layer，而不是 synthetic merged lower+upper view。
     *       The result is the persistent upper layer, not a synthetic merged lower+upper view.
     */
    [[nodiscard]] Result<domain::WorkspaceListing>
    list(const domain::RuntimeId& runtime, const domain::OperatorWorkspacePath& path) const;

private:
    /** @brief 独立 operator UNIX socket 的绝对路径 / Absolute independent operator UNIX socket
     * path. */
    std::string socket_path_;
};

} // namespace wspctl::presentation
