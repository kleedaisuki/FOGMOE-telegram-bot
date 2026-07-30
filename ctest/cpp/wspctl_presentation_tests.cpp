/**
 * @file wspctl_presentation_tests.cpp
 * @brief presentation Unix gateway 单元测试 / Presentation Unix gateway unit tests.
 */

#include "wspctl/presentation/systemd_notify.hpp"
#include "wspctl/presentation/unix_gateway.hpp"

#include <array>
#include <cerrno>
#include <cstddef>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <string>
#include <string_view>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/un.h>
#include <unistd.h>

namespace {

/** @brief 测试失败计数 / Test failure count. */
unsigned int g_failures{0U};

/**
 * @brief 断言一个条件 / Assert one condition.
 * @param condition 待断言条件 / Condition to assert.
 * @param message 失败说明 / Failure description.
 */
void expect(const bool condition, const std::string& message) {
    if (!condition) {
        ++g_failures;
        std::cerr << "FAIL: " << message << '\n';
    }
}

/**
 * @brief 测试 AF_UNIX endpoint 的边界验证 / Test AF_UNIX endpoint boundary validation.
 * @note embedded NUL 必须在连接前拒绝，否则 `sockaddr_un::sun_path` 的 C-string 拷贝会截断。
 *       An embedded NUL must be rejected before connection, otherwise C-string copying into
 *       `sockaddr_un::sun_path` would truncate it.
 */
void test_socket_path_validation() {
    expect(wspctl::presentation::UnixGatewayClient::validate_socket_path("/run/wspctl/broker.sock")
               .has_value(),
           "accept an absolute AF_UNIX endpoint");
    expect(!wspctl::presentation::UnixGatewayClient::validate_socket_path("").has_value(),
           "reject an empty endpoint");
    expect(
        !wspctl::presentation::UnixGatewayClient::validate_socket_path("relative.sock").has_value(),
        "reject a relative endpoint");
    /** @brief 含 NUL 的 endpoint / Endpoint containing a NUL. */
    std::string embedded_nul{"/run/wspctl/"};
    embedded_nul.push_back('\0');
    embedded_nul += "broker.sock";
    expect(!wspctl::presentation::UnixGatewayClient::validate_socket_path(embedded_nul).has_value(),
           "reject an endpoint with an embedded NUL");
    /** @brief 刚好达到 sun_path 上限的 endpoint / Endpoint exactly at the sun_path limit. */
    std::string too_long(sizeof(sockaddr_un::sun_path), 'x');
    too_long.front() = '/';
    expect(!wspctl::presentation::UnixGatewayClient::validate_socket_path(too_long).has_value(),
           "reject an endpoint at the sun_path limit");
}

/**
 * @brief 绑定一个仅供 readiness 测试接收的 AF_UNIX datagram socket /
 * Bind an AF_UNIX datagram socket used only to receive a readiness test message.
 * @param endpoint filesystem `/...` 或 abstract `@...` endpoint /
 *        Filesystem `/...` or abstract `@...` endpoint.
 * @return 已绑定 FD；失败为 -1 / Bound descriptor, or -1 on failure.
 */
[[nodiscard]] int bind_notification_receiver(const std::string& endpoint) {
    /** @brief 测试接收 socket / Test receiver socket. */
    const int receiver = socket(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0);
    if (receiver < 0) {
        return -1;
    }
    /** @brief 测试接收地址 / Test receiver address. */
    sockaddr_un address{};
    address.sun_family = AF_UNIX;
    if (endpoint.front() == '@') {
        address.sun_path[0] = '\0';
        std::memcpy(address.sun_path + 1, endpoint.data() + 1, endpoint.size() - 1U);
    } else {
        std::memcpy(address.sun_path, endpoint.c_str(), endpoint.size() + 1U);
    }
    /** @brief 实际接收地址长度 / Effective receiver address length. */
    const auto address_length = static_cast<socklen_t>(
        offsetof(sockaddr_un, sun_path) + endpoint.size() + (endpoint.front() == '/' ? 1U : 0U));
    if (bind(receiver, reinterpret_cast<const sockaddr*>(&address), address_length) != 0) {
        static_cast<void>(close(receiver));
        return -1;
    }
    return receiver;
}

/**
 * @brief 验收一个 filesystem 或 abstract systemd readiness endpoint /
 * Accept one filesystem or abstract systemd readiness endpoint.
 * @param endpoint 待绑定 endpoint / Endpoint to bind.
 */
void test_systemd_notification_endpoint(const std::string& endpoint) {
    if (endpoint.front() == '/') {
        static_cast<void>(unlink(endpoint.c_str()));
    }
    /** @brief readiness 接收 FD / Readiness receiver descriptor. */
    const int receiver = bind_notification_receiver(endpoint);
    expect(receiver >= 0, "bind systemd notification receiver");
    if (receiver < 0) {
        return;
    }
    /** @brief 防止 notifier 回归让测试永久阻塞的接收上限 / Receive bound preventing a notifier
     * regression from hanging the test. */
    const timeval receive_timeout{.tv_sec = 1, .tv_usec = 0};
    expect(setsockopt(receiver, SOL_SOCKET, SO_RCVTIMEO, &receive_timeout,
                      sizeof(receive_timeout)) == 0,
           "bound systemd notification receive time");
    expect(setenv("NOTIFY_SOCKET", endpoint.c_str(), 1) == 0, "set NOTIFY_SOCKET");
    const auto notified = wspctl::presentation::notify_systemd_ready();
    expect(notified.has_value(), "send systemd readiness notification");
    expect(std::getenv("NOTIFY_SOCKET") == nullptr, "clear NOTIFY_SOCKET after one notification");

    /** @brief 接收到的 readiness payload / Received readiness payload. */
    std::array<char, 256> payload{};
    const ssize_t received =
        notified.has_value() ? recv(receiver, payload.data(), payload.size(), 0) : -1;
    expect(received > 0, "receive systemd readiness notification");
    if (received > 0) {
        const std::string_view message{payload.data(), static_cast<std::size_t>(received)};
        expect(message == "READY=1\nSTATUS=Accepting runtime requests",
               "send exact READY and STATUS fields");
    }
    static_cast<void>(close(receiver));
    if (endpoint.front() == '/') {
        static_cast<void>(unlink(endpoint.c_str()));
    }
}

/**
 * @brief 测试 systemd readiness 的两类地址、一次性环境与 fail-closed 输入 /
 * Test both systemd readiness address forms, one-shot environment handling, and fail-closed input.
 */
void test_systemd_readiness_notification() {
    /** @brief 避免并行测试碰撞的 PID 文本 / PID text avoiding parallel-test collisions. */
    const std::string process_id = std::to_string(getpid());
    test_systemd_notification_endpoint("@wspctl-notify-" + process_id);
    test_systemd_notification_endpoint("/tmp/wspctl-notify-" + process_id + ".sock");

    static_cast<void>(unsetenv("NOTIFY_SOCKET"));
    expect(!wspctl::presentation::notify_systemd_ready().has_value(),
           "reject a requested readiness notification without NOTIFY_SOCKET");

    expect(setenv("NOTIFY_SOCKET", "relative.sock", 1) == 0, "set malformed NOTIFY_SOCKET");
    expect(!wspctl::presentation::notify_systemd_ready().has_value(),
           "reject a relative systemd notification endpoint");
    expect(std::getenv("NOTIFY_SOCKET") == nullptr, "clear malformed NOTIFY_SOCKET fail-closed");

    /** @brief 超过 sockaddr_un 上限的 endpoint / Endpoint beyond the sockaddr_un limit. */
    std::string oversized(sizeof(sockaddr_un::sun_path), 'x');
    oversized.front() = '@';
    expect(setenv("NOTIFY_SOCKET", oversized.c_str(), 1) == 0, "set oversized NOTIFY_SOCKET");
    expect(!wspctl::presentation::notify_systemd_ready().has_value(),
           "reject an oversized systemd notification endpoint");
    expect(std::getenv("NOTIFY_SOCKET") == nullptr, "clear oversized NOTIFY_SOCKET fail-closed");
}

} // namespace

/**
 * @brief presentation CTest 入口 / Presentation CTest entry point.
 * @return 成功为 0 / Zero on success.
 */
int main() {
    test_socket_path_validation();
    test_systemd_readiness_notification();
    return g_failures == 0U ? EXIT_SUCCESS : EXIT_FAILURE;
}
