#pragma once

#include "wspctl/infrastructure/common.hpp"

#include <filesystem>
#include <sys/types.h>

namespace wspctl {

/**
 * @brief 校验 Bot 与 operator endpoint 的权限隔离 / Validate privilege separation between Bot and
 * operator endpoints.
 * @param bot_socket Bot 专属 UNIX socket 路径 / Bot-exclusive UNIX socket path.
 * @param bot_uid Bot 专属 UNIX UID / Bot-exclusive UNIX UID.
 * @param operator_socket operator 专属 UNIX socket 路径 / Operator-exclusive UNIX socket path.
 * @param operator_uid operator 专属 UNIX UID / Operator-exclusive UNIX UID.
 * @return 成功或 fail-closed 配置错误 / Success or a fail-closed configuration error.
 * @note 两 socket 必须位于互不包含的 parent directory、使用不同规范路径且两个 UID 也必须不同；
 *       这样只 bind-mount Bot directory 时不会意外带入 operator endpoint，Bot UID 即使知道
 *       operator 路径也不能通过 filesystem ACL 或 `SO_PEERCRED` 获得 operator 权限。/
 *       The sockets must use disjoint parent directories, different normalized paths, and
 *       different UIDs. A bind mount of the Bot directory therefore cannot accidentally include
 *       the operator endpoint, and the Bot UID cannot gain operator privileges through either
 *       filesystem ACLs or `SO_PEERCRED`, even if it learns the operator path.
 */
[[nodiscard]] Result<void>
validate_operator_endpoint_separation(const std::filesystem::path& bot_socket, uid_t bot_uid,
                                      const std::filesystem::path& operator_socket,
                                      uid_t operator_uid);

/**
 * @brief 判断已连接 peer 是否为被授权 operator / Check whether a connected peer is the authorized
 * operator.
 * @param peer_uid `SO_PEERCRED` 返回的 peer UID / Peer UID returned by `SO_PEERCRED`.
 * @param operator_uid endpoint 配置的唯一 operator UID / Sole operator UID configured for the
 * endpoint.
 * @return peer UID 完全匹配时为真 / True when the peer UID matches exactly.
 */
[[nodiscard]] bool is_authorized_operator_peer(uid_t peer_uid, uid_t operator_uid) noexcept;

} // namespace wspctl
