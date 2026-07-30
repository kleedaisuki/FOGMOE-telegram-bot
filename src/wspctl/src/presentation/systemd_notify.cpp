#include "wspctl/presentation/systemd_notify.hpp"

#include <cerrno>
#include <cstddef>
#include <cstdlib>
#include <cstring>
#include <string>
#include <string_view>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

namespace wspctl::presentation {
namespace {

/** @brief systemd readiness 数据报 / systemd readiness datagram. */
constexpr std::string_view kReadyMessage{"READY=1\nSTATUS=Accepting runtime requests"};

} // namespace

Result<void> notify_systemd_ready() {
    /** @brief systemd 注入的通知 endpoint / Notification endpoint injected by systemd. */
    const char* raw_endpoint = std::getenv("NOTIFY_SOCKET");
    if (raw_endpoint == nullptr || raw_endpoint[0] == '\0') {
        return std::unexpected(make_error(ErrorCode::invalid_argument,
                                          "systemd readiness requested without NOTIFY_SOCKET"));
    }
    /** @brief 在清除环境前取得的 endpoint 副本 / Endpoint copied before clearing the environment.
     */
    const std::string endpoint{raw_endpoint};
    if (unsetenv("NOTIFY_SOCKET") != 0) {
        return std::unexpected(errno_error(ErrorCode::internal, "clear NOTIFY_SOCKET"));
    }
    if ((endpoint.front() != '/' && endpoint.front() != '@') ||
        endpoint.size() >= sizeof(sockaddr_un::sun_path)) {
        return std::unexpected(make_error(
            ErrorCode::invalid_argument,
            "NOTIFY_SOCKET is not a representable filesystem or abstract AF_UNIX endpoint"));
    }

    /** @brief 只用于单次 readiness 数据报的 socket / Socket used for one readiness datagram. */
    const int notification_fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0);
    if (notification_fd < 0) {
        return std::unexpected(
            errno_error(ErrorCode::io_failure, "create systemd notification socket"));
    }

    /** @brief systemd notification socket 地址 / systemd notification socket address. */
    sockaddr_un address{};
    address.sun_family = AF_UNIX;
    if (endpoint.front() == '@') {
        address.sun_path[0] = '\0';
        std::memcpy(address.sun_path + 1, endpoint.data() + 1, endpoint.size() - 1U);
    } else {
        std::memcpy(address.sun_path, endpoint.c_str(), endpoint.size() + 1U);
    }
    /** @brief 实际参与寻址的 sockaddr 长度 / sockaddr length participating in addressing. */
    const auto address_length = static_cast<socklen_t>(
        offsetof(sockaddr_un, sun_path) + endpoint.size() + (endpoint.front() == '/' ? 1U : 0U));
    /** @brief readiness 数据报发送结果 / Readiness datagram send result. */
    const ssize_t sent =
        sendto(notification_fd, kReadyMessage.data(), kReadyMessage.size(), MSG_NOSIGNAL,
               reinterpret_cast<const sockaddr*>(&address), address_length);
    /** @brief 在 close 覆盖 errno 前保存的发送错误 / Send errno preserved before close can
     * overwrite it. */
    const int send_error = errno;
    static_cast<void>(close(notification_fd));
    if (sent < 0) {
        errno = send_error;
        return std::unexpected(
            errno_error(ErrorCode::io_failure, "send systemd readiness notification"));
    }
    if (static_cast<std::size_t>(sent) != kReadyMessage.size()) {
        return std::unexpected(make_error(
            ErrorCode::io_failure, "systemd readiness notification was only partially sent"));
    }
    return {};
}

} // namespace wspctl::presentation
