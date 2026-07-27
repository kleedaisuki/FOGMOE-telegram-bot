#include "wspctl/infrastructure/operator_endpoint.hpp"

#include <sys/un.h>

namespace wspctl {
namespace {

/**
 * @brief 验证一个 operator endpoint path 的 wire 可表示性 / Validate wire representability of an operator endpoint path.
 * @param path 待验证 endpoint 路径 / Endpoint path to validate.
 * @return 成功或参数错误 / Success or an invalid-argument error.
 */
[[nodiscard]] Result<void> validate_endpoint_path(const std::filesystem::path& path) {
    const std::string value = path.string();
    if (value.empty() || !path.is_absolute() || path.filename().empty() ||
        value.find('\0') != std::string::npos || value.size() >= sizeof(sockaddr_un::sun_path)) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "operator endpoint path is not a valid absolute AF_UNIX path"));
    }
    return {};
}

/**
 * @brief 判断一个规范目录是否包含另一个目录 / Check whether one normalized directory contains another.
 * @param ancestor 候选祖先目录 / Candidate ancestor directory.
 * @param descendant 候选后代目录 / Candidate descendant directory.
 * @return 相等或 ancestor 为严格祖先时为真 / True when equal or when ancestor is a strict ancestor.
 */
[[nodiscard]] bool contains_directory(
    const std::filesystem::path& ancestor,
    const std::filesystem::path& descendant) noexcept {
    auto ancestor_component = ancestor.begin();
    auto descendant_component = descendant.begin();
    while (ancestor_component != ancestor.end()) {
        if (descendant_component == descendant.end() || *ancestor_component != *descendant_component) {
            return false;
        }
        ++ancestor_component;
        ++descendant_component;
    }
    return true;
}

}  // namespace

Result<void> validate_operator_endpoint_separation(
    const std::filesystem::path& bot_socket,
    const uid_t bot_uid,
    const std::filesystem::path& operator_socket,
    const uid_t operator_uid) {
    if (const auto bot_path = validate_endpoint_path(bot_socket); !bot_path) {
        return std::unexpected(bot_path.error());
    }
    if (const auto operator_path = validate_endpoint_path(operator_socket); !operator_path) {
        return std::unexpected(operator_path.error());
    }
    if (bot_uid == 0U) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "Bot endpoint UID must not be root"));
    }
    if (bot_uid == operator_uid) {
        return std::unexpected(make_error(ErrorCode::permission_denied, "operator endpoint UID must differ from the Bot endpoint UID"));
    }
    if (bot_socket.lexically_normal() == operator_socket.lexically_normal()) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "operator and Bot endpoints must use different socket paths"));
    }
    const std::filesystem::path bot_parent = bot_socket.parent_path().lexically_normal();
    const std::filesystem::path operator_parent = operator_socket.parent_path().lexically_normal();
    if (contains_directory(bot_parent, operator_parent) || contains_directory(operator_parent, bot_parent)) {
        return std::unexpected(make_error(
            ErrorCode::invalid_argument,
            "operator and Bot socket parent directories must be disjoint"));
    }
    return {};
}

bool is_authorized_operator_peer(const uid_t peer_uid, const uid_t operator_uid) noexcept {
    return peer_uid == operator_uid;
}

}  // namespace wspctl
